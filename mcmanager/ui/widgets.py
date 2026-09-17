"""Reusable styled widgets: cards, pulsing status dot, buttons, fields, log box."""
from __future__ import annotations

import customtkinter as ctk

from .. import theme as T
from . import wallpaper


class Card(ctk.CTkFrame):
    def __init__(self, master, **kw):
        # flat bg only as an early-start fallback; blend_corners() repaints
        # it with the sampled wallpaper color once the layout is known, so
        # the little squares outside the rounded corners stay invisible.
        # NOTE: styling defaults go through kw.setdefault() - passing them
        # straight to super() would make a caller that also passes e.g.
        # corner_radius=... raise "got multiple values" and kill the whole
        # render loop (that bug emptied the Files list in v1.4.1).
        kw.setdefault("bg_color", T.CORNER_BG)
        kw.setdefault("fg_color", T.SURFACE)
        kw.setdefault("corner_radius", T.CARD_RADIUS)
        kw.setdefault("border_width", 1)
        kw.setdefault("border_color", T.BORDER)
        super().__init__(master, **kw)
        wallpaper.blend_corners(self)


class StatusDot(ctk.CTkLabel):
    """Small pulsing dot; violet=running, gray=stopped."""

    PULSE = ["#7C5CFF", "#5F43E8", "#A79BFF", "#6E55F2"]

    def __init__(self, master, running: bool = False, size: int = 12):
        self.running = running
        self._size = size
        self._idx = 0
        self._pulse_job = None
        super().__init__(master, text="●", font=T.font(size + 4, "bold"),
                         text_color="#3A405C" if not running else T.ACCENT)
        if running:
            self._pulse()

    def set_running(self, running: bool) -> None:
        self.running = running
        self.configure(text_color=T.ACCENT if running else "#3A405C")
        if running and self._pulse_job is None:
            self._pulse()

    def _pulse(self) -> None:
        if not self.winfo_exists() or not self.running:
            self._pulse_job = None
            return
        self._idx = (self._idx + 1) % len(self.PULSE)
        self.configure(text_color=self.PULSE[self._idx])
        self._pulse_job = self.after(450, self._pulse)


class AccentButton(ctk.CTkButton):
    def __init__(self, master, **kw):
        # radius kept small: large radii on short CTkButtons render stray
        # 1-2px spike pixels at the corner-arc tangent lines (measured)
        kw.setdefault("fg_color", T.ACCENT)
        kw.setdefault("hover_color", T.ACCENT_DARK)
        kw.setdefault("text_color", T.ON_ACCENT)
        kw.setdefault("corner_radius", T.BUTTON_RADIUS)
        kw.setdefault("font", T.font(13, "bold"))
        super().__init__(master, **kw)
        wallpaper.blend_corners(self)


class GhostButton(ctk.CTkButton):
    def __init__(self, master, **kw):
        kw.setdefault("fg_color", "transparent")
        kw.setdefault("hover_color", T.SURFACE2)
        kw.setdefault("text_color", T.TEXT)
        kw.setdefault("border_width", 1)
        kw.setdefault("border_color", T.BORDER)
        kw.setdefault("corner_radius", T.BUTTON_RADIUS)
        kw.setdefault("font", T.font(13))
        super().__init__(master, **kw)
        wallpaper.blend_corners(self)


class DangerButton(ctk.CTkButton):
    def __init__(self, master, **kw):
        kw.setdefault("fg_color", "transparent")
        kw.setdefault("hover_color", "#2A1626")
        kw.setdefault("text_color", T.RED)
        kw.setdefault("border_width", 1)
        kw.setdefault("border_color", "#5A2440")
        kw.setdefault("corner_radius", T.BUTTON_RADIUS)
        kw.setdefault("font", T.font(13))
        super().__init__(master, **kw)
        wallpaper.blend_corners(self)


class LabeledField(ctk.CTkFrame):
    """Caption + entry combo used by forms."""

    def __init__(self, master, label: str, value: str = "", placeholder: str = "",
                 show: str | None = None, width: int = 220, read_only: bool = False):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=label, font=T.font(12), text_color=T.TEXT_DIM,
                     anchor="w").grid(row=0, column=0, sticky="ew")
        self.var = ctk.StringVar(value=value)
        self.entry = ctk.CTkEntry(self, textvariable=self.var, show=show,
                                  placeholder_text=placeholder, width=width,
                                  height=36, font=T.font(13),
                                  corner_radius=T.BUTTON_RADIUS,
                                  fg_color=T.SURFACE2, border_color=T.BORDER)
        self.entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        if read_only:
            self.entry.configure(state="disabled")

    def get(self) -> str:
        return self.var.get().strip()


class SectionTitle(ctk.CTkLabel):
    def __init__(self, master, text: str):
        super().__init__(master, text=text, font=T.font(19, "bold"),
                         text_color=T.TEXT, anchor="w")


def tag_config(tb, name: str, **kw) -> None:
    """tag_config that works on CTkTextbox across versions."""
    try:
        tb.tag_config(name, **kw)
    except AttributeError:
        tb._textbox.tag_config(name, **kw)


class LogBox(ctk.CTkTextbox):
    """Monospace log with INFO / WARN / ERR coloring.

    Shows a dimmed placeholder line while empty so the panel does not look
    like a dead black rectangle before the first line arrives."""

    PLACEHOLDER = "waiting for output...\n"

    def __init__(self, master, height: int | None = None):
        super().__init__(master, font=T.mono(12), fg_color="#0A0C14",
                         bg_color=T.SURFACE,
                         text_color=T.TEXT, border_width=1, border_color=T.BORDER,
                         corner_radius=T.BUTTON_RADIUS, wrap="word",
                         height=height or 420)
        wallpaper.blend_corners(self)
        for name, color in (("info", T.TEXT_DIM), ("ok", T.ACCENT_SOFT),
                            ("warn", T.AMBER), ("err", T.RED),
                            ("ph", "#3A405C")):
            tag_config(self, name, foreground=color)
        self._count = 0
        self._showing_placeholder = False
        self.show_placeholder()

    # ------------------------------------------------------------- placeholder
    def show_placeholder(self, text: str | None = None) -> None:
        self.delete("1.0", "end")
        self.insert("1.0", (text or self.PLACEHOLDER), "ph")
        self._showing_placeholder = True
        self._count = 0

    def _clear_placeholder(self) -> None:
        if self._showing_placeholder:
            self.delete("1.0", "end")
            self._showing_placeholder = False

    def append(self, text: str) -> None:
        tag = "info"
        low = text.lower()
        if "error" in low or "err]" in low or "failed" in low or "exception" in low:
            tag = "err"
        elif "warn" in low:
            tag = "warn"
        elif text.startswith(("[start]", "[stop]", "[done]", "[probe]")) or "installed" in low:
            tag = "ok"
        self._clear_placeholder()
        self.insert("end", text + "\n", tag)
        self._count += 1
        if self._count > 3000:
            self.delete("1.0", "1000.0")
            self._count = 2000
        self.see("end")

    def append_line_colored(self, text: str) -> None:
        low = text.lower()
        if "warn" in low:
            tag = "warn"
        elif "error" in low or "severe" in low or "exception" in low:
            tag = "err"
        elif low.strip().startswith("*") or "listening on" in low or "done (" in low:
            tag = "ok"
        else:
            tag = "info"
        self._clear_placeholder()
        self.insert("end", text + "\n", tag)
        self._count += 1
        if self._count > 4000:
            self.delete("1.0", "1500.0")
            self._count = 2500
        self.see("end")


class Toast(ctk.CTkLabel):
    """Floating status pill anchored bottom-center of the app.

    Fully removed from the layout manager while idle (a geometry-managed
    empty CTkLabel renders as a dark rounded box floating over the page)
    and lifted above the console/terminal input bars when visible."""

    def __init__(self, master):
        super().__init__(master, text="", font=T.font(13, "bold"),
                         fg_color=T.SURFACE2, corner_radius=10,
                         text_color=T.TEXT, height=36)
        self._job = None
        self._shown = False

    def show(self, message: str, ok: bool = True) -> None:
        if not self.winfo_exists():
            return
        self.configure(text=message,
                       text_color=T.ACCENT_SOFT if ok else T.RED)
        if not self._shown:
            # sit clearly above the bottom input bar (~93%..99% of height)
            self.place(relx=0.5, rely=0.85, anchor="s")
            self._shown = True
        self.lift()
        if self._job:
            try:
                self.after_cancel(self._job)
            except Exception:  # noqa: BLE001
                pass
        self._job = self.after(4000, self._hide)

    def _hide(self) -> None:
        self._job = None
        self._shown = False
        if self.winfo_exists():
            self.place_forget()
