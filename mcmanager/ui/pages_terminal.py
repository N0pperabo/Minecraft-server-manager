"""Full SSH terminal over a persistent paramiko shell channel."""
from __future__ import annotations

import customtkinter as ctk

from .. import theme as T
from ..models import Server
from . import wallpaper
from .widgets import GhostButton, LogBox


class TerminalPage(ctk.CTkFrame):
    def __init__(self, master, app, server: Server | None):
        super().__init__(master, fg_color=T.BG, corner_radius=0)
        self.app = app
        self.server = server
        self.shell = None

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=28, pady=(24, 6))
        ctk.CTkLabel(head, text="Terminal", font=T.font(19, "bold"),
                     text_color=T.TEXT).pack(side="left")
        self.state = ctk.CTkLabel(head, text="connecting...", font=T.font(11),
                                  text_color=T.AMBER)
        self.state.pack(side="left", padx=12)
        GhostButton(head, text="Clear", width=76, height=28,
                    command=self._clear).pack(side="right")

        card = ctk.CTkFrame(self, fg_color="transparent")
        card.pack(fill="both", expand=True, padx=28, pady=(0, 10))
        self.log = LogBox(card)
        self.log.pack(fill="both", expand=True)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=28, pady=(0, 16))
        self.entry = ctk.CTkEntry(bottom, placeholder_text="shell command - press Enter to run",
                                  height=40, font=T.mono(12),
                                  corner_radius=T.BUTTON_RADIUS,
                                  fg_color=T.SURFACE, border_color=T.BORDER)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", self._on_enter)
        send_btn = ctk.CTkButton(bottom, text="Send", width=110, height=40,
                      corner_radius=T.BUTTON_RADIUS,
                      fg_color=T.ACCENT, hover_color=T.ACCENT_DARK,
                      text_color=T.ON_ACCENT, font=T.font(13, "bold"),
                      command=self._on_enter)
        send_btn.pack(side="left", padx=(8, 0))
        wallpaper.blend_corners(send_btn)

    # ------------------------------------------------------------- shell io
    def on_show(self) -> None:
        if self.server is None or self.shell is not None:
            return
        srv = self.server
        self.app.runner.run(
            lambda: self.app.ssh.shell(srv, self._push),
            on_success=self._opened,
            on_error=lambda e: self.state.configure(text=str(e)[:80], text_color=T.RED),
        )

    def on_hide(self) -> None:
        self._close()

    def _opened(self, shell) -> None:
        self.shell = shell
        if self.winfo_exists():
            self.state.configure(text="connected", text_color=T.ACCENT_SOFT)
            self.entry.focus_set()

    def _push(self, line: str) -> None:
        # called from the shell reader thread -> route through queue
        self.app.runner.emit(
            lambda l: self.winfo_exists() and self.log.append_line_colored(l), line)

    def _on_enter(self, event=None) -> None:
        cmd = self.entry.get().strip()
        self.entry.delete(0, "end")
        if not cmd or self.shell is None:
            return
        self.log.append(f"$ {cmd}")
        self.shell.send(cmd)

    def _clear(self) -> None:
        self.log.show_placeholder()

    # ---------------------------------------------------------------- close
    def _close(self) -> None:
        if self.shell is not None:
            try:
                self.shell.close()
            except Exception:  # noqa: BLE001
                pass
            self.shell = None
