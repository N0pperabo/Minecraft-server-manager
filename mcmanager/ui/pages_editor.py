"""Add / edit server modal dialog with connection test."""
from __future__ import annotations

import customtkinter as ctk

from .. import theme as T
from ..models import Server
from .widgets import AccentButton, GhostButton, LabeledField


class _Editor(ctk.CTkToplevel):
    def __init__(self, app, server: Server | None, on_saved):
        super().__init__(app)
        self.app = app
        self.server = server
        self.on_saved = on_saved

        self.title("Edit server" if server else "Add server")
        self.geometry("460x520")
        self.configure(fg_color=T.BG)
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()

        ctk.CTkLabel(self, text="Edit server" if server else "New server",
                     font=T.font(19, "bold"), text_color=T.TEXT).pack(pady=(22, 2))
        ctk.CTkLabel(self, text="SSH account of your Linux machine",
                     font=T.font(11), text_color=T.TEXT_DIM).pack(pady=(0, 14))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=30)

        self.f_name = LabeledField(body, "Server name", value=server.name if server else "",
                                   placeholder="My Minecraft Box")
        self.f_name.pack(fill="x", pady=6)
        self.f_host = LabeledField(body, "IP address / hostname",
                                   value=server.host if server else "",
                                   placeholder="203.0.113.10")
        self.f_host.pack(fill="x", pady=6)
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=6)
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
        self.f_pass.pack(fill="x", pady=6)

        self.status = ctk.CTkLabel(self, text="", font=T.font(11),
                                   text_color=T.TEXT_DIM)
        self.status.pack(pady=(2, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", pady=(6, 20), padx=30)
        # primary action at the bottom-right, as in standard dialogs
        AccentButton(btns, text="Save server", width=150,
                     command=self._save).pack(side="right")
        GhostButton(btns, text="Test connection", width=140,
                    command=self._test).pack(side="right", padx=(0, 8))
        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())

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

    def _test(self) -> None:
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

    def _save(self) -> None:
        data = self._collect()
        if not data:
            return
        name, host, user, password, port = data
        if self.server:
            self.server.name, self.server.host = name, host
            self.server.username, self.server.password = user, password
            self.server.port = port
            self.app.store.update(self.server)
            srv = self.server
        else:
            srv = self.app.store.add(name, host, user, password, port)
        self.app.select_server(srv.id)
        self.destroy()
        self.on_saved(srv)


def open_editor(app, server: Server | None = None, on_saved=lambda srv: None):
    return _Editor(app, server, on_saved)
