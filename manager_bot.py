#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MOR Userbot Manager — owner-only Telegram control panel.

The original MORSELF userbot logic remains in MORSELF.py.  This module adds a
headless owner-only bot, interactive account login, persistent configuration,
and safe start/stop controls for a single account on a server.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import html
import json
import logging
import os
import re
import signal
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import MORSELF as core
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

APP_TITLE = "🤖 MOR Userbot Manager"
DEFAULT_SESSION_NAME = "telegram_auto_delete_on_silence"
PHONE_RE = re.compile(r"^\+?[0-9][0-9\s\-()]{6,24}$")
CODE_RE = re.compile(r"^[0-9\s\-]{3,12}$")
SENSITIVE_ACTIONS = {"ai:key", "login:code", "login:password"}


@dataclass
class PendingInput:
    action: str
    prompt: str
    secret: bool = False


def atomic_json_write(path: Path, value: Any) -> None:
    """Write JSON atomically and limit permissions to the service account."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def safe_markdown(value: Any) -> str:
    return str(value).replace("`", "ˋ").replace("*", "﹡").replace("_", "_\u200b")


def bool_icon(value: Any) -> str:
    return "🟢 ON" if bool(value) else "🔴 OFF"


def toggle_label(name: str, value: Any) -> str:
    return f"{'🟢' if value else '⚪'} {name}: {'ON' if value else 'OFF'}"


def copyable_keyword(value: Any) -> str:
    """نمایش کلیدواژه به‌صورت متن قابل انتخاب/کپی در تلگرام."""
    return f"<code>{html.escape(str(value))}</code>"


class UserbotRuntime:
    """Owns one Telethon userbot session without ever stopping the bot event loop."""

    def __init__(self, data_dir: Path, session_name: str = DEFAULT_SESSION_NAME) -> None:
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.data_dir.chmod(0o700)
        except OSError:
            pass
        self.session_path = self._select_existing_session(session_name)
        self.config_path = self.data_dir / "userbot_config.json"
        self.log_path = self.data_dir / "telegram_auto_delete.log"
        self.app: Optional[core.MORSELFApp] = None
        self.login_client: Optional[TelegramClient] = None
        self.login_phone: str = ""
        self.login_stage: str = ""
        self.last_start_error: str = ""
        self.operation_lock = asyncio.Lock()
        self.config = self._load_config()
        # MORSELF's pre-existing persistence methods use this module global.
        core.CONFIG_PATH = self.config_path

    @staticmethod
    def _looks_like_telethon_session(path: Path) -> bool:
        """تشخیص فایل SQLite مربوط به Session، حتی اگر پسوند نداشته باشد."""
        if not path.is_file() or path.name.endswith(("-journal", ".lock")):
            return False
        if path.suffix == ".session":
            return True
        try:
            with path.open("rb") as handle:
                return handle.read(16) == b"SQLite format 3\x00"
        except OSError:
            return False

    @staticmethod
    def _existing_session_path(path: Path) -> Optional[Path]:
        """Return the real Telethon session file without renaming or recreating it."""
        if UserbotRuntime._looks_like_telethon_session(path):
            return path
        if not path.name.endswith(".session"):
            explicit = path.with_name(path.name + ".session")
            if explicit.is_file():
                return explicit
        for candidate in sorted(path.parent.glob(path.name + ".session*")):
            if UserbotRuntime._looks_like_telethon_session(candidate):
                return candidate
        return None

    @classmethod
    def _session_exists(cls, path: Path) -> bool:
        return cls._existing_session_path(path) is not None

    def _select_existing_session(self, requested_name: str) -> Path:
        """Reuse an authorized session under any previous package name; never rename it."""
        requested = Path(requested_name).expanduser()
        if not requested.is_absolute():
            requested = self.data_dir / requested
        candidates = [requested]
        config_path = self.data_dir / "userbot_config.json"
        try:
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            saved_name = saved.get("session_name") if isinstance(saved, dict) else None
            if saved_name:
                saved_path = Path(str(saved_name)).expanduser()
                if not saved_path.is_absolute():
                    saved_path = self.data_dir / saved_path
                candidates.append(saved_path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        for stem in ("morself_userbot", "morself_session", "userbot", "telegram_auto_delete_on_silence"):
            candidates.append(self.data_dir / stem)
        # نسخه‌های قدیمی ممکن است Session را با نام دلخواه یا داخل یک زیرپوشه ذخیره کرده باشند.
        for candidate in sorted(self.data_dir.rglob("*.session")):
            candidates.append(candidate)
        for candidate in sorted(self.data_dir.iterdir()):
            if candidate.is_file() and candidate not in candidates:
                candidates.append(candidate)
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate)
            existing = self._existing_session_path(candidate) if key not in seen else None
            if existing is not None:
                return existing
            seen.add(key)
        # If no prior file exists, this is the only place where a new stem is selected.
        return requested

    def _load_config(self) -> dict[str, Any]:
        defaults = copy.deepcopy(core.CONFIG_TEMPLATE)
        defaults.update({
            "phone": "",
            "session_name": str(self.session_path),
            "log_file": str(self.log_path),
            "state_file": str(self.data_dir / "silenced_chats.json"),
            "ai_config_file": str(self.data_dir / "ai_chats.json"),
            "bad_ai_insults_file": str(self.data_dir / "bad_ai_insults.txt"),
            "temp_media_dir": str(self.data_dir / "temp_media"),
            "ai_globally_enabled": True,
        })
        if self.config_path.exists():
            try:
                saved = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    defaults.update(saved)
            except (OSError, json.JSONDecodeError):
                pass
        # Paths are server-managed so a user cannot redirect sensitive files through the bot.
        defaults["session_name"] = str(self.session_path)
        defaults["log_file"] = str(self.log_path)
        defaults["state_file"] = str(self.data_dir / "silenced_chats.json")
        defaults["ai_config_file"] = str(self.data_dir / "ai_chats.json")
        defaults["bad_ai_insults_file"] = str(self.data_dir / "bad_ai_insults.txt")
        defaults["temp_media_dir"] = str(self.data_dir / "temp_media")
        defaults["deleted_backup_dir"] = str(self.data_dir / "deleted_message_backup")
        return defaults

    def save_config(self) -> None:
        atomic_json_write(self.config_path, self.config)

    def session_files_exist(self) -> bool:
        return self._session_exists(self.session_path)

    async def session_is_authorized(self) -> bool:
        if not self.session_files_exist():
            return False
        probe = TelegramClient(str(self.session_path), core.OFFICIAL_API_ID, core.OFFICIAL_API_HASH)
        try:
            await probe.connect()
            return bool(await probe.is_user_authorized())
        except Exception as exc:
            self.last_start_error = f"Session قابل شناسایی/اتصال نیست: {exc}"
            return False
        finally:
            try:
                await probe.disconnect()
            except Exception:
                pass

    async def start(self) -> tuple[bool, str]:
        """Connect the saved user session and attach every original MORSELF feature."""
        async with self.operation_lock:
            if self.app and self.app.client and self.app.client.is_connected() and self.app.monitoring:
                return True, "سرویس از قبل فعال است."
            if not await self.session_is_authorized():
                return False, "ابتدا از بخش «ورود / خروج اکانت» وارد حساب تلگرام شوید."
            logger = core.configure_logging(self.log_path)
            app = core.MORSELFApp(self.config, logger)
            app.loop = asyncio.get_running_loop()
            try:
                await app._connect_and_login()
                await app._enable_monitoring()
            except Exception as exc:
                self.last_start_error = str(exc)
                try:
                    if app.client and app.client.is_connected():
                        await app.client.disconnect()
                except Exception:
                    pass
                app._release_session_lock()
                return False, f"اتصال سرویس ناموفق بود: {exc}"
            self.app = app
            self.last_start_error = ""
            self.save_config()
            return True, "✅ سرویس‌ها فعال شدند و مانیتورینگ در پس‌زمینه در حال اجرا است."

    async def stop(self) -> tuple[bool, str]:
        """Stop only the userbot; never call MORSELF._shutdown_async (it stops this loop)."""
        async with self.operation_lock:
            app = self.app
            if app is None:
                return True, "سرویس از قبل متوقف است."
            try:
                if app.client and app.monitoring:
                    app.client.remove_event_handler(app._on_new_message)
                    app.client.remove_event_handler(app._on_message_edited)
                    app.client.remove_event_handler(app._on_message_deleted)
                    app.monitoring = False
                await app._cancel_typing_tasks()
                for task_name in ("presence_task", "offline_guard_task"):
                    task = getattr(app, task_name, None)
                    if task and not task.done():
                        task.cancel()
                    setattr(app, task_name, None)
                if app.client and app.client.is_connected():
                    await app.client.disconnect()
            except Exception as exc:
                logging.getLogger("morself_manager").exception("Userbot stop failed")
                return False, f"توقف سرویس با خطا مواجه شد: {exc}"
            finally:
                app.client = None
                app._release_session_lock()
                self.app = None
            return True, "⏹ سرویس یوزربات متوقف شد؛ پنل مدیریتی همچنان فعال است."

    async def begin_login(self, phone: str) -> tuple[bool, str]:
        # Stop outside the lock because stop() protects its own lifecycle section.
        if self.app:
            await self.stop()
        async with self.operation_lock:
            if self.login_client is not None:
                return False, "یک روند ورود در حال اجراست؛ کد را وارد کنید یا /cancel بزنید."
            phone = phone.replace(" ", "").replace("-", "")
            client = TelegramClient(str(self.session_path), core.OFFICIAL_API_ID, core.OFFICIAL_API_HASH)
            try:
                await client.connect()
                if await client.is_user_authorized():
                    await client.disconnect()
                    return False, "این session از قبل وارد شده است؛ برای تعویض حساب ابتدا خروج را بزنید."
                await client.send_code_request(phone)
            except Exception as exc:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return False, f"ارسال کد ناموفق بود: {exc}"
            self.login_client = client
            self.login_phone = phone
            self.login_stage = "code"
            self.config["phone"] = phone
            self.save_config()
            return True, "📩 کد تلگرام ارسال شد. کد را همین‌جا وارد کنید."

    async def submit_login_code(self, code: str) -> tuple[bool, str]:
        client = self.login_client
        if client is None or self.login_stage != "code":
            return False, "روند ورود فعالی وجود ندارد."
        try:
            await client.sign_in(phone=self.login_phone, code=re.sub(r"[\s-]", "", code))
        except SessionPasswordNeededError:
            self.login_stage = "password"
            return True, "🔐 تأیید دومرحله‌ای فعال است. رمز دومرحله‌ای را ارسال کنید."
        except Exception as exc:
            return False, f"کد پذیرفته نشد: {exc}"
        return await self._finish_login()

    async def submit_login_password(self, password: str) -> tuple[bool, str]:
        client = self.login_client
        if client is None or self.login_stage != "password":
            return False, "در حال حاضر رمز دومرحله‌ای درخواست نشده است."
        try:
            await client.sign_in(password=password)
        except Exception as exc:
            return False, f"رمز پذیرفته نشد: {exc}"
        return await self._finish_login()

    async def _finish_login(self) -> tuple[bool, str]:
        client = self.login_client
        if client is None:
            return False, "اتصال ورود از دسترس خارج شد."
        try:
            me = await client.get_me()
            account = " ".join(filter(None, [getattr(me, "first_name", ""), getattr(me, "last_name", "")]))
            account = account or getattr(me, "username", "") or str(me.id)
            await client.disconnect()
        except Exception as exc:
            return False, f"تکمیل ورود ناموفق بود: {exc}"
        finally:
            self.login_client = None
            self.login_stage = ""
            self.login_phone = ""
        ok, detail = await self.start()
        if not ok:
            return False, f"✅ ورود برای {account} انجام شد، اما راه‌اندازی سرویس ناموفق بود: {detail}"
        return True, f"✅ ورود موفق\n👤 اکانت: {account}\n💾 Session به‌صورت امن ذخیره شد.\n\n{detail}"

    async def cancel_login(self) -> None:
        client, self.login_client = self.login_client, None
        self.login_stage = ""
        self.login_phone = ""
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def logout(self) -> tuple[bool, str]:
        await self.cancel_login()
        await self.stop()
        client = TelegramClient(str(self.session_path), core.OFFICIAL_API_ID, core.OFFICIAL_API_HASH)
        try:
            await client.connect()
            if await client.is_user_authorized():
                await client.log_out()
            await client.disconnect()
            for path in self.session_path.parent.glob(self.session_path.name + ".session*"):
                try:
                    path.unlink()
                except OSError:
                    pass
            lock = self.session_path.with_name(self.session_path.name + ".lock")
            try:
                lock.unlink()
            except OSError:
                pass
            self.config["phone"] = ""
            self.save_config()
            return True, "✅ از حساب خارج شدید و فایل session حذف شد. تنظیمات قابلیت‌ها حفظ شده‌اند."
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            return False, f"خروج از حساب ناموفق بود: {exc}"

    async def account_summary(self) -> tuple[str, Optional[Any]]:
        app = self.app
        if app and app.client and app.client.is_connected():
            try:
                return "connected", await app.client.get_me()
            except Exception:
                return "error", None
        if self.last_start_error:
            return "error", None
        if await self.session_is_authorized():
            return "saved", None
        return "none", None

    async def apply_runtime_change(self, key: str) -> None:
        self.save_config()
        app = self.app
        if not app or not app.client or not app.client.is_connected():
            return
        if key == "always_online":
            await app._restart_presence_if_needed()
        elif key == "auto_typing" and not self.config.get("auto_typing", True):
            await app._cancel_typing_tasks()

    def require_active_app(self) -> tuple[Optional[core.MORSELFApp], str]:
        if not self.app or not self.app.client or not self.app.client.is_connected() or not self.app.monitoring:
            return None, "برای این عملیات ابتدا سرویس یوزربات را روشن کنید."
        return self.app, ""

    async def resolve_entity(self, target: str) -> Any:
        app, error = self.require_active_app()
        if app is None:
            raise RuntimeError(error)
        target = target.strip()
        identifier: Any = int(target) if target.lstrip("-").isdigit() else target
        return await app.client.get_entity(identifier)

    async def readable_entity(self, entity: Any) -> str:
        name = " ".join(filter(None, [getattr(entity, "first_name", ""), getattr(entity, "last_name", "")]))
        name = name or getattr(entity, "title", "") or getattr(entity, "username", "") or "بدون نام"
        username = getattr(entity, "username", None)
        return f"{name}" + (f" (@{username})" if username else "") + f"\n🆔 {getattr(entity, 'id', '—')}"

    async def delete_forwarded_message(self, message: Any) -> tuple[bool, str]:
        """Delete the original message represented by a forwarded bot message."""
        app, error = self.require_active_app()
        if app is None:
            return False, error
        origin = getattr(message, "forward_origin", None)
        source_chat = getattr(origin, "chat", None) if origin else None
        source_id = getattr(origin, "message_id", None) if origin else None
        # Compatibility with older python-telegram-bot fields.
        if source_chat is None:
            source_chat = getattr(message, "forward_from_chat", None)
        if source_id is None:
            source_id = getattr(message, "forward_from_message_id", None)
        if source_chat is None or source_id is None:
            return False, "❌ تلگرام شناسهٔ پیام اصلی را همراه این Forward نفرستاده است؛ این پیام قابل حذف خودکار نیست."
        try:
            entity = await app.client.get_entity(int(getattr(source_chat, "id", source_chat)))
            await app.client.delete_messages(entity, [int(source_id)], revoke=True)
            return True, "✅ پیام اصلی از چت مبدأ حذف شد."
        except Exception as exc:
            logging.getLogger("morself_manager").exception("Forward deletion failed")
            return False, f"❌ حذف پیام اصلی ناموفق بود: {exc}"


class MORManagerBot:
    PARENT_MENUS = {
        "features": "main", "status": "main", "account": "main", "service": "main",
        "settings": "main", "help": "main", "silence": "features", "silence_manage": "silence",
        "silence_keywords": "silence", "specific_delete": "silence", "online": "features",
        "media": "features", "deleted_backup": "features", "liveloc": "features",
        "automation": "features", "keywords": "automation", "ai": "settings",
        "memory": "settings", "chats": "memory", "logs": "settings",
    }

    def parent_menu(self, menu: str) -> str:
        return self.PARENT_MENUS.get(menu, "main")

    def __init__(self, token: str, owner_id: int, data_dir: Path) -> None:
        self.token = token
        self.owner_id = owner_id
        self.runtime = UserbotRuntime(data_dir)
        self.menu_state_path = self.runtime.data_dir / "manager_menu.json"
        self.pending: dict[int, PendingInput] = {}
        self.transient_prompt_id: Optional[int] = None
        self.menu_message_id: Optional[int] = self._load_menu_message_id()
        self.online_target: Any = None
        self.backup_report_days: int = 7
        self.backup_report_rows: list[dict[str, Any]] = []
        self.current_menu = "main"
        self.menu_history: list[str] = []
        self.bot_username: str = ""
        self.app = Application.builder().token(token).concurrent_updates(False).build()
        self._register_handlers()

    def _load_menu_message_id(self) -> Optional[int]:
        try:
            value = json.loads(self.menu_state_path.read_text(encoding="utf-8"))
            message_id = value.get("message_id") if isinstance(value, dict) else value
            return int(message_id) if message_id else None
        except (OSError, ValueError, TypeError):
            return None

    def _save_menu_message_id(self) -> None:
        if self.menu_message_id:
            atomic_json_write(self.menu_state_path, {"chat_id": self.owner_id, "message_id": self.menu_message_id})

    def _register_handlers(self) -> None:
        self.app.add_handler(CommandHandler("start", self.command_start))
        self.app.add_handler(CommandHandler("menu", self.command_start))
        self.app.add_handler(CommandHandler("cancel", self.command_cancel))
        self.app.add_handler(CallbackQueryHandler(self.on_callback))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        self.app.add_handler(MessageHandler(filters.FORWARDED & ~filters.COMMAND, self.on_text))
        self.app.add_handler(MessageHandler(filters.LOCATION, self.on_text))

    def is_owner(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        return bool(user and chat and chat.type == "private" and user.id == self.owner_id)

    async def reject_if_not_owner(self, update: Update) -> bool:
        if self.is_owner(update):
            return False
        if update.effective_message:
            await update.effective_message.reply_text("⛔ این پنل فقط در گفت‌وگوی خصوصیِ مالک سرویس مجاز است.")
        return True

    async def command_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.reject_if_not_owner(update):
            return
        start_args = list(getattr(context, "args", None) or [])
        unsilence_id: Optional[int] = None
        if start_args and start_args[0].startswith("unsil_"):
            try:
                unsilence_id = int(start_args[0][6:])
            except ValueError:
                unsilence_id = None
        if update.effective_message:
            await self.clear_transient_prompt(context.bot, update.effective_message.chat_id)
            try:
                await update.effective_message.delete()
            except Exception:
                pass
        self.pending.pop(self.owner_id, None)
        self.current_menu = "main"
        self.menu_history.clear()
        if unsilence_id is not None:
            await self._remove_silenced_chat(unsilence_id)
            self.current_menu = "silence"
            text, keys = await self.silence_menu_view()
            if self.menu_message_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_message.chat_id,
                        message_id=self.menu_message_id,
                        text=text,
                        reply_markup=keys,
                        parse_mode=ParseMode.HTML,
                    )
                    return
                except Exception:
                    self.menu_message_id = None
            sent = await update.effective_message.reply_text(text, reply_markup=keys, parse_mode=ParseMode.HTML)
            self.menu_message_id = sent.message_id
            self._save_menu_message_id()
            return
        if self.menu_message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_message.chat_id,
                    message_id=self.menu_message_id,
                    text=await self.main_text(),
                    reply_markup=self.main_keyboard(),
                )
                return
            except Exception:
                try:
                    await context.bot.delete_message(chat_id=update.effective_message.chat_id, message_id=self.menu_message_id)
                except Exception:
                    pass
                self.menu_message_id = None
        sent = await update.effective_message.reply_text(await self.main_text(), reply_markup=self.main_keyboard())
        self.menu_message_id = sent.message_id
        self._save_menu_message_id()

    async def command_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.reject_if_not_owner(update):
            return
        if update.effective_message:
            await self.clear_transient_prompt(context.bot, update.effective_message.chat_id)
            try:
                await update.effective_message.delete()
            except Exception:
                pass
        previous = self.pending.pop(self.owner_id, None)
        if previous and previous.action.startswith("login"):
            await self.runtime.cancel_login()
        self.current_menu = "main"
        self.menu_history.clear()
        await self.command_start(update, context)

    async def main_text(self) -> str:
        state, me = await self.runtime.account_summary()
        if state == "connected" and me:
            display = " ".join(filter(None, [getattr(me, "first_name", ""), getattr(me, "last_name", "")]))
            display = display or getattr(me, "username", "") or str(me.id)
            phone = getattr(me, "phone", None) or self.runtime.config.get("phone", "—")
            account = f"✅ متصل به: {display}\n📱 {phone}"
        elif state == "saved":
            account = "🟡 Session ذخیره شده؛ سرویس در حال اجرا نیست."
        else:
            account = "⚪ هنوز اکانتی وارد نشده است."
        service = "🟢 فعال" if self.runtime.app and self.runtime.app.monitoring else "🔴 متوقف"
        return (
            f"{APP_TITLE}\n"
            "━━━━━━━━━━━━\n"
            f"👤 حساب شما\n{account}\n\n"
            f"⚡ وضعیت اجرا: {service}\n\n"
            "از منوی زیر هر بخش را جداگانه مدیریت کنید.\n"
            "دکمهٔ «▶️ اجرا» یوزربات را روشن می‌کند؛ «⏹ توقف» فقط خود یوزربات را متوقف می‌کند و پنل باز می‌ماند."
        )

    @staticmethod
    def main_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🧩 قابلیت‌ها", callback_data="menu:features")],
            [InlineKeyboardButton("🛠 تنظیمات", callback_data="menu:settings"),
             InlineKeyboardButton("🔐 حساب", callback_data="menu:account")],
            [InlineKeyboardButton("❓ راهنما", callback_data="menu:help")],
        ])

    @staticmethod
    def back_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:back")]])

    def navigation_keyboard(self) -> InlineKeyboardMarkup:
        """Show the main navigation only on the home screen; use Back elsewhere."""
        return self.main_keyboard() if self.current_menu == "main" else self.back_keyboard()

    async def edit_or_reply(self, query: Any, text: str, keyboard: InlineKeyboardMarkup, **kwargs: Any) -> None:
        try:
            await query.edit_message_text(text, reply_markup=keyboard, **kwargs)
            self.menu_message_id = query.message.message_id
            self._save_menu_message_id()
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            sent = await query.get_bot().send_message(chat_id=query.message.chat_id, text=text, reply_markup=keyboard, **kwargs)
            self.menu_message_id = sent.message_id
            self._save_menu_message_id()

    async def reply_in_menu(self, query: Any, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None, **kwargs: Any) -> None:
        """Render a notice in the existing panel instead of creating a new message."""
        await self.edit_or_reply(query, text, reply_markup or self.navigation_keyboard(), **kwargs)

    async def _edit_panel_from_update(self, update: Update, text: str) -> None:
        message = update.effective_message
        if message is None:
            return
        markup = self.navigation_keyboard()
        if self.menu_message_id:
            try:
                await update.get_bot().edit_message_text(
                    chat_id=message.chat_id, message_id=self.menu_message_id,
                    text=text, reply_markup=markup)
                return
            except Exception:
                pass
        sent = await update.get_bot().send_message(chat_id=message.chat_id, text=text, reply_markup=markup)
        self.menu_message_id = sent.message_id
        self._save_menu_message_id()

    def set_pending(self, action: str, prompt: str, secret: bool = False) -> None:
        self.pending[self.owner_id] = PendingInput(action, prompt, secret)

    async def clear_transient_prompt(self, bot: Any, chat_id: int) -> None:
        """Remove the last input prompt so the inline menu stays visually clean."""
        message_id = self.transient_prompt_id
        self.transient_prompt_id = None
        if message_id is not None:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass

    async def prompt_for_input(self, query: Any, action: str, prompt: str, text: str, *, secret: bool = False, **kwargs: Any) -> None:
        await self.clear_transient_prompt(query.get_bot(), query.message.chat_id)
        self.set_pending(action, prompt, secret=secret)
        await self.edit_or_reply(query, text, self.navigation_keyboard(), **kwargs)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not self.is_owner(update):
            await query.answer("دسترسی ندارید.", show_alert=True)
            return
        await query.answer()
        data = query.data or ""
        if data == "noop":
            return
        if data.startswith("menu:"):
            await self.clear_transient_prompt(context.bot, query.message.chat_id)
            self.pending.pop(self.owner_id, None)
            target = data.split(":", 1)[1]
            if target == "back":
                target = self.parent_menu(self.current_menu)
                self.menu_history.clear()
            elif target == "main":
                self.menu_history.clear()
            await self.show_menu(query, target)
            return
        await self.handle_action(query, data)

    async def show_menu(self, query: Any, menu: str) -> None:
        if menu == "main":
            self.menu_history.clear()
        self.current_menu = menu
        cfg = self.runtime.config
        if menu == "silence" and not self.bot_username:
            try:
                me = await query.get_bot().get_me()
                self.bot_username = str(getattr(me, "username", "") or "").lstrip("@")
            except Exception:
                pass
        if menu == "main":
            await self.edit_or_reply(query, await self.main_text(), self.main_keyboard())
        elif menu == "features":
            text = (
                "🧩 قابلیت‌های MOR\n"
                "━━━━━━━━━━━━\n"
                "همهٔ قابلیت‌های یوزربات از اینجا در دسترس هستند.\n"
                "اول قابلیت موردنظر را انتخاب کن و بعد تنظیمش کن."
            )
            keys = InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 ذخیرهٔ مدیای زمان‌دار", callback_data="menu:media")],
                [InlineKeyboardButton("🤫 مدیریت سکوت", callback_data="menu:silence"),
                 InlineKeyboardButton("👁 بررسی آنلاین", callback_data="menu:online")],
                [InlineKeyboardButton("🗑 پیام‌های پاک شده", callback_data="menu:deleted_backup")],
                [InlineKeyboardButton("📍 لایو لوکیشن فیک", callback_data="menu:liveloc")],
                [InlineKeyboardButton("⚙️ اتوماسیون‌ها", callback_data="menu:automation")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:back")],
            ])
            await self.edit_or_reply(query, text, keys)
        elif menu == "status":
            text = await self.status_text()
            keys = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 تازه‌سازی", callback_data="menu:status")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:back")],
            ])
            await self.edit_or_reply(query, text, keys)
        elif menu == "account":
            state, me = await self.runtime.account_summary()
            label = "✅ متصل" if state == "connected" else "🔴 خطا در اجرای سرویس" if state == "error" else "🟡 session ذخیره‌شده" if state == "saved" else "⚪ وارد نشده"
            identity = ""
            if me:
                identity = "\n" + await self.runtime.readable_entity(me)
            running = bool(self.runtime.app and self.runtime.app.monitoring)
            text = (
                f"🔐 حساب تلگرام\n━━━━━━━━━━━━\nوضعیت حساب: {label}{identity}\n"
                f"وضعیت یوزربات: {'🟢 روشن' if running else '🔴 خاموش'}\n"
                + (f"خطای آخر: {safe_markdown(self.runtime.last_start_error)}\n" if state == "error" else "") + "\n"
                "از اینجا ورود، خروج و روشن/خاموش‌کردن یوزربات را انجام بده."
            )
            account_buttons = []
            if state != "connected":
                account_buttons.append([InlineKeyboardButton("➕ ورود به اکانت", callback_data="account:login")])
            if state in {"connected", "saved"}:
                account_buttons.append([InlineKeyboardButton("🚪 خروج از اکانت", callback_data="account:logout")])
            account_buttons.append([
                InlineKeyboardButton("▶️ روشن‌کردن یوزربات", callback_data="service:start"),
                InlineKeyboardButton("⏹ خاموش‌کردن یوزربات", callback_data="service:stop"),
            ])
            account_buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:back")])
            keys = InlineKeyboardMarkup(account_buttons)
            await self.edit_or_reply(query, text, keys)
        elif menu == "service":
            running = bool(self.runtime.app and self.runtime.app.monitoring)
            text = "⚡ اجرای یوزربات\n━━━━━━━━━━━━\n" + ("🟢 یوزربات در حال اجرا است." if running else "🔴 یوزربات متوقف است.") + "\n\nخاموش‌کردن این بخش، پنل مدیریتی را خاموش نمی‌کند."
            keys = InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ روشن کردن", callback_data="service:start"),
                 InlineKeyboardButton("⏹ خاموش کردن", callback_data="service:stop")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:back")],
            ])
            await self.edit_or_reply(query, text, keys)
        elif menu == "silence":
            text, keys = await self.silence_menu_view()
            await self.edit_or_reply(query, text, keys, parse_mode=ParseMode.HTML)
        elif menu == "silence_manage":
            await self.show_menu(query, "silence")
        elif menu == "specific_delete":
            enabled = bool(cfg.get("specific_delete_enabled", True))
            keyword = str(cfg.get("specific_delete_keyword", "حذف پیام"))
            text = (
                "🗑 حذف پیام\n"
                "━━━━━━━━━━━━\n"
                f"وضعیت قابلیت: {bool_icon(enabled)}\n"
                f"کلیدواژه: {copyable_keyword(keyword)}\n\n"
                "برای حذف در گروه یا چت خصوصی، روی پیام هدف Reply کن و کلیدواژه را بفرست؛ یا همان پیام را برای این ربات Forward کن.\n"
                "ربات پیام اصلی را از چت مبدأ حذف می‌کند؛ این قابلیت هیچ ارتباطی با آرشیو پیام‌های حذف/ویرایش‌شده ندارد."
            )
            keys = InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_label("حذف پیام", enabled), callback_data="specific_delete:toggle")],
                [InlineKeyboardButton("✏️ تنظیم کلیدواژه", callback_data="specific_delete:keyword")],
                [InlineKeyboardButton("⬅️ بازگشت به مدیریت سکوت", callback_data="menu:back")],
            ])
            await self.edit_or_reply(query, text, keys, parse_mode=ParseMode.HTML)
        elif menu == "silence_keywords":
            silence_keyword = str(cfg.get("silence_command") or "سکوت")
            cancel_keyword = str(cfg.get("cancel_command") or "لغو سکوت")
            delete_keyword = str(cfg.get("specific_delete_keyword") or "حذف پیام")
            text = (
                "📝 کلیدواژه‌های سکوت\n"
                "━━━━━━━━━━━━\n"
                f"🤫 فعال‌کردن سکوت: {copyable_keyword(silence_keyword)}\n"
                f"🔔 لغوکردن سکوت: {copyable_keyword(cancel_keyword)}\n"
                f"🗑 حذف پیام Reply‌شده: {copyable_keyword(delete_keyword)}\n\n"
                "روی هر دکمه بزن و متن دلخواهت را ارسال کن."
            )
            keys = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ کلیدواژهٔ فعال‌کردن سکوت", callback_data="silence_keyword:silence_command")],
                [InlineKeyboardButton("✏️ کلیدواژهٔ لغوکردن سکوت", callback_data="silence_keyword:cancel_command")],
                [InlineKeyboardButton("✏️ کلیدواژهٔ حذف پیام", callback_data="specific_delete:keyword")],
                [InlineKeyboardButton("⬅️ بازگشت به مدیریت سکوت", callback_data="menu:silence")],
            ])
            await self.edit_or_reply(query, text, keys, parse_mode=ParseMode.HTML)
        elif menu == "deleted_backup":
            enabled = bool(cfg.get("deleted_backup_enabled", True))
            text = (
                "🗑 پیام‌های پاک شده\n"
                "━━━━━━━━━━━━\n"
                f"وضعیت قابلیت: {bool_icon(enabled)}\n"
                f"مدت نگهداری: {cfg.get('deleted_backup_retention_days', 30)} روز\n"
                "این قابلیت فقط برای چت‌های خصوصی (PV) است؛ گروه‌ها و کانال‌ها ثبت نمی‌شوند.\n"
                "در گزارش، زمان، گوینده، زمان پاک‌شدن و تمام مرحله‌های ویرایش نمایش داده می‌شود.\n"
                "این قابلیت باعث سین‌خوردن یا آنلاین‌شدن جداگانه نمی‌شود."
            )
            keys = InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_label("پیام‌های پاک شده", enabled), callback_data="backup:toggle")],
                [InlineKeyboardButton("📅 بررسی پیام‌های پاک شده", callback_data="backup:list")],
                [InlineKeyboardButton("⏳ تنظیم مدت نگهداری", callback_data="backup:days")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:back")],
            ])
            await self.edit_or_reply(query, text, keys)
        elif menu == "liveloc":
            text = (
                "📍 لایو لوکیشن فیک\n"
                "━━━━━━━━━━━━\n"
                f"وضعیت قابلیت: {bool_icon(cfg.get('live_location_enabled', True))}\n"
                f"کلیدواژه: {copyable_keyword(cfg.get('live_location_keyword', 'لوکیشن زنده'))}\n"
                f"مدت روشن‌بودن: {cfg.get('live_location_default_period', 900)} ثانیه\n\n"
                "روش استفاده:\n"
                "1) همین‌جا، داخل چت ربات، یک لوکیشن معمولی بفرست.\n"
                "2) داخل چتی که می‌خواهی لوکیشن برود، کلیدواژه را بفرست.\n\n"
                "بعد از مدت تعیین‌شده، لایو لوکیشن خودکار خاموش می‌شود.\n\n"
                "ساده و سریع: لوکیشن را به ربات بده، کلیدواژه را در چت مقصد بفرست."
            )
            keys = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    toggle_label("لایو لوکیشن", cfg.get("live_location_enabled", True)),
                    callback_data="liveloc:toggle")],
                [InlineKeyboardButton("⏱ تنظیم مدت زمان روشن بودن", callback_data="liveloc:period")],
                [InlineKeyboardButton("✏️ تنظیم کلیدواژه", callback_data="liveloc:keyword")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:features")],
            ])
            await self.edit_or_reply(query, text, keys, parse_mode=ParseMode.HTML)
        elif menu == "online":
            text = (
                "👁 Online Checker\n\n"
                f"کلمهٔ دستور داخلی: {copyable_keyword(cfg.get('online_check_keyword'))}\n"
                f"انتظار پیش‌فرض: {cfg.get('online_check_default_wait_seconds')} ثانیه\n"
                f"سقف انتظار: {cfg.get('online_check_max_wait_seconds')} ثانیه\n"
                f"فاصلهٔ مجاز بین بررسی‌ها: {cfg.get('online_check_cooldown_seconds')} ثانیه\n\n"
                "یک کاربر را از طریق @username یا شناسهٔ عددی بررسی کنید."
            )
            keys = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔎 بررسی یک کاربر", callback_data="online:check")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:back")],
            ])
            await self.edit_or_reply(query, text, keys, parse_mode=ParseMode.HTML)
        elif menu == "media":
            text = (
                "💾 ذخیرهٔ مدیای زمان‌دار و محافظت‌شده\n\n"
                "این قابلیت‌ها توسط منطق اصلی MORSELF اجرا می‌شوند و محتوا را در Saved Messages همان اکانت می‌فرستند."
            )
            keys = InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_label("ذخیرهٔ خودکار", cfg.get("auto_save_expiring")), callback_data="set:auto_save_expiring")],
                [InlineKeyboardButton(toggle_label("پیام‌های ورودی", cfg.get("save_expiring_incoming")), callback_data="set:save_expiring_incoming"),
                 InlineKeyboardButton(toggle_label("پیام‌های خروجی", cfg.get("save_expiring_outgoing")), callback_data="set:save_expiring_outgoing")],
                [InlineKeyboardButton(toggle_label("پیام‌های محافظت‌شده", cfg.get("save_protected_messages")), callback_data="set:save_protected_messages")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:back")],
            ])
            await self.edit_or_reply(query, text, keys)
        elif menu == "automation":
            text = "⚙️ اتوماسیون‌ها\n━━━━━━━━━━━━\n" + "\n".join([
                f"🟢 Always Online: {bool_icon(cfg.get('always_online'))}",
                f"✅ Auto Read: {bool_icon(cfg.get('auto_mark_read'))}",
                f"✍️ Auto Typing: {bool_icon(cfg.get('auto_typing'))}",
            ])
            keys = InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_label("Always Online", cfg.get("always_online")), callback_data="set:always_online")],
                [InlineKeyboardButton(toggle_label("Auto Read", cfg.get("auto_mark_read")), callback_data="set:auto_mark_read")],
                [InlineKeyboardButton(toggle_label("Auto Typing", cfg.get("auto_typing")), callback_data="set:auto_typing")],
                [InlineKeyboardButton("⚙️ تنظیم کلیدواژه‌ها", callback_data="auto:keywords")],
                [InlineKeyboardButton("⬅️ بازگشت به قابلیت‌ها", callback_data="menu:features")],
            ])
            await self.edit_or_reply(query, text, keys)
        elif menu == "keywords":
            keyword_rows = [
                ("online_on_keyword", "🟢 روشن‌کردن آنلاین", "online on"),
                ("online_off_keyword", "🔴 خاموش‌کردن آنلاین", "online off"),
                ("read_on_keyword", "✅ روشن‌کردن سین", "read on"),
                ("read_off_keyword", "⛔ خاموش‌کردن سین", "read off"),
                ("typing_on_keyword", "✍️ روشن‌کردن تایپینگ", "typing on"),
                ("typing_off_keyword", "⏹ خاموش‌کردن تایپینگ", "typing off"),
            ]
            lines = ["📝 تنظیم کلیدواژه‌ها", "━━━━━━━━━━━━", "مقدار فعلی کنار هر قابلیت نوشته شده است.", "برای تغییر، روی همان دکمه بزن:", ""]
            buttons = []
            for key, label, default in keyword_rows:
                current = str(cfg.get(key) or default)
                lines.append(f"{label}: {copyable_keyword(current)}")
                buttons.append([InlineKeyboardButton(f"✏️ {label}", callback_data=f"keyword:{key}")])
            lines.append("\nپیش‌فرض‌ها فقط نمونه‌اند؛ هر متن کوتاه و دلخواهی می‌توانی وارد کنی.")
            buttons.append([InlineKeyboardButton("⬅️ بازگشت به اتوماسیون‌ها", callback_data="menu:automation")])
            await self.edit_or_reply(query, "\n".join(lines), InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)
        elif menu == "ai":
            api_ready = bool(cfg.get("ai_api_key")) and bool(cfg.get("ai_base_url"))
            text = (
                "🤖 AI Control\n\n"
                f"وضعیت کلی: {bool_icon(cfg.get('ai_globally_enabled', True))}\n"
                f"اتصال API: {'✅ تنظیم شده' if api_ready else '⚪ تنظیم نشده'}\n"
                f"مدل: {safe_markdown(cfg.get('ai_model', 'gpt-4o-mini'))}\n"
                f"کلیدواژهٔ روشن‌کردن: {copyable_keyword(cfg.get('ai_on_keyword', 'AI'))}\n"
                f"کلیدواژهٔ خاموش‌کردن: {copyable_keyword(cfg.get('ai_off_keyword', 'off AI'))}\n"
                f"Base URL: {safe_markdown(cfg.get('ai_base_url') or '—')}\n\n"
                "فعال‌سازی AI در هر چت، مانند نسخهٔ اصلی، با پیام «AI» انجام می‌شود."
            )
            keys = InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_label("AI روشن/خاموش", cfg.get("ai_globally_enabled", True)), callback_data="set:ai_globally_enabled")],
                [InlineKeyboardButton("🔑 API Key", callback_data="ai:key"), InlineKeyboardButton("🌐 Base URL", callback_data="ai:url")],
                [InlineKeyboardButton("🧠 Model", callback_data="ai:model"), InlineKeyboardButton("🔎 شناسایی مدل‌ها", callback_data="ai:models")],
                [InlineKeyboardButton("⚙️ تنظیمات پاسخ", callback_data="ai:advanced")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:back")],
            ])
            await self.edit_or_reply(query, text, keys, parse_mode=ParseMode.HTML)
        elif menu == "memory":
            app = self.runtime.app
            records = app.ai_chats if app else self._load_ai_state()
            enabled = sum(1 for item in records.values() if isinstance(item, dict) and item.get("enabled"))
            turns = sum(len(item.get("history", [])) // 2 for item in records.values() if isinstance(item, dict) and isinstance(item.get("history"), list))
            text = f"📚 حافظهٔ چت‌ها\n\nچت‌های ثبت‌شده: {len(records)}\nAI فعال در چت‌ها: {enabled}\nتعداد تقریبی نوبت‌های حافظه: {turns}\n"
            keys = InlineKeyboardMarkup([
                [InlineKeyboardButton("📄 مشاهدهٔ خلاصه", callback_data="memory:list")],
                [InlineKeyboardButton("🗑 پاک کردن همهٔ حافظه‌ها", callback_data="memory:clear")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:back")],
            ])
            await self.edit_or_reply(query, text, keys)
        elif menu == "chats":
            app = self.runtime.app
            records = app.ai_chats if app else self._load_ai_state()
            active = [key for key, value in records.items() if isinstance(value, dict) and value.get("enabled")]
            text = "📊 چت‌های فعال\n\n" + ("\n".join(f"• `{safe_markdown(key)}`" for key in active[:50]) if active else "فعلاً چت AI فعالی ثبت نشده است.")
            await self.edit_or_reply(query, text, self.back_keyboard())
        elif menu == "logs":
            await self.edit_or_reply(query, self.log_text(), InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 تازه‌سازی", callback_data="menu:logs")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:back")],
            ]))
        elif menu == "settings":
            text = "⚙️ تنظیمات\n━━━━━━━━━━━━\nتنظیمات عمومی برنامه و هوش مصنوعی را از اینجا مدیریت کن."
            keys = InlineKeyboardMarkup([
                [InlineKeyboardButton("🤖 تنظیمات هوش مصنوعی", callback_data="menu:ai")],
                [InlineKeyboardButton("💾 ذخیرهٔ تنظیمات", callback_data="settings:save")],
                [InlineKeyboardButton("🧾 مشاهدهٔ تنظیمات امن", callback_data="settings:view")],
                [InlineKeyboardButton("🛠 ویرایش پیشرفته", callback_data="settings:advanced")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:back")],
            ])
            await self.edit_or_reply(query, text, keys)
        elif menu == "help":
            text = (
                "❓ راهنما\n\n"
                "• /start یا /menu: نمایش پنل\n"
                "• /cancel: لغو ورود یا ویرایش در حال انجام\n"
                "• فقط صاحب شناسهٔ عددی ثبت‌شده در فرمان اجرا به پنل دسترسی دارد.\n\n"
                "در اولین استفاده: ورود اکانت ← شماره ← کد ← در صورت نیاز رمز دومرحله‌ای. "
                "پس از ورود، session با دسترسی محدود روی سرور ذخیره می‌شود و سرویس خودکار روشن خواهد شد."
            )
            await self.edit_or_reply(query, text, self.back_keyboard())
        else:
            await self.edit_or_reply(query, await self.main_text(), self.main_keyboard())

    async def handle_action(self, query: Any, data: str) -> None:
        if data.startswith("backup:page:"):
            try:
                page = int(data.rsplit(":", 1)[1])
            except ValueError:
                page = 0
            text, keyboard = self._backup_report_page(page)
            await self.edit_or_reply(query, text, keyboard)
        elif data == "backup:html":
            await self._send_backup_html(query)
        elif data == "account:login":
            if self.runtime.login_client:
                await self.prompt_for_input(query, "login:code", "کد ورود تلگرام را وارد کنید:", "روند ورود قبلی باز است. کد ورود را ارسال کنید یا /cancel بزنید.", secret=True)
                return
            await self.prompt_for_input(query, "login:phone", "شماره تلفن را با کد کشور وارد کنید؛ نمونه: +98912xxxxxxx", "📱 شماره تلفن را با کد کشور وارد کنید؛ نمونه: +98912xxxxxxx\n\nبرای لغو: /cancel")
        elif data == "account:logout":
            await self.reply_in_menu(query, "⚠️ خروج، session ذخیره‌شده را حذف می‌کند اما تنظیمات قابلیت‌ها حفظ می‌شوند. ادامه می‌دهید؟", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("بله، خروج از حساب", callback_data="account:logout_yes"), InlineKeyboardButton("انصراف", callback_data="menu:account")],
            ]))
        elif data == "account:logout_yes":
            ok, message = await self.runtime.logout()
            await self.reply_in_menu(query, message)
            await self.show_menu(query, "main")
        elif data == "service:start":
            ok, message = await self.runtime.start()
            await self.reply_in_menu(query, message)
            if not ok:
                return
            await self.show_menu(query, "main")
        elif data == "service:stop":
            ok, message = await self.runtime.stop()
            await self.reply_in_menu(query, message)
            if not ok:
                return
            await self.show_menu(query, "main")
        elif data.startswith("set:"):
            key = data.split(":", 1)[1]
            if key not in self.editable_boolean_keys():
                await self.reply_in_menu(query, "این تنظیم قابل تغییر نیست.")
                return
            self.runtime.config[key] = not bool(self.runtime.config.get(key))
            await self.runtime.apply_runtime_change(key)
            await self.reply_in_menu(query, f"✅ {key} → {bool_icon(self.runtime.config[key])}")
            destinations = {
                "auto_save_expiring": "media", "save_expiring_incoming": "media", "save_expiring_outgoing": "media", "save_protected_messages": "media",
                "always_online": "automation", "auto_mark_read": "automation", "auto_typing": "automation", "ai_globally_enabled": "ai",
            }
            await self.show_menu(query, "main")
        elif data == "sil:manage":
            await self.show_menu(query, "silence_manage")
        elif data == "sil:add":
            await self.prompt_for_input(query, "silence:add", "@username یا شناسهٔ عددی چت را وارد کنید:", "@username یا شناسهٔ عددی چت خصوصی یا گروهی را وارد کنید.")
        elif data.startswith("sil:unsil:"):
            try:
                chat_id = int(data.split(":", 2)[2])
            except (TypeError, ValueError):
                await self.reply_in_menu(query, "❌ شناسهٔ چت نامعتبر است.")
                return
            app, error = self.runtime.require_active_app()
            if app is not None:
                app.silenced_chats.discard(chat_id)
                app._save_silenced_state()
                await self.reply_in_menu(query, f"✅ سکوت از چت 🆔 {chat_id} لغو شد.")
            else:
                path = Path(self.runtime.config["state_file"])
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    ids = {int(value) for value in raw} if isinstance(raw, list) else set()
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    ids = set()
                ids.discard(chat_id)
                atomic_json_write(path, sorted(ids))
                await self.reply_in_menu(query, f"✅ سکوت از چت 🆔 {chat_id} لغو شد.")
            await self.show_menu(query, "silence_manage")
        elif data == "sil:clear":
            await self.reply_in_menu(query, "همهٔ چت‌های ساکت پاک شوند؟", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧹 پاک کردن همه", callback_data="sil:clear_yes"), InlineKeyboardButton("انصراف", callback_data="sil:manage")],
            ]))
        elif data == "sil:clear_yes":
            app, error = self.runtime.require_active_app()
            if app:
                app.silenced_chats.clear()
                app._save_silenced_state()
                await self.reply_in_menu(query, "✅ فهرست سکوت پاک شد.")
            else:
                atomic_json_write(Path(self.runtime.config["state_file"]), [])
                await self.reply_in_menu(query, "✅ فهرست سکوتِ ذخیره‌شده پاک شد؛ در اجرای بعدی سرویس اعمال می‌شود.")
            await self.show_menu(query, "silence_manage")
        elif data == "sil:commands":
            await self.show_menu(query, "silence_keywords")
        elif data == "specific_delete:menu":
            await self.show_menu(query, "specific_delete")
        elif data == "specific_delete:toggle":
            self.runtime.config["specific_delete_enabled"] = not bool(self.runtime.config.get("specific_delete_enabled", True))
            self.runtime.save_config()
            await self.show_menu(query, "specific_delete")
        elif data == "specific_delete:keyword":
            current = str(self.runtime.config.get("specific_delete_keyword", "حذف پیام"))
            await self.prompt_for_input(
                query, "specific_delete:keyword", "کلیدواژهٔ حذف پیام را وارد کنید:",
                f"✏️ کلیدواژهٔ فعلی: {copyable_keyword(current)}\n\n"
                "کلیدواژهٔ جدید را بفرست. برای لغو: /cancel",
                parse_mode=ParseMode.HTML,
            )
        elif data == "backup:toggle":
            self.runtime.config["deleted_backup_enabled"] = not bool(self.runtime.config.get("deleted_backup_enabled", True))
            self.runtime.save_config()
            await self.show_menu(query, "deleted_backup")
        elif data == "delete:toggle":
            self.runtime.config["delete_request_enabled"] = not bool(self.runtime.config.get("delete_request_enabled", True))
            self.runtime.save_config()
            await self.show_menu(query, "deleted_backup")
        elif data == "backup:days":
            await self.prompt_for_input(query, "backup:days", "تعداد روز نگهداری را وارد کنید:", "📅 تعداد روز نگهداری را بفرست؛ مثال: `30`", parse_mode=ParseMode.MARKDOWN)
        elif data == "backup:list":
            await self.prompt_for_input(query, "backup:list", "تعداد روز برای جست‌وجو را وارد کنید:", "🔎 آرشیو چند روز اخیر را بررسی کنم؟ مثال: `7`", parse_mode=ParseMode.MARKDOWN)
        elif data == "liveloc:toggle":
            self.runtime.config["live_location_enabled"] = not bool(
                self.runtime.config.get("live_location_enabled", True))
            self.runtime.save_config()
            await self.show_menu(query, "liveloc")
        elif data == "liveloc:keyword":
            current = str(self.runtime.config.get("live_location_keyword", "لوکیشن زنده"))
            await self.prompt_for_input(
                query, "liveloc:keyword", "کلیدواژهٔ لایو لوکیشن:",
                f"✏️ کلیدواژهٔ فعلی: {copyable_keyword(current)}\n\nکلیدواژهٔ جدید را بفرست.",
                parse_mode=ParseMode.HTML,
            )
        elif data == "liveloc:period":
            current = int(self.runtime.config.get("live_location_default_period", 900))
            await self.prompt_for_input(
                query, "liveloc:period", "تنظیم مدت زمان روشن بودن لایو لوکیشن:",
                f"⏱ مدت فعلی: {current} ثانیه\nعدد جدید را بفرست. بعد از این مدت، لایو لوکیشن خودکار خاموش می‌شود (۶۰ تا ۸۶۴۰۰ ثانیه).",
            )
        elif data.startswith("silence_keyword:"):
            key = data.split(":", 1)[1]
            labels = {
                "silence_command": "فعال‌کردن سکوت",
                "cancel_command": "لغوکردن سکوت",
            }
            if key not in labels:
                await self.reply_in_menu(query, "این کلیدواژه معتبر نیست.")
                return
            current = str(self.runtime.config.get(key) or "—")
            await self.prompt_for_input(query, f"silence_keyword:{key}", f"متن جدید برای {labels[key]}",
                f"✏️ کلیدواژهٔ «{labels[key]}»\n\n"
                f"مقدار فعلی: {copyable_keyword(current)}\n\n"
                "متن جدید را بفرست. مثال: `سکوت`\nبرای لغو: /cancel",
                parse_mode=ParseMode.HTML,
            )
        elif data == "online:check":
            await self.prompt_for_input(query, "online:check", "یوزرنیم، آیدی عددی یا پیام فورواردی را بفرستید:", "👤 یوزرنیم مثل `@username`، آیدی عددی یا یک پیام فورواردی از کاربر را بفرستید.", parse_mode=ParseMode.MARKDOWN)
        elif data == "online:settings":
            await self.prompt_for_input(query, "settings:multi", "کلید=مقدار", "تنظیمات پیشرفته را با کلید=مقدار بفرستید.", parse_mode=ParseMode.MARKDOWN)
        elif data == "auto:keywords":
            await self.show_menu(query, "keywords")
        elif data.startswith("keyword:"):
            key = data.split(":", 1)[1]
            allowed = {
                "online_on_keyword": "روشن‌کردن آنلاین",
                "online_off_keyword": "خاموش‌کردن آنلاین",
                "read_on_keyword": "روشن‌کردن سین",
                "read_off_keyword": "خاموش‌کردن سین",
                "typing_on_keyword": "روشن‌کردن تایپینگ",
                "typing_off_keyword": "خاموش‌کردن تایپینگ",
            }
            if key not in allowed:
                await self.reply_in_menu(query, "این کلیدواژه معتبر نیست.")
                return
            current = self.runtime.config.get(key) or "—"
            await self.prompt_for_input(query, f"keyword:{key}", f"متن جدید برای {allowed[key]}",
                f"✏️ کلیدواژهٔ «{allowed[key]}»\n\n"
                f"مقدار فعلی: {copyable_keyword(current)}\n\n"
                "متن جدید را بفرست. مثال: `online on`\nبرای لغو: /cancel",
                parse_mode=ParseMode.HTML,
            )
        elif data == "ai:key":
            await self.prompt_for_input(query, "ai:key", "API Key را وارد کنید:", "🔑 API Key را ارسال کنید. پیام پس از دریافت از گفت‌وگو پاک می‌شود؛ برای لغو /cancel بزنید.", secret=True)
        elif data == "ai:url":
            await self.prompt_for_input(query, "ai:url", "Base URL را وارد کنید:", "🌐 Base URL سازگار با OpenAI را وارد کنید؛ نمونه: `https://api.example.com/v1`", parse_mode=ParseMode.MARKDOWN)
        elif data == "ai:model":
            await self.prompt_for_input(query, "ai:model", "نام مدل را وارد کنید:", "🧠 نام مدل را وارد کنید؛ نمونه: `gpt-4o-mini`")
        elif data == "ai:models":
            await self.reply_in_menu(query, await self.discover_models())
        elif data == "ai:advanced":
            await self.prompt_for_input(query, "settings:multi", "کلید=مقدار", "تنظیم AI را با `کلید=مقدار` بفرستید:\n`ai_system_prompt`\n`ai_request_timeout_seconds`\n`ai_max_retries`\n`ai_streaming_enabled`\n`ai_stream_interval_seconds`\n`ai_stream_chars_per_update`\n`ai_max_response_chars`\n`ai_history_limit`\n`ai_history_item_chars`\n`ai_send_stickers`", parse_mode=ParseMode.MARKDOWN)
        elif data == "memory:list":
            await self.reply_in_menu(query, self.memory_list_text())
        elif data == "memory:clear":
            await self.reply_in_menu(query, "⚠️ تاریخچهٔ همهٔ چت‌های AI پاک شود؟ تنظیم API و وضعیت فعال چت‌ها حذف نمی‌شود.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 پاک کردن حافظه‌ها", callback_data="memory:clear_yes"), InlineKeyboardButton("انصراف", callback_data="menu:memory")],
            ]))
        elif data == "memory:clear_yes":
            records = self.runtime.app.ai_chats if self.runtime.app else self._load_ai_state()
            for item in records.values():
                if isinstance(item, dict):
                    item["history"] = []
            if self.runtime.app:
                self.runtime.app._save_ai_state()
            else:
                atomic_json_write(Path(self.runtime.config["ai_config_file"]), records)
            await self.reply_in_menu(query, "✅ حافظهٔ مکالمه‌ها پاک شد.")
            await self.show_menu(query, "memory")
        elif data == "settings:save":
            self.runtime.save_config()
            await self.reply_in_menu(query, "✅ تنظیمات در فایل امن سرور ذخیره شد.")
        elif data == "settings:view":
            await self.reply_in_menu(query, self.safe_config_text(), parse_mode=ParseMode.MARKDOWN)
        elif data == "settings:advanced":
            await self.prompt_for_input(query, "settings:all", "کلید=مقدار",
                "🛠 ویرایش پیشرفته\n\n"
                "یک خط با قالب `key=value` بفرستید. کلیدهای معتبر از تنظیمات اصلی MORSELF قابل ویرایش‌اند؛ "
                "مسیرها، شماره و session توسط سرویس مدیریت می‌شوند.\n\n"
                "برای مقادیر بولی: true/false؛ برای نگاشت‌ها و فهرست‌ها: JSON معتبر.\n"
                "نمونه: `online_check_default_wait_seconds=10` یا `ai_system_prompt=...`",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await self.reply_in_menu(query, "گزینه ناشناخته است؛ /start را بزنید.")

    @staticmethod
    def _backup_report_sort_key(row: dict[str, Any]) -> str:
        return str(row.get("deleted_at") or row.get("edited_at") or row.get("created_at") or "")

    @staticmethod
    def _tehran_date(value: Any) -> str:
        """Render stored UTC ISO timestamps as readable Gregorian Tehran time."""
        if not value:
            return "—"
        try:
            raw = str(value).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            local = parsed.astimezone(ZoneInfo("Asia/Tehran"))
            return local.strftime("%Y/%m/%d  %H:%M:%S")
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _report_text(value: Any, limit: int = 260) -> str:
        text = str(value or "[بدون متن / مدیا]").strip()
        return text if len(text) <= limit else text[:limit - 1] + "…"

    def _history_transitions(self, row: dict[str, Any]) -> list[tuple[str, str, str]]:
        """Return simple before/after transitions for every recorded edit."""
        versions = sorted(row.get("versions") or [], key=lambda item: int(item[0]))
        transitions: list[tuple[str, str, str]] = []
        if not versions:
            return transitions
        for index, (version_no, changed_at, old_text, _media) in enumerate(versions):
            new_text = versions[index + 1][2] if index + 1 < len(versions) else row.get("text")
            transitions.append((self._tehran_date(changed_at), old_text or "[بدون متن / مدیا]", new_text or "[بدون متن / مدیا]"))
        return transitions

    async def _prepare_backup_rows(self, days: int) -> list[dict[str, Any]]:
        rows = self.runtime.app.deleted_backup_report(days) if self.runtime.app else []
        app = self.runtime.app
        for row in rows:
            row["sender_label"] = "کاربر ناشناس"
            sender_id = row.get("sender_id")
            if not sender_id:
                row["sender_label"] = "خود اکانت" if row.get("direction") == "out" else "کاربر ناشناس"
                continue
            try:
                entity = await app.client.get_entity(int(sender_id)) if app and app.client else None
                name = " ".join(filter(None, [getattr(entity, "first_name", ""), getattr(entity, "last_name", "")])) if entity else ""
                username = getattr(entity, "username", None) if entity else None
                row["sender_label"] = (name or "کاربر") + (f" @{username}" if username else "")
            except Exception:
                row["sender_label"] = "کاربر ناشناس"
        return rows

    def _backup_report_page(self, page: int = 0, page_size: int = 5) -> tuple[str, InlineKeyboardMarkup]:
        rows = sorted(self.backup_report_rows, key=self._backup_report_sort_key, reverse=True)
        total_pages = max(1, (len(rows) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        start = page * page_size
        current = rows[start:start + page_size]
        lines = ["گزارش پیام‌ها", "━━━━━━━━━━━━━━━━━━━━", f"{self.backup_report_days} روز اخیر | صفحهٔ {page + 1}/{total_pages} | کل: {len(rows)}", ""]
        for index, row in enumerate(current, start=start + 1):
            status = "🗑 حذف‌شده" if row.get("status") == "deleted" else "✏️ ویرایش‌شده"
            who = row.get("sender_label") or ("خود اکانت" if row.get("direction") == "out" else "کاربر ناشناس")
            lines.extend([
                f"{status}",
                f"فرستنده: {who}",
                f"تاریخ: {self._tehran_date(row.get('deleted_at') or row.get('edited_at') or row.get('created_at'))}",
            ])
            transitions = self._history_transitions(row)
            if transitions:
                for step, (changed_at, before, after) in enumerate(transitions, start=1):
                    lines.extend([f"مرحلهٔ {step} | {changed_at}", f"از: {self._report_text(before)}", f"به: {self._report_text(after)}", ""])
            else:
                lines.extend([f"متن: {self._report_text(row.get('text'))}", ""])
        if not current:
            lines.append("✅ در این بازه موردی پیدا نشد.")
        buttons = []
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton("◀️ صفحهٔ قبل", callback_data=f"backup:page:{page - 1}"))
        if page < total_pages - 1:
            navigation.append(InlineKeyboardButton("صفحهٔ بعد ▶️", callback_data=f"backup:page:{page + 1}"))
        if navigation:
            buttons.append(navigation)
        buttons.append([InlineKeyboardButton("📄 دریافت خروجی HTML", callback_data="backup:html")])
        buttons.append([InlineKeyboardButton("⬅️ بازگشت به مدیریت پیام‌ها", callback_data="menu:deleted_backup")])
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    def _backup_report_html(self) -> str:
        rows = sorted(self.backup_report_rows, key=self._backup_report_sort_key, reverse=True)
        cards = []
        for row in rows:
            status = "حذف‌شده" if row.get("status") == "deleted" else "ویرایش‌شده"
            status_class = "deleted" if row.get("status") == "deleted" else "edited"
            transitions = self._history_transitions(row)
            steps = []
            for index, (changed_at, before, after) in enumerate(transitions, start=1):
                steps.append(
                    f"<div class='step'><div class='step-head'><span class='step-label'>مرحلهٔ {index}</span><time>{html.escape(changed_at)}</time></div>"
                    f"<div class='change'><div><label>از این متن</label><p>{html.escape(str(before))}</p></div>"
                    f"<div class='arrow'>→</div><div><label>به این متن</label><p>{html.escape(str(after))}</p></div></div></div>"
                )
            if not steps:
                steps.append(f"<div class='step'><label>متن پیام</label><p>{html.escape(str(row.get('text') or '[بدون متن / مدیا]'))}</p></div>")
            cards.append(
                f"<article class='card {status_class}'>"
                f"<div class='card-top'><span class='status'>{html.escape(status)}</span></div>"
                "<div class='meta-grid'>"
                f"<div><label>کاربر</label><strong>{html.escape(str(row.get('sender_label') or ('خود اکانت' if row.get('direction') == 'out' else 'کاربر ناشناس')))}</strong></div>"
                f"<div><label>تاریخ پیام</label><strong>{html.escape(self._tehran_date(row.get('created_at')))}</strong></div>"
                f"<div><label>نوع مدیا</label><strong>{html.escape(str(row.get('media_type') or 'متن'))}</strong></div>"
                "</div>"
                f"<section class='changes'><h3>تغییرات مرحله‌به‌مرحله</h3>{''.join(steps)}</section>"
                "</article>"
            )
        return (
            "<!doctype html><html lang='fa' dir='rtl'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>گزارش پیام‌های حذف‌شده و ویرایش‌شده</title><style>"
            "*{box-sizing:border-box}body{font-family:Tahoma,'Segoe UI',Arial,sans-serif;background:linear-gradient(135deg,#eef3ff,#f8fbff);color:#152238;margin:0;padding:28px;line-height:1.8}"
            ".wrap{max-width:1100px;margin:auto}.hero{background:linear-gradient(120deg,#142b55,#315f9b);color:white;border-radius:24px;padding:32px;margin-bottom:22px;box-shadow:0 14px 40px #193b702b}.hero h1{margin:0 0 6px;font-size:30px}.hero p{margin:0;color:#dce8ff}.stats{display:flex;gap:12px;flex-wrap:wrap;margin-top:22px}.stat{background:#ffffff1c;border:1px solid #ffffff2e;border-radius:14px;padding:10px 16px}.card{background:#fff;border:1px solid #dce5f2;border-radius:20px;padding:24px;margin:18px 0;box-shadow:0 8px 28px #1e467414;position:relative;overflow:hidden}.card.deleted{border-top:5px solid #e05252}.card.edited{border-top:5px solid #4779d1}.card-top{display:flex;justify-content:space-between;align-items:center}.status{display:inline-block;background:#eaf0ff;color:#24539a;border-radius:99px;padding:5px 14px;font-weight:bold}.deleted .status{background:#fff0f0;color:#b93636}.meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin:16px 0}.meta-grid>div{background:#f5f8fc;border:1px solid #e5ebf4;border-radius:12px;padding:10px 13px}.meta-grid label,.step label{display:block;color:#71809a;font-size:12px;margin-bottom:3px}.meta-grid strong{font-size:14px;overflow-wrap:anywhere}.changes{background:#fbfcff;border:1px solid #e4eaf4;border-radius:14px;padding:15px 17px}.changes h3{font-size:16px;margin:0 0 12px;color:#274b82}.step{background:white;border:1px solid #e1e8f2;border-radius:12px;padding:12px;margin:10px 0}.step-head{color:#2a4f88;font-weight:bold;border-bottom:1px solid #edf1f7;padding-bottom:9px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}.step-label{white-space:nowrap}.step-head time{display:inline-block;font-size:12px;color:#7b8ba3;font-weight:normal;direction:ltr;unicode-bidi:embed;white-space:nowrap;background:#f1f4f9;border-radius:6px;padding:2px 7px}.change{display:grid;grid-template-columns:1fr auto 1fr;gap:12px;align-items:center;margin-top:10px}.change>div:not(.arrow){background:#f7f9fc;border-radius:9px;padding:9px;min-width:0}.change p,.step>p{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.arrow{font-size:25px;color:#5078b8;font-weight:bold}.foot{color:#78879b;text-align:center;margin-top:25px;font-size:12px}@media(max-width:600px){body{padding:12px}.hero,.card{padding:18px}.hero h1{font-size:23px}.change{grid-template-columns:1fr}.arrow{transform:rotate(90deg);text-align:center}}</style></head><body><main class='wrap'>"
            f"<header class='hero'><h1>گزارش پیام‌ها</h1><p>گزارش کامل حذف و ویرایش، مرحله‌به‌مرحله</p><div class='stats'><span class='stat'>بازه: {self.backup_report_days} روز</span><span class='stat'>تعداد: {len(rows)} مورد</span><span class='stat'>جدیدترین ابتدا</span></div></header>"
            + ("".join(cards) or "<div class='card'>موردی پیدا نشد.</div>")
            + "<div class='foot'>MORSELF Manager · گزارش تولیدشده با جزئیات کامل</div></main></body></html>"
        )

    async def _send_backup_html(self, query: Any) -> None:
        path = Path(tempfile.gettempdir()) / f"morself-backup-{self.owner_id}.html"
        path.write_text(self._backup_report_html(), encoding="utf-8")
        try:
            # InputFile treats a string as file content, not as a filesystem path.
            # Pass an opened binary handle so Telegram receives the actual HTML file.
            with path.open("rb") as handle:
                await query.get_bot().send_document(
                    chat_id=query.message.chat_id,
                    document=InputFile(handle, filename="morself-deleted-backup.html"),
                    caption=f"📄 گزارش HTML کامل | {self.backup_report_days} روز اخیر",
                )
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.reject_if_not_owner(update):
            return
        message = update.effective_message
        if message is None:
            return
        location = getattr(message, "location", None)
        if location is not None:
            latitude = getattr(location, "latitude", None)
            longitude = getattr(location, "longitude", None)
            if latitude is None or longitude is None:
                return
            self.runtime.config["live_location_bot_geo"] = {
                "lat": float(latitude),
                "long": float(longitude),
                "saved_at": core.MORSELFApp._utc_iso(),
            }
            self.runtime.config["live_location_saved_geo"] = self.runtime.config["live_location_bot_geo"]
            self.runtime.save_config()
            try:
                await message.delete()
            except Exception:
                pass
            await self._edit_panel_from_update(
                update,
                "✅ لوکیشن دریافت و ذخیره شد.\nحالا در چت مقصد، کلیدواژهٔ لایو لوکیشن را بفرست.",
            )
            return
        if (getattr(message, "forward_origin", None) is not None
                or getattr(message, "forward_from_chat", None) is not None):
            if not self.runtime.config.get("specific_delete_enabled", True):
                await self._edit_panel_from_update(update, "🔴 قابلیت حذف پیام خاموش است.")
                try:
                    await message.delete()
                except Exception:
                    pass
                return
            ok, result = await self.runtime.delete_forwarded_message(message)
            self.pending.pop(self.owner_id, None)
            try:
                await message.delete()
            except Exception:
                pass
            await self._edit_panel_from_update(update, result)
            return
        text = (message.text or "").strip()
        keyword = str(self.runtime.config.get("specific_delete_keyword", "حذف پیام")).strip()
        if (self.runtime.config.get("specific_delete_enabled", True)
                and text == keyword):
            try:
                await message.delete()
            except Exception:
                pass
            self.set_pending("specific_delete:await_forward", "پیام را برای حذف Forward کن:")
            await self._edit_panel_from_update(update, "🗑 حالا همان پیام را برای ربات Forward کن تا از چت اصلی حذف شود.")
            return
        pending = self.pending.get(self.owner_id)
        if pending is None:
            try:
                await message.delete()
            except Exception:
                pass
            await self.command_start(update, context)
            return
        if not text:
            return
        await self.clear_transient_prompt(context.bot, message.chat_id)
        try:
            await message.delete()
        except Exception:
            pass
        if pending.secret:
            try:
                await message.delete()
            except Exception:
                pass
        try:
            response, keep_pending = await self.process_pending(pending, text, message)
        except Exception as exc:
            logging.getLogger("morself_manager").exception("Pending action failed")
            response, keep_pending = f"❌ خطا: {exc}", True
        if not keep_pending:
            self.pending.pop(self.owner_id, None)
        keyboard = self.navigation_keyboard()
        if not keep_pending:
            if pending.action == "backup:list":
                _, keyboard = self._backup_report_page(0)
            elif pending.action.startswith("keyword:"):
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 ویرایش یک کلیدواژهٔ دیگر", callback_data="menu:keywords")],
                    [InlineKeyboardButton("⬅️ بازگشت به اتوماسیون‌ها", callback_data="menu:automation")],
                ])
            elif pending.action.startswith("silence_keyword:"):
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 ویرایش کلیدواژهٔ دیگر", callback_data="menu:silence_keywords")],
                    [InlineKeyboardButton("⬅️ بازگشت به مدیریت سکوت", callback_data="menu:silence")],
                ])
            elif pending.action == "specific_delete:keyword":
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 بازگشت به کلیدواژه‌های سکوت", callback_data="menu:silence_keywords")],
                    [InlineKeyboardButton("⬅️ بازگشت به مدیریت سکوت", callback_data="menu:silence")],
                ])
        if self.menu_message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=message.chat_id,
                    message_id=self.menu_message_id,
                    text=response,
                    reply_markup=keyboard,
                )
            except Exception:
                try:
                    await context.bot.delete_message(chat_id=message.chat_id, message_id=self.menu_message_id)
                except Exception:
                    pass
                self.menu_message_id = None
        if self.menu_message_id is None:
            sent = await context.bot.send_message(chat_id=message.chat_id, text=response, reply_markup=keyboard)
            self.menu_message_id = sent.message_id
            self._save_menu_message_id()

    async def process_pending(self, pending: PendingInput, text: str, message: Any = None) -> tuple[str, bool]:
        action = pending.action
        if action == "login:phone":
            if not PHONE_RE.fullmatch(text):
                return "فرمت شماره معتبر نیست. نمونه: +98912xxxxxxx", True
            ok, reply = await self.runtime.begin_login(text)
            if ok:
                self.set_pending("login:code", "کد ورود را وارد کنید:", secret=True)
                return reply, True
            return f"❌ {reply}", True
        if action == "login:code":
            if not CODE_RE.fullmatch(text):
                return "کد باید فقط شامل رقم باشد.", True
            ok, reply = await self.runtime.submit_login_code(text)
            if ok and self.runtime.login_stage == "password":
                self.set_pending("login:password", "رمز دومرحله‌ای را وارد کنید:", secret=True)
                return reply, True
            return reply if ok else f"❌ {reply}", not ok
        if action == "login:password":
            ok, reply = await self.runtime.submit_login_password(text)
            return reply if ok else f"❌ {reply}", not ok
        if action in {"silence:add", "silence:remove"}:
            app, error = self.runtime.require_active_app()
            if app is None:
                if not text.lstrip("-").isdigit():
                    return f"❌ {error}\nبرای ویرایش در حالت توقف فقط شناسهٔ عددی چت پذیرفته می‌شود.", False
                chat_id = int(text)
                try:
                    raw = json.loads(Path(self.runtime.config["state_file"]).read_text(encoding="utf-8"))
                    saved_ids = {int(value) for value in raw} if isinstance(raw, list) else set()
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    saved_ids = set()
                if action == "silence:add":
                    saved_ids.add(chat_id)
                    verb = "به فهرست سکوتِ ذخیره‌شده اضافه شد"
                else:
                    saved_ids.discard(chat_id)
                    verb = "از فهرست سکوتِ ذخیره‌شده حذف شد"
                path = Path(self.runtime.config["state_file"])
                atomic_json_write(path, sorted(saved_ids))
                return f"✅ 🆔 {chat_id}\n{verb}. در اجرای بعدی سرویس اعمال می‌شود.", False
            entity = await self.runtime.resolve_entity(text)
            entity_id = int(getattr(entity, "id"))
            label = await self.runtime.readable_entity(entity)
            if action == "silence:add":
                app.silenced_chats.add(entity_id)
                verb = "به فهرست سکوت اضافه شد"
            else:
                app.silenced_chats.discard(entity_id)
                verb = "از فهرست سکوت حذف شد"
            app._save_silenced_state()
            return f"✅ {label}\n{verb}.", False
        if action == "online:check":
            target: Any = None
            forward_origin = getattr(message, "forward_origin", None)
            forwarded_user = getattr(forward_origin, "sender_user", None)
            if forwarded_user is not None and getattr(forwarded_user, "id", None):
                target = int(forwarded_user.id)
            else:
                candidate = text.split()[0] if text.split() else ""
                if not (candidate.startswith("@") or candidate.lstrip("-").isdigit()):
                    return "❌ یوزرنیم، آیدی عددی یا پیام فورواردی معتبر بفرست.", True
                target = candidate
            self.online_target = await self.runtime.resolve_entity(target)
            self.set_pending("online:wait", "زمان بررسی را به ثانیه وارد کنید:")
            return "⏱ زمان بررسی را به ثانیه بفرست.\nمثال: `5`\nحداکثر زمان: " + str(self.runtime.config.get("online_check_max_wait_seconds", 60)) + " ثانیه", True
        if action == "online:wait":
            if not text.isdigit():
                return "⏱ فقط عدد وارد کن؛ مثال: `5`", True
            wait = max(0, min(int(text), int(self.runtime.config.get("online_check_max_wait_seconds", 60))))
            entity = self.online_target
            self.online_target = None
            if entity is None:
                return "❌ کاربر انتخاب نشده است؛ دوباره از بررسی آنلاین شروع کن.", False
            app, error = self.runtime.require_active_app()
            if app is None:
                return f"❌ {error}", False
            if getattr(entity, "bot", False):
                return "ℹ️ این شناسه یک ربات است؛ وضعیت آنلاین برای آن قابل بررسی نیست.", False
            user_id = int(getattr(entity, "id"))
            if app.self_id and user_id == app.self_id:
                return "ℹ️ حساب خودتان قابل بررسی نیست.", False
            result = await app._check_user_online(user_id, wait_seconds=wait)
            status = result.get("status", "unknown")
            icon = {"online": "🟢", "offline": "🔴", "rate_limited": "⏸", "discarded": "⚠️"}.get(status, "❔")
            label = await self.runtime.readable_entity(entity)
            return f"👁 نتیجهٔ بررسی\n\n👤 کاربر: {label}\nوضعیت: {icon} {safe_markdown(status)}\nزمان انتظار: {wait} ثانیه\nجزئیات: {safe_markdown(result.get('detail', '—'))}", False
        if action == "backup:days":
            if not text.isdigit() or not 1 <= int(text) <= 3650:
                return "تعداد روز باید بین ۱ تا ۳۶۵۰ باشد.", True
            self.runtime.config["deleted_backup_retention_days"] = int(text)
            self.runtime.save_config()
            return f"✅ مدت نگهداری آرشیو روی {text} روز تنظیم شد.", False
        if action == "backup:list":
            if not text.isdigit() or not 1 <= int(text) <= 3650:
                return "تعداد روز باید بین ۱ تا ۳۶۵۰ باشد.", True
            self.backup_report_days = int(text)
            self.backup_report_rows = await self._prepare_backup_rows(self.backup_report_days)
            report_text, _ = self._backup_report_page(0)
            return report_text, False
        if action == "delete:keyword":
            value = " ".join(text.split())
            if not value or len(value) > 80:
                return "کلیدواژه باید بین ۱ تا ۸۰ کاراکتر باشد.", True
            self.runtime.config["delete_request_keyword"] = value
            self.runtime.save_config()
            return f"✅ کلیدواژهٔ حذف پیام خواست ذخیره شد:\n`{safe_markdown(value)}`", False
        if action == "specific_delete:keyword":
            value = " ".join(text.split())
            if not value or len(value) > 80:
                return "کلیدواژه باید بین ۱ تا ۸۰ کاراکتر باشد.", True
            self.runtime.config["specific_delete_keyword"] = value
            self.runtime.save_config()
            return f"✅ کلیدواژهٔ حذف پیام مشخص ذخیره شد:\n`{safe_markdown(value)}`", False
        if action.startswith("keyword:"):
            key = action.split(":", 1)[1]
            if key not in {
                "online_on_keyword", "online_off_keyword",
                "read_on_keyword", "read_off_keyword",
                "typing_on_keyword", "typing_off_keyword",
            }:
                return "این کلیدواژه قابل ویرایش نیست.", False
            value = " ".join(text.split())
            if not value or len(value) > 80:
                return "کلیدواژه باید بین ۱ تا ۸۰ کاراکتر باشد.", True
            self.runtime.config[key] = value
            self.runtime.save_config()
            return f"✅ کلیدواژه ذخیره شد:\n`{key}` = `{value}`", False
        if action.startswith("silence_keyword:"):
            key = action.split(":", 1)[1]
            if key not in {"silence_command", "cancel_command"}:
                return "این کلیدواژهٔ سکوت قابل ویرایش نیست.", False
            value = " ".join(text.split())
            if not value or len(value) > 80:
                return "کلیدواژه باید بین ۱ تا ۸۰ کاراکتر باشد.", True
            self.runtime.config[key] = value
            self.runtime.save_config()
            return f"✅ کلیدواژهٔ سکوت ذخیره شد:\n`{key}` = `{value}`", False
        if action == "liveloc:keyword":
            value = " ".join(text.split())
            if not value or len(value) > 80:
                return "کلیدواژه باید بین ۱ تا ۸۰ کاراکتر باشد.", True
            self.runtime.config["live_location_keyword"] = value
            self.runtime.save_config()
            return f"✅ کلیدواژهٔ لایو لوکیشن ذخیره شد:\n`{safe_markdown(value)}`", False
        if action == "liveloc:period":
            if not text.isdigit():
                return "فقط عدد بفرست.", True
            value = int(text)
            if not 60 <= value <= 86400:
                return "مدت باید بین ۶۰ تا ۸۶۴۰۰ ثانیه باشد.", True
            self.runtime.config["live_location_default_period"] = value
            self.runtime.save_config()
            return f"✅ مدت زمان روشن بودن لایو لوکیشن روی {value} ثانیه تنظیم شد. پس از آن خودکار خاموش می‌شود.", False
        if action == "ai:key":
            if len(text) < 8:
                return "API Key خیلی کوتاه است؛ دوباره وارد کنید.", True
            self.runtime.config["ai_api_key"] = text
            self.runtime.save_config()
            return "✅ API Key ذخیره شد.", False
        if action == "ai:url":
            value = text.rstrip("/")
            if not re.match(r"^https?://[^\s]+$", value):
                return "Base URL باید با http:// یا https:// شروع شود.", True
            self.runtime.config["ai_base_url"] = value
            self.runtime.save_config()
            return "✅ Base URL ذخیره شد.", False
        if action == "ai:model":
            if len(text) > 200 or "\n" in text:
                return "نام مدل نامعتبر است.", True
            self.runtime.config["ai_model"] = text
            self.runtime.save_config()
            return f"✅ مدل AI روی {safe_markdown(text)} ذخیره شد.", False
        if action.startswith("settings:"):
            return await self.apply_setting(text)
        return "عملیات منقضی شده است؛ دوباره از منو شروع کنید.", False

    def editable_boolean_keys(self) -> set[str]:
        return {
            "auto_save_expiring", "save_expiring_incoming", "save_expiring_outgoing", "save_protected_messages",
            "always_online", "auto_mark_read", "auto_typing", "ai_globally_enabled", "ai_streaming_enabled",
            "ai_send_stickers", "debug_logging", "online_check_auto_delete_command",
            "live_location_enabled",
        }

    def protected_keys(self) -> set[str]:
        return {"phone", "session_name", "log_file", "state_file", "ai_config_file", "bad_ai_insults_file", "temp_media_dir", "api_id", "api_hash"}

    async def apply_setting(self, raw: str) -> tuple[str, bool]:
        if "=" not in raw:
            return "قالب نادرست است؛ نمونه: `key=value`", True
        key, value = (part.strip() for part in raw.split("=", 1))
        if not key or key in self.protected_keys() or key not in core.CONFIG_TEMPLATE and key != "ai_globally_enabled":
            return "این کلید قابل ویرایش نیست یا وجود ندارد. از «مشاهدهٔ تنظیمات امن» برای فهرست کلیدها استفاده کنید.", True
        old = self.runtime.config.get(key, core.CONFIG_TEMPLATE.get(key))
        try:
            parsed = self.coerce_value(value, old)
        except ValueError as exc:
            return f"مقدار نامعتبر است: {exc}", True
        if key == "ai_api_key":
            return "API Key را فقط از منوی AI وارد کنید.", True
        self.runtime.config[key] = parsed
        await self.runtime.apply_runtime_change(key)
        return f"✅ {key} ذخیره شد.", False

    @staticmethod
    def coerce_value(value: str, prototype: Any) -> Any:
        if isinstance(prototype, bool):
            lowered = value.casefold()
            if lowered in {"true", "1", "on", "yes", "بله", "روشن"}:
                return True
            if lowered in {"false", "0", "off", "no", "خیر", "خاموش"}:
                return False
            raise ValueError("برای مقدار بولی از true یا false استفاده کنید")
        if isinstance(prototype, int) and not isinstance(prototype, bool):
            return int(value)
        if isinstance(prototype, float):
            return float(value)
        if isinstance(prototype, (dict, list)):
            parsed = json.loads(value)
            if not isinstance(parsed, type(prototype)):
                raise ValueError("نوع JSON با تنظیم اصلی هماهنگ نیست")
            return parsed
        return value

    def _load_ai_state(self) -> dict[str, dict[str, Any]]:
        path = Path(self.runtime.config["ai_config_file"])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {str(key): value for key, value in data.items() if isinstance(value, dict)} if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _silence_count_from_disk(self) -> int:
        try:
            values = json.loads(Path(self.runtime.config["state_file"]).read_text(encoding="utf-8"))
            return len(values) if isinstance(values, list) else 0
        except (OSError, json.JSONDecodeError):
            return 0

    async def silence_list_text(self) -> str:
        app, error = self.runtime.require_active_app()
        if app is None:
            return f"📋 فهرست سکوت\n\n{error}\n\nفقط تعداد ذخیره‌شده: {self._silence_count_from_disk()}"
        if not app.silenced_chats:
            return "📋 فهرست سکوت\n\nچتی در فهرست نیست."
        lines = []
        for chat_id in sorted(app.silenced_chats):
            try:
                entity = await app.client.get_entity(chat_id)
                lines.append("• " + (await self.runtime.readable_entity(entity)).replace("\n", " | "))
            except Exception:
                lines.append(f"• 🆔 {chat_id}")
        return "📋 فهرست چت‌های ساکت\n\n" + "\n".join(lines[:80])

    async def _remove_silenced_chat(self, chat_id: int) -> None:
        app, _ = self.runtime.require_active_app()
        if app is not None:
            app.silenced_chats.discard(chat_id)
            app._save_silenced_state()
            return
        path = Path(self.runtime.config["state_file"])
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            ids = {int(value) for value in raw} if isinstance(raw, list) else set()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            ids = set()
        ids.discard(chat_id)
        atomic_json_write(path, sorted(ids))

    async def silence_menu_view(self) -> tuple[str, InlineKeyboardMarkup]:
        text, list_keys = await self.silence_management_view()
        status = "🟢 فعال" if self.runtime.app and self.runtime.app.monitoring else "🟡 آماده؛ سرویس متوقف است"
        text = f"🤫 مدیریت سکوت\n\nوضعیت: {status}\n" + text.split("\n", 2)[-1]
        rows = list(list_keys.inline_keyboard)
        rows.insert(-1, [InlineKeyboardButton("📝 تنظیم کلیدواژه‌های سکوت", callback_data="sil:commands")])
        return text, InlineKeyboardMarkup(rows)

    async def silence_management_view(self) -> tuple[str, InlineKeyboardMarkup]:
        """فهرست متنی چت‌های ساکت و دکمهٔ لغو برای هر چت."""
        app, _ = self.runtime.require_active_app()
        if app is not None:
            chat_ids = sorted(app.silenced_chats)
        else:
            try:
                raw = json.loads(Path(self.runtime.config["state_file"]).read_text(encoding="utf-8"))
                chat_ids = sorted({int(value) for value in raw}) if isinstance(raw, list) else []
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                chat_ids = []

        lines = ["⚙️ مدیریت چت‌های ساکت", "━━━━━━━━━━━━"]
        buttons: list[list[InlineKeyboardButton]] = []
        if not chat_ids:
            lines.append("\nچت ساکتی ثبت نشده است.")
        else:
            lines.append("\nچت‌های فعلی:")
            for chat_id in chat_ids[:80]:
                display_name = f"چت {chat_id}"
                if app is None:
                    label = f"🆔 {chat_id} | نوع چت پس از روشن‌شدن سرویس مشخص می‌شود"
                else:
                    try:
                        entity = await app.client.get_entity(chat_id)
                        title = " ".join(filter(None, [getattr(entity, "first_name", ""), getattr(entity, "last_name", "")]))
                        title = title or getattr(entity, "title", "") or getattr(entity, "username", "") or str(chat_id)
                        display_name = title
                        if getattr(entity, "title", None):
                            chat_type = "گروه" if getattr(entity, "megagroup", True) else "کانال"
                        else:
                            chat_type = "پی‌وی"
                        label = f"{title} | {chat_type} | 🆔 {chat_id}"
                    except Exception:
                        label = f"🆔 {chat_id} | نوع چت نامشخص"
                lines.append(f"• {html.escape(label)}")
                button_name = display_name.replace("\n", " ").strip()
                if len(button_name) > 42:
                    button_name = button_name[:39] + "..."
                if self.bot_username:
                    link = f"https://t.me/{self.bot_username}?start=unsil_{chat_id}"
                    lines[-1] += f' — <a href="{html.escape(link, quote=True)}">لغو</a>'
        buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:back")])
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    def memory_list_text(self) -> str:
        records = self.runtime.app.ai_chats if self.runtime.app else self._load_ai_state()
        if not records:
            return "📄 حافظهٔ چت‌ها\n\nداده‌ای ثبت نشده است."
        lines = []
        for chat_id, record in list(records.items())[:80]:
            history = record.get("history", []) if isinstance(record, dict) else []
            enabled = bool(record.get("enabled")) if isinstance(record, dict) else False
            lines.append(f"• `{safe_markdown(chat_id)}` — {'🟢 فعال' if enabled else '⚪ خاموش'} — {len(history) // 2} نوبت")
        return "📄 خلاصهٔ حافظهٔ چت‌ها\n\n" + "\n".join(lines)

    async def status_text(self) -> str:
        state, me = await self.runtime.account_summary()
        running = bool(self.runtime.app and self.runtime.app.monitoring)
        config = self.runtime.config
        account = "—"
        if me:
            account = await self.runtime.readable_entity(me)
        elif config.get("phone"):
            account = str(config["phone"])
        return (
            "🟢 وضعیت ربات\n\n"
            f"سرویس یوزربات: {'🟢 فعال' if running else '🔴 متوقف'}\n"
            f"Session: {'✅ معتبر' if state in {'connected', 'saved'} else '⚪ ندارد'}\n"
            f"اکانت: {account}\n\n"
            f"🤫 چت‌های ساکت: {len(self.runtime.app.silenced_chats) if self.runtime.app else self._silence_count_from_disk()}\n"
            f"💾 ذخیرهٔ مدیا: {bool_icon(config.get('auto_save_expiring'))}\n"
            f"🟢 همیشه آنلاین: {bool_icon(config.get('always_online'))}\n"
            f"✅ سین خودکار: {bool_icon(config.get('auto_mark_read'))}\n"
            f"✍️ تایپ خودکار: {bool_icon(config.get('auto_typing'))}\n"
            f"🤖 AI کلی: {bool_icon(config.get('ai_globally_enabled', True))}"
        )

    async def discover_models(self) -> str:
        api_key = str(self.runtime.config.get("ai_api_key", "")).strip()
        base_url = str(self.runtime.config.get("ai_base_url", "")).strip().rstrip("/")
        if not api_key or not base_url:
            return "⚪ ابتدا API Key و Base URL را در بخش AI تنظیم کنید."
        def request_models() -> list[str]:
            import urllib.request
            url = base_url if base_url.endswith("/models") else base_url + "/models"
            request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            return sorted({str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")})
        try:
            models = await asyncio.to_thread(request_models)
        except Exception as exc:
            return f"❌ شناسایی مدل‌ها ناموفق بود: {exc}"
        if not models:
            return "⚪ مدلی در پاسخ API یافت نشد."
        self.runtime.config["ai_models_cache"] = models
        self.runtime.save_config()
        return "🧠 مدل‌های شناسایی‌شده:\n\n" + "\n".join(f"• `{safe_markdown(model)}`" for model in models[:100])

    def safe_config_text(self) -> str:
        hidden = {"ai_api_key", "phone", "session_name", "log_file", "state_file", "ai_config_file", "bad_ai_insults_file", "temp_media_dir"}
        values = {key: value for key, value in self.runtime.config.items() if key not in hidden and key != "ai_models_cache"}
        encoded = json.dumps(values, ensure_ascii=False, indent=2)
        if len(encoded) > 3500:
            encoded = encoded[:3450] + "\n… (برای همهٔ موارد از ویرایش پیشرفته استفاده کنید)"
        return "🧾 تنظیمات امن (کلید API و مسیرها پنهان هستند)\n\n```json\n" + encoded + "\n```"

    def log_text(self) -> str:
        try:
            lines = self.runtime.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-35:]
        except OSError:
            lines = []
        return "📜 لاگ‌های اخیر\n\n" + ("```\n" + "\n".join(lines)[-3400:] + "\n```" if lines else "هنوز لاگی ثبت نشده است.")

    async def run(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
        logging.getLogger("morself_manager").info("Manager bot started for owner_id=%s", self.owner_id)
        try:
            if await self.runtime.session_is_authorized():
                ok, message = await self.runtime.start()
                logging.getLogger("morself_manager").info("Saved session startup: %s", message)
                if not ok:
                    logging.getLogger("morself_manager").error(
                        "Saved session startup failed. session_path=%s data_dir=%s error=%s",
                        self.runtime.session_path, self.runtime.data_dir, self.runtime.last_start_error or message,
                    )
            else:
                logging.getLogger("morself_manager").warning(
                    "No authorized session found at %s; login is required only once.",
                    self.runtime.session_path,
                )
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop_event.set)
                except NotImplementedError:
                    pass
            await stop_event.wait()
        finally:
            await self.runtime.stop()
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Owner-only MORSELF Telegram manager")
    parser.add_argument("--bot-token", default=os.environ.get("MOR_BOT_TOKEN", ""), help="Telegram BotFather token")
    parser.add_argument("--owner-id", default=os.environ.get("MOR_OWNER_ID", ""), help="Numeric Telegram ID allowed to manage the service")
    parser.add_argument("--data-dir", default=os.environ.get("MOR_DATA_DIR", "./data"), help="Private state directory")
    parser.add_argument("--session-name", default=DEFAULT_SESSION_NAME, help="Session file stem (letters, numbers, _ and -)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.bot_token:
        print("خطا: --bot-token یا متغیر MOR_BOT_TOKEN لازم است.", file=sys.stderr)
        return 2
    try:
        owner_id = int(args.owner_id)
        if owner_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        print("خطا: --owner-id باید یک شناسهٔ عددی مثبت تلگرام باشد.", file=sys.stderr)
        return 2
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.session_name):
        print("خطا: --session-name فقط می‌تواند حروف، عدد، _ و - داشته باشد.", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    manager = MORManagerBot(args.bot_token, owner_id, Path(args.data_dir))
    if args.session_name != DEFAULT_SESSION_NAME:
        manager.runtime = UserbotRuntime(Path(args.data_dir), args.session_name)
    try:
        asyncio.run(manager.run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
