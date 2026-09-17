"""Dialog that scans the remote machine for existing Minecraft
installations and lets the user link one to this server entry (adopt).

v2.0 (issue 20): the default scan is FAST - running java processes plus
the directories where Minecraft installs actually live (home, /opt,
/srv, /data, mounts) with a depth cap.  A full `find /` deep scan is
available as an explicit second button for installs in unusual places,
because it can take minutes on big filesystems.
"""
from __future__ import annotations

import customtkinter as ctk

from .. import detect
from .. import theme as T
from ..models import Server
from . import wallpaper
from .widgets import AccentButton, Card, GhostButton

class _Detector(ctk.CTkToplevel):
    def __init__(self, app, server: Server, on_done):
        super().__init__(app)
        self.app = app
        self.server = server
        self.on_done = on_done
        self._dots = 0
        self._deep = False

        self.title("Find existing Minecraft servers")
        self.geometry("680x580")
        self.configure(fg_color=T.BG)
        self.transient(app)
        self.grab_set()
        wallpaper.attach(self)

        ctk.CTkLabel(self, text=f"Scanning {server.host}...",
                     font=T.font(17, "bold"), text_color=T.TEXT).pack(pady=(22, 2))
        ctk.CTkLabel(
            self,
            text="FAST scan (default): running java processes + common install roots\n"
                 "(/home, /opt, /srv, /data, mounts) - takes seconds.\n"
                 "DEEP scan: the ENTIRE filesystem, every folder, no fixed path - slower.",
            font=T.font(11), text_color=T.TEXT_DIM,
            justify="center").pack(pady=(0, 8))
        self.status = ctk.CTkLabel(self, text="searching",
                                   font=T.font(12), text_color=T.AMBER)
        self.status.pack(pady=4)
        self._scanning = True
        self._dots = 0
        self._animate()

        self.results = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        self.results.pack(fill="both", expand=True, padx=22, pady=(8, 6))
        wallpaper.attach(self.results)
        ctk.CTkLabel(self.results, text="Scanning - found servers will appear here...",
                     font=T.font(12), text_color=T.TEXT_DIM,
                     anchor="w").pack(fill="x", padx=14, pady=10)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", pady=(0, 14))
        GhostButton(btns, text="Deep full-disk scan (slow)", width=200,
                    command=self._deep_scan).pack(side="left", padx=(22, 6))
        GhostButton(btns, text="Close", width=110,
                    command=self.destroy).pack(side="right", padx=(6, 22))

        self.app.runner.run(
            lambda: detect.run_detection(self.app.ssh, self.server, deep=False),
            on_success=self._render,
            on_error=lambda e: self._finish_scan() or self.status.configure(
                text=f"Detection failed: {e}", text_color=T.RED),
        )

    def _deep_scan(self):
        if self._scanning:
            return
        self._deep = True
        self._scanning = True
        self._dots = 0
        self._animate()
        self.status.configure(text="starting deep scan...", text_color=T.AMBER)
        self.app.runner.run(
            lambda: detect.run_detection(self.app.ssh, self.server, deep=True),
            on_success=self._render,
            on_error=lambda e: self._finish_scan() or self.status.configure(
                text=f"Deep scan failed: {e}", text_color=T.RED),
        )

    # ------------------------------------------------------------- animation
    def _animate(self) -> None:
        if not self.winfo_exists() or not self._scanning:
            return
        self._dots = (self._dots + 1) % 4
        what = "deep-scanning the whole disk" if self._deep else "fast-scanning common roots"
        self.status.configure(text=what + "." * self._dots)
        self.after(450, self._animate)

    def _finish_scan(self) -> None:
        self._scanning = False

    # ------------------------------------------------------------- rendering
    def _render(self, candidates) -> None:
        if not self.winfo_exists():
            return
        self._scanning = False
        self._deep = False
        wallpaper.clear(self.results)
        if not candidates:
            card = Card(self.results)
            card.pack(fill="x", pady=6, padx=6)
            ctk.CTkLabel(card, text="No Minecraft installation found.",
                         font=T.font(14, "bold"), text_color=T.TEXT).pack(
                anchor="w", padx=16, pady=(14, 2))
            ctk.CTkLabel(card, text="Nothing in the common places. Try the DEEP "
                                    "full-disk scan, or use the Setup Wizard "
                                    "to install one from scratch.",
                         font=T.font(12), text_color=T.TEXT_DIM,
                         anchor="w", wraplength=560, justify="left").pack(
                anchor="w", padx=16, pady=(0, 14))
            self.status.configure(text="Scan finished - nothing found",
                                  text_color=T.TEXT_DIM)
            return
        running = sum(1 for c in candidates if c.get("running"))
        extra = f"  ·  {running} running" if running else ""
        self.status.configure(text=f"Found {len(candidates)} installation(s){extra}",
                              text_color=T.ACCENT_SOFT)
        for c in candidates:
            self._row(c)

    def _row(self, c: dict) -> None:
        card = Card(self.results)
        card.pack(fill="x", pady=6, padx=6)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=10)

        left = ctk.CTkFrame(inner, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        title = (f"{T.platform_label(c['platform'])}"
                 + (f"  {c['version']}" if c["version"] else ""))
        ctk.CTkLabel(left, text=title, font=T.font(14, "bold"),
                     text_color=T.TEXT, anchor="w").pack(anchor="w")

        if c.get("running"):
            badge_txt = f"● RUNNING  (pid {c.get('pid', '?')})"
            badge_col = T.ACCENT_SOFT
            if c.get("screen_name"):
                badge_txt += f"  ·  screen: {c['screen_name']}"
            elif c.get("rcon") and c.get("rcon_port"):
                badge_txt += f"  ·  RCON port {c['rcon_port']}"
        else:
            badge_txt = "○ stopped"
            badge_col = T.TEXT_DIM
        ctk.CTkLabel(left, text=badge_txt, font=T.font(11, "bold"),
                     text_color=badge_col, anchor="w").pack(anchor="w")

        flags = [f"port {c.get('port', '25565')}",
                 "eula OK" if c.get("eula_ok") else "eula missing",
                 "start.sh" if c.get("has_start")
                 else ("run.sh" if c.get("has_run") else "no start script")]
        if c.get("rcon") and c.get("rcon_port") and not c.get("running"):
            flags.append("RCON")
        ctk.CTkLabel(left, text="   |   ".join(flags), font=T.font(10),
                     text_color=T.TEXT_DIM, anchor="w").pack(anchor="w")
        ctk.CTkLabel(left, text=c["dir"], font=T.mono(10),
                     text_color=T.INFO, anchor="w").pack(anchor="w")

        AccentButton(inner, text="Use this server", width=130,
                     command=lambda: self._adopt(c)).pack(side="right")

    # ------------------------------------------------------------------ adopt
    def _adopt(self, c: dict) -> None:
        self.status.configure(text="linking installation...", text_color=T.AMBER)

        def success(notes: str):
            if not self.winfo_exists():
                return
            self.app.store.update(self.server)
            self.app.notify(f"Linked: {c['dir']}")
            self.destroy()
            self.on_done(self.server)

        self.app.runner.run(
            lambda: detect.adopt(self.app.ssh, self.server, c, self.server.ram_mb),
            on_success=success,
            on_error=lambda e: self.status.configure(
                text=f"Failed: {e}", text_color=T.RED),
        )


def open_detector(app, server: Server, on_done=lambda s: None):
    return _Detector(app, server, on_done)
