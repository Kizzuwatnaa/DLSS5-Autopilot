"""Colours, type, glyphs and scale for the whole window - one place.

Every screen and dialog takes its look from here, so a colour changed once
changes everywhere and no dialog drifts into a look of its own.
"""
from __future__ import annotations

import tkinter.font as tkfont

BG = "#0a0b0d"          # window ground
RAIL = "#0d0e11"        # the navigation rail
SURF = "#14161a"        # fields, rows, panels
SURF2 = "#1b1e23"       # buttons, menus, a raised surface
LINE = "#23272d"        # hairlines, hover on SURF2
TEXT = "#ece9e2"
MUTED = "#8c929b"
DIM = "#5a6069"
AMBER = "#e6ac50"       # the tool's own accent; a game page uses the game's
OK = "#86d493"
WARN = "#ec6f5f"
LOG_BG = "#08090b"

MONO_FAMILIES = ("Cascadia Mono", "Consolas", "Courier New")
ICON_FAMILIES = ("Segoe Fluent Icons", "Segoe MDL2 Assets", "Segoe UI Symbol")

# Segoe MDL2 Assets / Fluent Icons code points (the same in both)
GLYPH = {
    "search": "\ue721", "scan": "\ue72c", "gear": "\ue713", "play": "\ue768",
    "check": "\ue73e", "cross": "\ue711", "game": "\ue7fc", "video": "\ue714",
    "remix": "\ue706", "help": "\ue897", "eye": "\ue7b3", "down": "\ue70d",
    "up": "\ue70e", "back": "\ue72b", "folder": "\ue8b7", "trash": "\ue74d",
    "compare": "\ue8a9", "people": "\ue716", "pilot": "\ue709",
    "close": "\ue8bb", "add": "\ue710", "warn": "\ue7ba", "info": "\ue946",
    "bug": "\uebe8", "copy": "\ue8c8", "download": "\ue896", "link": "\ue71b",
    "hide": "\ued1a", "more": "\ue712", "refresh": "\ue72c", "save": "\ue74e",
    "camera": "\ue722", "screen": "\ue7f4", "file": "\ue8a5", "update": "\ue777", "chip": "\ue950",
}

SCALE = 1.0             # pixels per 96-DPI pixel, set once at start


def set_scale(dpi: float) -> None:
    global SCALE
    SCALE = max(1.0, dpi / 96.0)


def px(n: float) -> int:
    """A layout length in pixels at the current scale."""
    return max(1, round(n * SCALE))


_families: dict[str, str] = {}


def _first_available(names: tuple[str, ...]) -> str:
    key = names[0]
    if key not in _families:
        have = set(tkfont.families())
        _families[key] = next((n for n in names if n in have), names[-1])
    return _families[key]


def mono(size: int = 10, bold: bool = False) -> tuple:
    """Type. Sizes are points, which Tk scales with the display on its own."""
    return (_first_available(MONO_FAMILIES), size, "bold" if bold else "normal")


def icons(size: int = 12) -> tuple:
    return (_first_available(ICON_FAMILIES), size)


_measure: dict[tuple, tkfont.Font] = {}


def width(text: str, font: tuple) -> int:
    import tkinter as _tk
    key = (font, id(_tk._default_root))
    f = _measure.get(key)
    if f is None:
        f = _measure[key] = tkfont.Font(font=font)
    return f.measure(text)


def fit(text: str, font: tuple, room: int) -> str:
    """The text, cut with an ellipsis where it would not fit in `room` px."""
    if width(text, font) <= room:
        return text
    while text and width(text + "\u2026", font) > room:
        text = text[:-1]
    return text.rstrip() + "\u2026"


API_NAMES = {"DX9": "DirectX 9", "DX10": "DirectX 10", "DX11": "DirectX 11",
             "DX12": "DirectX 12", "OPENGL": "OpenGL", "VULKAN": "Vulkan"}


def api_name(api: str | None) -> str:
    return API_NAMES.get(str(api or "").upper().replace(" ", ""), str(api or "?"))
