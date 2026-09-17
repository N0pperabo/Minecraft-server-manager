"""Live system monitor: CPU / RAM / disk gauges + Minecraft process info."""
from __future__ import annotations

import customtkinter as ctk
from tkinter import Canvas

from .. import theme as T
from ..models import Server
from .widgets import Card, GhostButton


def fmt_gb(n: int) -> str:
    return f"{n / (1024 ** 3):.1f} GB"


class Gauge(Canvas):
    """Round progress gauge drawn on a raw tkinter canvas.

    Lives inside a Card (fg T.SURFACE); the canvas bg matches it so the
    gauge blends seamlessly with the card surface."""

    def __init__(self, master, title: str, size: int = 150):
        super().__init__(master, width=size, height=size + 30,
                         bg=T.SURFACE, highlightthickness=0)
        self.size = size
        self.title = title
        self.set(0.0, "")

    def set(self, fraction: float, text: str) -> None:
        self.delete("all")
        s = self.size
        pad = 14
        w = 10
        self.create_arc(pad, pad, s - pad, s - pad, start=90, extent=359,
                        style="arc", outline=T.BORDER, width=w)
        color = T.ACCENT if fraction < 0.65 else (T.AMBER if fraction < 0.85 else T.RED)
        self.create_arc(pad, pad, s - pad, s - pad, start=90,
                        extent=-max(0.5, min(99.5, fraction * 100)) * 3.596,
                        style="arc", outline=color, width=w)
        self.create_text(s / 2, s / 2 - 4, text=text,
                         fill=T.TEXT, font=(T.MONO_FAMILY, 15, "bold"))
        self.create_text(s / 2, s + 4, text=self.title,
                         fill=T.TEXT_DIM, font=(T.FONT_FAMILY, 11))


class MonitorPage(ctk.CTkFrame):
    def __init__(self, master, app, server: Server | None):
        super().__init__(master, fg_color=T.BG, corner_radius=0)
        self.app = app
        self.server = server
        self._auto = True

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=28, pady=(24, 6))
        ctk.CTkLabel(head, text="Monitor", font=T.font(19, "bold"),
                     text_color=T.TEXT).pack(side="left")
        self.state = ctk.CTkLabel(head, text="", font=T.font(11),
                                  text_color=T.TEXT_DIM)
        self.state.pack(side="left", padx=12)
        GhostButton(head, text="Refresh now", width=110, height=28,
                    command=self._refresh).pack(side="right")

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=28, pady=(6, 0))
        for title, setter_name in (("CPU", "g_cpu"), ("RAM", "g_ram"),
                                   ("DISK", "g_disk")):
            card = Card(row)
            card.pack(side="left", padx=(0, 16), fill="both", expand=True)
            gauge = Gauge(card, title)
            gauge.pack(padx=8, pady=6)
            setattr(self, setter_name, gauge)

        info = Card(self)
        info.pack(fill="both", expand=True, padx=28, pady=(16, 20))
        self.info = ctk.CTkLabel(info, text="waiting for data...", font=T.mono(12),
                                 text_color=T.TEXT, anchor="nw", justify="left")
        self.info.pack(fill="both", expand=True, padx=18, pady=16)

    # ------------------------------------------------------------------ poll
    def on_show(self) -> None:
        self._auto = True
        self._refresh()

    def on_hide(self) -> None:
        self._auto = False

    def _refresh(self) -> None:
        if self.server is None:
            return
        srv = self.server
        self.state.configure(text="sampling...", text_color=T.TEXT_DIM)
        self.app.runner.run(
            lambda: self.app.control.monitor(srv),
            on_success=self._render,
            on_error=lambda e: self.state.configure(text=str(e)[:80], text_color=T.RED))
        if self._auto:
            self.after(3000, self._auto_tick)

    def _auto_tick(self) -> None:
        if self._auto and self.winfo_exists():
            self._refresh()

    def _render(self, st: dict) -> None:
        if not self.winfo_exists():
            return
        ram_frac = st["ram_used"] / max(1, st["ram_total"])
        disk_frac = st["disk_used"] / max(1, st["disk_total"])
        self.g_cpu.set(st["cpu"] / 100.0, f"{st['cpu']:.0f}%")
        self.g_ram.set(ram_frac, f"{ram_frac * 100:.0f}%")
        self.g_disk.set(disk_frac, f"{disk_frac * 100:.0f}%")

        mc = ("Minecraft process: not detected" if not st["mc_rss_kb"] else
              f"Minecraft process: running  |  uptime {st['mc_uptime']}  |  "
              f"RSS {fmt_gb(st['mc_rss_kb'] * 1024)}")
        lines = [
            f"RAM     {fmt_gb(st['ram_used'])} / {fmt_gb(st['ram_total'])}",
            f"Disk    {fmt_gb(st['disk_used'])} / {fmt_gb(st['disk_total'])}",
            f"Load    {st['load']:.2f}",
            "",
            mc,
        ]
        self.info.configure(text="\n".join(lines))
        self.state.configure(text="live - refreshing every 3s", text_color=T.ACCENT_SOFT)
