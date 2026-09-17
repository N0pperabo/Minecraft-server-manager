"""Dialog that scans the WHOLE filesystem (plus running java processes) of
the remote machine for existing Minecraft installations and lets the user
link one to this server entry (adopt)."""
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

        self.title("Find existing Minecraft servers")
        self.geometry("680x560")
        self.configure(fg_color=T.BG)
        self.transient(app)
        self.grab_set()
        wallpaper.attach(self)

        ctk.CTkLabel(self, text=f"Scanning {server.host}...",
                     font=T.font(17, "bold"), text_color=T.TEXT).pack(pady=(22, 2))
        ctk.CTkLabel(
            self,
            text="Checks the ENTIRE filesystem - every folder, no fixed path - for Paper /\n"
                 "Purpur / Folia / Spigot / Bukkit / Fabric / Quilt / Forge / NeoForge /\n"
                 "Vanilla / Mohist / proxies..., plus every running java process.",
            font=T.font(11), text_color=T.TEXT_DIM,
            justify="center").pack(pady=(0, 10))
        self.status = ctk.CTkLabel(self, text="searching",
                                   font=T.font(12), text_color=T.AMBER)
        self.status.pack(pady=4)
        self._scanning = True
        self._dots = 0
        self._animate()

        self.results = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        self.results.pack(fill="both", expand=True, padx=22, pady=(8, 12))
        wallpaper.attach(self.results)
        ctk.CTkLabel(self.results, text="Scanning - found servers will appear here...",
                     font=T.font(12), text_color=T.TEXT_DIM,
                     anchor="w").pack(fill="x", padx=14, pady=10)

        GhostButton(self, text="Close", width=110,
                    command=self.destroy).pack(pady=(0, 16))

        self.app.runner.run(
            lambda: detect.run_detection(self.app.ssh, self.server),
            on_success=self._render,
            on_error=lambda e: self._finish_scan() or self.status.configure(
                text=f"Detection failed: {e}", text_color=T.RED),
        )

    # ------------------------------------------------------------- animation
    def _animate(self) -> None:
        if not self.winfo_exists() or not self._scanning:
            return
        self._dots = (self._dots + 1) % 4
        self.status.configure(text="searching the whole disk" + "." * self._dots)
        self.after(450, self._animate)

    def _finish_scan(self) -> None:
        self._scanning = False

    # ------------------------------------------------------------- rendering
    def _render(self, candidates) -> None:
        if not self.winfo_exists():
            return
        self._scanning = False
        wallpaper.clear(self.results)
        if not candidates:
            card = Card(self.results)
            card.pack(fill="x", pady=6, padx=6)
            ctk.CTkLabel(card, text="No Minecraft installation found anywhere on the disk.",
                         font=T.font(14, "bold"), text_color=T.TEXT).pack(
                anchor="w", padx=16, pady=(14, 2))
            ctk.CTkLabel(card, text="Use the Setup Wizard to install one from scratch.",
                         font=T.font(12), text_color=T.TEXT_DIM).pack(
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
