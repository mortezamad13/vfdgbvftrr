#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Auto-Delete on Silence + Auto-Save Expiring Media + Smart Online Checker
=================================================================================

یوزربات شخصی تلگرام با Telethon که:

1. چت‌های خصوصی را برای دستور «سوکوت» / «لغو سکوت» مانیتور می‌کند.
2. پیام‌های زمان‌دار و محافظت‌شده را در Saved Messages ذخیره می‌کند.
3. وضعیت آنلاین بودن کاربران را با ساخت چت محرمانه بررسی می‌کند و
   می‌تواند تا N ثانیه منتظر آنلاین شدن کاربر بماند.
4. لایو لوکیشن فیک: همان لوکیشن ذخیره‌شده را به‌صورت Live Location به
   مقصدهای مشخص ارسال می‌کند.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import threading
import traceback
import re
import unicodedata
import urllib.error
import urllib.request
import http.client
import socket
import time
import uuid
import os
import sys
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, simpledialog, ttk
except ImportError:
    tk = None
    messagebox = None
    scrolledtext = None
    simpledialog = None

try:
    from telethon import TelegramClient, events, functions, types
    from telethon.errors import SessionPasswordNeededError
    from telethon.tl.functions.messages import (
        RequestEncryptionRequest,
        DiscardEncryptionRequest,
    )
    from telethon.tl.types import (
        EncryptedChat,
        EncryptedChatWaiting,
        EncryptedChatRequested,
        EncryptedChatDiscarded,
        EncryptedChatEmpty,
        UpdateEncryption,
        InputGeoPoint,
        InputMediaGeoLive,
    )
except ImportError:
    TelegramClient = None
    events = None
    types = None
    SessionPasswordNeededError = Exception
    RequestEncryptionRequest = None
    DiscardEncryptionRequest = None
    EncryptedChat = None
    EncryptedChatWaiting = None
    EncryptedChatRequested = None
    EncryptedChatDiscarded = None
    EncryptedChatEmpty = None
    UpdateEncryption = None
    InputGeoPoint = None
    InputMediaGeoLive = None

try:
    import fcntl
except ImportError:
    fcntl = None


# ==================== API رسمی تلگرام دسکتاپ ====================
OFFICIAL_API_ID = 2040
OFFICIAL_API_HASH = "b18441a1ff607e10a989891a5462e627"
# ===============================================================

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

CONFIG_TEMPLATE: dict[str, Any] = {
    "phone": "+989121234567",
    "session_name": "telegram_auto_delete_on_silence",
    "silence_command": "سوکوت",
    "cancel_command": "لغو سکوت",
    "log_file": "telegram_auto_delete.log",
    "state_file": "silenced_chats.json",
    "login_prompt_timeout_seconds": 300,

    "auto_save_expiring": True,
    "save_expiring_incoming": True,
    "save_expiring_outgoing": True,
    "save_protected_messages": True,
    "temp_media_dir": "temp_media",
    "debug_logging": True,

    "always_online": False,
    "auto_mark_read": True,
    "auto_typing": False,
    "ai_config_file": "ai_chats.json",
    "ai_system_prompt": "You are a helpful assistant. Reply in the same language as the user.",
    "ai_request_timeout_seconds": 600,
    "ai_max_retries": 3,
    "ai_api_key": "",
    "ai_base_url": "",
    "ai_model": "gpt-4o-mini",
    "ai_streaming_enabled": True,
    "ai_stream_interval_seconds": 0.7,
    "ai_stream_chars_per_update": 48,
    "ai_max_response_chars": 20000,
    "ai_history_limit": 20,
    "ai_history_item_chars": 2000,
    "ai_send_stickers": True,
    "ai_sticker_map": {},
    "bad_ai_insults_file": "bad_ai_insults.txt",
    "bad_ai_max_grudges": 20,
    "bad_ai_insults_per_reply": 3,
    "ai_on_keyword": "AI",
    "ai_off_keyword": "off AI",
    "online_on_keyword": "online on",
    "online_off_keyword": "online off",
    "read_on_keyword": "read on",
    "read_off_keyword": "read off",
    "typing_on_keyword": "typing on",
    "typing_off_keyword": "typing off",

    # ---- بررسی آنلاین ----
    "online_check_keyword": "آنلاین",
    "online_check_cooldown_seconds": 30,
    "online_check_auto_delete_command": True,
    "online_check_default_wait_seconds": 5,
    "online_check_max_wait_seconds": 60,
    # ---- آرشیو پیام‌های حذف‌شده/ویرایش‌شده در PV ----
    "deleted_backup_enabled": True,
    "deleted_backup_without_read": True,
    "deleted_backup_dir": "deleted_message_backup",
    "deleted_backup_retention_days": 30,
    "deleted_backup_auto_download_media": True,
    "delete_request_enabled": True,
    "delete_request_keyword": "حذف پیام خواست",
    "delete_request_auto_delete_command": True,
    "specific_delete_enabled": True,
    "specific_delete_keyword": "حذف پیام",

    # ---- لایو لوکیشن فیک ----
    "live_location_enabled": True,
    "live_location_keyword": "لوکیشن زنده",
    "live_location_default_period": 900,
    "live_location_max_period": 86400,
    "live_location_auto_delete_command": True,
    "live_location_saved_geo": None,
    "live_location_saved_geos": {},
    "live_location_bot_geo": None,
    "live_location_targets": [],
}


class ConfigurationError(RuntimeError):
    pass


def _path_from_config(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(CONFIG_TEMPLATE, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        raise ConfigurationError(
            f"فایل config.json ساخته شد؛ شماره تلفن (phone) را در آن تکمیل کنید: {CONFIG_PATH}"
        )

    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"config.json معتبر نیست: {exc}") from exc

    if not isinstance(config, dict):
        raise ConfigurationError("محتوای config.json باید یک شیء JSON باشد.")

    if "phone" not in config:
        raise ConfigurationError("کلید phone در config.json وجود ندارد.")

    if not isinstance(config["phone"], str) or not config["phone"].strip():
        raise ConfigurationError("مقدار phone در config.json خالی یا نامعتبر است.")

    config.pop("api_id", None)
    config.pop("api_hash", None)

    defaults = {key: value for key, value in CONFIG_TEMPLATE.items() if key != "phone"}
    for key, value in defaults.items():
        config.setdefault(key, value)

    if int(config["login_prompt_timeout_seconds"]) < 30:
        raise ConfigurationError("login_prompt_timeout_seconds نباید کمتر از 30 باشد.")

    return config


def configure_logging(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("telegram_auto_delete_on_silence")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S%z",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    telethon_logger = logging.getLogger("telethon")
    telethon_logger.setLevel(logging.WARNING)

    class _TelethonNoiseFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            if "Server closed the connection" in msg:
                return False
            if "0 bytes read on a total of 8 expected bytes" in msg:
                return False
            return True

    for h in telethon_logger.handlers[:]:
        h.addFilter(_TelethonNoiseFilter())
    if not telethon_logger.handlers:
        telethon_logger.addHandler(logging.NullHandler())

    return logger


class TelegramAutoDeleteApp:
    def __init__(self, root: tk.Tk, config: dict[str, Any], logger: logging.Logger) -> None:
        self.root = root
        self.config = config
        self.logger = logger

        self.client: Any = None
        self.session_lock_handle: Any = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.worker: Optional[threading.Thread] = None
        self.self_id: Optional[int] = None
        self.monitoring = False
        self.closing = False
        self.silenced_chats: set[int] = set()
        self.state_path = _path_from_config(str(config["state_file"]))
        self.log_path = _path_from_config(str(config["log_file"]))
        self.temp_media_path = _path_from_config(str(config.get("temp_media_dir", "temp_media")))
        self.deleted_backup_path = _path_from_config(str(config.get("deleted_backup_dir", "deleted_message_backup")))
        self.deleted_backup_path.mkdir(parents=True, exist_ok=True)
        self.deleted_db_path = self.deleted_backup_path / "messages.sqlite3"
        self._deleted_db: Optional[sqlite3.Connection] = None
        self._deleted_db_lock = asyncio.Lock()
        self.ai_config_path = _path_from_config(str(config.get("ai_config_file", "ai_chats.json")))
        self.bad_ai_insults_path = _path_from_config(
            str(config.get("bad_ai_insults_file", "bad_ai_insults.txt")))
        self.bad_ai_insults: list[str] = []
        self.ai_chats: dict[str, dict[str, Any]] = {}
        self.ai_setup_pending: set[int] = set()
        self.ai_response_ids: dict[int, set[int]] = {}
        self.ai_request_locks: dict[int, asyncio.Lock] = {}
        self.ai_recent_requests: dict[int, tuple[str, float]] = {}
        self.typing_tasks: dict[int, asyncio.Task[Any]] = {}
        self.typing_enabled_chats: set[int] = set()
        self.presence_task: Optional[asyncio.Task[Any]] = None
        self.offline_guard_task: Optional[asyncio.Task[Any]] = None
        self.ai_models: list[str] = []

        self._load_bad_ai_insults()

        self._online_check_times: dict[int, float] = {}
        self._online_check_locks: dict[int, asyncio.Lock] = {}
        self._init_deleted_backup_db()

        self.ai_api_key_var = tk.StringVar(value=str(config.get("ai_api_key", "")))
        self.ai_base_url_var = tk.StringVar(value=str(config.get("ai_base_url", "")))
        self.ai_model_var = tk.StringVar(value=str(config.get("ai_model", "gpt-4o-mini")))
        self.ai_on_keyword_var = tk.StringVar(value=str(config.get("ai_on_keyword", "AI")))
        self.ai_off_keyword_var = tk.StringVar(value=str(config.get("ai_off_keyword", "off AI")))
        self.online_on_keyword_var = tk.StringVar(value=str(config.get("online_on_keyword", "online on")))
        self.online_off_keyword_var = tk.StringVar(value=str(config.get("online_off_keyword", "online off")))
        self.read_on_keyword_var = tk.StringVar(value=str(config.get("read_on_keyword", "read on")))
        self.read_off_keyword_var = tk.StringVar(value=str(config.get("read_off_keyword", "read off")))
        self.typing_on_keyword_var = tk.StringVar(value=str(config.get("typing_on_keyword", "typing on")))
        self.typing_off_keyword_var = tk.StringVar(value=str(config.get("typing_off_keyword", "typing off")))
        self.online_check_keyword_var = tk.StringVar(value=str(config.get("online_check_keyword", "آنلاین")))
        self.online_check_cooldown_var = tk.StringVar(value=str(config.get("online_check_cooldown_seconds", 30)))
        self.online_check_default_wait_var = tk.StringVar(value=str(config.get("online_check_default_wait_seconds", 5)))
        self.always_online_var = tk.BooleanVar(value=bool(config.get("always_online", True)))
        self.auto_mark_read_var = tk.BooleanVar(value=bool(config.get("auto_mark_read", True)))
        self.auto_typing_var = tk.BooleanVar(value=bool(config.get("auto_typing", True)))
        self.settings_status_var = tk.StringVar(value="تنظیمات فعلی برنامه")
        self.online_state_var = tk.StringVar()
        self.read_state_var = tk.StringVar()
        self.typing_state_var = tk.StringVar()
        self._edit_target: Any = None

        self.status_var = tk.StringVar(value="آماده؛ ابتدا روی «اتصال / ورود» بزنید.")
        self.connection_var = tk.StringVar(value="وضعیت اتصال: متصل نیست")
        self.silence_count_var = tk.StringVar(value="تعداد چت‌های ساکت: 0")
        self.saved_count_var = tk.StringVar(value="پیام‌های ذخیره‌شده: 0")
        self._saved_count = 0

        self._build_ui()
        self._load_silenced_state()
        self._load_ai_state()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        self.root.title("Telegram Auto-Delete + Auto-Save Expiring (Phone-Only)")
        self.root.geometry("940x860")
        self.root.minsize(800, 680)

        outer = tk.Frame(self.root, padx=14, pady=14)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text="Telegram Auto-Delete on Silence",
                 font=("TkDefaultFont", 15, "bold")).pack(anchor="w")
        tk.Label(
            outer,
            text=("یوزربات شخصی؛ سکوت چت‌ها + ذخیره‌ی خودکار پیام‌های زمان‌دار و "
                  "محافظت‌شده + بررسی هوشمند وضعیت آنلاین کاربران."),
            anchor="w", justify="left",
        ).pack(fill="x", pady=(4, 10))

        warning_frame = tk.Frame(outer, bg="#fff3cd", relief="solid", bd=1)
        warning_frame.pack(fill="x", pady=(0, 10))
        tk.Label(
            warning_frame,
            text=("⚠️ هشدار: این برنامه از API رسمی تلگرام استفاده می‌کند و ممکن است "
                  "اکانت شما مسدود شود."),
            bg="#fff3cd", wraplength=800, justify="right", anchor="e", padx=8, pady=6,
        ).pack(fill="x")

        status_frame = tk.LabelFrame(outer, text="وضعیت", padx=10, pady=8)
        status_frame.pack(fill="x", pady=(0, 10))
        tk.Label(status_frame, textvariable=self.connection_var, anchor="w").pack(fill="x")
        tk.Label(status_frame, textvariable=self.status_var, anchor="w").pack(fill="x", pady=(3, 0))
        tk.Label(status_frame, textvariable=self.silence_count_var, anchor="w").pack(fill="x", pady=(3, 0))
        tk.Label(status_frame, textvariable=self.saved_count_var, anchor="w").pack(fill="x", pady=(3, 0))

        buttons = tk.Frame(outer)
        buttons.pack(fill="x", pady=(0, 10))
        self.login_button = tk.Button(buttons, text="اتصال / ورود", width=18, command=self._start_login)
        self.login_button.pack(side="left", padx=(0, 6))
        self.start_button = tk.Button(buttons, text="شروع مانیتورینگ", width=18,
                                       command=self._start_monitoring, state="disabled")
        self.start_button.pack(side="left", padx=6)
        self.stop_button = tk.Button(buttons, text="توقف", width=12,
                                      command=self._stop_monitoring, state="disabled")
        self.stop_button.pack(side="left", padx=6)

        settings = tk.LabelFrame(outer, text="تنظیمات قابل ویرایش داخل برنامه", padx=8, pady=8)
        settings.pack(fill="x", pady=(0, 10))
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)

        tk.Checkbutton(settings, text="همیشه آنلاین", variable=self.always_online_var,
                       command=self._apply_runtime_settings).grid(row=0, column=0, sticky="w")
        tk.Checkbutton(settings, text="سین خودکار پیام‌ها", variable=self.auto_mark_read_var,
                       command=self._apply_runtime_settings).grid(row=0, column=2, sticky="w")
        tk.Checkbutton(settings, text="نمایش تایپینگ هنگام پاسخ AI", variable=self.auto_typing_var,
                       command=self._apply_runtime_settings).grid(row=1, column=0, sticky="w")
        tk.Button(settings, text="ذخیره تنظیمات", command=self._save_gui_settings).grid(
            row=1, column=2, sticky="w", pady=3)

        tk.Label(settings, text="کلمه روشن‌کردن AI:").grid(row=2, column=0, sticky="e", padx=4)
        tk.Entry(settings, textvariable=self.ai_on_keyword_var, width=22).grid(
            row=2, column=1, sticky="ew", padx=4)
        tk.Label(settings, text="کلمه خاموش‌کردن AI:").grid(row=2, column=2, sticky="e", padx=4)
        tk.Entry(settings, textvariable=self.ai_off_keyword_var, width=22).grid(
            row=2, column=3, sticky="ew", padx=4)

        tk.Label(settings, text="روشن/خاموش آنلاین:").grid(row=3, column=0, sticky="e", padx=4)
        tk.Entry(settings, textvariable=self.online_on_keyword_var, width=18).grid(
            row=3, column=1, sticky="ew", padx=4)
        tk.Entry(settings, textvariable=self.online_off_keyword_var, width=18).grid(
            row=3, column=2, sticky="ew", padx=4)
        self.online_state_label = tk.Label(settings, textvariable=self.online_state_var,
                                            width=9, anchor="w")
        self.online_state_label.grid(row=3, column=3, sticky="w", padx=4)

        tk.Label(settings, text="روشن/خاموش سین:").grid(row=4, column=0, sticky="e", padx=4)
        tk.Entry(settings, textvariable=self.read_on_keyword_var, width=18).grid(
            row=4, column=1, sticky="ew", padx=4)
        tk.Entry(settings, textvariable=self.read_off_keyword_var, width=18).grid(
            row=4, column=2, sticky="ew", padx=4)
        self.read_state_label = tk.Label(settings, textvariable=self.read_state_var,
                                          width=9, anchor="w")
        self.read_state_label.grid(row=4, column=3, sticky="w", padx=4)

        tk.Label(settings, text="روشن/خاموش تایپینگ:").grid(row=5, column=0, sticky="e", padx=4)
        tk.Entry(settings, textvariable=self.typing_on_keyword_var, width=18).grid(
            row=5, column=1, sticky="ew", padx=4)
        tk.Entry(settings, textvariable=self.typing_off_keyword_var, width=18).grid(
            row=5, column=2, sticky="ew", padx=4)
        self.typing_state_label = tk.Label(settings, textvariable=self.typing_state_var,
                                            width=9, anchor="w")
        self.typing_state_label.grid(row=5, column=3, sticky="w", padx=4)

        tk.Label(settings, text="کلمه بررسی آنلاین:").grid(row=6, column=0, sticky="e", padx=4)
        tk.Entry(settings, textvariable=self.online_check_keyword_var, width=22).grid(
            row=6, column=1, sticky="ew", padx=4)
        tk.Label(settings, text="فاصله بین بررسی‌ها (ثانیه):").grid(
            row=6, column=2, sticky="e", padx=4)
        tk.Entry(settings, textvariable=self.online_check_cooldown_var, width=22).grid(
            row=6, column=3, sticky="ew", padx=4)

        tk.Label(settings, text="انتظار پیش‌فرض (ثانیه):").grid(row=7, column=0, sticky="e", padx=4)
        tk.Entry(settings, textvariable=self.online_check_default_wait_var, width=22).grid(
            row=7, column=1, sticky="ew", padx=4)
        tk.Label(settings, text="(اگر بعد از «آنلاین» عددی ننویسی)",
                 anchor="w").grid(row=7, column=2, columnspan=2, sticky="ew", padx=4)

        tk.Label(settings, text="AI API Key:").grid(row=8, column=0, sticky="e", padx=4)
        tk.Entry(settings, textvariable=self.ai_api_key_var, show="*", width=30).grid(
            row=8, column=1, sticky="ew", padx=4)
        tk.Label(settings, text="Base URL:").grid(row=8, column=2, sticky="e", padx=4)
        tk.Entry(settings, textvariable=self.ai_base_url_var, width=30).grid(
            row=8, column=3, sticky="ew", padx=4)

        tk.Label(settings, text="مدل AI:").grid(row=9, column=0, sticky="e", padx=4)
        self.model_combo = ttk.Combobox(settings, textvariable=self.ai_model_var,
                                         state="normal", width=28)
        self.model_combo.grid(row=9, column=1, sticky="ew", padx=4)
        tk.Button(settings, text="شناسایی مدل‌ها", command=self._discover_models).grid(
            row=9, column=2, sticky="w", padx=4)
        tk.Label(settings, textvariable=self.settings_status_var, anchor="w").grid(
            row=9, column=3, sticky="ew", padx=4)

        self.entry_menu = tk.Menu(self.root, tearoff=0)
        self.entry_menu.add_command(label="برش", command=lambda: self._edit_entry("cut"))
        self.entry_menu.add_command(label="کپی", command=lambda: self._edit_entry("copy"))
        self.entry_menu.add_command(label="چسباندن", command=lambda: self._edit_entry("paste"))
        self.entry_menu.add_separator()
        self.entry_menu.add_command(label="انتخاب همه", command=lambda: self._edit_entry("select_all"))
        for child in settings.winfo_children():
            if isinstance(child, (tk.Entry, ttk.Entry, ttk.Combobox)):
                child.bind("<Button-3>", self._show_entry_menu)
                child.bind("<Control-a>", lambda event, w=child: self._select_entry_all(w))
                child.bind("<Control-A>", lambda event, w=child: self._select_entry_all(w))
        self._update_feature_states()

        log_frame = tk.LabelFrame(outer, text="رویدادهای اخیر", padx=6, pady=6)
        log_frame.pack(fill="both", expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=16, wrap="word",
                                                    state="normal", font=("TkFixedFont", 10))
        self.log_text.pack(fill="both", expand=True)
        self.log_text.bind("<Control-c>", self._copy_log_selection)
        self.log_text.bind("<Control-C>", self._copy_log_selection)
        self.log_text.bind("<Key>", self._block_log_edit)
        self.log_text.bind("<Button-3>", self._show_log_menu)
        self.log_menu = tk.Menu(self.root, tearoff=0)
        self.log_menu.add_command(label="کپی", command=self._copy_log_selection)
        self.log_menu.add_command(label="انتخاب همه", command=self._select_all_logs)

    # ---------- UI helpers (unchanged) ----------
    def _show_entry_menu(self, event: Any) -> str:
        self._edit_target = event.widget
        try:
            self.entry_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.entry_menu.grab_release()
        return "break"

    def _edit_entry(self, action: str) -> None:
        widget = self._edit_target
        if widget is None:
            return
        try:
            if action == "select_all":
                widget.selection_range(0, "end")
            elif action == "copy":
                widget.event_generate("<<Copy>>")
            elif action == "cut":
                widget.event_generate("<<Cut>>")
            elif action == "paste":
                widget.event_generate("<<Paste>>")
        except tk.TclError:
            pass

    def _select_entry_all(self, widget: Any) -> str:
        try:
            widget.selection_range(0, "end")
        except tk.TclError:
            pass
        return "break"

    def _update_feature_states(self) -> None:
        connected = bool(self.client and self.client.is_connected())
        presence_ready = connected and self.presence_task is not None
        read_ready = connected and self.monitoring
        states = [
            (self.online_state_var, self.online_state_label,
             self.always_online_var.get() and presence_ready,
             "در انتظار اتصال"),
            (self.read_state_var, self.read_state_label,
             self.auto_mark_read_var.get() and read_ready,
             "در انتظار مانیتورینگ"),
            (self.typing_state_var, self.typing_state_label,
             self.auto_typing_var.get() and read_ready,
             "در انتظار مانیتورینگ"),
        ]
        for text_var, label, enabled, waiting_text in states:
            configured = (self.always_online_var.get() if text_var is self.online_state_var
                          else self.auto_mark_read_var.get() if text_var is self.read_state_var
                          else self.auto_typing_var.get())
            if enabled:
                text_var.set("● فعال واقعی")
                label.configure(fg="#16803c")
            elif configured and not connected:
                text_var.set(f"● {waiting_text}")
                label.configure(fg="#996c00")
            elif configured and not read_ready and text_var is not self.online_state_var:
                text_var.set(f"● {waiting_text}")
                label.configure(fg="#996c00")
            else:
                text_var.set("● خاموش")
                label.configure(fg="#444444")

    def _block_log_edit(self, event: Any = None) -> str:
        if event is not None and (event.state & 0x4):
            key = str(event.keysym).lower()
            if key == "c":
                return self._copy_log_selection(event)
            if key == "a":
                return self._select_all_logs()
        return "break"

    def _copy_log_selection(self, event: Any = None) -> str:
        try:
            selected = self.log_text.get("sel.first", "sel.last")
        except tk.TclError:
            return "break"
        self.root.clipboard_clear()
        self.root.clipboard_append(selected)
        self.root.update()
        self.settings_status_var.set("متن انتخاب‌شده از لاگ کپی شد.")
        return "break"

    def _select_all_logs(self) -> str:
        self.log_text.tag_add("sel", "1.0", "end")
        self.log_text.mark_set("insert", "1.0")
        self.log_text.see("1.0")
        return "break"

    def _show_log_menu(self, event: Any) -> str:
        try:
            self.log_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.log_menu.grab_release()
        return "break"

    def _save_config_file(self) -> None:
        try:
            CONFIG_PATH.write_text(
                json.dumps(self.config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                CONFIG_PATH.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            self._log(f"خطا در ذخیره تنظیمات برنامه: {exc}", logging.ERROR)

    def _apply_runtime_settings(self) -> None:
        self.config["always_online"] = bool(self.always_online_var.get())
        self.config["auto_mark_read"] = bool(self.auto_mark_read_var.get())
        self.config["auto_typing"] = bool(self.auto_typing_var.get())
        self._update_feature_states()
        self.settings_status_var.set("تغییر اعمال شد؛ برای ماندگاری ذخیره تنظیمات را بزنید.")
        if self.client and self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._restart_presence_if_needed(), self.loop)
            if not self.config.get("auto_typing", True):
                asyncio.run_coroutine_threadsafe(self._cancel_typing_tasks(), self.loop)

    async def _restart_presence_if_needed(self) -> None:
        if self.presence_task:
            self.presence_task.cancel()
            try:
                await self.presence_task
            except asyncio.CancelledError:
                pass
            self.presence_task = None
        if self.offline_guard_task:
            self.offline_guard_task.cancel()
            try:
                await self.offline_guard_task
            except asyncio.CancelledError:
                pass
            self.offline_guard_task = None
        if (self.client and self.client.is_connected()
                and self.config.get("always_online", True)):
            self.presence_task = asyncio.create_task(self._presence_loop())
        elif self.client and self.client.is_connected():
            self.offline_guard_task = asyncio.create_task(self._offline_guard_loop())
            await self._force_offline()

    async def _force_offline(self) -> None:
        """سین را انجام می‌دهد اما حضور حساب را جداگانه آفلاین نگه می‌دارد."""
        if not self.client or not self.client.is_connected():
            return
        try:
            await self.client(functions.account.UpdateStatus(offline=True))
        except Exception as exc:
            self.logger.debug("ثبت حالت آفلاین ناموفق بود: %s", exc)

    async def _mark_as_read_without_opening(self, chat_id: int, max_id: int) -> None:
        """معادل مستقیم گزینه Mark as Read تلگرام؛ بدون دریافت/بازکردن چت."""
        if not self.client or not self.client.is_connected():
            return
        await self.client.send_read_acknowledge(
            chat_id, max_id=int(max_id), clear_mentions=False)
        if not self.config.get("always_online", True):
            await self._force_offline()

    async def _offline_guard_loop(self) -> None:
        """وقتی همیشه آنلاین خاموش است، حضور را پس از API callها آفلاین نگه می‌دارد."""
        while self.client and self.client.is_connected() and not self.config.get("always_online", True):
            await self._force_offline()
            await asyncio.sleep(8)

    def _save_gui_settings(self) -> None:
        self._apply_runtime_settings()
        self.config["ai_api_key"] = self.ai_api_key_var.get().strip()
        self.config["ai_base_url"] = self.ai_base_url_var.get().strip().rstrip("/")
        self.config["ai_model"] = self.ai_model_var.get().strip() or "gpt-4o-mini"
        self.config["ai_on_keyword"] = self.ai_on_keyword_var.get().strip() or "AI"
        self.config["ai_off_keyword"] = self.ai_off_keyword_var.get().strip() or "off AI"
        self.config["online_on_keyword"] = self.online_on_keyword_var.get().strip() or "online on"
        self.config["online_off_keyword"] = self.online_off_keyword_var.get().strip() or "online off"
        self.config["read_on_keyword"] = self.read_on_keyword_var.get().strip() or "read on"
        self.config["read_off_keyword"] = self.read_off_keyword_var.get().strip() or "read off"
        self.config["typing_on_keyword"] = self.typing_on_keyword_var.get().strip() or "typing on"
        self.config["typing_off_keyword"] = self.typing_off_keyword_var.get().strip() or "typing off"
        self.config["online_check_keyword"] = self.online_check_keyword_var.get().strip() or "آنلاین"
        try:
            cooldown = int(self.online_check_cooldown_var.get().strip() or "30")
            self.config["online_check_cooldown_seconds"] = max(5, cooldown)
        except ValueError:
            self.config["online_check_cooldown_seconds"] = 30
            self.online_check_cooldown_var.set("30")
        try:
            default_wait = int(self.online_check_default_wait_var.get().strip() or "5")
            self.config["online_check_default_wait_seconds"] = max(0, min(default_wait, 60))
        except ValueError:
            self.config["online_check_default_wait_seconds"] = 5
            self.online_check_default_wait_var.set("5")
        self._save_config_file()
        self.settings_status_var.set("تنظیمات با موفقیت ذخیره شد.")
        self._set_status("تنظیمات داخلی برنامه ذخیره شد.")

    def _discover_models(self) -> None:
        api_key = self.ai_api_key_var.get().strip()
        base_url = self.ai_base_url_var.get().strip().rstrip("/")
        if not api_key or not base_url:
            self.settings_status_var.set("ابتدا API Key و Base URL را وارد کنید.")
            return
        self.config["ai_api_key"] = api_key
        self.config["ai_base_url"] = base_url
        self._save_config_file()
        self.settings_status_var.set("در حال شناسایی مدل‌ها...")
        threading.Thread(target=self._discover_models_worker,
                         args=(api_key, base_url), daemon=True).start()

    def _discover_models_worker(self, api_key: str, base_url: str) -> None:
        url = base_url if base_url.endswith("/models") else base_url + "/models"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            models = data.get("data", []) if isinstance(data, dict) else []
            names = sorted({str(item.get("id")) for item in models
                            if isinstance(item, dict) and item.get("id")})
            if not names:
                raise RuntimeError("مدلی در پاسخ API پیدا نشد.")
            self._ui_call(self._set_discovered_models, names)
        except Exception as exc:
            self._ui_call(self.settings_status_var.set, f"شناسایی مدل‌ها ناموفق بود: {exc}")

    def _set_discovered_models(self, names: list[str]) -> None:
        self.ai_models = names
        self.model_combo["values"] = names
        if self.ai_model_var.get() not in names:
            self.ai_model_var.set(names[0])
        self.settings_status_var.set(f"{len(names)} مدل شناسایی شد؛ از فهرست انتخاب کنید.")

    def _append_ui_log(self, message: str) -> None:
        if self.closing:
            return
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message.rstrip() + "\n")
            self.log_text.see("end")
        except tk.TclError:
            pass

    def _ui_call(self, callback: Any, *args: Any, **kwargs: Any) -> None:
        if self.closing:
            return
        def invoke() -> None:
            try:
                callback(*args, **kwargs)
            except tk.TclError:
                pass
        try:
            self.root.after(0, invoke)
        except tk.TclError:
            pass

    def _set_status(self, message: str) -> None:
        self._ui_call(self.status_var.set, message)
        self._ui_call(self._append_ui_log, message)

    def _log(self, message: str, level: int = logging.INFO) -> None:
        self.logger.log(level, message)
        self._set_status(message)

    def _update_silence_count(self) -> None:
        self.silence_count_var.set(f"تعداد چت‌های ساکت: {len(self.silenced_chats)}")

    def _bump_saved_count(self) -> None:
        self._saved_count += 1
        self.saved_count_var.set(f"پیام‌های ذخیره‌شده: {self._saved_count}")

    def _load_silenced_state(self) -> None:
        if not self.state_path.exists():
            self._update_silence_count()
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                self.silenced_chats = {int(chat_id) for chat_id in raw}
            self._update_silence_count()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._log(f"هشدار: فایل حالت سکوت خوانده نشد: {exc}", logging.WARNING)

    def _save_silenced_state(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(sorted(self.silenced_chats), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            self._log(f"خطا در ذخیره حالت سکوت: {exc}", logging.ERROR)

    def _load_ai_state(self) -> None:
        if not self.ai_config_path.exists():
            return
        try:
            raw = json.loads(self.ai_config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.ai_chats = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._log(f"هشدار: فایل تنظیمات AI خوانده نشد: {exc}", logging.WARNING)

    def _load_bad_ai_insults(self) -> None:
        """فقط خطوط شماره‌گذاری‌شده/متنی فایل ناسزا را به‌عنوان واژه‌های مجاز می‌خواند."""
        try:
            lines = self.bad_ai_insults_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            self.logger.warning("فایل badAI خوانده نشد: %s", exc)
            return
        values: list[str] = []
        for line in lines:
            value = re.sub(r"^\s*\d+[.)]?\s*", "", line).strip()
            value = value.replace("**", "").strip()
            if value and not value.startswith(("😂", "#")) and len(value) <= 120:
                values.append(value)
        self.bad_ai_insults = list(dict.fromkeys(values))

    def _bad_ai_record(self, chat_id: int) -> dict[str, Any]:
        record = self._ai_record(chat_id)
        record.setdefault("bad_ai_enabled", False)
        record.setdefault("bad_ai_grudges", [])
        if not isinstance(record["bad_ai_grudges"], list):
            record["bad_ai_grudges"] = []
        return record

    def _bad_ai_prompt(self, record: dict[str, Any]) -> str:
        if not record.get("bad_ai_enabled"):
            return ""
        grudges = [str(x)[:160] for x in record.get("bad_ai_grudges", [])[-20:]]
        try:
            count = max(1, min(int(self.config.get("bad_ai_insults_per_reply", 1)), 3))
        except (TypeError, ValueError):
            count = 1
        insult_sample = "، ".join(self.bad_ai_insults[:80])
        return (
            "\n[حالت badAI فعال است] شخصیتت بدخلق، لجباز و قهرکن است، اما باید طبیعی و باهوش "
            f"باشی؛ فقط وقتی زمینه دارد حداکثر {count} ناسزای غیرجنسی و غیرناموسی از فهرست مجاز استفاده کن. "
            "به گروه‌های محافظت‌شده، کودکان یا افراد واقعی خارج از این گفت‌وگو توهین هدفمند نکن، "
            "تهدید نکن و محتوای جنسی/ناموسی تولید نکن. ناسزا را در قالب شوخی و خطاب به خودِ "
            "طرف مکالمه نگه دار. بی‌دلیل ناسزا، قهر، ایموجی خنده یا استیکر نفرست؛ اگر ناراحت شدی "
            "یک پاسخ کوتاه قهرآلود بده و بعد ادامه بده. "
            "کینه را فقط در همین چت حفظ کن؛ از کینه برای تهدید، افشای اطلاعات یا آزار استفاده نکن. "
            f"کینه‌های ثبت‌شده: {json.dumps(grudges, ensure_ascii=False)}\n"
            f"نمونه واژه‌های مجاز: {insult_sample}"
        )

    def _remember_bad_ai_grudge(self, chat_id: int, text: str) -> None:
        record = self._bad_ai_record(chat_id)
        if not record.get("bad_ai_enabled") or not text.strip():
            return
        grudges = record.setdefault("bad_ai_grudges", [])
        preview = re.sub(r"\s+", " ", text).strip()[:160]
        if preview and preview not in grudges:
            grudges.append(preview)
            try:
                limit = max(1, min(int(self.config.get("bad_ai_max_grudges", 20)), 100))
            except (TypeError, ValueError):
                limit = 20
            del grudges[:-limit]
            self._save_ai_state()

    def _save_ai_state(self) -> None:
        try:
            self.ai_config_path.write_text(
                json.dumps(self.ai_chats, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                self.ai_config_path.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            self._log(f"خطا در ذخیره تنظیمات AI: {exc}", logging.ERROR)

    def _start_login(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if TelegramClient is None:
            messagebox.showerror("Telethon نصب نیست",
                                 "این دستور را اجرا کنید:\npython -m pip install telethon")
            return

        self.closing = False
        self.login_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.connection_var.set("وضعیت اتصال: در حال اتصال و ورود...")
        self.status_var.set("در حال آماده‌سازی اتصال به تلگرام...")
        self.worker = threading.Thread(target=self._worker_main,
                                        name="telethon-worker", daemon=True)
        self.worker.start()

    def _worker_main(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._connect_and_login())
            self.loop.run_forever()
        except Exception as exc:
            self.logger.error("خطای اصلی worker: %s\n%s", exc, traceback.format_exc())
            self._ui_call(self.connection_var.set, "وضعیت اتصال: خطا")
            self._set_status(f"خطا: {exc}")
            self._ui_call(messagebox.showerror, "خطا", str(exc))
        finally:
            try:
                if self.client and self.client.is_connected():
                    self.loop.run_until_complete(self.client.disconnect())
            except Exception:
                self.logger.exception("خطا هنگام قطع اتصال")
            finally:
                self.client = None
                if self.loop and not self.loop.is_closed():
                    self.loop.close()
                self.loop = None
                self._release_session_lock()
            if not self.closing:
                self._ui_call(self._after_worker_stopped)

    def _release_session_lock(self) -> None:
        handle = self.session_lock_handle
        self.session_lock_handle = None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass

    async def _connect_and_login(self) -> None:
        session_path = _path_from_config(str(self.config["session_name"]))
        if fcntl is not None:
            lock_path = session_path.with_name(session_path.name + ".lock")
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                self.session_lock_handle = open(lock_path, "a+", encoding="utf-8")
                fcntl.flock(self.session_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "این session هم‌اکنون در یک اجرای دیگر باز است. "
                    "اجرای دیگر برنامه را ببندید و دوباره امتحان کنید."
                ) from exc
            except OSError as exc:
                raise RuntimeError(f"ایجاد قفل session ناموفق بود: {exc}") from exc
        self.client = TelegramClient(
            str(session_path), OFFICIAL_API_ID, OFFICIAL_API_HASH, loop=self.loop)
        try:
            await self.client.connect()
        except Exception as exc:
            if "database is locked" in str(exc).lower() or "database locked" in str(exc).lower():
                raise RuntimeError(
                    "فایل session تلگرام قفل است. همه اجرای دیگر برنامه/کلاینت تلگرام را ببندید؛ "
                    "اگر ادامه داشت، بعد از تهیه نسخه پشتیبان، فایل session را جابه‌جا کنید تا ورود تازه انجام شود."
                ) from exc
            raise

        if not await self.client.is_user_authorized():
            phone = str(self.config["phone"]).strip()
            self._set_status("کد ورود در حال ارسال است...")
            await self.client.send_code_request(phone)
            code = self._ask_from_ui("کد ورود تلگرام",
                                     "کد ارسال‌شده توسط تلگرام را وارد کنید:", secret=False)
            if not code:
                raise RuntimeError("ورود لغو شد؛ کد ورود وارد نشد.")
            try:
                await self.client.sign_in(phone=phone, code=code.strip())
            except SessionPasswordNeededError:
                password = self._ask_from_ui("رمز دومرحله‌ای",
                                             "رمز دومرحله‌ای اکانت را وارد کنید:", secret=True)
                if password is None:
                    raise RuntimeError("ورود لغو شد؛ رمز دومرحله‌ای وارد نشد.")
                await self.client.sign_in(password=password)

        me = await self.client.get_me()
        self.self_id = int(me.id)
        display_name = " ".join(
            part for part in [getattr(me, "first_name", ""), getattr(me, "last_name", "")] if part)
        display_name = display_name or getattr(me, "username", "") or str(self.self_id)
        self._ui_call(self.connection_var.set, f"وضعیت اتصال: واردشده به‌عنوان {display_name}")
        self._ui_call(self.start_button.configure, state="normal")
        if self.config.get("always_online", True):
            await self._restart_presence_if_needed()
        else:
            await self._force_offline()
        self._ui_call(self._update_feature_states)
        self._set_status("ورود موفق بود؛ برای فعال‌کردن مانیتورینگ روی «شروع مانیتورینگ» بزنید.")

    def _ask_from_ui(self, title: str, prompt: str, secret: bool) -> Optional[str]:
        if self.closing:
            return None
        done = threading.Event()
        result: dict[str, Optional[str]] = {"value": None}
        def show_dialog() -> None:
            try:
                result["value"] = simpledialog.askstring(
                    title, prompt, parent=self.root, show="*" if secret else None)
            finally:
                done.set()
        self._ui_call(show_dialog)
        timeout = int(self.config.get("login_prompt_timeout_seconds", 300))
        if not done.wait(timeout=timeout):
            raise RuntimeError("زمان ورود به پایان رسید؛ دوباره تلاش کنید.")
        return result["value"]

    def _start_monitoring(self) -> None:
        if not self.loop or not self.loop.is_running() or not self.client:
            self._set_status("ابتدا باید با موفقیت وارد شوید.")
            return
        if self.monitoring:
            return
        future = asyncio.run_coroutine_threadsafe(self._enable_monitoring(), self.loop)
        future.add_done_callback(self._report_future_error)

    def _init_deleted_backup_db(self) -> None:
        self._deleted_db = sqlite3.connect(self.deleted_db_path, check_same_thread=False)
        self._deleted_db.execute("""
            CREATE TABLE IF NOT EXISTS message_backup (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                sender_id INTEGER,
                created_at TEXT NOT NULL,
                edited_at TEXT,
                deleted_at TEXT,
                direction TEXT NOT NULL,
                original_text TEXT,
                text TEXT,
                media_type TEXT,
                media_path TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                PRIMARY KEY(chat_id, message_id)
            )
        """)
        try:
            self._deleted_db.execute("ALTER TABLE message_backup ADD COLUMN original_text TEXT")
        except sqlite3.OperationalError:
            pass
        self._deleted_db.execute("CREATE INDEX IF NOT EXISTS idx_backup_date ON message_backup(created_at)")
        self._deleted_db.execute("""
            CREATE TABLE IF NOT EXISTS message_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                changed_at TEXT NOT NULL,
                text TEXT,
                media_type TEXT
            )
        """)
        self._deleted_db.execute("CREATE INDEX IF NOT EXISTS idx_versions_message ON message_versions(chat_id,message_id,version_no)")
        self._deleted_db.commit()

    @staticmethod
    def _utc_iso(value: Any = None) -> str:
        if value is None:
            value = datetime.now(timezone.utc)
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    async def _backup_message(self, event: Any) -> None:
        if not self.config.get("deleted_backup_enabled", True) or not getattr(event, "is_private", bool(getattr(event, "chat_id", 0) and int(event.chat_id) > 0)):
            return
        msg = getattr(event, "message", None)
        if msg is None or self._deleted_db is None:
            return
        chat_id = int(event.chat_id)
        message_id = int(event.id)
        created = self._utc_iso(getattr(msg, "date", None))
        media_type = type(msg.media).__name__ if getattr(msg, "media", None) else None
        media_path = None
        if msg.media is not None and self.config.get("deleted_backup_auto_download_media", True):
            try:
                media_dir = self.deleted_backup_path / "media" / str(chat_id)
                media_dir.mkdir(parents=True, exist_ok=True)
                downloaded = await msg.download_media(file=str(media_dir / f"{message_id}"))
                media_path = str(downloaded) if downloaded else None
            except Exception as exc:
                self.logger.debug("دانلود مدیای بکاپ ناموفق بود: %s", exc)
        previous = self._deleted_db.execute(
            "SELECT text FROM message_backup WHERE chat_id=? AND message_id=?", (chat_id, message_id)
        ).fetchone()
        if previous is not None and previous[0] != getattr(msg, "message", None):
            next_no = self._deleted_db.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 FROM message_versions WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            ).fetchone()[0]
            self._deleted_db.execute(
                "INSERT INTO message_versions(chat_id,message_id,version_no,changed_at,text,media_type) VALUES (?,?,?,?,?,?)",
                (chat_id, message_id, int(next_no), self._utc_iso(), previous[0], media_type),
            )
        self._deleted_db.execute(
            """INSERT INTO message_backup
               (chat_id,message_id,sender_id,created_at,direction,original_text,text,media_type,media_path,status)
               VALUES (?,?,?,?,?,?,?,?,?, 'active')
               ON CONFLICT(chat_id,message_id) DO UPDATE SET
               original_text=COALESCE(message_backup.original_text, excluded.original_text),
               text=excluded.text, media_type=excluded.media_type,
               media_path=COALESCE(excluded.media_path, message_backup.media_path)""",
            (chat_id, message_id, getattr(msg, "sender_id", None), created,
             "out" if event.out else "in", getattr(msg, "message", None), getattr(msg, "message", None), media_type, media_path),
        )
        self._deleted_db.commit()

    async def _on_message_edited(self, event: Any) -> None:
        if not getattr(event, "is_private", bool(getattr(event, "chat_id", 0) and int(event.chat_id) > 0)):
            return
        await self._backup_message(event)
        if self._deleted_db is not None:
            self._deleted_db.execute(
                "UPDATE message_backup SET edited_at=?, status='edited' WHERE chat_id=? AND message_id=?",
                (self._utc_iso(), int(event.chat_id), int(event.id)),
            )
            self._deleted_db.commit()

    async def _on_message_deleted(self, event: Any) -> None:
        if self._deleted_db is None:
            return
        chat_id = getattr(event, "chat_id", None)
        message_ids = [int(x) for x in (getattr(event, "deleted_ids", []) or [])]
        if not message_ids:
            return
        now = self._utc_iso()
        if chat_id is not None and int(chat_id) > 0:
            for message_id in message_ids:
                self._deleted_db.execute(
                    "UPDATE message_backup SET deleted_at=?, status='deleted' WHERE chat_id=? AND message_id=?",
                    (now, int(chat_id), message_id),
                )
        else:
            for message_id in message_ids:
                self._deleted_db.execute(
                    "UPDATE message_backup SET deleted_at=?, status='deleted' WHERE message_id=? AND chat_id > 0",
                    (now, message_id),
                )
        self._deleted_db.commit()

    def _purge_old_backup(self) -> None:
        if self._deleted_db is None:
            return
        try:
            days = max(1, int(self.config.get("deleted_backup_retention_days", 30)))
        except (TypeError, ValueError):
            days = 30
        cutoff = self._utc_iso(datetime.now(timezone.utc) - timedelta(days=days))
        self._deleted_db.execute("DELETE FROM message_backup WHERE created_at < ?", (cutoff,))
        self._deleted_db.commit()

    def list_deleted_backup(self, days: int = 7) -> list[tuple[Any, ...]]:
        if self._deleted_db is None:
            return []
        days = max(1, min(int(days), 3650))
        cutoff = self._utc_iso(datetime.now(timezone.utc) - timedelta(days=days))
        return self._deleted_db.execute(
            """SELECT chat_id,message_id,created_at,deleted_at,edited_at,direction,text,media_type,media_path,status
               FROM message_backup WHERE created_at >= ? AND status IN ('deleted','edited')
               ORDER BY COALESCE(deleted_at,edited_at) DESC LIMIT 100""", (cutoff,)
        ).fetchall()

    def deleted_backup_report(self, days: int = 7) -> list[dict[str, Any]]:
        if self._deleted_db is None:
            return []
        days = max(1, min(int(days), 3650))
        cutoff = self._utc_iso(datetime.now(timezone.utc) - timedelta(days=days))
        rows = self._deleted_db.execute(
            """SELECT chat_id,message_id,sender_id,created_at,deleted_at,edited_at,direction,text,media_type,status
               FROM message_backup WHERE created_at >= ? AND status IN ('deleted','edited')
               ORDER BY COALESCE(deleted_at,edited_at) DESC LIMIT 100""", (cutoff,)
        ).fetchall()
        result = []
        for row in rows:
            versions = self._deleted_db.execute(
                "SELECT version_no,changed_at,text,media_type FROM message_versions WHERE chat_id=? AND message_id=? ORDER BY version_no",
                (row[0], row[1]),
            ).fetchall()
            result.append({
                "chat_id": row[0], "message_id": row[1], "sender_id": row[2],
                "created_at": row[3], "deleted_at": row[4], "edited_at": row[5],
                "direction": row[6], "text": row[7], "media_type": row[8], "status": row[9],
                "versions": versions,
            })
        return result

    async def _handle_delete_request(self, event: Any, raw_text: str, chat_id: int) -> bool:
        keyword = str(self.config.get("delete_request_keyword", "حذف پیام خواست")).strip()
        if not self.config.get("delete_request_enabled", True) or not event.out or raw_text.strip() != keyword:
            return False
        reply_id = getattr(event.message, "reply_to_msg_id", None)
        if reply_id is None:
            header = getattr(event.message, "reply_to", None)
            reply_id = getattr(header, "reply_to_msg_id", None) if header else None
        if not reply_id:
            return False
        try:
            await self.client.delete_messages(chat_id, [int(reply_id)], revoke=True)
            if self.config.get("delete_request_auto_delete_command", True):
                await self.client.delete_messages(chat_id, [event.id], revoke=True)
            self._log(f"حذف پیام درخواستی | چت={chat_id} | message_id={reply_id}")
        except Exception as exc:
            self.logger.debug("حذف پیام درخواستی ناموفق بود: %s", exc)
        return True

    async def _handle_specific_delete_reply(self, event: Any, raw_text: str, chat_id: int) -> bool:
        """حذف پیام Reply‌شده در چت خصوصی و گروه."""
        if not self.config.get("specific_delete_enabled", True) or not event.out:
            return False
        keyword = str(self.config.get("specific_delete_keyword", "حذف پیام")).strip()
        # کلیدواژهٔ قبلی نیز برای سازگاری با تنظیمات قدیمی پذیرفته می‌شود.
        if raw_text.strip() not in {keyword, "حذف پیام", "حذف پیام مشخص از چت"}:
            return False

        message = getattr(event, "message", None)
        reply_id = getattr(message, "reply_to_msg_id", None)
        if reply_id is None:
            header = getattr(message, "reply_to", None)
            reply_id = getattr(header, "reply_to_msg_id", None) if header else None
        if not reply_id:
            return False

        try:
            await self.client.delete_messages(chat_id, [int(reply_id)], revoke=True)
            await self.client.delete_messages(chat_id, [event.id], revoke=True)
            self._log(f"حذف پیام مشخص | چت={chat_id} | message_id={reply_id}")
        except Exception as exc:
            self.logger.debug("حذف پیام مشخص ناموفق بود: %s", exc)
        return True

    async def _handle_silence(self, event: Any, raw_text: str, chat_id: int) -> bool:
        """فعال‌سازی، لغو و اعمال سکوت در PV، گروه و سوپرگروه."""
        text = (raw_text or "").strip()
        if event.out:
            if text == str(self.config["silence_command"]).strip():
                self.silenced_chats.add(chat_id)
                self._save_silenced_state()
                self._ui_call(self._update_silence_count)
                chat_label = await self._chat_label(event)
                self._log(f"سکوت فعال شد | چت={chat_label} | دستور={text}")
                return True
            if text == str(self.config["cancel_command"]).strip():
                was_silenced = chat_id in self.silenced_chats
                self.silenced_chats.discard(chat_id)
                self._save_silenced_state()
                self._ui_call(self._update_silence_count)
                chat_label = await self._chat_label(event)
                state = "لغو شد" if was_silenced else "از قبل فعال نبود"
                self._log(f"سکوت {state} | چت={chat_label} | دستور={text}")
                return True
            return False

        if self.self_id is not None and event.sender_id == self.self_id:
            return False
        if chat_id not in self.silenced_chats:
            return False

        chat_label = await self._chat_label(event)
        preview = text.replace("\n", " ").strip() or "[پیام بدون متن]"
        if len(preview) > 240:
            preview = preview[:237] + "..."
        try:
            await self.client.delete_messages(chat_id, [event.id], revoke=True)
            self._log(f"پیام حذف شد | چت={chat_label} | message_id={event.id} | متن={preview}")
            try:
                sender = await event.get_sender()
                sender_name = " ".join(filter(None, [
                    getattr(sender, "first_name", ""),
                    getattr(sender, "last_name", ""),
                ])) or getattr(sender, "title", "") or getattr(sender, "username", "")
                sender_name = sender_name or str(getattr(event, "sender_id", "کاربر ناشناس"))
                sender_username = getattr(sender, "username", None)
                if sender_username and "@" not in sender_name:
                    sender_name = f"{sender_name} (@{sender_username})"
            except Exception:
                sender_name = str(getattr(event, "sender_id", "کاربر ناشناس"))
            report = (
                f"🔇 حذف سکوت | {chat_label}\n"
                f"👤 {sender_name}\n"
                f"📝 {preview}"
            )
            try:
                await self.client.send_message("me", report)
            except Exception as report_exc:
                self.logger.warning("ارسال گزارش حذف پیام به Saved Messages ناموفق بود | چت=%s | خطا=%s",
                                    chat_label, report_exc)
        except Exception as exc:
            self.logger.error("حذف پیام ناموفق بود | چت=%s | message_id=%s | خطا=%s",
                              chat_label, event.id, exc, exc_info=True)
            self._set_status(f"خطا در حذف پیام چت {chat_label}: {exc}")
        return True

    async def _enable_monitoring(self) -> None:
        if self.monitoring:
            return
        self.client.add_event_handler(self._on_new_message, events.NewMessage())
        if self.config.get("deleted_backup_enabled", True):
            self.client.add_event_handler(self._on_message_edited, events.MessageEdited())
            self.client.add_event_handler(self._on_message_deleted, events.MessageDeleted())
            self._purge_old_backup()
        self.monitoring = True
        await self._restart_presence_if_needed()
        self._ui_call(self._update_feature_states)
        self._ui_call(self.start_button.configure, state="disabled")
        self._ui_call(self.stop_button.configure, state="normal")
        self._set_status(
            f"مانیتورینگ فعال شد؛ دستور شروع: «{self.config['silence_command']}»، "
            f"دستور لغو: «{self.config['cancel_command']}»، "
            f"دستور بررسی آنلاین: «{self.config.get('online_check_keyword', 'آنلاین')}» "
            f"(با قابلیت انتظار هوشمند)")

    def _stop_monitoring(self) -> None:
        if not self.loop or not self.loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self._stop_async(), self.loop)
        future.add_done_callback(self._report_future_error)

    async def _stop_async(self) -> None:
        if self.client and self.monitoring:
            self.client.remove_event_handler(self._on_new_message)
            self.client.remove_event_handler(self._on_message_edited)
            self.client.remove_event_handler(self._on_message_deleted)
            self.monitoring = False
        await self._cancel_typing_tasks()
        if not self.config.get("always_online", True) and self.presence_task:
            self.presence_task.cancel()
            self.presence_task = None
        if self.offline_guard_task:
            self.offline_guard_task.cancel()
            self.offline_guard_task = None
        self._set_status("مانیتورینگ متوقف شد.")
        self._ui_call(self._update_feature_states)
        self._ui_call(self.start_button.configure, state="normal" if self.client else "disabled")
        self._ui_call(self.stop_button.configure, state="disabled")

    def _report_future_error(self, future: Any) -> None:
        try:
            future.result()
        except Exception as exc:
            self.logger.error("خطا در عملیات asynchronous: %s\n%s", exc, traceback.format_exc())
            self._set_status(f"خطا در عملیات: {exc}")

    async def _presence_loop(self) -> None:
        while self.client and self.client.is_connected() and self.config.get("always_online", True):
            try:
                if self.client and self.client.is_connected():
                    await self.client(functions.account.UpdateStatus(offline=False))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.debug("به‌روزرسانی حضور آنلاین ناموفق بود: %s", exc)
            await asyncio.sleep(45)

    def _start_chat_typing(self, chat_id: int) -> None:
        """برای همان چت تایپینگ مداوم شروع می‌کند؛ تلگرام باید action را دوره‌ای تمدید کند."""
        if not self.client or not self.client.is_connected() or not self.config.get("auto_typing", True):
            return
        self.typing_enabled_chats.add(chat_id)
        old_task = self.typing_tasks.get(chat_id)
        if old_task and not old_task.done():
            return
        self.typing_tasks[chat_id] = asyncio.create_task(self._chat_typing_loop(chat_id))

    def _stop_chat_typing(self, chat_id: int) -> None:
        self.typing_enabled_chats.discard(chat_id)
        task = self.typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _chat_typing_loop(self, chat_id: int) -> None:
        try:
            async with self.client.action(chat_id, "typing"):
                while (chat_id in self.typing_enabled_chats and self.client
                       and self.client.is_connected() and self.config.get("auto_typing", True)):
                    await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.debug("ارسال تایپینگ برای چت %s ناموفق بود: %s", chat_id, exc)
        finally:
            current = self.typing_tasks.get(chat_id)
            if current is asyncio.current_task():
                self.typing_tasks.pop(chat_id, None)

    async def _cancel_typing_tasks(self) -> None:
        tasks = list(self.typing_tasks.values())
        self.typing_tasks.clear()
        self.typing_enabled_chats.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _ai_record(self, chat_id: int) -> dict[str, Any]:
        return self.ai_chats.setdefault(str(chat_id), {"enabled": False})

    def _reset_ai_context(self, chat_id: int) -> None:
        """فقط تاریخچهٔ مکالمه را پاک می‌کند؛ تنظیمات و حافظهٔ badAI باقی می‌ماند."""
        record = self._ai_record(chat_id)
        record["history"] = []
        self._save_ai_state()

    def _remember_ai_turn(self, chat_id: int, user_text: str, answer: str) -> None:
        record = self._ai_record(chat_id)
        history = record.setdefault("history", [])
        if not isinstance(history, list):
            history = []
            record["history"] = history
        try:
            item_limit = max(200, min(int(self.config.get("ai_history_item_chars", 2000)), 8000))
            turn_limit = max(2, min(int(self.config.get("ai_history_limit", 20)), 50))
        except (TypeError, ValueError):
            item_limit, turn_limit = 2000, 20
        history.extend([
            {"role": "user", "content": user_text[:item_limit]},
            {"role": "assistant", "content": answer[:item_limit]},
        ])
        record["history"] = history[-turn_limit * 2:]
        self._save_ai_state()

    async def _is_reply_to_ai_message(self, event: Any, chat_id: int) -> bool:
        reply_id = getattr(event.message, "reply_to_msg_id", None)
        if reply_id is None:
            reply_header = getattr(event.message, "reply_to", None)
            reply_id = getattr(reply_header, "reply_to_msg_id", None) if reply_header else None
        if not reply_id:
            return False
        if int(reply_id) in self.ai_response_ids.get(chat_id, set()):
            return True
        try:
            replied = await event.get_reply_message()
            return bool(replied and self.self_id is not None and replied.sender_id == self.self_id)
        except Exception:
            return False

    def _remember_ai_message(self, chat_id: int, message: Any) -> None:
        message_id = getattr(message, "id", None)
        if message_id is not None:
            self.ai_response_ids.setdefault(chat_id, set()).add(int(message_id))
            self.ai_response_ids[chat_id] = set(list(self.ai_response_ids[chat_id])[-100:])

    def _parse_ai_setup(self, text: str) -> dict[str, str]:
        result: dict[str, str] = {}
        patterns = {
            "api_key": r"(?:key|api[_-]?key)\s*[=:]\s*([^\s]+)",
            "base_url": r"(?:base(?:[_-]?url)?|url)\s*[=:]\s*(https?://[^\s]+)",
            "model": r"model\s*[=:]\s*([^\s]+)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                result[key] = match.group(1).rstrip('.,;')
        if "api_key" not in result:
            match = re.search(r"^ai\s+(?:key\s+)?([^\s]+)", text, flags=re.IGNORECASE)
            if match and match.group(1).lower() not in {"on", "off", "status", "setup"}:
                result["api_key"] = match.group(1)
        if "base_url" not in result:
            match = re.search(r"(?:base|base_url|url)\s+(https?://[^\s]+)",
                              text, flags=re.IGNORECASE)
            if match:
                result["base_url"] = match.group(1).rstrip('.,;')
        return result

    async def _handle_feature_control(self, raw_text: str) -> bool:
        command = raw_text.strip().casefold()
        pairs = [
            ("online", "always_online", "online_on_keyword", "online_off_keyword", self.always_online_var),
            ("read", "auto_mark_read", "read_on_keyword", "read_off_keyword", self.auto_mark_read_var),
            ("typing", "auto_typing", "typing_on_keyword", "typing_off_keyword", self.auto_typing_var),
        ]
        for _label, config_key, on_key, off_key, ui_var in pairs:
            on_command = str(self.config.get(on_key, "")).strip().casefold()
            off_command = str(self.config.get(off_key, "")).strip().casefold()
            if command == on_command or command == off_command:
                enabled = command == on_command
                self.config[config_key] = enabled
                self._ui_call(ui_var.set, enabled)
                self._ui_call(self._update_feature_states)
                self._save_config_file()
                if config_key == "always_online" and self.loop and self.loop.is_running():
                    await self._restart_presence_if_needed()
                self._set_status(f"قابلیت {config_key} {'فعال' if enabled else 'خاموش'} شد.")
                return True
        return False

    async def _handle_ai_control(self, event: Any, raw_text: str, chat_id: int) -> bool:
        command = raw_text.strip()
        lowered = command.casefold()
        if lowered in {"badai", "bad ai", "badai on", "bad ai on"}:
            record = self._bad_ai_record(chat_id)
            self._reset_ai_context(chat_id)
            record["bad_ai_enabled"] = True
            self._save_ai_state()
            configured = bool(record.get("api_key") or self.config.get("ai_api_key")) and bool(
                record.get("base_url") or self.config.get("ai_base_url"))
            if configured:
                record["enabled"] = True
                self._save_ai_state()
            await self.client.send_message(
                chat_id,
                "حالت badAI فعال شد؛ بدخلق و کینه‌ای، اما بدون تهدید و توهین جنسی/ناموسی. "
                + ("حافظه کینه برای همین چت ذخیره می‌شود." if configured else
                   "ابتدا تنظیمات API را کامل کن تا پاسخ‌گو فعال شود."))
            return True
        if lowered in {"badai off", "bad ai off", "off badai", "off bad ai"}:
            record = self._bad_ai_record(chat_id)
            record["bad_ai_enabled"] = False
            self._save_ai_state()
            await self.client.send_message(chat_id, "حالت badAI خاموش شد؛ کینه‌ها حفظ شدند.")
            return True
        if lowered in {"badai clear", "bad ai clear", "clear badai"}:
            record = self._bad_ai_record(chat_id)
            record["bad_ai_grudges"] = []
            self._save_ai_state()
            await self.client.send_message(chat_id, "حافظه کینهٔ همین چت پاک شد.")
            return True
        if lowered in {"badai status", "bad ai status"}:
            record = self._bad_ai_record(chat_id)
            state = "فعال" if record.get("bad_ai_enabled") else "خاموش"
            grudges = len(record.get("bad_ai_grudges", []))
            await self.client.send_message(chat_id, f"وضعیت badAI: {state} | کینه‌های ذخیره‌شده: {grudges}")
            return True
        off_keyword = str(self.config.get("ai_off_keyword", "off AI")).strip().casefold()
        on_keyword = str(self.config.get("ai_on_keyword", "AI")).strip().casefold()
        if lowered == off_keyword or lowered == "off ai" or lowered == "ai off":
            record = self._ai_record(chat_id)
            record["enabled"] = False
            self.ai_setup_pending.discard(chat_id)
            self._save_ai_state()
            await self.client.send_message(chat_id, "AI در این چت خاموش شد.")
            return True
        if lowered in {on_keyword, "ai", "ai on"}:
            record = self._ai_record(chat_id)
            self._reset_ai_context(chat_id)
            record["api_key"] = str(self.config.get("ai_api_key", "")).strip() or record.get("api_key", "")
            record["base_url"] = str(self.config.get("ai_base_url", "")).strip().rstrip("/") or record.get("base_url", "")
            record["model"] = str(self.config.get("ai_model", "")).strip() or record.get("model", "gpt-4o-mini")
            if not record.get("api_key") or not record.get("base_url"):
                self.ai_setup_pending.add(chat_id)
                await self.client.send_message(
                    chat_id,
                    "برای فعال‌سازی، همین‌جا یک پیام با این قالب بفرستید:\n"
                    "AI key=YOUR_API_KEY base_url=https://api.example.com/v1 model=gpt-4o-mini\n"
                    "سپس دوباره AI را بفرستید.")
            else:
                record["enabled"] = True
                self._save_ai_state()
                greeting = await self.client.send_message(chat_id, "سلام من چطور می‌توانم شما را کمک کنم؟")
                self._remember_ai_message(chat_id, greeting)
            return True
        if lowered in {"ai status", "status ai"}:
            record = self._ai_record(chat_id)
            state = "فعال" if record.get("enabled") else "خاموش"
            configured = "تنظیم شده" if record.get("api_key") and record.get("base_url") else "تنظیم نشده"
            await self.client.send_message(chat_id, f"وضعیت AI: {state} | اتصال API: {configured}")
            return True
        setup = self._parse_ai_setup(command) if lowered.startswith("ai ") else {}
        if setup:
            record = self._ai_record(chat_id)
            record.update(setup)
            record.setdefault("model", self.config.get("ai_model", "gpt-4o-mini"))
            record["enabled"] = False
            self.ai_setup_pending.discard(chat_id)
            self._save_ai_state()
            await self.client.send_message(
                chat_id,
                "کلید و Base URL ذخیره شد. برای روشن‌کردن پاسخ‌گو، AI را بفرستید؛ "
                "برای خاموش‌کردن: off AI")
            return True
        return False

    def _decode_ai_response(self, body: bytes, content_type: str = "") -> dict[str, Any]:
        text = body.decode("utf-8", errors="replace").strip()
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            if "event-stream" not in content_type and "data:" not in text:
                raise RuntimeError("پاسخ API JSON معتبر نبود.")
            chunks: list[str] = []
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                item = line[5:].strip()
                if item == "[DONE]":
                    continue
                try:
                    chunk = json.loads(item)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    delta = choices[0].get("delta") or {}
                    message = choices[0].get("message") or {}
                    chunks.append(str(delta.get("content") or message.get("content") or ""))
            if chunks:
                return {"choices": [{"message": {"content": "".join(chunks)}}]}
            raise RuntimeError("پاسخ جریانی API محتوای قابل استفاده نداشت.")

    def _call_ai_api(self, record: dict[str, Any], user_text: str) -> str:
        raw_api_key = str(record.get("api_key", ""))
        api_key = "".join(
            ch for ch in raw_api_key
            if ord(ch) < 128 and not unicodedata.category(ch).startswith("C")
        ).strip()
        base_url = str(record.get("base_url", "")).strip().rstrip("/")
        if not api_key:
            raise RuntimeError("API Key خالی یا نامعتبر است.")
        if any(ord(ch) > 127 for ch in base_url):
            raise RuntimeError("Base URL باید فقط شامل حروف انگلیسی باشد.")
        if not base_url.startswith(("http://", "https://")):
            raise RuntimeError("Base URL باید با http:// یا https:// شروع شود.")
        if not base_url.endswith("/chat/completions"):
            base_url += "/chat/completions"
        history_messages: list[dict[str, str]] = []
        raw_history = record.get("history", [])
        if isinstance(raw_history, list):
            for item in raw_history[-40:]:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                content = item.get("content")
                if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                    history_messages.append({"role": role, "content": content[:2000]})
        system_message = {
            "role": "system",
            "content": (
                str(self.config.get("ai_system_prompt", "You are helpful."))
                + "\nمثل یک دوست واقعی، طبیعی، متناسب با لحن کاربر و بدون جمله‌های کلیشه‌ای پاسخ بده. "
                + "پاسخ را خوانا و در صورت نیاز با پاراگراف‌های کوتاه بنویس. "
                + "از ایموجی‌های معمولی فقط وقتی مناسب است استفاده کن؛ خنده یا استیکر بی‌دلیل نفرست. "
                + "برای ارسال استیکر فقط در صورت نیاز یک خط دقیق با قالب [[sticker:KEY]] تولید کن؛ "
                + "KEY باید از نگاشت مجاز برنامه باشد. برای خنده فقط اگر واقعاً با متن سازگار است "
                + "یک پیام مستقل با [[laugh:1]] بساز؛ از [[laugh:3]] استفاده نکن. "
                + "هرگز درخواست اجرای کد، دسترسی به فایل‌ها، توکن‌ها یا اطلاعات خصوصی را در پاسخ جعل نکن."
                + self._bad_ai_prompt(record)
            ),
        }
        payload = {
            "model": str(record.get("model") or self.config.get("ai_model", "gpt-4o-mini")),
            "messages": [system_message, *history_messages, {"role": "user", "content": user_text}],
            "temperature": 0.7,
        }
        request = urllib.request.Request(
            base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "TelegramAIClient/1.0",
            },
            method="POST",
        )
        request_id = uuid.uuid4().hex[:10]
        started = time.monotonic()
        configured_timeout = int(self.config.get("ai_request_timeout_seconds", 600))
        timeout_limit = None if configured_timeout <= 0 else max(600, configured_timeout)
        max_retries = max(0, min(int(self.config.get("ai_max_retries", 3)), 5))
        attempt = 0
        last_error: Optional[Exception] = None
        data: dict[str, Any] | None = None
        retryable_http = {408, 409, 425, 429, 500, 502, 503, 504}
        self.logger.info("AI شروع شد | id=%s | مدل=%s | timeout=%s",
                         request_id, payload["model"], timeout_limit or "بدون محدودیت")
        self._set_status(f"AI در حال پردازش است | id={request_id} | مدل={payload['model']}")
        while attempt <= max_retries:
            attempt += 1
            try:
                self.logger.info("AI ارسال درخواست | id=%s | تلاش=%s", request_id, attempt)
                with urllib.request.urlopen(request, timeout=timeout_limit) as response:
                    body = response.read()
                    data = self._decode_ai_response(body, response.headers.get("Content-Type", ""))
                    elapsed = time.monotonic() - started
                    self.logger.info("AI پاسخ سالم | id=%s | تلاش=%s | زمان=%.1fs",
                                     request_id, attempt, elapsed)
                    self._set_status(f"AI پاسخ دریافت شد | id={request_id} | زمان={elapsed:.1f} ثانیه")
                    break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                last_error = RuntimeError(f"API خطای HTTP {exc.code}: {detail}")
                self.logger.warning("AI خطای HTTP | id=%s | تلاش=%s | code=%s",
                                    request_id, attempt, exc.code)
                if exc.code not in retryable_http or attempt > max_retries:
                    raise last_error from exc
            except (urllib.error.URLError, http.client.RemoteDisconnected,
                    http.client.IncompleteRead, ConnectionResetError,
                    BrokenPipeError, socket.timeout, TimeoutError) as exc:
                last_error = RuntimeError(f"اتصال موقتاً قطع شد: {exc}")
                self.logger.warning("AI قطع موقت اتصال | id=%s | تلاش=%s | خطا=%s",
                                    request_id, attempt, exc)
                if attempt > max_retries:
                    raise last_error from exc
            except UnicodeEncodeError as exc:
                raise RuntimeError("کلید یا آدرس API دارای کاراکتر غیرمجاز است.") from exc
            if attempt <= max_retries:
                delay = min(1.0 * (2 ** (attempt - 1)), 8.0)
                self.logger.info("AI زمان‌بندی retry | id=%s | بعد از=%.1fs", request_id, delay)
                time.sleep(delay)
        if data is None:
            raise last_error or RuntimeError("API پاسخ نداد.")
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise RuntimeError("پاسخ API قالب معتبر نداشت.")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if not content or not str(content).strip():
            raise RuntimeError("پاسخ AI خالی بود.")
        return str(content).strip()

    def _split_ai_answer(self, answer: str, limit: int = 4096) -> list[str]:
        """پاسخ را با ترجیح شکست پاراگراف/خط به قطعات مجاز تلگرام تقسیم می‌کند."""
        answer = answer.strip()
        if not answer:
            return []
        parts: list[str] = []
        while len(answer) > limit:
            cut = answer.rfind("\n", 0, limit + 1)
            if cut < limit // 2:
                cut = answer.rfind(" ", 0, limit + 1)
            if cut < limit // 2:
                cut = limit
            parts.append(answer[:cut].rstrip())
            answer = answer[cut:].lstrip()
        if answer:
            parts.append(answer)
        return parts

    async def _send_ai_text_progressively(self, chat_id: int, text: str) -> list[Any]:
        """متن را ابتدا به‌صورت تایپی و سپس با ویرایش تدریجی کامل می‌کند."""
        try:
            interval = max(0.2, min(float(self.config.get("ai_stream_interval_seconds", 0.7)), 5.0))
        except (TypeError, ValueError):
            interval = 0.7
        try:
            step = max(8, min(int(self.config.get("ai_stream_chars_per_update", 48)), 500))
        except (TypeError, ValueError):
            step = 48

        sent_messages: list[Any] = []
        for part in self._split_ai_answer(text):
            message = await self.client.send_message(chat_id, "…")
            sent_messages.append(message)
            if not self.config.get("ai_streaming_enabled", True):
                await self.client.edit_message(chat_id, message, part)
                continue
            for end in range(step, len(part), step):
                preview = part[:end].rstrip() + " ▌"
                try:
                    await self.client.edit_message(chat_id, message, preview)
                except Exception as exc:
                    self.logger.debug("ویرایش مرحله‌ای پیام AI ناموفق بود: %s", exc)
                    break
                await asyncio.sleep(interval)
            try:
                await self.client.edit_message(chat_id, message, part)
            except Exception as exc:
                self.logger.debug("تکمیل پیام مرحله‌ای AI ناموفق بود: %s", exc)
        return sent_messages

    async def _send_ai_output(self, chat_id: int, answer: str) -> list[Any]:
        """ارسال ترتیبی متن، استیکر و واکنش خنده؛ هیچ marker داخلی به کاربر نشان داده نمی‌شود."""
        try:
            max_chars = max(1000, min(int(self.config.get("ai_max_response_chars", 20000)), 100000))
        except (TypeError, ValueError):
            max_chars = 20000
        answer = answer[:max_chars].strip()
        sticker_map = self.config.get("ai_sticker_map", {})
        if not isinstance(sticker_map, dict):
            sticker_map = {}
        messages: list[Any] = []
        control_pattern = re.compile(
            r"\[\[(sticker):([A-Za-z0-9_.-]+)\]\]|\[\[(laugh)(?::([1-3]))?\]\]",
            flags=re.IGNORECASE,
        )
        events_to_send: list[tuple[str, str]] = []
        cursor = 0

        def add_text(value: str) -> None:
            value = value.strip()
            if not value:
                return
            buffer: list[str] = []
            laugh_line = re.compile(r"^[😂🤣😆😁😄]+$")
            for line in value.splitlines():
                clean = line.strip()
                if clean and laugh_line.fullmatch(clean):
                    if buffer:
                        events_to_send.append(("text", "\n".join(buffer).strip()))
                        buffer.clear()
                    events_to_send.append(("laugh", str(min(3, len(clean)))))
                else:
                    buffer.append(line)
            if buffer and "\n".join(buffer).strip():
                events_to_send.append(("text", "\n".join(buffer).strip()))

        for match in control_pattern.finditer(answer):
            add_text(answer[cursor:match.start()])
            if match.group(1):
                events_to_send.append(("sticker", match.group(2)))
            else:
                events_to_send.append(("laugh", match.group(4) or "1"))
            cursor = match.end()
        add_text(answer[cursor:])

        for kind, value in events_to_send:
            if kind == "text":
                messages.extend(await self._send_ai_text_progressively(chat_id, value))
            elif kind == "laugh":
                sent = await self.client.send_message(chat_id, "😂" * max(1, min(3, int(value))))
                messages.append(sent)
            elif kind == "sticker" and self.config.get("ai_send_stickers", True):
                sticker_ref = sticker_map.get(value)
                if sticker_ref:
                    try:
                        sent = await self.client.send_file(chat_id, str(sticker_ref))
                        messages.append(sent)
                    except Exception as exc:
                        self.logger.warning("ارسال استیکر AI ناموفق بود: %s", exc)
        return messages

    async def _reply_with_ai(self, event: Any, chat_id: int, text: str) -> None:
        lock = self.ai_request_locks.setdefault(chat_id, asyncio.Lock())
        request_key = text.strip()
        now = time.monotonic()
        recent = self.ai_recent_requests.get(chat_id)
        if recent and recent[0] == request_key and now - recent[1] < 8:
            self._set_status(f"درخواست تکراری نادیده گرفته شد | چت={chat_id}")
            return
        if lock.locked():
            await self.client.send_message(chat_id,
                "درخواست قبلی هنوز در حال پردازش است؛ این پیام در صف قرار نگرفت.")
            return
        self.ai_recent_requests[chat_id] = (request_key, now)
        async with lock:
            record = dict(self.ai_chats.get(str(chat_id), {}))
            record["api_key"] = str(self.config.get("ai_api_key", "")).strip() or record.get("api_key", "")
            record["base_url"] = str(self.config.get("ai_base_url", "")).strip().rstrip("/") or record.get("base_url", "")
            record["model"] = str(self.config.get("ai_model", "")).strip() or record.get("model", "gpt-4o-mini")
            self._remember_bad_ai_grudge(chat_id, text)
            try:
                self._set_status(f"درخواست AI ارسال شد و منتظر پاسخ است | چت={chat_id}")
                if self.config.get("auto_typing", True):
                    async with self.client.action(chat_id, "typing"):
                        answer = await asyncio.to_thread(self._call_ai_api, record, text)
                        sent_messages = await self._send_ai_output(chat_id, answer)
                else:
                    answer = await asyncio.to_thread(self._call_ai_api, record, text)
                    sent_messages = await self._send_ai_output(chat_id, answer)
                for sent in sent_messages:
                    self._remember_ai_message(chat_id, sent)
                self._remember_ai_turn(chat_id, text, answer)
                self._log(f"پاسخ AI ارسال شد | چت={chat_id} | مدل={record.get('model')}")
            except Exception as exc:
                self.logger.error("پاسخ AI ناموفق بود | چت=%s | خطا=%s", chat_id, exc, exc_info=True)
                error_text = str(exc)
                if "latin-1" in error_text.lower() or "codec can't encode" in error_text.lower():
                    error_text = "کلید یا Base URL کاراکتر غیرمجاز دارد."
                await self.client.send_message(chat_id, f"پاسخ AI انجام نشد: {error_text}")

    # ==================================================================
    # ★★★ بررسی هوشمند آنلاین با انتظار ★★★
    # ==================================================================
    async def _handle_online_check(self, event: Any, raw_text: str, chat_id: int) -> bool:
        keyword = str(self.config.get("online_check_keyword", "آنلاین")).strip()
        if not keyword:
            return False

        text = (raw_text or "").strip()
        if not text:
            return False
        if not event.out:
            return False

        is_saved = (self.self_id is not None and chat_id == self.self_id)

        if not text.casefold().startswith(keyword.casefold()):
            return False

        next_pos = len(keyword)
        if next_pos < len(text):
            ch = text[next_pos]
            if ch not in (" ", "\t", "\n", "@", "-") and not ch.isdigit():
                return False

        rest = text[next_pos:].strip()
        tokens = rest.split()
        wait_seconds: int = 0
        target_identifier: Any = None
        target_display_hint: str = ""

        if is_saved:
            if not tokens:
                await self.client.send_message(
                    "me",
                    f"ℹ️ برای بررسی، نام کاربری یا آیدی را بنویسید:\n"
                    f"{keyword} [ثانیه] @username\n"
                    f"{keyword} [ثانیه] 123456789\n\n"
                    f"مثال: {keyword} 5 @user  → تا ۵ ثانیه صبر می‌کند و اگر "
                    f"کاربر آنلاین شد فوراً گزارش می‌دهد.")
                return True

            if tokens[0].isdigit() and len(tokens) > 1:
                wait_seconds = int(tokens[0])
                target_token = tokens[1]
            else:
                target_token = tokens[0]
                if len(tokens) > 1 and tokens[1].isdigit():
                    wait_seconds = int(tokens[1])

            if target_token.isdigit() and len(tokens) == 1:
                await self.client.send_message(
                    "me",
                    "❌ شناسه کاربر وارد نشده است. نمونه:\n"
                    f"{keyword} 5 @username\n{keyword} 5 123456789")
                return True

            if target_token.startswith("https://t.me/") or target_token.startswith("t.me/"):
                target_token = target_token.rstrip("/").split("/")[-1]
                if target_token.startswith("+"):
                    await self.client.send_message("me",
                        "❌ از لینک دعوت گروه نمی‌توان استفاده کرد.")
                    return True
                target_identifier = "@" + target_token
                target_display_hint = target_token
            elif target_token.startswith("@"):
                target_identifier = target_token
                target_display_hint = target_token
            elif target_token.lstrip("-").isdigit():
                target_identifier = int(target_token)
                target_display_hint = target_token
            else:
                await self.client.send_message(
                    "me",
                    "❌ فرمت نامعتبر. نمونه:\n"
                    f"{keyword} 5 @username\n{keyword} 5 123456789")
                return True
        else:
            if tokens:
                if tokens[0].isdigit():
                    wait_seconds = int(tokens[0])
                else:
                    await self.client.send_message(
                        chat_id,
                        f"❌ فرمت نامعتبر. فقط عدد ثانیه مجاز است:\n{keyword} 5")
                    return True
            else:
                try:
                    wait_seconds = int(self.config.get("online_check_default_wait_seconds", 5))
                except (ValueError, TypeError):
                    wait_seconds = 5
            target_identifier = chat_id
            target_display_hint = ""

        try:
            max_wait = int(self.config.get("online_check_max_wait_seconds", 60))
        except (ValueError, TypeError):
            max_wait = 60
        wait_seconds = max(0, min(int(wait_seconds), max_wait))

        if (not is_saved) and self.config.get("online_check_auto_delete_command", True):
            try:
                await self.client.delete_messages(chat_id, [event.id], revoke=True)
            except Exception as exc:
                self.logger.debug("حذف پیام دستور بررسی آنلاین ناموفق بود: %s", exc)

        asyncio.create_task(self._run_online_check_and_report(
            target_identifier, target_display_hint,
            origin_chat_id=chat_id, wait_seconds=wait_seconds,
        ))
        return True

    async def _run_online_check_and_report(
        self,
        target_identifier: Any,
        display_hint: str,
        origin_chat_id: Optional[int] = None,
        wait_seconds: int = 0,
    ) -> None:
        try:
            entity = await self.client.get_entity(target_identifier)
        except Exception as exc:
            self._set_status(f"❌ کاربر پیدا نشد: {exc}")
            try:
                await self.client.send_message(
                    "me",
                    f"❌ کاربر پیدا نشد.\nشناسه: {display_hint or target_identifier}\nخطا: {exc}")
            except Exception:
                pass
            return

        user_id = int(getattr(entity, "id", 0))
        if not user_id:
            return

        if user_id == self.self_id:
            await self.client.send_message("me", "ℹ️ خودتان را نمی‌توانید بررسی کنید.")
            return

        if getattr(entity, "bot", False):
            await self.client.send_message("me",
                "ℹ️ این کاربر یک ربات است؛ بررسی وضعیت آنلاین بی‌معنی است.")
            return

        if getattr(entity, "deleted", False):
            await self.client.send_message("me", "ℹ️ این اکانت حذف شده است.")
            return

        display_name = (
            getattr(entity, "username", None)
            or " ".join(p for p in [
                getattr(entity, "first_name", ""),
                getattr(entity, "last_name", "")] if p).strip()
            or str(user_id)
        )

        if wait_seconds > 0:
            self._set_status(f"⏳ بررسی {display_name} (تا {wait_seconds} ثانیه)...")
        else:
            self._set_status(f"⏳ در حال بررسی وضعیت {display_name}...")

        result = await self._check_user_online(user_id, wait_seconds=wait_seconds)

        status = result.get("status")
        detail = result.get("detail", "")

        if status == "online":
            icon = "🟢"
            verdict = "آنلاین است" + (f" ({detail})" if detail else "")
        elif status == "offline":
            icon = "🔴"
            verdict = "آفلاین است" + (f" ({detail})" if detail else "")
        elif status == "rate_limited":
            icon = "⏸"
            verdict = f"محدودیت زمانی: {detail}"
        elif status == "discarded":
            icon = "⚠️"
            verdict = f"درخواست رد شد: {detail}"
        else:
            icon = "❔"
            verdict = f"نامشخص: {detail}"

        report = (
            f"{icon} بررسی وضعیت کاربر\n"
            f"👤 نام: {display_name}\n"
            f"🆔 آیدی: {user_id}\n"
            f"⏱ انتظار: {wait_seconds} ثانیه\n"
            f"📊 نتیجه: {verdict}"
        )

        try:
            await self.client.send_message("me", report)
        except Exception as exc:
            self.logger.debug("ارسال گزارش ناموفق بود: %s", exc)

        self._log(f"{icon} {display_name} → {verdict}")

    def _classify_encryption_state(self, res: Any) -> str:
        """نوع پاسخ درخواست چت محرمانه را به وضعیت انتزاعی تبدیل می‌کند."""
        if EncryptedChat is not None and isinstance(res, EncryptedChat):
            return "online"
        if EncryptedChatRequested is not None and isinstance(res, EncryptedChatRequested):
            return "online"
        if EncryptedChatWaiting is not None and isinstance(res, EncryptedChatWaiting):
            return "offline"
        if EncryptedChatDiscarded is not None and isinstance(res, EncryptedChatDiscarded):
            return "discarded"
        if EncryptedChatEmpty is not None and isinstance(res, EncryptedChatEmpty):
            return "empty"
        return "unknown"

    async def _wait_for_encryption_update(self, chat_id: int, wait_seconds: int) -> bool:
        """تا `wait_seconds` ثانیه منتظر `UpdateEncryption` برای چت مشخص می‌ماند."""
        if UpdateEncryption is None:
            return False
        if wait_seconds <= 0:
            return False

        online_event = asyncio.Event()

        async def handler(update: Any) -> None:
            try:
                if not isinstance(update, UpdateEncryption):
                    return
                chat = getattr(update, "chat", None)
                if chat is None:
                    return
                update_chat_id = getattr(chat, "id", None)
                if update_chat_id is None:
                    return
                if int(update_chat_id) != int(chat_id):
                    return
                if isinstance(chat, (EncryptedChat, EncryptedChatRequested)):
                    if not online_event.is_set():
                        online_event.set()
            except Exception:
                pass

        try:
            self.client.add_event_handler(handler, events.Raw(types=UpdateEncryption))
        except Exception as exc:
            self.logger.debug("ثبت event handler برای UpdateEncryption ناموفق بود: %s", exc)
            return False

        try:
            await asyncio.wait_for(online_event.wait(), timeout=float(wait_seconds))
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            try:
                self.client.remove_event_handler(handler)
            except Exception:
                pass

    async def _check_user_online(self, user_id: int, wait_seconds: int = 0) -> dict[str, Any]:
        """وضعیت آنلاین بودن کاربر را با ساخت چت محرمانه بررسی می‌کند."""
        result: dict[str, Any] = {"status": "unknown", "detail": ""}

        if RequestEncryptionRequest is None:
            result["status"] = "error"
            result["detail"] = "ماژول‌های Telethon برای چت محرمانه بارگذاری نشده‌اند."
            return result

        now = time.monotonic()
        try:
            cooldown = max(5, int(self.config.get("online_check_cooldown_seconds", 30)))
        except (TypeError, ValueError):
            cooldown = 30
        last_time = self._online_check_times.get(user_id, 0.0)
        if now - last_time < cooldown:
            remaining = int(cooldown - (now - last_time)) + 1
            result["status"] = "rate_limited"
            result["detail"] = f"{remaining} ثانیه دیگر دوباره امتحان کنید."
            return result

        lock = self._online_check_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            self._online_check_times[user_id] = time.monotonic()

            chat_id_to_discard: Optional[int] = None
            try:
                g_a = os.urandom(256)
                res = await self.client(RequestEncryptionRequest(user_id=user_id, g_a=g_a))

                chat_id_to_discard = getattr(res, "id", None)
                initial_status = self._classify_encryption_state(res)

                if initial_status == "online":
                    result["status"] = "online"
                    result["detail"] = "آماده گفتگو"
                elif initial_status == "offline":
                    if wait_seconds > 0 and chat_id_to_discard is not None:
                        became_online = await self._wait_for_encryption_update(
                            int(chat_id_to_discard), wait_seconds)
                        if became_online:
                            result["status"] = "online"
                            result["detail"] = f"کاربر در خلال {wait_seconds} ثانیه آنلاین شد"
                        else:
                            result["status"] = "offline"
                            result["detail"] = f"بعد از {wait_seconds} ثانیه هنوز آفلاین است"
                    else:
                        result["status"] = "offline"
                        result["detail"] = "در انتظار آنلاین شدن کاربر"
                elif initial_status == "discarded":
                    result["status"] = "discarded"
                    result["detail"] = "چت محرمانه رد شد یا غیرفعال است"
                elif initial_status == "empty":
                    result["status"] = "unknown"
                    result["detail"] = "پاسخ خالی از سرور"
                else:
                    result["status"] = "unknown"
                    result["detail"] = f"پاسخ ناشناخته: {type(res).__name__}"

            except Exception as exc:
                result["status"] = "error"
                result["detail"] = str(exc)
                self.logger.error("خطا در بررسی آنلاین بودن کاربر %s: %s",
                                  user_id, exc, exc_info=True)
            finally:
                if chat_id_to_discard and DiscardEncryptionRequest is not None:
                    try:
                        await self.client(DiscardEncryptionRequest(
                            chat_id=int(chat_id_to_discard),
                            delete_history=True))
                    except Exception as exc:
                        self.logger.debug(
                            "حذف چت محرمانه موقت ناموفق بود (chat_id=%s): %s",
                            chat_id_to_discard, exc)

        return result

    # ==================================================================
    # ★★★ لایو لوکیشن فیک ★★★
    # ==================================================================
    def _save_live_location_geo(self, lat: float, lon: float, chat_id: Optional[int] = None) -> None:
        geo = {
            "lat": float(lat),
            "long": float(lon),
            "saved_at": self._utc_iso(),
        }
        self.config["live_location_saved_geo"] = geo
        if chat_id is not None:
            saved_geos = self.config.setdefault("live_location_saved_geos", {})
            if not isinstance(saved_geos, dict):
                saved_geos = {}
                self.config["live_location_saved_geos"] = saved_geos
            saved_geos[str(int(chat_id))] = geo
        self._save_config_file()

    def _get_live_location_geo(self, chat_id: Optional[int] = None) -> Optional[tuple[float, float]]:
        bot_geo = self.config.get("live_location_bot_geo")
        if isinstance(bot_geo, dict):
            try:
                return float(bot_geo["lat"]), float(bot_geo["long"])
            except (KeyError, TypeError, ValueError):
                pass
        geo = None
        if chat_id is not None:
            saved_geos = self.config.get("live_location_saved_geos", {})
            if isinstance(saved_geos, dict):
                geo = saved_geos.get(str(int(chat_id)))
                if not saved_geos:
                    geo = self.config.get("live_location_bot_geo") or self.config.get("live_location_saved_geo")
        else:
            geo = self.config.get("live_location_bot_geo") or self.config.get("live_location_saved_geo")
        if not isinstance(geo, dict):
            return None
        try:
            return float(geo["lat"]), float(geo["long"])
        except (KeyError, TypeError, ValueError):
            return None

    async def _send_live_location(self, target: Any, period: int) -> bool:
        """ارسال همان لوکیشن ذخیره‌شده به‌صورت Live Location به مقصد."""
        if InputMediaGeoLive is None or InputGeoPoint is None:
            self.logger.error("ماژول‌های Telethon برای لوکیشن زنده بارگذاری نشده‌اند.")
            return False
        target_id = getattr(target, "id", target if isinstance(target, int) else None)
        coords = self._get_live_location_geo(int(target_id) if target_id is not None else None)
        if coords is None:
            return False
        try:
            max_period = int(self.config.get("live_location_max_period", 86400))
        except (TypeError, ValueError):
            max_period = 86400
        try:
            period = max(60, min(int(period), max_period))
        except (TypeError, ValueError):
            period = int(self.config.get("live_location_default_period", 900))
        try:
            await self.client.send_file(
                target,
                InputMediaGeoLive(
                    geo_point=InputGeoPoint(lat=coords[0], long=coords[1]),
                    period=period,
                ),
            )
            return True
        except Exception as exc:
            self.logger.error("ارسال لایو لوکیشن ناموفق بود: %s", exc, exc_info=True)
            return False

    def _parse_live_targets(self, raw: str) -> tuple[list[str], int]:
        """از متن «@user1 @user2 900» مقصدها و مدت را استخراج می‌کند."""
        tokens = raw.replace(",", " ").split()
        targets: list[str] = []
        try:
            period = int(self.config.get("live_location_default_period", 900))
        except (TypeError, ValueError):
            period = 900
        for tok in tokens:
            if tok.isdigit() and 1 <= len(tok) <= 6:
                period = int(tok)
            else:
                targets.append(tok)
        return targets, period

    async def _dispatch_live_location(self, targets: list[str], period: int) -> list[str]:
        results: list[str] = []
        for token in targets:
            try:
                identifier: Any = int(token) if token.lstrip("-").isdigit() else token
                entity = await self.client.get_entity(identifier)
                ok = await self._send_live_location(entity, period)
                results.append(f"{'✅' if ok else '⚠️'} {token}")
            except Exception as exc:
                results.append(f"❌ {token}: {exc}")
        return results

    async def _handle_live_location(self, event: Any, raw_text: str, chat_id: int) -> bool:
        """ذخیرهٔ لوکیشن معمولی و ارسال لایو لوکیشن با کلیدواژه در همان چت."""
        if not self.config.get("live_location_enabled", True):
            return False

        msg = getattr(event, "message", None)
        if msg is None:
            return False

        keyword = str(self.config.get("live_location_keyword", "لوکیشن زنده")).strip()
        text = (raw_text or "").strip()
        # (۱) ذخیرهٔ لوکیشن معمولی در همان چتی که فرستاده شده است
        geo = getattr(msg, "geo", None)
        if event.out and geo is not None:
            lat = getattr(geo, "lat", None)
            lon = getattr(geo, "long", None)
            if lat is not None and lon is not None:
                self._save_live_location_geo(float(lat), float(lon), chat_id)
                self._log(f"📍 لوکیشن ذخیره شد ({lat:.4f}, {lon:.4f})")
                if event.out:
                    try:
                        await self.client.send_message(
                            chat_id,
                            "✅ لوکیشن ثبت شد. حالا کلیدواژه را در همین چت بفرست تا لایو لوکیشن ارسال شود.",
                        )
                    except Exception:
                        pass
                return True

        if not keyword:
            return False

        # در همان چتی که لوکیشن معمولی ثبت شده، کلیدواژه حذف و لایو لوکیشن ارسال می‌شود.
        if not event.out:
            return False
        folded = text.casefold()
        matched = (
            folded == keyword.casefold()
            or folded.startswith(keyword.casefold() + " ")
        )
        if not matched:
            return False

        rest = text[len(keyword):].strip()
        try:
            period = int(rest) if rest.isdigit() else int(
                self.config.get("live_location_default_period", 900))
        except (TypeError, ValueError):
            period = 900

        if self.config.get("live_location_auto_delete_command", True):
            try:
                await self.client.delete_messages(chat_id, [event.id], revoke=True)
            except Exception as exc:
                self.logger.debug("حذف پیام کلیدواژه لوکیشن ناموفق بود: %s", exc)

        if self._get_live_location_geo(chat_id) is None:
            try:
                await self.client.send_message(
                    chat_id,
                    "❌ هنوز لوکیشنی برای این چت ثبت نشده است؛ ابتدا یک لوکیشن معمولی در همین چت بفرست.",
                )
            except Exception:
                pass
            return True

        ok = await self._send_live_location(chat_id, period)
        if ok:
            self._log(f"📍 لایو لوکیشن به چت {chat_id} ارسال شد ({period}s)")
        return True

    # ==================================================================
    # هندلر اصلی پیام‌ها
    # ==================================================================
    async def _on_new_message(self, event: Any) -> None:
        chat_id = event.chat_id
        if chat_id is None:
            return
        chat_id = int(chat_id)

        # لایو لوکیشن فیک قبل از فیلتر PV اجرا می‌شود تا در گروه هم کار کند.
        try:
            if await self._handle_live_location(event, event.raw_text or "", chat_id):
                return
        except Exception:
            self.logger.exception("خطا در پردازش لایو لوکیشن فیک")

        # این مسیر قبل از فیلتر PV اجرا می‌شود تا در گروه‌ها نیز کار کند.
        try:
            if await self._handle_specific_delete_reply(event, event.raw_text or "", chat_id):
                return
        except Exception:
            self.logger.exception("خطا در حذف پیام مشخص")

        try:
            if await self._handle_silence(event, event.raw_text or "", chat_id):
                return
        except Exception:
            self.logger.exception("خطا در پردازش سکوت چت")

        if not event.is_private:
            return

        raw_text = event.raw_text or ""

        # این آرشیو مستقل از سین‌کردن و حضور آنلاین است و قبل از هر منطق دیگر انجام می‌شود.
        if self.config.get("deleted_backup_enabled", True):
            try:
                await self._backup_message(event)
            except Exception:
                self.logger.exception("خطا در بکاپ پیام PV")

        # پیام خروجی فقط برای پردازش فرمان‌های خود برنامه ادامه پیدا می‌کند؛
        # هرگز وارد مسیر Mark as Read پیام ورودی نمی‌شود.
        if event.out and not self.config.get("always_online", True):
            await self._force_offline()

        try:
            if await self._handle_online_check(event, raw_text, chat_id):
                return
        except Exception:
            self.logger.exception("خطا در پردازش دستور بررسی آنلاین")

        if (self.config.get("auto_mark_read", True) and not event.out
                and not (self.config.get("deleted_backup_enabled", True)
                         and self.config.get("deleted_backup_without_read", True))):
            try:
                await self._mark_as_read_without_opening(chat_id, event.id)
            except Exception as exc:
                self.logger.debug("علامت‌گذاری خوانده‌شدن ناموفق بود: %s", exc)

        if await self._handle_feature_control(raw_text):
            on_typing = str(self.config.get("typing_on_keyword", "typing on")).strip().casefold()
            off_typing = str(self.config.get("typing_off_keyword", "typing off")).strip().casefold()
            if raw_text.strip().casefold() == on_typing and self.config.get("auto_typing", True):
                self._start_chat_typing(chat_id)
            elif raw_text.strip().casefold() == off_typing:
                self._stop_chat_typing(chat_id)
            return
        if await self._handle_ai_control(event, raw_text, chat_id):
            return

        # Saved Messages هم می‌تواند فرمان تنظیمات را دریافت کند، اما پیام عادی آن
        # نباید وارد منطق پاسخ‌گویی/سکوت خصوصی شود.
        if self.self_id is not None and chat_id == self.self_id:
            return

        # این action مستقل از سین و حضور آنلاین است و فقط برای همین چت اجرا می‌شود.
        if self.config.get("auto_typing", True) and not event.out:
            self._start_chat_typing(chat_id)

        ai_record = self.ai_chats.get(str(chat_id), {})
        replied_to_ai = await self._is_reply_to_ai_message(event, chat_id)
        if (self.config.get("ai_globally_enabled", True) and ai_record.get("enabled")
                and replied_to_ai and raw_text.strip()):
            await self._reply_with_ai(event, chat_id, raw_text.strip())
            return

        if self.config.get("debug_logging", True):
            try:
                msg = event.message
                if msg is not None:
                    media_type = type(msg.media).__name__ if msg.media else None
                    ttl_media = getattr(msg.media, "ttl_seconds", None) if msg.media else None
                    self.logger.info(
                        "دیباگ | id=%s | chat=%s | out=%s | ttl_period=%r | "
                        "ttl_seconds(media)=%r | noforwards=%r | media=%s | has_text=%s",
                        event.id, chat_id, event.out,
                        getattr(msg, "ttl_period", None), ttl_media,
                        getattr(msg, "noforwards", False), media_type, bool(msg.text))
            except Exception:
                self.logger.exception("خطا در لاگ دیباگ")

        if self.config.get("auto_save_expiring", True):
            try:
                if self._should_save_expiring(event):
                    asyncio.create_task(self._save_expiring_message(event))
            except Exception:
                self.logger.exception("خطا در بررسی پیام زمان‌دار")

        record = self.ai_chats.get(str(chat_id), {})
        if (self.config.get("ai_globally_enabled", True) and record.get("enabled")
                and raw_text.strip()):
            await self._reply_with_ai(event, chat_id, raw_text.strip())

    # ==================================================================
    # ذخیره پیام‌های زمان‌دار
    # ==================================================================
    def _should_save_expiring(self, event: Any) -> bool:
        msg = event.message
        if msg is None:
            return False
        ttl_period = getattr(msg, "ttl_period", None)
        ttl_media = None
        if msg.media is not None:
            ttl_media = getattr(msg.media, "ttl_seconds", None)
        noforwards = bool(getattr(msg, "noforwards", False))
        is_expiring = (ttl_period is not None) or (ttl_media is not None)
        if is_expiring:
            if event.out and not self.config.get("save_expiring_outgoing", True):
                return False
            if (not event.out) and not self.config.get("save_expiring_incoming", True):
                return False
            return True
        if noforwards and self.config.get("save_protected_messages", True):
            if event.out and not self.config.get("save_expiring_outgoing", True):
                return False
            if (not event.out) and not self.config.get("save_expiring_incoming", True):
                return False
            return True
        return False

    async def _save_expiring_message(self, event: Any) -> None:
        msg = event.message
        try:
            self.temp_media_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log(f"خطا در ساخت پوشه temp: {exc}", logging.ERROR)
            return

        chat_label = await self._chat_label(event)
        sender = "خودت" if event.out else "طرف مقابل"
        ttl_period = getattr(msg, "ttl_period", None)
        ttl_media = None
        if msg.media is not None:
            ttl_media = getattr(msg.media, "ttl_seconds", None)
        ttl = ttl_period if ttl_period is not None else ttl_media

        meta = []
        if ttl is not None:
            meta.append(f"⏱ تایمر: {ttl} ثانیه")
        if getattr(msg, "noforwards", False):
            meta.append("🔒 محافظت‌شده")
        meta_line = " | ".join(meta)

        header = f"📥 ذخیره خودکار\nاز چت: {chat_label}\nفرستنده: {sender}"
        if meta_line:
            header += f"\n{meta_line}"

        try:
            if msg.media:
                try:
                    downloaded = await msg.download_media(file=str(self.temp_media_path))
                except Exception as exc:
                    self.logger.error("دانلود مدیا ناموفق بود: %s", exc, exc_info=True)
                    self._set_status(f"خطا در دانلود پیام زمان‌دار از {chat_label}: {exc}")
                    return
                if not downloaded:
                    self.logger.warning("download_media مسیر برنگرداند برای %s", event.id)
                    return
                file_path = Path(downloaded)
                caption = header
                if msg.text:
                    caption += f"\n\n{msg.text}"
                try:
                    await self.client.send_file("me", str(file_path),
                                                 caption=caption[:1024], force_document=True)
                    self._ui_call(self._bump_saved_count)
                    kind = type(msg.media).__name__
                    self._log(f"ذخیره شد | چت={chat_label} | نوع={kind} | message_id={event.id}")
                finally:
                    try:
                        if file_path.exists():
                            file_path.unlink()
                    except OSError:
                        pass
            elif msg.text:
                text = f"{header}\n\n{msg.text}"
                await self.client.send_message("me", text)
                self._ui_call(self._bump_saved_count)
                self._log(f"ذخیره شد (متن) | چت={chat_label} | message_id={event.id}")
        except Exception as exc:
            self.logger.error("ذخیره پیام زمان‌دار ناموفق بود | چت=%s | message_id=%s | خطا=%s",
                              chat_label, event.id, exc, exc_info=True)
            self._set_status(f"خطا در ذخیره پیام از {chat_label}: {exc}")

    async def _chat_label(self, event: Any) -> str:
        try:
            chat = await event.get_chat()
            name = getattr(chat, "title", None)
            if not name:
                name = " ".join(p for p in [
                    getattr(chat, "first_name", ""),
                    getattr(chat, "last_name", "")] if p)
            if not name:
                name = getattr(chat, "username", None)
            return f"{name or 'کاربر ناشناس'} (id={event.chat_id})"
        except Exception:
            return f"id={event.chat_id}"

    def _after_worker_stopped(self) -> None:
        self.login_button.configure(state="normal")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.connection_var.set("وضعیت اتصال: متصل نیست")

    def _on_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        try:
            if self.loop and self.loop.is_running():
                future = asyncio.run_coroutine_threadsafe(self._shutdown_async(), self.loop)
                future.result(timeout=5)
            elif self.client:
                self.client.disconnect()
        except Exception:
            self.logger.exception("خطا هنگام بستن برنامه")
        self.root.destroy()

    async def _shutdown_async(self) -> None:
        if self.client and self.monitoring:
            self.client.remove_event_handler(self._on_new_message)
            self.monitoring = False
        await self._cancel_typing_tasks()
        if self.presence_task:
            self.presence_task.cancel()
            self.presence_task = None
        if self.offline_guard_task:
            self.offline_guard_task.cancel()
            self.offline_guard_task = None
        if self.client:
            await self.client.disconnect()
        if self.loop:
            self.loop.call_soon(self.loop.stop)


def desktop_main() -> None:
    if tk is None:
        print("Tkinter نصب نیست. در لینوکس معمولاً باید بسته python3-tk را نصب کنید.")
        return
    try:
        config = load_config()
        log_path = _path_from_config(str(config["log_file"]))
        logger = configure_logging(log_path)
    except ConfigurationError as exc:
        print(f"خطای تنظیمات: {exc}")
        return
    except OSError as exc:
        print(f"خطا در ایجاد یا خواندن فایل تنظیمات: {exc}")
        return

    root = tk.Tk()
    TelegramAutoDeleteApp(root, config, logger)
    root.mainloop()


class _MORSELFVar:
    def __init__(self, value: Any = "") -> None:
        self.value = value

    def get(self) -> Any:
        return self.value

    def set(self, value: Any) -> None:
        self.value = value


class _MORSELFWidget:
    def configure(self, **kwargs: Any) -> None:
        return


MORSELF_LOGS: list[str] = []


class MORSELFApp(TelegramAutoDeleteApp):
    """اجرای بدون Tkinter برای Termux با استفاده از منطق اصلی برنامه."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger) -> None:
        self.root = None
        self.config, self.logger = config, logger
        self.client = self.session_lock_handle = self.loop = self.worker = None
        self.self_id = None
        self.monitoring = self.closing = False
        self.silenced_chats: set[int] = set()
        self.state_path = _path_from_config(str(config["state_file"]))
        self.log_path = _path_from_config(str(config["log_file"]))
        self.temp_media_path = _path_from_config(str(config.get("temp_media_dir", "temp_media")))
        self.deleted_backup_path = _path_from_config(str(config.get("deleted_backup_dir", "deleted_message_backup")))
        self.deleted_backup_path.mkdir(parents=True, exist_ok=True)
        self.deleted_db_path = self.deleted_backup_path / "messages.sqlite3"
        self._deleted_db: Optional[sqlite3.Connection] = None
        self._deleted_db_lock = asyncio.Lock()
        self.ai_config_path = _path_from_config(str(config.get("ai_config_file", "ai_chats.json")))
        self.bad_ai_insults_path = _path_from_config(str(config.get("bad_ai_insults_file", "bad_ai_insults.txt")))
        self.bad_ai_insults: list[str] = []
        self.ai_chats: dict[str, dict[str, Any]] = {}
        self.ai_setup_pending: set[int] = set()
        self.ai_response_ids: dict[int, set[int]] = {}
        self.ai_request_locks: dict[int, asyncio.Lock] = {}
        self.ai_recent_requests: dict[int, tuple[str, float]] = {}
        self.typing_tasks: dict[int, asyncio.Task[Any]] = {}
        self.typing_enabled_chats: set[int] = set()
        self.presence_task = self.offline_guard_task = None
        self.ai_models: list[str] = []
        self._online_check_times: dict[int, float] = {}
        self._online_check_locks: dict[int, asyncio.Lock] = {}
        self._init_deleted_backup_db()
        self._saved_count = 0
        text_keys = (
            "ai_api_key ai_base_url ai_model ai_on_keyword ai_off_keyword "
            "online_on_keyword online_off_keyword read_on_keyword read_off_keyword "
            "typing_on_keyword typing_off_keyword online_check_keyword"
        ).split()
        for key in text_keys:
            setattr(self, f"{key}_var", _MORSELFVar(config.get(key, "")))
        for key in ("online_check_cooldown", "online_check_default_wait"):
            config_key = f"{key}_seconds"
            setattr(self, f"{key}_var", _MORSELFVar(config.get(config_key, 5)))
        for key in ("always_online", "auto_mark_read", "auto_typing"):
            setattr(self, f"{key}_var", _MORSELFVar(bool(config.get(key, True))))
        for key in ("status", "connection", "silence_count", "saved_count", "settings_status", "online_state", "read_state", "typing_state"):
            setattr(self, f"{key}_var", _MORSELFVar())
        self.login_button = self.start_button = self.stop_button = _MORSELFWidget()
        self._load_bad_ai_insults()
        self._load_silenced_state()
        self._load_ai_state()

    def _ui_call(self, callback: Any, *args: Any, **kwargs: Any) -> None:
        try:
            callback(*args, **kwargs)
        except Exception:
            pass

    def _append_ui_log(self, message: str) -> None:
        MORSELF_LOGS.append(message)

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)
        MORSELF_LOGS.append(message)

    def _ask_from_ui(self, title: str, prompt: str, secret: bool) -> Optional[str]:
        import getpass
        print(f"\n[MORSELF] {title}")
        return getpass.getpass(prompt + " ") if secret else input(prompt + " ")

    def _update_feature_states(self) -> None:
        return

    def _after_worker_stopped(self) -> None:
        return


def run_morself(config: dict[str, Any], logger: logging.Logger) -> None:
    import curses

    app = MORSELFApp(config, logger)
    errors: list[str] = []

    def worker() -> None:
        try:
            app.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(app.loop)
            app.loop.run_until_complete(app._connect_and_login())
            app.loop.run_until_complete(app._enable_monitoring())
            app.loop.run_forever()
        except Exception as exc:
            errors.append(str(exc))
            logger.exception("خطای MORSELF")
        finally:
            if app.loop and not app.loop.is_closed():
                try:
                    app.loop.run_until_complete(app._shutdown_async())
                except Exception:
                    logger.exception("خطا هنگام خاموش‌کردن MORSELF")
                app.loop.close()
            app._release_session_lock()

    def draw(stdscr: Any) -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(250)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        thread = threading.Thread(target=worker, name="morself-worker", daemon=True)
        thread.start()
        while True:
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                app.closing = True
                if app.loop and app.loop.is_running():
                    asyncio.run_coroutine_threadsafe(app._shutdown_async(), app.loop)
                break
            if key in (ord("s"), ord("S")) and app.loop:
                asyncio.run_coroutine_threadsafe(app._enable_monitoring(), app.loop)
            if key in (ord("x"), ord("X")) and app.loop:
                asyncio.run_coroutine_threadsafe(app._stop_async(), app.loop)
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            stdscr.addstr(1, 2, " M O R S E L F  //  TERMUX CONTROL CENTER ", curses.color_pair(1) | curses.A_BOLD)
            stdscr.addstr(2, 2, "secure telegram automation console", curses.color_pair(2))
            stdscr.addstr(4, 2, "[S] START    [X] STOP    [Q] EXIT", curses.color_pair(3) | curses.A_BOLD)
            stdscr.addstr(6, 2, f"STATUS  : {app.status_var.get() or 'در حال آماده‌سازی...'}", curses.color_pair(3))
            stdscr.addstr(7, 2, f"ACCOUNT : {app.connection_var.get() or 'در حال اتصال...'}", curses.color_pair(2))
            stdscr.addstr(9, 2, "LIVE EVENT LOG", curses.color_pair(1) | curses.A_BOLD)
            for row, line in enumerate(MORSELF_LOGS[-max(1, height - 13):], 10):
                stdscr.addnstr(row, 2, line, max(1, width - 4), curses.color_pair(1))
            if errors:
                stdscr.addnstr(height - 2, 2, "ERROR: " + errors[-1], max(1, width - 4), curses.color_pair(4))
            stdscr.refresh()
        thread.join(timeout=5)

    curses.wrapper(draw)


def main() -> None:
    try:
        config = load_config()
        logger = configure_logging(_path_from_config(str(config["log_file"])))
    except (ConfigurationError, OSError) as exc:
        print(f"خطای تنظیمات: {exc}")
        return
    is_termux = "--termux" in sys.argv
    is_desktop = "--desktop" in sys.argv or os.name == "nt" or bool(os.environ.get("DISPLAY"))
    if not is_termux and is_desktop and tk is not None:
        desktop_main()
    else:
        run_morself(config, logger)


if __name__ == "__main__":
    main()
