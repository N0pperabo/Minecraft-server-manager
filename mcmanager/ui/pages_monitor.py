"""Monitor page (v2.0): system gauges + real Minecraft metrics.

The v1 page showed CPU/RAM/disk only.  Now a second card shows the
numbers server admins actually need (issue 11): TPS (1/5/15m), MSPT,
players online, SLP latency, Java heap used/committed, GC algorithm,
entity count (Paper family), process uptime/RSS - collected through
RCON, the Server List Ping protocol and jcmd (see metrics.py).
"""
from __future__ import annotations

import customtkinter as ctk
from tkinter import Canvas

from .. import metrics as MX
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

        # -- Minecraft metrics card (issue 11) ---------------------------------
        mc_card = Card(self)
        mc_card.pack(fill="both", expand=True, padx=28, pady=(16, 4))
        ctk.CTkLabel(mc_card, text="Minecraft server", font=T.font(12, "bold"),
                     text_color=T.TEXT).pack(anchor="w", padx=16, pady=(12, 2))
        self.mc_info = ctk.CTkLabel(mc_card, text="collecting...",
                                    font=T.mono(12), text_color=T.TEXT,
                                    anchor="nw", justify="left")
        self.mc_info.pack(fill="both", expand=True, padx=16, pady=(2, 6))
        hint = ("TPS / MSPT need RCON (console page offers to enable it) - "
                "heap/GC need jcmd from the server's Java runtime.")
        ctk.CTkLabel(mc_card, text=hint, font=T.font(9),
                     text_color=T.TEXT_DIM, anchor="w", wraplength=640,
                     justify="left").pack(anchor="w", padx=16, pady=(0, 10))

        info = Card(self)
        info.pack(fill="both", expand=True, padx=28, pady=(4, 20))
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

        def work():
            st = self.app.control.monitor(srv)
            mc = MX.collect(self.app.ssh, srv, self.app.control) \
                if srv.mc_dir else {}
            return st, mc

        self.app.runner.run(
            work,
            on_success=lambda r: self._render(r[0], r[1]),
            on_error=lambda e: self.state.configure(text=str(e)[:80],
                                                    text_color=T.RED))
        if self._auto:
            self.after(5000, self._auto_tick)

    def _auto_tick(self) -> None:
        if self._auto and self.winfo_exists():
            self._refresh()

    # ---------------------------------------------------------------- render
    def _render(self, st: dict, mc: dict) -> None:
        if not self.winfo_exists():
            return
        ram_frac = st["ram_used"] / max(1, st["ram_total"])
        disk_frac = st["disk_used"] / max(1, st["disk_total"])
        self.g_cpu.set(st["cpu"] / 100.0, f"{st['cpu']:.0f}%")
        self.g_ram.set(ram_frac, f"{ram_frac * 100:.0f}%")
        self.g_disk.set(disk_frac, f"{disk_frac * 100:.0f}%")

        mc = mc or {}
        tps = mc.get("tps") or (None, None, None)
        mspt = mc.get("mspt") or (None, None, None)
        players = mc.get("players") or (None, None)
        heap_used = mc.get("heap_used")
        heap_committed = mc.get("heap_committed")
        heap_max = mc.get("heap_max_mb")
        latency = mc.get("latency_ms")
        state = mc.get("state", "n/a")

        def f3(v, unit=""):
            return "n/a" if v is None else f"{v:.1f}{unit}"

        lines = [
            f"state       {state}   ·   latency {f3(latency, ' ms')}   ·   "
            f"version {mc.get('version') or 'n/a'}",
            "players     "
            + ("n/a" if players[0] is None else f"{players[0]} / {players[1]}"),
            "TPS (1/5/15m)   "
            + ("n/a" if tps[0] is None else
               f"{tps[0]:.2f} / {tps[1]:.2f} / {tps[2]:.2f}"),
            "MSPT (1/5/15m)  "
            + ("n/a" if mspt[0] is None else
               f"{mspt[0]:.1f} / {mspt[1]:.1f} / {mspt[2]:.1f} ms"),
            "Java heap   "
            + ("n/a" if not heap_used else
               f"{heap_used / (1 << 20):.0f} MB used / "
               f"{(heap_committed or 0) / (1 << 20):.0f} MB committed"
               + (f" / {heap_max} MB -Xmx" if heap_max else "")),
            f"GC          {mc.get('gc') or 'n/a'}   ·   "
            f"entities {mc.get('entities') or 'n/a'}",
        ]
        if mc.get("uptime") or mc.get("rss_mb"):
            lines.append(f"process     uptime {mc.get('uptime') or 'n/a'}   ·   "
                         f"RSS {mc.get('rss_mb') or 'n/a'} MB")
        if mc.get("motd"):
            lines.append(f"MOTD        {mc['motd'][:70]}")
        self.mc_info.configure(text="\n".join(lines))

        syslines = [
            f"RAM     {fmt_gb(st['ram_used'])} / {fmt_gb(st['ram_total'])}",
            f"Disk    {fmt_gb(st['disk_used'])} / {fmt_gb(st['disk_total'])}",
            f"Load    {st['load']:.2f}",
            "",
            "Minecraft process: not detected" if not st["mc_rss_kb"] else
            f"Minecraft process: running  |  uptime {st['mc_uptime']}  |  "
            f"RSS {fmt_gb(st['mc_rss_kb'] * 1024)}",
        ]
        self.info.configure(text="\n".join(syslines))
        self.state.configure(text="live - refreshing every 5s",
                             text_color=T.ACCENT_SOFT)
