#!/usr/bin/env python3
"""Offline smoke tests for MOR Userbot Manager persistence and input validation."""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from manager_bot import MORManagerBot, UserbotRuntime


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        runtime = UserbotRuntime(root / "state")
        assert runtime.config["session_name"] == str(runtime.session_path)
        assert runtime.config["state_file"].startswith(str(runtime.data_dir))
        runtime.config["always_online"] = True
        runtime.config["ai_api_key"] = "secret-value"
        runtime.save_config()
        saved = json.loads(runtime.config_path.read_text(encoding="utf-8"))
        assert saved["always_online"] is True
        assert saved["ai_api_key"] == "secret-value"
        assert (runtime.config_path.stat().st_mode & 0o077) == 0

        assert MORManagerBot.coerce_value("true", False) is True
        assert MORManagerBot.coerce_value("خاموش", True) is False
        assert MORManagerBot.coerce_value("20", 5) == 20
        assert MORManagerBot.coerce_value('{"welcome":"ok"}', {}) == {"welcome": "ok"}

        manager = MORManagerBot("123456:abcdefghijklmnopqrstuvwx", 123456789, root / "manager")
        private_owner = type("Update", (), {
            "effective_user": type("User", (), {"id": 123456789})(),
            "effective_chat": type("Chat", (), {"type": "private"})(),
        })()
        group_owner = type("Update", (), {
            "effective_user": type("User", (), {"id": 123456789})(),
            "effective_chat": type("Chat", (), {"type": "group"})(),
        })()
        other_private_user = type("Update", (), {
            "effective_user": type("User", (), {"id": 987654321})(),
            "effective_chat": type("Chat", (), {"type": "private"})(),
        })()
        assert manager.is_owner(private_owner)
        assert not manager.is_owner(group_owner)
        assert not manager.is_owner(other_private_user)
        assert "ai_api_key" not in manager.safe_config_text()

        async def verify_no_session() -> None:
            assert await runtime.session_is_authorized() is False
            response, keep_pending = await manager.process_pending(
                type("Pending", (), {"action": "silence:add"})(), "123456789")
            assert "اضافه شد" in response and keep_pending is False
            stored = json.loads(Path(manager.runtime.config["state_file"]).read_text(encoding="utf-8"))
            assert stored == [123456789]
            response, keep_pending = await manager.process_pending(
                type("Pending", (), {"action": "silence:remove"})(), "123456789")
            assert "حذف شد" in response and keep_pending is False
            stored = json.loads(Path(manager.runtime.config["state_file"]).read_text(encoding="utf-8"))
            assert stored == []

        asyncio.run(verify_no_session())
    print("offline smoke tests passed")


if __name__ == "__main__":
    main()
