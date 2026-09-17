"""Live Minecraft console (v2.0).

- OUTPUT: a push-based `tail -F logs/latest.log` stream over SSH -
  lines appear the moment the server writes them (no 2.5 s polling),
  the stream follows log rotation and reconnects automatically after
  SSH drops or server restarts.  If streaming cannot start, the page
  falls back to the old polling automatically.
- INPUT: commands go through screen / tmux / RCON.  When RCON answers,
  the server's RESPONSE is echoed right below the command (a real
  console, issue 10) - `list` shows the players, `tps` shows TPS...
"""
from __future__ import annotations

import customtkinter as ctk
from tkinter import messagebox

from .. import theme as T
from ..control import ConsoleStream
from ..models import Server
from . import wallpaper
from .widgets import Card, GhostButton, LogBox

QUICK = ["list", "tps", "save-all", "whitelist list", "time set day",
         "weather clear", "difficulty peaceful"]


class ConsolePage(ctk.CTkFrame):
    def __init__(self, master, app, server: Server | None):
        super().__init__(master, fg_color=T.BG, corner_radius=0)
        self.app = app
        self.server = server
        self._polling = False
        self._stream: ConsoleStream | None = None
        self._fallback_poll = False

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=28, pady=(24, 6))
        ctk.CTkLabel(head, text="Console", font=T.font(19, "bold"),
                     text_color=T.TEXT).pack(side="left")
        self.state = ctk.CTkLabel(head, text="", font=T.font(11),
                                  text_color=T.TEXT_DIM)
        self.state.pack(side="left", padx=12)
        GhostButton(head, text="Clear", width=76, height=28,
                    command=self._clear).pack(side="right", padx=(6, 0))

        card = Card(self)
        card.pack(fill="both", expand=True, padx=28, pady=(0, 10))
        self.log = LogBox(card)
        self.log.pack(fill="both", expand=True, padx=12, pady=12)

        quick = ctk.CTkFrame(self, fg_color="transparent")
        quick.pack(fill="x", padx=28)
        for cmd in QUICK:
            qbtn = ctk.CTkButton(quick, text=cmd, height=28,
                          corner_radius=T.BUTTON_RADIUS,
                          font=T.mono(11), fg_color=T.SURFACE2,
                          hover_color="#242944", text_color=T.TEXT_DIM,
                          command=lambda c=cmd: self._send(c))
            qbtn.pack(side="left", padx=4)
            wallpaper.blend_corners(qbtn)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=28, pady=10)
        self.entry = ctk.CTkEntry(bottom, placeholder_text="Type a server command and press Enter...",
                                  height=40, font=T.mono(12),
                                  corner_radius=T.BUTTON_RADIUS,
                                  fg_color=T.SURFACE, border_color=T.BORDER)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self._send())
        send_btn = ctk.CTkButton(bottom, text="Send", width=110, height=40,
                      corner_radius=T.BUTTON_RADIUS,
                      fg_color=T.ACCENT, hover_color=T.ACCENT_DARK,
                      text_color=T.ON_ACCENT, font=T.font(13, "bold"),
                      command=self._send)
        send_btn.pack(side="left", padx=(8, 0))
        wallpaper.blend_corners(send_btn)

    # ------------------------------------------------------------- streaming
    def on_show(self) -> None:
        if self.server is None:
            return
        if not self.app.allowed(self.server, "console"):
            self.state.configure(text="console not allowed in restricted mode",
                                 text_color=T.RED)
            self.entry.configure(state="disabled")
            return
        self.entry.configure(state="normal")
        self._start_stream()

    def on_hide(self) -> None:
        self._stop_stream()

    def _stop_stream(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream = None
        self._polling = False

    def _start_stream(self) -> None:
        self._stop_stream()
        srv = self.server
        if srv is None:
            return
        self.state.configure(text="opening live stream...", text_color=T.TEXT_DIM)
        try:
            self._stream = ConsoleStream(
                self.app.ssh, srv,
                on_line=lambda line: self.app.runner.emit(
                    lambda l=line: (self.winfo_exists() and
                                    self.log.append_line_colored(l))),
                on_status=lambda st: self.app.runner.emit(
                    lambda s=st: self._stream_status(s)))
            self._stream.start(backlog=150)
        except Exception as exc:  # noqa: BLE001 - stream is an optimization
            self._fallback_poll = True
            self.state.configure(text=f"stream unavailable ({str(exc)[:40]}) "
                                      "- using polling", text_color=T.AMBER)
        if self._fallback_poll:
            self._polling = True
            self.state.configure(text="syncing log...", text_color=T.TEXT_DIM)
            self.app.runner.run(
                lambda: (self.app.control.console_init_offset(srv),
                         self.app.control.console_poll(srv))[1],
                on_success=self._first_batch, on_error=self._err)
            self.after(2500, self._poll)

    def _stream_status(self, st: str) -> None:
        if not self.winfo_exists():
            return
        if st == "live":
            self.state.configure(text="LIVE - streaming as it happens",
                                 text_color=T.ACCENT_SOFT)
        elif st == "reconnecting":
            self.state.configure(text="reconnecting...", text_color=T.AMBER)

    # -- polling fallback ------------------------------------------------------
    def _poll(self) -> None:
        if not self._polling or not self.winfo_exists() or self.server is None:
            return
        srv = self.server
        self.app.runner.run(
            lambda: self.app.control.console_poll(srv),
            on_success=self._batch, on_error=self._err)
        self.after(2500, self._poll)

    def _first_batch(self, lines) -> None:
        if self.winfo_exists() and isinstance(lines, list):
            for ln in lines:
                self.log.append_line_colored(ln)
            self.state.configure(text="live (polling)", text_color=T.ACCENT_SOFT)

    def _batch(self, lines) -> None:
        if self.winfo_exists() and isinstance(lines, list):
            for ln in lines:
                self.log.append_line_colored(ln)

    def _err(self, exc) -> None:
        if self.winfo_exists():
            self.state.configure(text=str(exc)[:80], text_color=T.RED)

    def _clear(self) -> None:
        self.log.show_placeholder()

    # ------------------------------------------------------------- sending
    def _send(self, preset: str | None = None) -> None:
        if self.server is None:
            return
        if not self.app.allowed(self.server, "console"):
            self.app.notify("Console commands are not allowed in "
                            "restricted mode.", ok=False)
            return
        command = preset if preset is not None else self.entry.get().strip()
        if preset is None:
            self.entry.delete(0, "end")
        if not command:
            return
        srv = self.server
        self.state.configure(text="sending...", text_color=T.TEXT_DIM)

        def work():
            return self.app.control.send_command(srv, command)

        def ok(result):
            if not self.winfo_exists():
                return
            # v2: send_command returns (channel, rcon_answer)
            if isinstance(result, tuple):
                channel, answer = result
            else:
                channel, answer = result, ""
            self.log.append(f"> {command}")
            if answer:
                for ln in str(answer).splitlines():
                    if ln.strip():
                        self.log.append_line_colored("  " + ln.strip())
            self.state.configure(text=f"sent via {channel}",
                                 text_color=T.ACCENT_SOFT)
            if srv.external_screen:
                self.app.store.update(srv)  # persist a discovered session

        def fail(exc):
            if not self.winfo_exists():
                return
            self.log.append("[!] " + str(exc))
            self.state.configure(text="send failed", text_color=T.RED)
            self._offer_fix(str(exc))

        self.app.runner.run(work, on_success=ok, on_error=fail)

    # ------------------------------------------------------------- auto fix
    def _offer_fix(self, msg: str) -> None:
        """When no console channel exists, offer to enable RCON
        automatically (the app edits server.properties itself)."""
        srv = self.server
        if srv is None or not srv.mc_dir or "No console channel" not in msg:
            self.app.notify(msg[:160], ok=False)
            return
        if not messagebox.askyesno(
                "No console channel",
                "MC Manager could not find a screen/tmux session to type "
                "the command into, and RCON is disabled.\n\n"
                "Fix it automatically?\n\n"
                "- RCON is enabled in server.properties with a generated "
                "password (nothing to do by hand)\n"
                "- the server restarts if it is running\n"
                "- after the restart, commands work through RCON"):
            return
        self.state.configure(text="enabling RCON + restarting...",
                             text_color=T.AMBER)

        def work():
            self.app.control.enable_rcon(srv)
            note = "RCON enabled - it will be active on the next start."
            if self.app.control.status(srv):
                self.app.control.restart(srv)
                note = ("RCON enabled - server is restarting. Give it ~30s "
                        "to boot, then send your command again.")
            return note

        def ok(note):
            if not self.winfo_exists():
                return
            self.app.store.update(srv)
            self.state.configure(text="RCON fixed", text_color=T.ACCENT_SOFT)
            self.app.notify(note)

        def fail2(exc):
            if self.winfo_exists():
                self.state.configure(text="fix failed", text_color=T.RED)
            self.app.notify(str(exc)[:160], ok=False)

        self.app.runner.run(work, on_success=ok, on_error=fail2)
