"""Generated wallpaper for the app background.

A deep navy -> indigo -> violet diagonal gradient with soft glows, a faint
minecraft-ish block pattern and a vignette - drawn with Pillow at runtime,
so no image files ship with the app.

`attach(master)` picks the best strategy:
- CTk frames (pages, sidebar): the image is drawn directly on the frame's
  internal canvas  -> the wallpaper covers the WHOLE page and cards float
  on top of it.
- plain tk frames (the scroll content of CTkScrollableFrame): a full-size
  CTkLabel with the image is placed behind the rows.
Both are tagged `_mcm_wallpaper` so rebuild loops can keep them (use
`clear()` instead of bare destroy loops).
"""
from __future__ import annotations

import threading

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFilter, ImageTk

_lock = threading.Lock()
_pil_cache: dict[tuple[int, int], Image.Image] = {}
_img_cache: dict[tuple[int, int], ctk.CTkImage] = {}

_TOP = (14, 16, 36)      # deep navy-indigo
_MID = (26, 31, 76)      # indigo
_BOTTOM = (50, 33, 104)  # violet


def _lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _gradient(w: int, h: int) -> Image.Image:
    small = Image.new("RGB", (max(2, w // 16), max(2, h // 16)))
    sw, sh = small.size
    px = small.load()
    for y in range(sh):
        ty = y / max(1, sh - 1)
        for x in range(sw):
            tx = x / max(1, sw - 1)
            t = (tx + ty) / 2.0
            px[x, y] = _lerp(_TOP, _MID, t * 2) if t < 0.5 \
                else _lerp(_MID, _BOTTOM, (t - 0.5) * 2)
    return small.resize((w, h), Image.BICUBIC)


def _glow(layer: Image.Image, cx: int, cy: int, r: int,
          color: tuple, alpha: int) -> None:
    ImageDraw.Draw(layer).ellipse(
        (cx - r, cy - r, cx + r, cy + r), fill=color + (alpha,))


def _generate(w: int, h: int) -> Image.Image:
    img = _gradient(w, h).convert("RGBA")

    # soft color glows
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    _glow(glow, int(w * 0.84), int(h * 0.08), int(w * 0.42), (124, 92, 255), 54)
    _glow(glow, int(w * 0.08), int(h * 0.92), int(w * 0.38), (76, 195, 255), 40)
    _glow(glow, int(w * 0.55), int(h * 0.52), int(w * 0.28), (95, 67, 232), 30)
    glow = glow.filter(ImageFilter.GaussianBlur(max(18, min(w, h) // 9)))
    img = Image.alpha_composite(img, glow)

    # faint scattered blocks (minecraft vibe, very low alpha)
    deco = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(deco)
    cell = max(30, w // 24)
    for row in range(h // cell + 2):
        for col in range(w // cell + 2):
            if (row * 7 + col * 13) % 11 != 0:  # deterministic scatter
                continue
            x, y = col * cell, row * cell
            s = max(10, cell // 2)
            if (row + col) % 3 == 0:
                d.rounded_rectangle((x, y, x + s, y + s), radius=4,
                                    outline=(167, 155, 255, 38), width=2)
            else:
                d.rounded_rectangle((x, y, x + s, y + s), radius=4,
                                    fill=(124, 92, 255, 20))
    deco = deco.filter(ImageFilter.GaussianBlur(0.6))
    img = Image.alpha_composite(img, deco)

    # vignette: darken the corners a little
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse(
        (-int(w * 0.35), -int(h * 0.35), int(w * 1.35), int(h * 1.35)), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(20, min(w, h) // 6)))
    dark = Image.new("RGBA", (w, h), (3, 4, 10, 0))
    dark.putalpha(mask.point(lambda v: int(70 * (255 - v) / 255)))
    return Image.alpha_composite(img, dark)


def _bucket(w: int, h: int) -> tuple[int, int]:
    return (max(280, (int(w) + 39) // 40 * 40),
            min(1280, max(280, (int(h) + 39) // 40 * 40)))


def pil_for(key: tuple[int, int]) -> Image.Image:
    # NOTE: never call PIL/CTk APIs while holding _lock - CTkImage creation
    # re-enters the Tk event loop (nested update_idletasks), which can call
    # back into these functions on the SAME thread -> non-reentrant deadlock.
    with _lock:
        pil = _pil_cache.get(key)
    if pil is None:
        pil = _generate(*key)
        with _lock:
            _pil_cache[key] = pil
            while len(_pil_cache) > 6:  # keep the cache bounded
                _pil_cache.pop(next(iter(_pil_cache)))
    return pil


def image_for(width: int, height: int) -> ctk.CTkImage:
    key = _bucket(width, height)
    with _lock:
        img = _img_cache.get(key)
    if img is None:
        img = ctk.CTkImage(light_image=pil_for(key), size=key)
        with _lock:
            _img_cache.setdefault(key, img)
    return img


def _refresh_loop(master, state: dict, apply) -> None:
    def refresh(_event=None) -> None:
        try:
            w, h = master.winfo_width(), master.winfo_height()
        except Exception:  # noqa: BLE001
            return
        if w < 60 or h < 60:
            return
        key = _bucket(w, h)
        if key == state["key"]:
            return
        state["key"] = key
        try:
            apply(key)
        except Exception:  # noqa: BLE001
            pass

    # add="+" is CRITICAL: a plain bind() would REPLACE the binding
    # CTkScrollableFrame installs on its content frame (the one that keeps
    # the canvas scrollregion in sync with the packed rows) - clobbering
    # it is exactly how "the list cannot be scrolled" happened in v1.4.1.
    master.bind("<Configure>", refresh, add="+")
    master.after(80, refresh)


def _find_wp_owner(master):
    """Nearest ancestor that OWNS a wallpaper image (its own bucket).
    Attached-but-inherited sub-frames are see-through, so we walk past
    them; an opaque CTk container blocks the search (nothing of the
    wallpaper shows through it)."""
    try:
        node = master.master
        while node is not None:
            if getattr(node, "_mcm_wallpaper_owner", False):
                return node
            fg = None
            try:
                fg = node.cget("fg_color")
            except Exception:  # noqa: BLE001
                fg = None
            if fg is not None and "transparent" not in str(fg):
                return None
            node = node.master
    except Exception:  # noqa: BLE001
        pass
    return None


def attach(master) -> None:
    """Put a generated wallpaper behind every widget of `master`.

    When `master` lives INSIDE an already-wallpapered frame, the parent's
    image is reused and translated so the gradient continues seamlessly
    (a small sub-frame generating its own wallpaper would show a crop of
    the gradient's top-left corner and create a visible seam)."""
    master._mcm_wallpaper_attached = True
    canvas = getattr(master, "_canvas", None)

    if canvas is not None:  # CTk frame -> draw straight on its canvas
        owner = _find_wp_owner(master)
        if owner is not None and owner is not master:
            _attach_inherited(master, canvas, owner)
            return
        master._mcm_wallpaper_owner = True
        state = {"key": None, "photo": None}

        def apply_canvas(key: tuple[int, int]) -> None:
            photo = ImageTk.PhotoImage(pil_for(key).convert("RGB"),
                                       master=canvas)
            state["photo"] = photo  # keep a reference (GC!)
            if state.get("item") is None:
                state["item"] = canvas.create_image(
                    0, 0, anchor="nw", image=photo, tags=("mcm_wallpaper",))
            else:
                canvas.itemconfigure(state["item"], image=photo)
            canvas.tag_raise("mcm_wallpaper")  # stay above CTk's fill parts

        _refresh_loop(master, state, apply_canvas)
        return

    # plain tk frame (e.g. the scroll content of a CTkScrollableFrame)
    master._mcm_wallpaper_owner = True
    _fit_scrollable_viewport(master)
    lbl = ctk.CTkLabel(master, text="", fg_color="transparent")
    lbl._mcm_wallpaper = True
    lbl.place(relx=0, rely=0, relwidth=1, relheight=1)
    lbl.lower()
    state = {"key": None}

    def apply_label(key: tuple[int, int]) -> None:
        lbl.configure(image=image_for(*key))

    _refresh_loop(master, state, apply_label)


_inherited: list = []


def _attach_inherited(master, canvas, owner) -> None:
    """Draw the OWNER's wallpaper image on master's canvas, translated by
    master's offset inside the owner, so the gradient continues through.
    Re-checked from the app tick because <Configure> on master does not
    fire when an ancestor moves master around."""
    entry = {"master": master, "canvas": canvas, "owner": owner,
             "key": None, "dx": None, "dy": None, "photo": None,
             "item": None}

    def apply(_event=None) -> None:
        try:
            w, h = master.winfo_width(), master.winfo_height()
            if w < 20 or h < 20:
                return
            key = _bucket(owner.winfo_width(), owner.winfo_height())
            dx = master.winfo_rootx() - owner.winfo_rootx()
            dy = master.winfo_rooty() - owner.winfo_rooty()
            if key == entry["key"] and (dx, dy) == (entry["dx"], entry["dy"]) \
                    and entry["item"] is not None:
                return
            entry["key"], entry["dx"], entry["dy"] = key, dx, dy
            photo = ImageTk.PhotoImage(pil_for(key).convert("RGB"),
                                       master=canvas)
            entry["photo"] = photo
            if entry["item"] is None:
                entry["item"] = canvas.create_image(
                    -dx, -dy, anchor="nw", image=photo,
                    tags=("mcm_wallpaper",))
            else:
                canvas.coords(entry["item"], -dx, -dy)
                canvas.itemconfigure(entry["item"], image=photo)
            canvas.tag_raise("mcm_wallpaper")
        except Exception:  # noqa: BLE001
            pass

    entry["apply"] = apply
    _inherited.append(entry)
    master.bind("<Configure>", apply, add="+")
    master.after(80, apply)


def _fit_scrollable_viewport(master) -> None:
    """Make the scroll-content frame at least as tall as the visible
    canvas, so the wallpaper covers the whole viewport even when the list
    has only a few rows."""
    try:
        canvas = master.master  # the tk.Canvas of the CTkScrollableFrame
        wins = [i for i in canvas.find_withtag("all")
                if canvas.type(i) == "window"]
        if not wins:
            return
        win = wins[0]

        def fit(_event=None) -> None:
            try:
                vh = canvas.winfo_height()
                if vh > 1:
                    canvas.itemconfigure(win, height=max(vh, master.winfo_reqheight()))
            except Exception:  # noqa: BLE001
                pass

        canvas.bind("<Configure>", fit, add="+")
        master.bind("<Configure>", fit, add="+")
        master.after(60, fit)
    except Exception:  # noqa: BLE001
        pass


def clear(frame) -> None:
    """Destroy all children of `frame` except the wallpaper label."""
    for w in frame.winfo_children():
        if getattr(w, "_mcm_wallpaper", False):
            continue
        try:
            w.destroy()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------
# Corner blending
#
# CTk paints the area OUTSIDE a widget's rounded corners with its flat
# `bg_color`. On top of the wallpaper gradient a fixed color shows up as
# little dark squares at every card corner. blend_corners() looks at what
# is REALLY painted behind the widget (the wallpaper of the nearest
# wallpapered ancestor, or the fg_color of the nearest opaque ancestor)
# and repaints `bg_color` to match, so rounded widgets melt into whatever
# is behind them.
# ---------------------------------------------------------------------

def _blend_source(widget):
    """(kind, data) of the nearest thing painted behind `widget`.

    kind 'wallpaper' -> data = the wallpaper OWNER ancestor (sample pixels)
    kind 'flat'      -> data = fg_color of nearest opaque CTk container
    kind None        -> unknown, keep current color."""
    try:
        node = widget.master
        while node is not None:
            if getattr(node, "_mcm_wallpaper_owner", False):
                # wallpaper is drawn ABOVE this frame's own fill -> it wins
                return ("wallpaper", node)
            fg = None
            try:
                fg = node.cget("fg_color")
            except Exception:  # noqa: BLE001
                fg = None
            if fg is not None and "transparent" not in str(fg):
                return ("flat", fg)
            node = node.master
    except Exception:  # noqa: BLE001
        pass
    return (None, None)


def _flat_hex(fg) -> str | None:
    """Dark-mode hex for a cget('fg_color') value (str or (light, dark))."""
    try:
        if isinstance(fg, (tuple, list)):
            fg = fg[-1]
        fg = str(fg).strip()
        if not fg.startswith("#") or len(fg) != 7:
            return None
        return fg.upper()
    except Exception:  # noqa: BLE001
        return None


def _sample_corners(widget, host) -> tuple[str, str, str, str] | None:
    """Per-corner wallpaper colors (tl, tr, br, bl) behind `widget`."""
    try:
        hw, hh = host.winfo_width(), host.winfo_height()
        if hw < 60 or hh < 60:
            return None
        img = pil_for(_bucket(hw, hh)).convert("RGB")
        iw, ih = img.size
        # widget position inside the host (identity mapping: the wallpaper
        # image is drawn from the host's top-left corner)
        dx = widget.winfo_rootx() - host.winfo_rootx()
        dy = widget.winfo_rooty() - host.winfo_rooty()
        w, h = widget.winfo_width(), widget.winfo_height()
        if w < 4 or h < 4:
            return None
        pts = [(dx + 2, dy + 2), (dx + w - 3, dy + 2),
               (dx + w - 3, dy + h - 3), (dx + 2, dy + h - 3)]
        out = []
        for x, y in pts:
            x = min(max(x, 0), iw - 1)
            y = min(max(y, 0), ih - 1)
            r, g, b = img.getpixel((x, y))
            out.append(f"#{r:02X}{g:02X}{b:02X}")
        return tuple(out)
    except Exception:  # noqa: BLE001
        return None


def _avg(colors: tuple) -> str:
    rs = sum(int(c[1:3], 16) for c in colors)
    gs = sum(int(c[3:5], 16) for c in colors)
    bs = sum(int(c[5:7], 16) for c in colors)
    n = len(colors)
    return f"#{rs // n:02X}{gs // n:02X}{bs // n:02X}"


def blend_corners(widget) -> None:
    """Keep `widget`'s outside-corner paint matched to whatever is behind
    it: the exact per-corner wallpaper pixels when it floats on the
    wallpaper, or the fg_color of the opaque card it sits inside.

    CTkFrame and CTkButton accept `background_corner_colors` (four
    quadrant rects placed UNDER the rounded body), which lets each corner
    carry its own sampled color - a perfect match on the gradient. Widgets
    without that option fall back to a single averaged bg_color.

    A widget's own <Configure> does NOT fire when an ANCESTOR moves (e.g.
    the sidebar settling during the first layout pass), so blended widgets
    also register in a global list that refresh_blends() re-checks - the
    app's 100ms tick calls it and re-samples anything that moved."""
    entry = {"widget": widget, "apply": None, "pos": None, "key": None}

    def apply(_event=None) -> None:
        try:
            pos = (widget.winfo_rootx(), widget.winfo_rooty(),
                   widget.winfo_width(), widget.winfo_height())
            if pos == entry["pos"] and entry["key"] is not None:
                return
            entry["pos"] = pos
            kind, data = _blend_source(widget)
            colors = None
            if kind == "flat":
                hexc = _flat_hex(data)
                if hexc:
                    colors = (hexc, hexc, hexc, hexc)
            elif kind == "wallpaper":
                colors = _sample_corners(widget, data)
            if not colors:
                return
            key = tuple(colors)
            if key == entry["key"]:
                return
            entry["key"] = key
            avg = _avg(colors)
            try:
                # best: exact per-corner colors (CTkFrame / CTkButton)
                widget.configure(background_corner_colors=colors,
                                 bg_color=avg)
            except Exception:  # noqa: BLE001
                # textbox / entry etc: single flat color only
                widget.configure(bg_color=avg)
        except Exception:  # noqa: BLE001
            pass

    entry["apply"] = apply
    _blended.append(entry)
    try:
        widget.bind("<Configure>", apply, add="+")
        widget.after(60, apply)
    except Exception:  # noqa: BLE001
        pass


_blended: list = []


def refresh_blends() -> None:
    """Re-sample any blended widget / inherited wallpaper that moved since
    the last check. Call from the app's periodic tick; cheap (a few winfo
    calls each)."""
    for entry in list(_blended):
        widget = entry["widget"]
        try:
            if not widget.winfo_exists():
                _blended.remove(entry)
                continue
            pos = (widget.winfo_rootx(), widget.winfo_rooty(),
                   widget.winfo_width(), widget.winfo_height())
            if pos != entry["pos"]:
                entry["apply"]()
        except Exception:  # noqa: BLE001
            try:
                _blended.remove(entry)
            except ValueError:
                pass
    for entry in list(_inherited):
        master, owner = entry["master"], entry["owner"]
        try:
            if not master.winfo_exists() or not owner.winfo_exists():
                _inherited.remove(entry)
                continue
            dx = master.winfo_rootx() - owner.winfo_rootx()
            dy = master.winfo_rooty() - owner.winfo_rooty()
            if (dx, dy) != (entry["dx"], entry["dy"]):
                entry["apply"]()
        except Exception:  # noqa: BLE001
            try:
                _inherited.remove(entry)
            except ValueError:
                pass


# kept for backwards compatibility with earlier call sites
def match_corners(widget) -> None:
    blend_corners(widget)
