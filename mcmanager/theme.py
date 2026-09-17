"""Color palette, fonts and small styling helpers (dark indigo/violet theme)."""
import customtkinter as ctk

# ---- palette -------------------------------------------------------------
BG = "#0E1017"          # window background (deep navy)
SURFACE = "#161926"     # cards
SURFACE2 = "#1E2233"    # elevated / hover
BORDER = "#2B3050"
TEXT = "#EAECF8"
TEXT_DIM = "#9AA0BF"
ACCENT = "#7C5CFF"      # violet - primary action color
ACCENT_DARK = "#5F43E8"
ACCENT_SOFT = "#A79BFF"
INFO = "#4CC3FF"
AMBER = "#FFB020"
RED = "#FF5D5D"
ON_ACCENT = "#FFFFFF"   # text placed on accent buttons
# fallback color painted just outside rounded card corners when the
# wallpaper sampler cannot run (early startup, dialogs without wallpaper)
CORNER_BG = "#20244C"

# ---- geometry ------------------------------------------------------------
# Small radii only: measured on X11, CTkButton renders stray 1-2px "spike"
# pixels at the arc/strip tangent lines when corner_radius is large relative
# to the widget height (e.g. r=12 on h=28). r<=8 measured artifact-free.
BUTTON_RADIUS = 8       # buttons / entries / small rows
CARD_RADIUS = 12        # cards / big containers

# ---- fonts ---------------------------------------------------------------
# "Segoe UI" / "Consolas" exist on Windows only; on Linux/macOS Tk silently
# substitutes a fallback whose metrics differ, which makes labels render
# with clipped or loosely-fit text. Pick the best family that actually
# exists on this machine, in priority order, once per process.
_UI_CANDIDATES = [
    ("Windows", ["Segoe UI", "Roboto", "Ubuntu", "Noto Sans", "DejaVu Sans",
                 "Helvetica", "Arial"]),
    ("Darwin", ["SF Pro Text", "Helvetica Neue", "Avenir Next", "Menlo"]),
    ("Linux", ["Noto Sans", "Ubuntu", "Cantarell", "DejaVu Sans", "FreeSans",
               "Helvetica", "Arial"]),
]
_MONO_CANDIDATES = [
    ("Windows", ["Consolas", "Cascadia Mono", "DejaVu Sans Mono", "Courier New"]),
    ("Darwin", ["Menlo", "SF Mono", "Monaco", "Courier New"]),
    ("Linux", ["JetBrains Mono", "Fira Mono", "Sarasa Mono SC", "DejaVu Sans Mono",
               "Noto Sans Mono", "FreeMono", "Courier New"]),
]

_family_cache: dict = {}


def _existing_family(key: str, candidates: list[str]) -> str:
    """First family from `candidates` that is really installed.
    Needs a Tk root (created lazily by customtkinter's scaling logic);
    falls back to the first candidate when detection is impossible."""
    if key in _family_cache:
        return _family_cache[key]
    chosen = candidates[0]
    try:
        import tkinter
        from tkinter import font as tkfont
        root = tkinter._default_root
        if root is not None:
            installed = set(tkfont.families(root))
            for fam in candidates:
                if fam in installed:
                    chosen = fam
                    break
    except Exception:  # noqa: BLE001
        pass
    _family_cache[key] = chosen
    return chosen


def _platform_candidates(table) -> list[str]:
    import sys
    for plat, fams in table:
        if sys.platform.startswith(plat.lower() if plat != "Darwin" else "dar"):
            return fams
    # unknown platform: try every suggestion, Windows list first
    merged: list[str] = []
    for _, fams in table:
        merged += [f for f in fams if f not in merged]
    return merged


def _ui_family() -> str:
    return _existing_family("ui", _platform_candidates(_UI_CANDIDATES))


def _mono_family() -> str:
    return _existing_family("mono", _platform_candidates(_MONO_CANDIDATES))


# NOTE: resolved lazily (a Tk root must exist for family detection),
# so read them via T.FONT_FAMILY / T.MONO_FAMILY only after the app window
# is up; T.font() / T.mono() always pick the right family themselves.
def __getattr__(name):
    if name == "FONT_FAMILY":
        return _ui_family()
    if name == "MONO_FAMILY":
        return _mono_family()
    raise AttributeError(name)


_font_cache: dict = {}


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    key = ("f", size, weight)
    if key not in _font_cache:
        _font_cache[key] = ctk.CTkFont(family=_ui_family(), size=size,
                                       weight=weight)
    return _font_cache[key]


def mono(size: int = 12) -> ctk.CTkFont:
    key = ("m", size)
    if key not in _font_cache:
        _font_cache[key] = ctk.CTkFont(family=_mono_family(), size=size)
    return _font_cache[key]


def configure() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    # a root window exists by now -> re-detect installed font families
    # once, then forget previously cached CTkFont objects (they may hold
    # the pre-root fallback family)
    _family_cache.pop("ui", None)
    _family_cache.pop("mono", None)
    _font_cache.clear()


PLATFORM_LABELS = {
    "paper": "Paper (plugins)",
    "purpur": "Purpur (plugins)",
    "spigot": "Spigot (plugins)",
    "bukkit": "Bukkit (plugins)",
    "folia": "Folia (plugins)",
    "pufferfish": "Pufferfish (plugins)",
    "fabric": "Fabric (mods)",
    "quilt": "Quilt (mods)",
    "vanilla": "Vanilla",
    "forge": "Forge (mods)",
    "neoforge": "NeoForge (mods)",
    "mohist": "Mohist (hybrid)",
    "arclight": "Arclight (hybrid)",
    "banner": "Banner (hybrid)",
    "catserver": "CatServer (hybrid)",
    "magma": "Magma (hybrid)",
    "cardboard": "Cardboard (hybrid)",
    "sponge": "Sponge",
    "bungeecord": "BungeeCord (proxy)",
    "waterfall": "Waterfall (proxy)",
    "velocity": "Velocity (proxy)",
}


def platform_label(p: str) -> str:
    return PLATFORM_LABELS.get(p, p or "Not set up")
