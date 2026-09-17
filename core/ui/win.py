"""Windows' part of the window: DPI, the title bar, the icon.

Tk draws the client area only; the frame is Windows'. Without these a dark
app comes with a white title bar and Python's feather in the taskbar, and a
125%/150% display stretches the whole window like a bitmap.
"""
from __future__ import annotations

import base64
import ctypes

from .. import prefs
from . import theme as T


def dpi_aware() -> None:
    """Per-monitor DPI awareness, before the first Tk window exists."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def apply_scale(root) -> None:
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(root.winfo_id()) or 96
    except Exception:
        dpi = 96
    root.tk.call("tk", "scaling", dpi / 72.0)
    T.set_scale(dpi)


def ico_path():
    return prefs.FILE.parent / "dlss5-autopilot.ico"


def set_icons(root) -> None:
    """The .ico beside settings.json (the title bar wants a real file) and the
    PNG for the taskbar."""
    try:
        from ..icon_png import ICON_ICO_B64, ICON_PNG_B64
        root._icon_png = __import__("tkinter").PhotoImage(master=root, data=base64.b64decode(ICON_PNG_B64))
        root.iconphoto(True, root._icon_png)
        ico = ico_path()
        data = base64.b64decode(ICON_ICO_B64)
        if not ico.is_file() or ico.read_bytes() != data:
            ico.parent.mkdir(parents=True, exist_ok=True)
            ico.write_bytes(data)
        root.iconbitmap(default=str(ico))
    except Exception:
        pass


def own_icon(win) -> None:
    try:
        ico = ico_path()
        if ico.is_file():
            win.iconbitmap(str(ico))
    except Exception:
        pass


def dark_titlebar(win) -> None:
    """The title bar in the window's colours (Windows 10 20H1 dark mode,
    Windows 11 caption and text colour; older systems ignore the calls)."""
    own_icon(win)
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if not hwnd:
            win.bind("<Map>", lambda _e: dark_titlebar(win), add="+")
            return
        dwm = ctypes.windll.dwmapi

        def colorref(c: str) -> int:
            r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
            return (b << 16) | (g << 8) | r
        for attr, val in ((20, 1), (35, colorref(T.RAIL)), (36, colorref(T.MUTED)),
                          (34, colorref(T.LINE))):
            v = ctypes.c_int(val)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4)
    except Exception:
        pass
