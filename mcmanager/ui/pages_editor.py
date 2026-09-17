"""Add / edit server modal dialog with connection test + policies.

v2.0: adds a Policies section (issue 31) - runner mode, Java override,
extra JVM args, auto-restart, schedules/retention and per-server
permission flags (issue 24).  Secrets are stored encrypted at rest
(secure.CredentialVault) - noted in the dialog.
"""
from __future__ import annotations

import customtkinter as ctk

from .. import theme as T
from ..models import Server
from .widgets import AccentButton, GhostButton, LabeledField

RUNNERS = ["auto", "screen", "tmux", "direct"]


class _Editor(ctk.CTkToplevel):
    def __init__(self, app, server: Server | None, on_saved):
        super().__init__(app)
        self.app = app
        self.server = server
        self.on_saved = on_saved

        self.title("Edit server" if server else "Add server")
        self.geometry("500x680")
        self.configure(fg_color=T.BG)
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()

        ctk.CTkLabel(self, text="Edit server" if server else "New server",
                     font=T.font(19, "bold"), text_color=T.TEXT).pack(pady=(20, 2))
        ctk.CTkLabel(self, text="SSH account of the Linux machine - the password "
                                "is stored encrypted on this computer",
                     font=T.font(10), text_color=T.TEXT_DIM,
                     wraplength=430, justify="center").pack(pady=(0, 10))

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=28)

        self.f_name = LabeledField(body, "Server name", value=server.name if server else "",
                                   placeholder="My Minecraft Box")
        self.f_name.pack(fill="x", pady=5)
        self.f_host = LabeledField(body, "IP address / hostname",
                                   value=server.host if server else "",
                                   placeholder="203.0.113.10")
        self.f_host.pack(fill="x", pady=5)
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=5)
        row.grid_columnconfigure(0, weight=2)
        row.grid_columnconfigure(1, weight=1)
        self.f_user = LabeledField(row, "Username",
                                   value=server.username if server else "root")
        self.f_user.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.f_port = LabeledField(row, "Port (default 22)",
                                   value=str(server.port) if server else "22")
        self.f_port.grid(row=0, column=1, sticky="ew")
        self.f_pass = LabeledField(body, "Password", show="*",
                                   value=server.password if server else "",
                                   placeholder="SSH password")
        self.f_pass.pack(fill="x", pady=5)

        # ---- policies (issue 31) -------------------------------------------
        pol = ctk.CTkFrame(body, fg_color=T.SURFACE, corner_radius=T.CARD_RADIUS,
                           border_width=1, border_color=T.BORDER)
        pol.pack(fill="x", pady=(10, 4), ipady=4)
        ctk.CTkLabel(pol, text="Policies (optional)", font=T.font(12, "bold"),
                     text_color=T.ACCENT_SOFT, anchor="w").pack(
            fill="x", padx=12, pady=(8, 2))

        prow = ctk.CTkFrame(pol, fg_color="transparent")
        prow.pack(fill="x", padx=12, pady=(0, 4))
        prow.grid_columnconfigure(0, weight=1)
        prow.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(prow, text="Process runner", font=T.font(10),
                     text_color=T.TEXT_DIM, anchor="w").grid(
            row=0, column=0, sticky="ew", padx=4)
        ctk.CTkLabel(prow, text="Java (major, 0 = auto)", font=T.font(10),
                     text_color=T.TEXT_DIM, anchor="w").grid(
            row=0, column=1, sticky="ew", padx=4)
        self.runner_menu = ctk.CTkOptionMenu(
            prow, values=RUNNERS, width=140, height=30, font=T.font(11),
            fg_color=T.SURFACE2, button_color=T.ACCENT_DARK,
            button_hover_color=T.ACCENT)
        self.runner_menu.set((server.runner if server else "auto") or "auto")
        self.runner_menu.grid(row=1, column=0, sticky="ew", padx=4)
        self.f_java = LabeledField(prow, "",
                                   value=str(server.java_major if server else 0))
        self.f_java.entry.configure(height=30)
        self.f_java.grid(row=1, column=1, sticky="ew", padx=4)

        self.f_jvm = LabeledField(pol, "Extra JVM args (e.g. -XX:+UseZGC)",
                                  value=server.extra_jvm if server else "")
        self.f_jvm.pack(fill="x", padx=12, pady=4)
        sched = ctk.CTkFrame(pol, fg_color="transparent")
        sched.pack(fill="x", padx=12, pady=(0, 2))
        sched.grid_columnconfigure((0, 1, 2), weight=1)
        self.f_btime = self._mini(sched, "Backup HH:MM",
                                  server.backup_time if server else "", 0)
        self.f_rtime = self._mini(sched, "Restart HH:MM",
                                  server.restart_time if server else "", 1)
        self.f_keep = self._mini(sched, "Keep N",
                                 str(server.backup_keep if server else 10), 2)

        self.var_restart = ctk.BooleanVar(value=server.auto_restart if server else True)
        ctk.CTkSwitch(pol, text="Auto-restart after crash (watchdog)",
                      variable=self.var_restart, font=T.font(11),
                      progress_color=T.ACCENT).pack(anchor="w", padx=12, pady=4)

        # -- permissions (issue 24) ------------------------------------------
        perm = ctk.CTkFrame(body, fg_color=T.SURFACE, corner_radius=T.CARD_RADIUS,
                            border_width=1, border_color=T.BORDER)
        perm.pack(fill="x", pady=(4, 4), ipady=4)
        ctk.CTkLabel(perm, text="Allowed in restricted mode", font=T.font(12, "bold"),
                     text_color=T.ACCENT_SOFT, anchor="w").pack(
            fill="x", padx=12, pady=(8, 2))
        ctk.CTkLabel(perm, text="applies only when a master password is set "
                                "(Settings) and the app is locked",
                     font=T.font(9), text_color=T.TEXT_DIM, anchor="w").pack(
            fill="x", padx=12)
        pgrid = ctk.CTkFrame(perm, fg_color="transparent")
        pgrid.pack(fill="x", padx=12, pady=(2, 8))
        for i in range(2):
            pgrid.grid_columnconfigure(i, weight=1)
        self.perm_vars = {}
        for idx, (key, label) in enumerate((
                ("allow_console", "Console commands"),
                ("allow_files", "File management"),
                ("allow_restart", "Start / Stop / Restart"),
                ("allow_edit", "Edit settings"))):
            var = ctk.BooleanVar(value=bool(getattr(server, key)) if server else True)
            self.perm_vars[key] = var
            ctk.CTkCheckBox(pgrid, text=label, variable=var, font=T.font(11),
                            checkbox_width=18, checkbox_height=18,
                            corner_radius=4, fg_color=T.ACCENT,
                            hover_color=T.ACCENT_DARK).grid(
                row=idx // 2, column=idx % 2, sticky="w", padx=6, pady=3)

        self.status = ctk.CTkLabel(self, text="", font=T.font(11),
                                   text_color=T.TEXT_DIM)
        self.status.pack(pady=(4, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", pady=(4, 16), padx=28)
        AccentButton(btns, text="Save server", width=150,
                     command=self._save).pack(side="right")
        GhostButton(btns, text="Test connection", width=140,
                    command=self._test).pack(side="right", padx=(0, 8))
        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())

    def _mini(self, parent, label, value, col):
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.grid(row=0, column=col, sticky="ew", padx=4)
        ctk.CTkLabel(box, text=label, font=T.font(10), text_color=T.TEXT_DIM,
                     anchor="w").pack(fill="x")
        entry = ctk.CTkEntry(box, height=30, font=T.mono(11),
                             fg_color=T.SURFACE2, border_color=T.BORDER,
                             corner_radius=T.BUTTON_RADIUS)
        entry.delete(0, "end")
        entry.insert(0, value)
        entry.pack(fill="x")
        return entry

    # ------------------------------------------------------------- helpers
    def _collect(self) -> tuple | None:
        host = self.f_host.get()
        if not host:
            self.status.configure(text="Host is required.", text_color=T.RED)
            return None
        try:
            port = int(self.f_port.get() or 22)
        except ValueError:
            self.status.configure(text="Port must be a number.", text_color=T.RED)
            return None
        name = self.f_name.get() or host
        return name, host, self.f_user.get() or "root", self.f_pass.get(), port

    def _test(self):
        data = self._collect()
        if not data:
            return
        _, host, user, password, port = data
        self.status.configure(text="Connecting...", text_color=T.AMBER)
        probe = Server(id="probe", name="probe", host=host, port=port,
                       username=user, password=password)
        self.app.runner.run(
            lambda: self.app.ssh.test(probe),
            on_success=lambda msg: self.status.configure(text=msg, text_color=T.ACCENT_SOFT),
            on_error=lambda e: self.status.configure(text=str(e), text_color=T.RED),
        )

    def _save(self):
        data = self._collect()
        if not data:
            return
        name, host, user, password, port = data
        if self.server:
            srv = self.server
            srv.name, srv.host = name, host
            srv.username, srv.password = user, password
            srv.port = port
        else:
            srv = self.app.store.add(name, host, user, password, port)
        # policies
        srv.runner = self.runner_menu.get() or "auto"
        try:
            srv.java_major = max(0, int(self.f_java.get().strip() or "0"))
        except ValueError:
            srv.java_major = 0
        srv.extra_jvm = self.f_jvm.get().strip()
        srv.backup_time = self.f_btime.get().strip()
        srv.restart_time = self.f_rtime.get().strip()
        if srv.backup_time and srv.parsed_backup_time() is None:
            self.status.configure(text="Backup time must look like 04:30",
                                  text_color=T.RED)
            return
        if srv.restart_time and srv.parsed_restart_time() is None:
            self.status.configure(text="Restart time must look like 04:30",
                                  text_color=T.RED)
            return
        try:
            srv.backup_keep = max(1, int(self.f_keep.get().strip() or "10"))
        except ValueError:
            pass
        srv.auto_restart = bool(self.var_restart.get())
        for key, var in self.perm_vars.items():
            setattr(srv, key, bool(var.get()))
        self.app.store.update(srv)
        self.app.select_server(srv.id)
        self.destroy()
        self.on_saved(srv)


def open_editor(app, server: Server | None = None, on_saved=lambda srv: None):
    return _Editor(app, server, on_saved)
