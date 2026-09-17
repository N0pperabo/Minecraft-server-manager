"""Main window: sidebar navigation + page host. Pages are rebuilt on switch.

v2.0: the app object is also the service hub - it owns the operation
lock registry, the crash-recovery watchdog, the scheduler and the
notifier, exposes notify_event() for every background service, confirms
new SSH host keys (TOFU) on the Tk thread, and enforces restricted mode
(master password + per-server permission flags, issue 24).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from pathlib import Path

import customtkinter as ctk
from tkinter import messagebox

from .. import theme as T
from ..models import Server, ServerStore
from ..control import ServerControl
from ..ssh_manager import SSHManager
from ..tasks import TaskRunner
from ..oplock import LOCKS
from ..notify import NOTIFIER
from ..sched import Scheduler
from ..health import Watchdog
from ..secure import HOST_KEYS
from . import wallpaper
from .widgets import Toast

PAGES_DASHBOARD = [
    ("dashboard", "Dashboard"),
]
PAGES_SERVER = [
    ("server", "Control"),
    ("wizard", "Setup Wizard"),
    ("detect", None),  # handled by dialog, not a page
    ("browse", "Mods & Plugins"),
    ("console", "Console"),
    ("terminal", "Terminal"),
    ("files", "Files"),
    ("monitor", "Monitor"),
    ("backup", "Backups"),
]
PAGES_SERVER = [(k, l) for k, l in PAGES_SERVER if l]

SECURITY_FILE = Path.home() / ".mcmanager" / "security.json"


# ================================================================== lockdown =
def _pbkdf2(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt,
                               200_000).hex()


def load_master_hash() -> dict:
    try:
        if SECURITY_FILE.exists():
            data = json.loads(SECURITY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("salt") and data.get("hash"):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


class MCManagerApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        T.configure()
        self.title("MC Manager - Minecraft Server Controller")
        self.geometry("1180x740")
        self.minsize(980, 620)
        self.configure(fg_color=T.BG)

        self.store = ServerStore()
        self.ssh = SSHManager()
        self.control = ServerControl(self.ssh)
        self.runner = TaskRunner()
        self.locks = LOCKS
        self.selected_id: str | None = self.store.servers[0].id if self.store.servers else None
        self._current_page: ctk.CTkFrame | None = None
        self._current_key: str | None = None

        # ---- restricted mode (issue 24) ------------------------------------
        self._master = load_master_hash()
        self.restricted = bool(self._master)

        # ---- background services -------------------------------------------
        # TOFU host-key confirmation happens on worker threads; the callback
        # marshals the question to the Tk main loop through the task queue.
        HOST_KEYS.first_use_callback = self._confirm_host_key
        self.watchdog = Watchdog(self)
        self.scheduler = Scheduler(self)
        try:
            self.watchdog.start()
            self.scheduler.start()
        except RuntimeError:
            pass  # already started (tests)
        self._journal_checked: set[str] = set()
        LOCKS.on_change = self._on_lock_change

        self._build_sidebar()
        self.content = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=0)
        self.content.pack(side="right", fill="both", expand=True)

        self.toast = Toast(self)
        self.after(100, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.show_page("dashboard")
        self._center()

    # -------------------------------------------------------------- helpers
    def server(self) -> Server | None:
        return self.store.get(self.selected_id)

    def notify(self, message: str, ok: bool = True) -> None:
        self.toast.show(message, ok)

    # -- notification hub (issue 18) ----------------------------------------
    def notify_event(self, kind: str, srv, message: str, ok: bool = True) -> None:
        """Called from watchdog/scheduler/updates (any thread)."""
        if getattr(srv, "notify", True) or kind == "crash":
            NOTIFIER.publish(kind, getattr(srv, "name", "?"), message, ok)
        self.runner.emit(lambda: self.toast.show(message, ok))

    def _on_lock_change(self, server_id: str, op: str | None) -> None:
        name = next((s.name for s in self.store.servers if s.id == server_id),
                    server_id)
        what = f"busy: {op}" if op else "idle"
        self.runner.emit(lambda: self.toast.show(f"{name}: {what}", bool(not op)))

    # -- permissions (issue 24) ------------------------------------------------
    def allowed(self, srv: Server | None, action: str) -> bool:
        """Gate actions by restricted mode + per-server permission flags."""
        if srv is None:
            return False
        if not self.restricted:
            return True
        return {
            "console": srv.allow_console,
            "files": srv.allow_files,
            "restart": srv.allow_restart,
            "edit": srv.allow_edit,
        }.get(action, False)

    def set_master_password(self, password: str) -> None:
        salt = secrets.token_bytes(16)
        SECURITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECURITY_FILE.write_text(json.dumps(
            {"salt": salt.hex(), "hash": _pbkdf2(password, salt)}),
            encoding="utf-8")
        try:
            os.chmod(SECURITY_FILE, 0o600)
        except OSError:
            pass
        self._master = load_master_hash()
        self.restricted = True

    def clear_master_password(self) -> None:
        try:
            SECURITY_FILE.unlink()
        except OSError:
            pass
        self._master = {}
        self.restricted = False

    def try_unlock(self, password: str) -> bool:
        if not self._master:
            self.restricted = False
            return True
        want = self._master.get("hash", "")
        salt = bytes.fromhex(self._master.get("salt") or "")
        if secrets.compare_digest(_pbkdf2(password, salt), want):
            self.restricted = False
            return True
        return False

    # -- host key confirmation (issue 3) -----------------------------------------
    def _confirm_host_key(self, hostname: str, fingerprint: str) -> bool:
        if not self.winfo_exists():
            return True
        ev = threading.Event()
        box = {"ok": False}

        def ask():
            try:
                box["ok"] = messagebox.askyesno(
                    "New SSH host key",
                    f"First connection to:\n{hostname}\n\n"
                    f"Host key fingerprint:\n{fingerprint}\n\n"
                    "Trust and remember this key?\n\n"
                    "(A changed key later will be REJECTED as a possible "
                    "man-in-the-middle attack.)",
                    parent=self)
            finally:
                ev.set()

        self.runner.emit(ask)
        ev.wait(timeout=120)
        return box["ok"]

    # -- journal recovery on connect (issue 33) -----------------------------------
    def check_journal(self, srv: Server) -> None:
        """Offer resume/rollback when a previous operation was interrupted."""
        if srv.id in self._journal_checked or not srv.mc_dir:
            return
        self._journal_checked.add(srv.id)

        def work():
            from ..oplock import Journal
            j = Journal(self.ssh, srv).recover()
            return j

        def cb(j):
            if not j or not self.winfo_exists():
                return
            op, step = j.get("op", "?"), j.get("step", "?")
            rec = j.get("recovery", [])
            options = []
            if "rollback" in rec:
                options.append("Roll back (restore previous state)")
            options.append("Discard journal (server looks fine)")
            choice = _choice_dialog(
                self, "Interrupted operation detected",
                f"An '{op}' was interrupted at step '{step}' (SSH drop or "
                "app exit).\nWhat should MC Manager do?",
                options)
            if choice == 0 and "rollback" in rec:
                self._recover_rollback(srv, op, j)
            else:
                from ..oplock import Journal
                self.runner.run(lambda: Journal(self.ssh, srv).clear())

        self.runner.run(work, on_success=cb, on_error=lambda e: None)

    def _recover_rollback(self, srv: Server, op: str, j: dict) -> None:
        data = j.get("data") or {}

        def work():
            from ..oplock import Journal
            journal = Journal(self.ssh, srv)
            if op == "update":
                from .. import updater
                return updater.rollback_last_update(self.ssh, srv, self.control)
            if op == "restore" and data.get("snapshot"):
                from .. import backup as B
                B.rollback_restore(self.ssh, srv, data["snapshot"], self.control)
                return data["snapshot"]
            journal.clear()
            return None

        def ok(res):
            self.notify(f"Rollback finished ({str(res)[:80]}).")
            self.show_page(self._current_key or "server")

        def fail(exc):
            self.notify(f"Rollback failed: {exc}", ok=False)

        self.runner.run(work, on_success=ok, on_error=fail)

    def select_server(self, server_id: str) -> None:
        self.selected_id = server_id
        srv = self.store.get(server_id)
        if srv is not None:
            self.check_journal(srv)
        self.show_page("server")

    def _center(self) -> None:
        self.update_idletasks()
        w, h = 1180, 740
        x = max(0, (self.winfo_screenwidth() - w) // 2)
        y = max(0, (self.winfo_screenheight() - h) // 3)
        self.geometry(f"{w}x{h}+{x}+{y}")

    # -------------------------------------------------------------- sidebar
    def _build_sidebar(self) -> None:
        bar = ctk.CTkFrame(self, width=210, fg_color=T.SURFACE, corner_radius=0)
        bar.pack(side="left", fill="y")
        bar.pack_propagate(False)
        wallpaper.attach(bar)   # vertical wallpaper strip behind the nav

        t1 = ctk.CTkLabel(bar, text="MC Manager", font=T.font(21, "bold"),
                          text_color=T.TEXT)
        t1.pack(pady=(24, 2))
        t2 = ctk.CTkLabel(bar, text="SERVER CONTROLLER", font=T.font(10),
                          text_color=T.ACCENT)
        t2.pack(pady=(0, 18))
        wallpaper.blend_corners(t1)
        wallpaper.blend_corners(t2)

        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        self._nav_box = ctk.CTkFrame(bar, fg_color="transparent")
        self._nav_box.pack(fill="both", expand=True, padx=12)
        wallpaper.attach(self._nav_box)

        footer = ctk.CTkFrame(bar, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=12, pady=16)
        wallpaper.attach(footer)

        # service row: notifications + settings + lock state
        tools = ctk.CTkFrame(footer, fg_color="transparent")
        tools.pack(fill="x", pady=(0, 6))
        bell = ctk.CTkButton(tools, text="Alerts", width=62, height=30,
                             corner_radius=T.BUTTON_RADIUS,
                             fg_color=T.SURFACE2, hover_color="#242944",
                             text_color=T.TEXT_DIM, font=T.font(11),
                             command=self._open_alerts)
        bell.pack(side="left", padx=(0, 6))
        wallpaper.blend_corners(bell)
        gear = ctk.CTkButton(tools, text="Settings", width=76, height=30,
                             corner_radius=T.BUTTON_RADIUS,
                             fg_color=T.SURFACE2, hover_color="#242944",
                             text_color=T.TEXT_DIM, font=T.font(11),
                             command=self._open_settings)
        gear.pack(side="left")
        wallpaper.blend_corners(gear)
        self._lock_btn = ctk.CTkButton(
            tools, text="", width=30, height=30,
            corner_radius=T.BUTTON_RADIUS, fg_color=T.SURFACE2,
            hover_color="#242944", font=T.font(12), text_color=T.AMBER,
            command=self._toggle_lock)
        if self._master:
            self._lock_btn.configure(text="\u25c7" if self.restricted else "\u25c6")
            self._lock_btn.pack(side="right")
        wallpaper.blend_corners(self._lock_btn)

        add_btn = ctk.CTkButton(footer, text="+  Add Server", height=40,
                      corner_radius=T.BUTTON_RADIUS,
                      fg_color=T.ACCENT, hover_color=T.ACCENT_DARK,
                      text_color=T.ON_ACCENT, font=T.font(13, "bold"),
                      command=self._open_add)
        add_btn.pack(fill="x")
        wallpaper.blend_corners(add_btn)
        vlbl = ctk.CTkLabel(footer, text="Python Edition v2.0.0", font=T.font(10),
                            text_color=T.TEXT_DIM)
        vlbl.pack(pady=(10, 0))
        wallpaper.blend_corners(vlbl)

    def _open_add(self) -> None:
        from .pages_editor import open_editor
        open_editor(self, on_saved=lambda srv: (
            setattr(self, "selected_id", srv.id), self._refresh_nav(),
            self.show_page("server")))

    def _open_alerts(self) -> None:
        from .dialogs import AlertsDialog
        AlertsDialog(self)

    def _open_settings(self) -> None:
        from .dialogs import SettingsDialog
        SettingsDialog(self)

    def _toggle_lock(self) -> None:
        if not self._master:
            self.notify("Set a master password in Settings to enable "
                        "restricted mode.")
            return
        if self.restricted:
            from .dialogs import UnlockDialog
            UnlockDialog(self)
        else:
            self.restricted = True
            self._sync_lock_ui()
            self.notify("Restricted mode ON - permissions per server apply.")

    def _sync_lock_ui(self) -> None:
        if not hasattr(self, "_lock_btn"):
            return
        if self._master:
            self._lock_btn.configure(
                text="\u25c7" if self.restricted else "\u25c6")
            if not self._lock_btn.winfo_ismapped():
                self._lock_btn.pack(side="right")
        else:
            self._lock_btn.pack_forget()

    def open_detector(self) -> None:
        from .pages_detect import open_detector
        srv = self.server()
        if srv is None:
            return
        open_detector(self, srv,
                      on_done=lambda s: (self._refresh_nav(), self.show_page("server")))

    def _refresh_nav(self) -> None:
        for w in self._nav_box.winfo_children():
            w.destroy()
        if not getattr(self._nav_box, "_mcm_wallpaper_attached", False):
            wallpaper.attach(self._nav_box)
        self._nav_buttons.clear()

        srv = self.server()
        entries = list(PAGES_DASHBOARD)
        if srv:
            entries += [(k, lbl) for k, lbl in PAGES_SERVER]

        for key, label in entries:
            active = key == self._current_key
            btn = ctk.CTkButton(
                self._nav_box, text=label, anchor="w", height=36,
                corner_radius=T.BUTTON_RADIUS, font=T.font(13, "bold" if active else "normal"),
                fg_color=T.SURFACE2 if active else "transparent",
                hover_color=T.SURFACE2,
                text_color=T.ACCENT if active else T.TEXT_DIM,
                command=lambda k=key: self.show_page(k),
            )
            btn.pack(fill="x", pady=2)
            wallpaper.blend_corners(btn)
            self._nav_buttons[key] = btn

        if srv:
            self._server_badge(srv)

    def _server_badge(self, srv) -> None:
        """Compact 'active server' card at the bottom of the nav."""
        box = ctk.CTkFrame(self._nav_box, fg_color=T.SURFACE2,
                           corner_radius=T.BUTTON_RADIUS, border_width=1,
                           border_color=T.BORDER, cursor="hand2")
        box.pack(fill="x", pady=(14, 4), ipady=4)
        wallpaper.blend_corners(box)
        ctk.CTkLabel(box, text="ACTIVE SERVER", font=T.font(9, "bold"),
                     text_color=T.TEXT_DIM, anchor="w").pack(
            fill="x", padx=12, pady=(7, 0))
        ctk.CTkLabel(box, text=srv.name, font=T.font(13, "bold"),
                     text_color=T.TEXT, anchor="w").pack(
            fill="x", padx=12, pady=(1, 0))
        sub = (f"{T.platform_label(srv.platform)}"
               + (f" · MC {srv.mc_version}" if srv.mc_version else ""))
        ctk.CTkLabel(box, text=sub, font=T.font(10), text_color=T.TEXT_DIM,
                     anchor="w", wraplength=160, justify="left").pack(
            fill="x", padx=12, pady=(0, 7))

        def go(_e=None):
            self.show_page("server")

        def hover_in(_e=None):
            box.configure(border_color=T.ACCENT)

        def hover_out(_e=None):
            box.configure(border_color=T.BORDER)

        box.bind("<Button-1>", go)
        box.bind("<Enter>", hover_in)
        box.bind("<Leave>", hover_out)

    # ------------------------------------------------------------- switching
    def show_page(self, key: str) -> None:
        if key != "dashboard" and self.server() is None:
            key = "dashboard"
        self._current_key = key
        self._refresh_nav()

        if self._current_page is not None:
            try:
                if hasattr(self._current_page, "on_hide"):
                    self._current_page.on_hide()
                self._current_page.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._current_page = None

        page_cls = self._page_class(key)
        page = page_cls(self.content, self, self.server())
        page.configure(fg_color="transparent")  # let the wallpaper show through
        wallpaper.attach(page)
        page.pack(fill="both", expand=True)
        self._current_page = page
        if hasattr(page, "on_show"):
            page.on_show()

    def _page_class(self, key: str):
        from .pages_dashboard import DashboardPage
        from .pages_server import ServerPage
        from .pages_wizard import WizardPage
        from .pages_browse import PluginsPage
        from .pages_console import ConsolePage
        from .pages_terminal import TerminalPage
        from .pages_files import FilesPage
        from .pages_monitor import MonitorPage
        from .pages_backup import BackupsPage
        return {
            "dashboard": DashboardPage, "server": ServerPage,
            "wizard": WizardPage, "browse": PluginsPage,
            "console": ConsolePage, "terminal": TerminalPage,
            "files": FilesPage, "monitor": MonitorPage,
            "backup": BackupsPage,
        }[key]

    # ------------------------------------------------------------ main loop
    def _tick(self) -> None:
        try:
            self.runner.poll()
            # re-sample corner-blended widgets that moved (layout settling,
            # window resize) - cheap winfo checks, no redraw when unchanged
            wallpaper.refresh_blends()
        except Exception:  # noqa: BLE001
            pass
        self.after(100, self._tick)

    def _on_close(self) -> None:
        try:
            if self._current_page is not None and hasattr(self._current_page, "on_hide"):
                self._current_page.on_hide()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.watchdog.stop()
            self.scheduler.stop()
        except Exception:  # noqa: BLE001
            pass
        self.ssh.close_all()
        self.destroy()


def _choice_dialog(parent, title: str, message: str, options: list[str]) -> int:
    """Small modal choice dialog; returns the index (or -1)."""
    win = ctk.CTkToplevel(parent)
    win.title(title)
    win.configure(fg_color=T.BG)
    win.transient(parent)
    win.grab_set()
    win.resizable(False, False)
    box = {"choice": -1}

    ctk.CTkLabel(win, text=title, font=T.font(16, "bold"),
                 text_color=T.TEXT).pack(pady=(20, 6), padx=24)
    ctk.CTkLabel(win, text=message, font=T.font(12), text_color=T.TEXT_DIM,
                 justify="left", wraplength=420).pack(padx=24, pady=(0, 14))

    def pick(i):
        box["choice"] = i
        win.destroy()

    for i, opt in enumerate(options):
        ctk.CTkButton(win, text=opt, height=36, corner_radius=T.BUTTON_RADIUS,
                      fg_color=T.SURFACE2, hover_color="#242944",
                      text_color=T.TEXT, font=T.font(12),
                      command=lambda idx=i: pick(idx)).pack(fill="x", padx=40,
                                                            pady=3)
    win.bind("<Escape>", lambda e: win.destroy())
    parent.wait_window(win)
    return box["choice"]


def launch() -> None:
    app = MCManagerApp()
    app.mainloop()
