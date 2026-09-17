"""Server control page: status, start/stop/restart, detect existing installs,
quick log, navigation."""
from __future__ import annotations

import customtkinter as ctk

from .. import detect
from .. import theme as T
from ..models import Server
from .widgets import (AccentButton, Card, GhostButton, LogBox, SectionTitle,
                      StatusDot)


class ServerPage(ctk.CTkFrame):
    def __init__(self, master, app, server: Server | None):
        super().__init__(master, fg_color=T.BG, corner_radius=0)
        self.app = app
        self.server = server
        self.running = False
        self._busy = False

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=28, pady=(26, 8))
        self.dot = StatusDot(head, running=False, size=14)
        self.dot.pack(side="left", padx=(0, 10))
        SectionTitle(head, server.name if server else "Server").pack(side="left")
        self.state_lbl = ctk.CTkLabel(head, text="checking...", font=T.font(12),
                                      text_color=T.TEXT_DIM)
        self.state_lbl.pack(side="left", padx=12)

        if server is None:
            return
        srv = server

        # -- info card -----------------------------------------------------
        info = Card(self)
        info.pack(fill="x", padx=28, pady=6)
        grid = ctk.CTkFrame(info, fg_color="transparent")
        grid.pack(fill="x", padx=18, pady=14)
        for i in range(3):
            grid.grid_columnconfigure(i, weight=1)
        cells = [
            ("SSH", f"{srv.username}@{srv.host}:{srv.port}"),
            ("Platform", T.platform_label(srv.platform)),
            ("Minecraft", srv.mc_version or "-"),
            ("Install dir", srv.mc_dir or "-"),
            ("RAM", f"{srv.ram_mb} MB"),
            ("Console session", srv.external_screen or srv.screen_tag),
        ]
        self._cells: dict[str, ctk.CTkLabel] = {}
        for i, (label, value) in enumerate(cells):
            cell = ctk.CTkFrame(grid, fg_color="transparent")
            cell.grid(row=i // 3, column=i % 3, sticky="ew", padx=6, pady=6)
            ctk.CTkLabel(cell, text=label, font=T.font(10),
                         text_color=T.TEXT_DIM, anchor="w").pack(anchor="w")
            lbl = ctk.CTkLabel(cell, text=value, font=T.font(12),
                               text_color=T.TEXT, anchor="w")
            lbl.pack(anchor="w")
            self._cells[label] = lbl

        # -- controls --------------------------------------------------------
        ctrl = Card(self)
        ctrl.pack(fill="x", padx=28, pady=10)
        row = ctk.CTkFrame(ctrl, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=14)
        AccentButton(row, text="Start", width=120,
                     command=self._start).pack(side="left", padx=4)
        GhostButton(row, text="Stop", width=100,
                    command=self._stop).pack(side="left", padx=4)
        GhostButton(row, text="Restart", width=110,
                    command=self._restart).pack(side="left", padx=4)
        GhostButton(row, text="Refresh status", width=130,
                    command=self.refresh_status).pack(side="left", padx=4)
        GhostButton(row, text="Find existing installs", width=170,
                    command=self.app.open_detector).pack(side="left", padx=4)
        if not srv.mc_dir:
            ctk.CTkLabel(row, text="No install linked yet - use the Wizard or Detect",
                         font=T.font(11), text_color=T.AMBER).pack(side="left", padx=10)

        # -- navigation grid -------------------------------------------------
        nav = Card(self)
        nav.pack(fill="x", padx=28, pady=10)
        grid2 = ctk.CTkFrame(nav, fg_color="transparent")
        grid2.pack(fill="x", padx=14, pady=14)
        for i in range(3):
            grid2.grid_columnconfigure(i, weight=1)
        links = [
            ("wizard", "Run Setup Wizard", "install MC automatically"),
            ("browse", "Mods & Plugins", "one-click from Modrinth"),
            ("console", "Console", "live log + commands"),
            ("terminal", "Terminal", "full SSH shell"),
            ("files", "Files", "SFTP upload / download"),
            ("monitor", "Monitor", "CPU / RAM / disk"),
        ]
        for i, (key, title, sub) in enumerate(links):
            cell = ctk.CTkButton(
                grid2, text=f"{title}\n{sub}", anchor="w",
                height=62, corner_radius=T.BUTTON_RADIUS, font=T.font(12),
                fg_color=T.SURFACE2, hover_color="#242944",
                text_color=T.TEXT,
                command=lambda k=key: self.app.show_page(k))
            cell.grid(row=i // 3, column=i % 3, sticky="ew", padx=6, pady=6)

        # -- quick log -------------------------------------------------------
        log_card = Card(self)
        log_card.pack(fill="both", expand=True, padx=28, pady=(10, 20))
        row2 = ctk.CTkFrame(log_card, fg_color="transparent")
        row2.pack(fill="x", padx=14, pady=(12, 0))
        ctk.CTkLabel(row2, text="Last log lines", font=T.font(12),
                     text_color=T.TEXT_DIM).pack(side="left")
        GhostButton(row2, text="Refresh", width=80, height=26,
                    command=self._tail).pack(side="right")
        self.log = LogBox(log_card, height=180)
        self.log.pack(fill="both", expand=True, padx=14, pady=(8, 14))

    # ------------------------------------------------------------- status
    def on_show(self) -> None:
        self.refresh_status()
        self._tail()
        self._auto_info()

    def _auto_info(self) -> None:
        """Keep platform / Minecraft version / console session current.
        The app detects the running version by itself (1.21.11, 26.2, ...)
        from logs and jar metadata - the user never types it."""
        srv = self.server
        if srv is None or not srv.mc_dir:
            return

        def cb(info):
            if not self.winfo_exists() or not info:
                return
            self.app.store.update(srv)
            self.app._refresh_nav()
            if "Platform" in self._cells:
                self._cells["Platform"].configure(
                    text=T.platform_label(srv.platform))
            if "Minecraft" in self._cells:
                self._cells["Minecraft"].configure(text=srv.mc_version or "-")
            if "Console session" in self._cells:
                self._cells["Console session"].configure(
                    text=srv.external_screen or srv.screen_tag)

        self.app.runner.run(
            lambda: detect.refresh_server_info(self.app.ssh, srv),
            on_success=cb, on_error=lambda e: None)

    def refresh_status(self) -> None:
        if self.server is None:
            return
        srv = self.server
        self.state_lbl.configure(text="checking...", text_color=T.TEXT_DIM)
        self.app.runner.run(
            lambda: self.app.control.status(srv),
            on_success=self._status_cb,
            on_error=lambda e: self._status_err(e),
        )

    def _status_cb(self, running: bool) -> None:
        if not self.winfo_exists():
            return
        self.running = bool(running)
        self.dot.set_running(self.running)
        if self.running:
            self.state_lbl.configure(text="RUNNING", text_color=T.ACCENT_SOFT)
        else:
            self.state_lbl.configure(text="stopped", text_color=T.TEXT_DIM)

    def _status_err(self, exc) -> None:
        if self.winfo_exists():
            self.state_lbl.configure(text=f"unreachable - {exc}", text_color=T.RED)

    def _tail(self) -> None:
        if self.server is None:
            return
        srv = self.server

        def work():
            self.app.control.console_init_offset(srv)
            return self.app.control.console_poll(srv)

        def cb(lines):
            if self.winfo_exists() and isinstance(lines, list):
                if not lines:
                    self.log.show_placeholder(
                        "no log output yet - is the server started?\n")
                    return
                self.log.delete("1.0", "end")
                for ln in lines[-25:]:
                    self.log.append_line_colored(ln)

        self.app.runner.run(work, on_success=cb,
                            on_error=lambda e: self._status_err(e))

    # ------------------------------------------------------------ lifecycle
    def _guard_busy(self) -> bool:
        if self._busy:
            self.app.notify("Another action is running...", ok=False)
            return True
        return False

    def _start(self) -> None:
        if self._guard_busy() or self.server is None:
            return
        srv = self.server
        if not srv.mc_dir or not srv.platform:
            self.app.notify("Run the Setup Wizard or Detect an existing install first.", ok=False)
            return
        self._busy = True
        self.state_lbl.configure(text="starting...", text_color=T.AMBER)
        self.app.runner.run(
            lambda: (self.app.control.start(srv, lambda m: self.app.runner.emit(
                lambda l: self.winfo_exists() and self.log.append(l), m)), None),
            on_success=lambda _: (self._set_busy(False), self.refresh_status(),
                                  self.app.notify("Server start command sent")),
            on_error=lambda e: (self._set_busy(False), self.refresh_status(),
                                self.app.notify(str(e), ok=False)),
        )

    def _stop(self) -> None:
        if self._guard_busy() or self.server is None:
            return
        srv = self.server
        self._busy = True
        self.state_lbl.configure(text="stopping...", text_color=T.AMBER)
        self.app.runner.run(
            lambda: (self.app.control.stop(srv, lambda m: self.app.runner.emit(
                lambda l: self.winfo_exists() and self.log.append(l), m)), None),
            on_success=lambda _: (self._set_busy(False), self.refresh_status(),
                                  self.app.notify("Server stopped")),
            on_error=lambda e: (self._set_busy(False), self.app.notify(str(e), ok=False)),
        )

    def _restart(self) -> None:
        if self._guard_busy() or self.server is None:
            return
        srv = self.server
        self._busy = True
        self.state_lbl.configure(text="restarting...", text_color=T.AMBER)

        def work():
            self.app.control.restart(srv, lambda m: self.app.runner.emit(
                lambda l: self.winfo_exists() and self.log.append(l), m))

        self.app.runner.run(
            work,
            on_success=lambda _: (self._set_busy(False), self.refresh_status(),
                                  self.app.notify("Server restarted")),
            on_error=lambda e: (self._set_busy(False), self.refresh_status(),
                                self.app.notify(str(e), ok=False)),
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
