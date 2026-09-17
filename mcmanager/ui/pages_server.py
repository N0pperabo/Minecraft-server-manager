"""Server control page: deep status, start/stop/restart, update with
rollback, quick log, navigation.

v2.0:
- status comes from health.deep_status (issue 22/23): RUNNING alone is
  not enough - the badge now shows ready / starting / unresponsive with
  online player count from the real Server List Ping.
- 'Check for updates' runs the guarded update pipeline (issue 14):
  pre-backup, checksum-verified download, health gate, auto-rollback.
- every action respects restricted-mode permissions (issue 24) and the
  operation locks (issue 25).
"""
from __future__ import annotations

import customtkinter as ctk
from tkinter import messagebox

from .. import detect
from .. import health as HLTH
from .. import theme as T
from .. import updater as UPD
from ..models import Server
from .widgets import (AccentButton, Card, GhostButton, LogBox, SectionTitle,
                      StatusDot)

STATE_TEXT = {
    "ready": ("READY", T.ACCENT_SOFT),
    "starting": ("STARTING...", T.AMBER),
    "unresponsive": ("NOT RESPONDING", T.AMBER),
    "stopped": ("stopped", T.TEXT_DIM),
    "unreachable": ("unreachable", T.RED),
}


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
        self.btn_start = AccentButton(row, text="Start", width=110,
                                      command=self._start)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = GhostButton(row, text="Stop", width=96,
                                    command=self._stop)
        self.btn_stop.pack(side="left", padx=4)
        self.btn_restart = GhostButton(row, text="Restart", width=104,
                                       command=self._restart)
        self.btn_restart.pack(side="left", padx=4)
        GhostButton(row, text="Refresh status", width=126,
                    command=self.refresh_status).pack(side="left", padx=4)
        self.btn_update = GhostButton(row, text="Check for updates", width=150,
                                      command=self._update_flow)
        self.btn_update.pack(side="left", padx=4)
        GhostButton(row, text="Find existing installs", width=170,
                    command=self.app.open_detector).pack(side="left", padx=4)
        if not srv.mc_dir:
            ctk.CTkLabel(row, text="No install linked yet - use the Wizard or Detect",
                         font=T.font(11), text_color=T.AMBER).pack(side="left", padx=10)

        # -- update progress line ----------------------------------------------
        self.upd_bar = ctk.CTkProgressBar(self, height=8,
                                          corner_radius=T.BUTTON_RADIUS)
        self.upd_bar.set(0)
        self.upd_label = ctk.CTkLabel(self, text="", font=T.font(10),
                                      text_color=T.TEXT_DIM, anchor="w")
        if not srv.mc_dir:
            return  # nothing below makes sense without an install

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
            ("backup", "Backups", "backup / restore / cleanup"),
            ("console", "Console", "live log + commands"),
            ("terminal", "Terminal", "full SSH shell"),
            ("files", "Files", "SFTP upload / download"),
            ("monitor", "Monitor", "TPS · heap · CPU / RAM"),
        ]
        for i, (key, title, sub) in enumerate(links):
            cell = ctk.CTkButton(
                grid2, text=f"{title}\n{sub}", anchor="w",
                height=58, corner_radius=T.BUTTON_RADIUS, font=T.font(12),
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
        self.log = LogBox(log_card, height=170)
        self.log.pack(fill="both", expand=True, padx=14, pady=(8, 14))

    # ------------------------------------------------------------- status
    def on_show(self) -> None:
        self._apply_permissions()
        self.refresh_status()
        self._tail()
        self._auto_info()

    def _apply_permissions(self):
        srv = self.server
        if srv is None:
            return
        can_restart = self.app.allowed(srv, "restart")
        for btn in (getattr(self, "btn_start", None),
                    getattr(self, "btn_stop", None),
                    getattr(self, "btn_restart", None)):
            if btn is not None:
                btn.configure(state="normal" if can_restart else "disabled")

    def _auto_info(self):
        """Keep platform / Minecraft version / console session current."""
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

    def refresh_status(self):
        if self.server is None:
            return
        srv = self.server
        self.state_lbl.configure(text="checking...", text_color=T.TEXT_DIM)
        if not srv.mc_dir:
            self._status_cb(None)
            return

        def work():
            return HLTH.deep_status(self.app.ssh, srv, self.app.control)

        self.app.runner.run(work, on_success=self._status_cb,
                            on_error=lambda e: self._status_err(e))

    def _status_cb(self, rep) -> None:
        if not self.winfo_exists():
            return
        if rep is None:
            self.running = False
            self.dot.set_running(False)
            self.state_lbl.configure(text="no install linked",
                                     text_color=T.AMBER)
            return
        self.running = bool(rep.running)
        self.dot.set_running(self.running)
        text, color = STATE_TEXT.get(rep.state, (rep.state, T.TEXT_DIM))
        if rep.state == "ready" and rep.slp.players_online >= 0:
            text += f" - {rep.slp.players_online}/{rep.slp.players_max} players"
        if rep.state == "unresponsive" and rep.detail:
            text += f" ({rep.detail[:40]})"
        self.state_lbl.configure(text=text, text_color=color)

    def _status_err(self, exc) -> None:
        if self.winfo_exists():
            self.state_lbl.configure(text=f"unreachable - {exc}", text_color=T.RED)

    def _tail(self):
        if self.server is None or not self.server.mc_dir:
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

    def _start(self):
        if self._guard_busy() or self.server is None:
            return
        srv = self.server
        if not self.app.allowed(srv, "restart"):
            self.app.notify("Start/stop is not allowed in restricted mode.",
                            ok=False)
            return
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

    def _stop(self):
        if self._guard_busy() or self.server is None:
            return
        srv = self.server
        if not self.app.allowed(srv, "restart"):
            self.app.notify("Start/stop is not allowed in restricted mode.",
                            ok=False)
            return
        self._busy = True
        self.state_lbl.configure(text="stopping...", text_color=T.AMBER)
        self.app.runner.run(
            lambda: (self.app.control.stop(srv, lambda m: self.app.runner.emit(
                lambda l: self.winfo_exists() and self.log.append(l), m)), None),
            on_success=lambda _: (self._set_busy(False), self.refresh_status(),
                                  self.app.notify("Server stopped")),
            on_error=lambda e: (self._set_busy(False), self.app.notify(str(e), ok=False)),
        )

    def _restart(self):
        if self._guard_busy() or self.server is None:
            return
        srv = self.server
        if not self.app.allowed(srv, "restart"):
            self.app.notify("Restart is not allowed in restricted mode.",
                            ok=False)
            return
        self._busy = True
        self.state_lbl.configure(text="restarting...", text_color=T.AMBER)

        def work():
            with self.app.locks.guard(srv.id, "restart"):
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

    # ------------------------------------------------------------- updating
    def _update_flow(self):
        if self._guard_busy() or self.server is None:
            return
        srv = self.server
        if not srv.mc_dir:
            self.app.notify("Link an install first.", ok=False)
            return
        self._busy = True
        self.state_lbl.configure(text="checking for updates...",
                                 text_color=T.AMBER)

        def work():
            return UPD.check_update(self.app.ssh, srv)

        def ok(report):
            self._set_busy(False)
            if not self.winfo_exists():
                return
            self.refresh_status()
            if report.get("nothing_to_do") or not report.get("available"):
                self.app.notify(
                    f"{srv.platform} {report.get('current_version') or '?'} "
                    "is up to date (latest: "
                    f"{report.get('latest_version') or report.get('latest_build', '?')}).")
                return
            self._offer_update(report)

        def fail(exc):
            self._set_busy(False)
            self.refresh_status()
            self.app.notify(f"Update check failed: {exc}", ok=False)

        self.app.runner.run(work, on_success=ok, on_error=fail)

    def _offer_update(self, report: dict):
        srv = self.server
        new = report.get("latest_version") or "?"
        cur = report.get("current_version") or "?"
        msg = (f"A newer build is available:\n\n"
               f"  installed : {srv.platform} {cur}\n"
               f"  latest    : {srv.platform} {new}\n\n"
               "The update will:\n"
               "  1. create a pre-update backup automatically\n"
               "  2. download + verify the checksum (sha256)\n"
               "  3. stop the server and swap the jar\n"
               "  4. start and HEALTH-CHECK the new version\n"
               "  5. roll back automatically if it fails to boot\n\n"
               "Continue?")
        if not messagebox.askyesno("Update available", msg):
            return
        self._run_update()

    def _run_update(self):
        srv = self.server
        self._busy = True
        self.upd_bar.pack(fill="x", padx=32, pady=(2, 0))
        self.upd_label.pack(fill="x", padx=32)

        def log(line):
            self.app.runner.emit(
                lambda l=line: (self.winfo_exists() and self.log.append(l)))

        def progress(frac, msg):
            self.app.runner.emit(lambda: (
                self.winfo_exists() and
                (self.upd_bar.set(max(0.0, min(1.0, frac))),
                 self.upd_label.configure(text=msg or ""))))

        def notify(kind, s, message, ok=True):
            self.app.notify_event(kind, s, message, ok)

        def work():
            with self.app.locks.guard(srv.id, "update"):
                return UPD.perform_update(self.app.ssh, srv,
                                          self.app.control, log=log,
                                          progress=progress, notify=notify)

        def ok(result):
            self._set_busy(False)
            if not self.winfo_exists():
                return
            self.upd_bar.set(0)
            self.upd_bar.pack_forget()
            self.upd_label.pack_forget()
            self.app.store.update(srv)
            self.refresh_status()
            if result.get("rolled_back"):
                self.app.notify("Update failed health check - rolled back "
                                "to the previous build.", ok=False)
            elif result.get("nothing_to_do"):
                self.app.notify("Already up to date.")
            else:
                self.app.notify(f"Updated to {srv.mc_version} and healthy.")

        def fail(exc):
            self._set_busy(False)
            if self.winfo_exists():
                self.upd_bar.pack_forget()
                self.upd_label.pack_forget()
            self.refresh_status()
            self.app.notify(f"Update failed: {exc}", ok=False)

        self.app.runner.run(work, on_success=ok, on_error=fail)
