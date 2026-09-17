"""Modal dialogs: Alerts history, Settings (webhooks + master password +
trusted hosts), Unlock.  Built on the same dark theme as the app."""
from __future__ import annotations

import time

import customtkinter as ctk
from tkinter import messagebox

from .. import theme as T
from ..notify import NOTIFIER
from ..secure import HOST_KEYS
from .widgets import AccentButton, GhostButton, LabeledField


class _Dialog(ctk.CTkToplevel):
    def __init__(self, app, title: str, w: int, h: int):
        super().__init__(app)
        self.app = app
        self.title(title)
        self.geometry(f"{w}x{h}")
        self.configure(fg_color=T.BG)
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.bind("<Escape>", lambda e: self.destroy())


# =================================================================== alerts ==
class AlertsDialog(_Dialog):
    """Notification history (crash / restart / backup / update / disk)."""

    def __init__(self, app):
        super().__init__(app, "Alerts & history", 640, 520)
        ctk.CTkLabel(self, text="Alerts & history", font=T.font(17, "bold"),
                     text_color=T.TEXT).pack(pady=(18, 2))
        ctk.CTkLabel(self, text="crash, restart, backup, update and disk events",
                     font=T.font(11), text_color=T.TEXT_DIM).pack(pady=(0, 10))

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20)

        items = NOTIFIER.history(limit=200)
        if not items:
            ctk.CTkLabel(body, text="no events yet", font=T.font(12),
                         text_color=T.TEXT_DIM).pack(pady=20)
        for ev in reversed(items):
            row = ctk.CTkFrame(body, fg_color=T.SURFACE, corner_radius=8,
                               border_width=1, border_color=T.BORDER)
            row.pack(fill="x", pady=3)
            color = T.ACCENT_SOFT if ev.get("ok") else T.RED
            when = time.strftime("%m-%d %H:%M", time.localtime(ev.get("ts", 0)))
            ctk.CTkLabel(row, text=f"{when}  ·  {ev.get('kind', '?')}  ·  "
                                   f"{ev.get('server', '?')}",
                         font=T.font(10, "bold"), text_color=color,
                         anchor="w").pack(fill="x", padx=10, pady=(6, 0))
            ctk.CTkLabel(row, text=ev.get("message", ""), font=T.font(11),
                         text_color=T.TEXT, anchor="w", wraplength=560,
                         justify="left").pack(fill="x", padx=10, pady=(0, 6))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", pady=8, padx=20)
        GhostButton(btns, text="Clear history", width=120, height=32,
                    command=self._clear).pack(side="left")
        AccentButton(btns, text="Close", width=100, height=32,
                     command=self.destroy).pack(side="right")

    def _clear(self):
        NOTIFIER.clear_history()
        self.destroy()
        self.app.notify("Alert history cleared.")


# ================================================================= settings ==
class SettingsDialog(_Dialog):
    def __init__(self, app):
        super().__init__(app, "Settings", 560, 620)
        ctk.CTkLabel(self, text="Settings", font=T.font(17, "bold"),
                     text_color=T.TEXT).pack(pady=(18, 2))
        ctk.CTkLabel(self, text="notifications, security and trusted hosts",
                     font=T.font(11), text_color=T.TEXT_DIM).pack(pady=(0, 12))

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24)

        # -- notifications -----------------------------------------------------
        ctk.CTkLabel(body, text="Notifications (webhooks)",
                     font=T.font(13, "bold"), text_color=T.ACCENT_SOFT,
                     anchor="w").pack(fill="x", pady=(4, 4))
        hooks = NOTIFIER.webhooks()
        self.f_discord = LabeledField(body, "Discord webhook URL",
                                      value=hooks.get("discord", ""),
                                      placeholder="https://discord.com/api/webhooks/...")
        self.f_discord.pack(fill="x", pady=3)
        self.f_generic = LabeledField(body, "Generic webhook URL (JSON POST)",
                                      value=hooks.get("generic", ""),
                                      placeholder="https://your.endpoint/hook")
        self.f_generic.pack(fill="x", pady=3)
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=(2, 10))
        GhostButton(row, text="Save webhooks", width=130, height=30,
                    command=self._save_hooks).pack(side="left")
        GhostButton(row, text="Send test alert", width=120, height=30,
                    command=self._test_hook).pack(side="left", padx=6)

        # -- master password (issue 24) ------------------------------------------
        ctk.CTkLabel(body, text="Restricted mode (master password)",
                     font=T.font(13, "bold"), text_color=T.ACCENT_SOFT,
                     anchor="w").pack(fill="x", pady=(8, 4))
        ctk.CTkLabel(body, text="With a master password set, the app starts "
                                "in restricted mode and per-server permissions "
                                "(console / files / restart / edit) are "
                                "enforced until you unlock.",
                     font=T.font(10), text_color=T.TEXT_DIM, anchor="w",
                     wraplength=480, justify="left").pack(fill="x")
        self.f_pass1 = LabeledField(body, "New master password (blank = keep)",
                                    show="*")
        self.f_pass1.pack(fill="x", pady=3)
        row2 = ctk.CTkFrame(body, fg_color="transparent")
        row2.pack(fill="x", pady=(2, 10))
        GhostButton(row2, text="Set password", width=130, height=30,
                    command=self._set_pass).pack(side="left")
        GhostButton(row2, text="Remove password", width=140, height=30,
                    command=self._del_pass).pack(side="left", padx=6)

        # -- trusted hosts (issue 3) ----------------------------------------------
        ctk.CTkLabel(body, text="Trusted SSH hosts (TOFU)",
                     font=T.font(13, "bold"), text_color=T.ACCENT_SOFT,
                     anchor="w").pack(fill="x", pady=(8, 4))
        ctk.CTkLabel(body, text="Remove an entry only after a LEGITIMATE host "
                                "key change (e.g. VPS reinstall) - otherwise a "
                                "changed key is rejected as a MITM attack.",
                     font=T.font(10), text_color=T.TEXT_DIM, anchor="w",
                     wraplength=480, justify="left").pack(fill="x")
        self.hosts_box = ctk.CTkScrollableFrame(body, height=130,
                                                fg_color=T.SURFACE)
        self.hosts_box.pack(fill="x", pady=6)
        self._render_hosts()

        AccentButton(self, text="Done", width=110, height=34,
                     command=self.destroy).pack(pady=(6, 16))

    # -- webhooks ----------------------------------------------------------
    def _save_hooks(self):
        NOTIFIER.set_webhooks(self.f_discord.get(), self.f_generic.get())
        self.app.notify("Webhook settings saved.")

    def _test_hook(self):
        NOTIFIER.set_webhooks(self.f_discord.get(), self.f_generic.get())
        NOTIFIER.publish("test", "Settings",
                         "This is a test notification from MC Manager.")
        self.app.notify("Test alert published (check your webhook).")

    # -- master password -----------------------------------------------------
    def _set_pass(self):
        pw = self.f_pass1.get()
        if len(pw) < 6:
            messagebox.showwarning("Too short",
                                   "Use at least 6 characters.", parent=self)
            return
        self.app.set_master_password(pw)
        self.app._sync_lock_ui()
        self.app.notify("Master password set - restricted mode enabled "
                        "on next start.")
        self.f_pass1.var.set("")

    def _del_pass(self):
        self.app.clear_master_password()
        self.app._sync_lock_ui()
        self.app.notify("Master password removed - restricted mode off.")

    # -- hosts -----------------------------------------------------------------
    def _render_hosts(self):
        for w in self.hosts_box.winfo_children():
            w.destroy()
        try:
            import paramiko
            hk = paramiko.HostKeys(str(HOST_KEYS.path))
            entries = [(h, k.get_name()) for h in hk.keys()
                       for k in (hk.lookup(h) or [])]
        except Exception:  # noqa: BLE001
            entries = []
        if not entries:
            ctk.CTkLabel(self.hosts_box, text="no trusted hosts yet",
                         font=T.font(11), text_color=T.TEXT_DIM).pack(pady=14)
            return
        for host, keytype in entries:
            row = ctk.CTkFrame(self.hosts_box, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=f"{host}   ({keytype})",
                         font=T.mono(11), text_color=T.TEXT,
                         anchor="w").pack(side="left", padx=8)
            GhostButton(row, text="Forget", width=70, height=24,
                        command=lambda h=host: self._forget(h)).pack(side="right")

    def _forget(self, host: str):
        HOST_KEYS.forget(host)
        self._render_hosts()
        self.app.notify(f"Trusted key for {host} removed.")


# =================================================================== unlock ==
class UnlockDialog(_Dialog):
    def __init__(self, app):
        super().__init__(app, "Unlock restricted mode", 420, 240)
        ctk.CTkLabel(self, text="Enter master password",
                     font=T.font(15, "bold"), text_color=T.TEXT).pack(pady=(20, 4))
        ctk.CTkLabel(self, text="restricted mode limits actions by the "
                                "per-server permission flags",
                     font=T.font(10), text_color=T.TEXT_DIM).pack(pady=(0, 10))
        self.f_pass = LabeledField(self, "Master password", show="*")
        self.f_pass.pack(fill="x", padx=30)
        self.status = ctk.CTkLabel(self, text="", font=T.font(11),
                                   text_color=T.RED)
        self.status.pack()
        AccentButton(self, text="Unlock", width=110, height=34,
                     command=self._try).pack(pady=(6, 4))
        self.bind("<Return>", lambda e: self._try())

    def _try(self):
        if self.app.try_unlock(self.f_pass.get()):
            self.app._sync_lock_ui()
            self.app.notify("Unlocked - full access for this session.")
            self.destroy()
        else:
            self.status.configure(text="wrong password")
