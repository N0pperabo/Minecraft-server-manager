"""Dashboard: server cards with live status checks."""
from __future__ import annotations

import customtkinter as ctk

from .. import theme as T
from ..models import Server
from . import wallpaper
from .widgets import Card, GhostButton, StatusDot, SectionTitle, DangerButton


class DashboardPage(ctk.CTkFrame):
    def __init__(self, master, app, server: Server | None):
        super().__init__(master, fg_color=T.BG, corner_radius=0)
        self.app = app
        self._dots: dict[str, StatusDot] = {}

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=28, pady=(26, 10))
        SectionTitle(head, "Your Servers").pack(side="left")
        ctk.CTkLabel(head, text="connect to any linux box and run minecraft in minutes",
                     font=T.font(12), text_color=T.TEXT_DIM).pack(side="left", padx=14)

        self.list_box = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        self.list_box.pack(fill="both", expand=True, padx=22, pady=(4, 20))
        wallpaper.attach(self.list_box)

        if not app.store.servers:
            empty = Card(self.list_box)
            empty.pack(fill="x", pady=30, padx=6)
            ctk.CTkLabel(empty, text="No servers yet", font=T.font(17, "bold"),
                         text_color=T.TEXT).pack(pady=(28, 4))
            ctk.CTkLabel(empty, text="Click  + Add Server  in the sidebar to connect\n"
                                     "to your first Linux machine.",
                         font=T.font(12), text_color=T.TEXT_DIM,
                         justify="center").pack(pady=(0, 26))
        else:
            for srv in app.store.servers:
                self._server_card(srv)

    # ------------------------------------------------------------- card ui
    def _server_card(self, srv: Server) -> None:
        card = Card(self.list_box)
        card.pack(fill="x", pady=7, padx=6)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=14)

        dot = StatusDot(inner, running=False)
        dot.pack(side="left", padx=(0, 12))
        self._dots[srv.id] = dot

        info = ctk.CTkFrame(inner, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(info, text=srv.name, font=T.font(16, "bold"),
                     text_color=T.TEXT, anchor="w").pack(anchor="w")
        plat = (f"{T.platform_label(srv.platform)}  ·  {srv.mc_version}"
                if srv.platform else "Not set up yet - open Control to install or detect")
        ctk.CTkLabel(info, text=f"{srv.username}@{srv.host}:{srv.port}   |   {plat}",
                     font=T.font(11), text_color=T.TEXT_DIM,
                     anchor="w").pack(anchor="w")

        btns = ctk.CTkFrame(inner, fg_color="transparent")
        btns.pack(side="right")
        GhostButton(btns, text="Manage", width=96,
                    command=lambda s=srv: self.app.select_server(s.id)
                    ).pack(side="left", padx=4)
        GhostButton(btns, text="Edit", width=70,
                    command=lambda s=srv: self._edit(s)).pack(side="left", padx=4)
        DangerButton(btns, text="Delete", width=74,
                     command=lambda s=srv: self._delete(s)).pack(side="left", padx=4)

        card.bind("<Enter>", lambda e, c=card: c.configure(border_color=T.ACCENT))
        card.bind("<Leave>", lambda e, c=card: c.configure(border_color=T.BORDER))

    # ------------------------------------------------------------- actions
    def _edit(self, srv: Server) -> None:
        from .pages_editor import open_editor
        open_editor(self.app, server=srv, on_saved=lambda s: self.on_show())

    def _delete(self, srv: Server) -> None:
        from tkinter import messagebox
        if not messagebox.askyesno(
                "Delete server",
                f"Remove '{srv.name}' from the app?\nNothing on the Linux machine is deleted."):
            return
        self.app.ssh.close(srv.id)
        self.app.store.delete(srv)
        if self.app.selected_id == srv.id:
            self.app.selected_id = self.app.store.servers[0].id if self.app.store.servers else None
        self.on_show()
        self.app.notify(f"'{srv.name}' removed")

    # ----------------------------------------------------------------- live
    def on_show(self) -> None:
        for srv in self.app.store.servers:
            self.app.runner.run(
                lambda s=srv: self.app.ssh.exec(
                    s, f"screen -ls 2>/dev/null | grep -c '\\.{s.screen_tag}[[:space:]]'; true",
                    timeout=15).stdout.strip(),
                on_success=self._make_status_cb(srv.id),
                on_error=lambda e, s=srv: None,
            )

    def _make_status_cb(self, server_id: str):
        def cb(result):
            running = bool(str(result).strip())
            dot = self._dots.get(server_id)
            if dot is not None and dot.winfo_exists():
                dot.set_running(running)
        return cb
