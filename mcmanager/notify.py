"""Notification system (issue 18): crash, restart, backup failure, disk,
updates, schedules - in-app history + optional Discord / generic webhook.

Events are emitted from background threads (watchdog, scheduler,
update flows) - `Notifier.publish()` is thread-safe and fans out to:
- the in-app toast/history (callback marshalled to the Tk thread by the
  app itself)
- a persisted history file (~/.mcmanager/notifications.json, capped)
- a Discord webhook (rich embed) and/or a generic JSON webhook
  (both fire-and-forget; failures never break the operation).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

CONFIG_DIR = Path.home() / ".mcmanager"
HISTORY_FILE = CONFIG_DIR / "notifications.json"
SETTINGS_FILE = CONFIG_DIR / "notify.json"
HISTORY_CAP = 300

# Discord embed colors by severity
COLORS = {"ok": 0x7C5CFF, "warn": 0xFFB020, "error": 0xFF5D5D}


class Notifier:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._history: list[dict] = []
        self._listeners: list = []      # callables(dict event)
        self._webhooks: dict = {"discord": "", "generic": ""}
        self._load()

    # -- persistence -----------------------------------------------------------
    def _load(self) -> None:
        try:
            if HISTORY_FILE.exists():
                data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
                self._history = data[-HISTORY_CAP:] if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            self._history = []
        try:
            if SETTINGS_FILE.exists():
                cfg = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                self._webhooks = {
                    "discord": str(cfg.get("discord") or ""),
                    "generic": str(cfg.get("generic") or ""),
                }
        except (OSError, json.JSONDecodeError):
            pass

    def _save_history(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            HISTORY_FILE.write_text(
                json.dumps(self._history[-HISTORY_CAP:], indent=1),
                encoding="utf-8")
        except OSError:
            pass

    # -- configuration ----------------------------------------------------------
    def webhooks(self) -> dict:
        with self._lock:
            return dict(self._webhooks)

    def set_webhooks(self, discord: str = "", generic: str = "") -> None:
        with self._lock:
            self._webhooks = {"discord": discord.strip(),
                              "generic": generic.strip()}
        try:
            SETTINGS_FILE.write_text(json.dumps(self._webhooks, indent=1),
                                     encoding="utf-8")
        except OSError:
            pass

    # -- subscription -------------------------------------------------------------
    def subscribe(self, cb) -> None:
        """cb(event_dict) - called in the EMITTING thread; UI code must
        marshal to its own main loop (the app does this via runner.emit)."""
        with self._lock:
            if cb not in self._listeners:
                self._listeners.append(cb)

    def history(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(self._history[-limit:])

    def clear_history(self) -> None:
        with self._lock:
            self._history = []
        self._save_history()

    # -- emit -----------------------------------------------------------------------
    def publish(self, kind: str, server_name: str, message: str,
                ok: bool = True) -> None:
        event = {"kind": kind, "server": server_name, "message": message,
                 "ok": bool(ok), "ts": time.time()}
        with self._lock:
            self._history.append(event)
            self._history = self._history[-HISTORY_CAP:]
            listeners = list(self._listeners)
        self._save_history()
        for cb in listeners:
            try:
                cb(event)
            except Exception:  # noqa: BLE001 - listeners must not break ops
                pass
        self._send_webhooks(event)

    def _send_webhooks(self, event: dict) -> None:
        url = (self._webhooks.get("discord") or "").strip()
        gurl = (self._webhooks.get("generic") or "").strip()
        if not url and not gurl:
            return
        def _post():
            try:
                import requests
                if url:
                    color = COLORS.get("ok" if event["ok"] else "error", 0x7C5CFF)
                    requests.post(url, json={
                        "username": "MC Manager",
                        "embeds": [{
                            "title": f"{event['kind']} - {event['server']}",
                            "description": event["message"][:1000],
                            "color": color,
                            "timestamp": time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(event["ts"])),
                        }]},
                        timeout=10)
                if gurl:
                    requests.post(gurl, json=event, timeout=10)
            except Exception:  # noqa: BLE001 - never break the operation
                pass
        threading.Thread(target=_post, daemon=True).start()


NOTIFIER = Notifier()
