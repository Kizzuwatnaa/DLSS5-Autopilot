"""Before/after screenshot pairing for "DLSS 5 on vs off" comparisons.

ReShade writes a screenshot on its own hotkey (ReShade.ini [INPUT]
KeyScreenshot, Print Screen by default) into [SCREENSHOT] SavePath, which is
the game folder unless the user changed it. The natural way to make a
comparison is: toggle neural rendering, shoot, toggle back, shoot again - so
the two newest files taken within a few minutes of each other are the pair.

Everything here is plain file and ini work; the window lives in compareui.
The export deliberately uses tkinter's own PNG codec instead of PIL, because
this tool ships with no third-party packages and must stay that way.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime
from pathlib import Path

from .reshade_ini import Ini

EXTS = {".png", ".jpg", ".jpeg", ".bmp"}
# Files this tool writes itself must never be offered as a "before" shot.
OUR_PREFIX = "dlss5_compare"
# "toggle, shoot, toggle, shoot" fits comfortably in this; anything further
# apart is more likely two unrelated screenshots from different sessions.
PAIR_WINDOW = 5 * 60
# Wider than this and tk's pixel copy takes long enough to feel like a hang.
EXPORT_MAX_W = 1920

# ReShade names files "<game> <yyyy-mm-dd hh-mm-ss>[_n].png". Copies and
# cloud syncs rewrite the modified time, so the name wins when it is there.
_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})")

# Virtual-key code -> what the key cap says. ReShade stores the raw VK.
_VK = {
    8: "Backspace", 9: "Tab", 13: "Enter", 19: "Pause", 20: "Caps Lock",
    27: "Escape", 32: "Space", 33: "Page Up", 34: "Page Down", 35: "End",
    36: "Home", 37: "Left", 38: "Up", 39: "Right", 40: "Down",
    44: "Print Screen", 45: "Insert", 46: "Delete", 144: "Num Lock",
    145: "Scroll Lock", 106: "Numpad *", 107: "Numpad +", 109: "Numpad -",
    110: "Numpad .", 111: "Numpad /", 186: ";", 187: "=", 188: ",",
    189: "-", 190: ".", 191: "/", 192: "`", 219: "[", 220: "\\", 221: "]",
    222: "'",
}
_VK.update({48 + i: str(i) for i in range(10)})
_VK.update({65 + i: chr(65 + i) for i in range(26)})
_VK.update({96 + i: f"Numpad {i}" for i in range(10)})
_VK.update({112 + i: f"F{i + 1}" for i in range(24)})

DEFAULT_KEY = "Print Screen"


# ------------------------------------------------------------------ finding
def save_path(install_dir: Path) -> Path:
    """Where ReShade puts screenshots for this game.

    ReShade resolves a relative SavePath against the game folder (the
    working directory at the time), so we do the same.
    """
    raw = Ini.load(install_dir / "ReShade.ini").get("SCREENSHOT", "SavePath")
    if not raw:
        return install_dir
    p = Path(raw.strip().strip('"'))
    if not p.is_absolute():
        p = install_dir / p
    try:
        return p.resolve()
    except OSError:
        return p


def taken(path: Path) -> float:
    """When the shot was taken, as a unix timestamp."""
    m = _STAMP.search(path.stem)
    if m:
        try:
            return datetime(*map(int, m.groups())).timestamp()
        except ValueError:
            pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def is_ours(path: Path) -> bool:
    return path.name.lower().startswith(OUR_PREFIX)


def find_screenshots(install_dir: Path) -> list[Path]:
    """Image files in the game folder and the ReShade save folder, newest first.

    Only the top level of each folder is read: game folders can hold hundreds
    of thousands of files, and ReShade never nests its screenshots deeper.
    """
    install_dir = Path(install_dir)
    folders = [install_dir]
    sp = save_path(install_dir)
    try:
        if sp.resolve() != install_dir.resolve():
            folders.append(sp)
    except OSError:
        folders.append(sp)
    seen: set[str] = set()
    found: list[tuple[float, Path]] = []
    for folder in folders:
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.suffix.lower() not in EXTS or is_ours(p):
                continue
            key = str(p).lower()
            if key in seen:
                continue
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            seen.add(key)
            found.append((taken(p), p))
    found.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in found]


def pair(files: list[Path]) -> tuple[Path, Path] | None:
    """The two newest shots taken within PAIR_WINDOW of each other.

    Returned oldest first, because the first shot in the flow is the "before"
    one - the label swap in the window covers the other habit.
    """
    files = list(files)
    if len(files) < 2:
        return None
    stamped = sorted(((taken(p), p) for p in files), key=lambda t: t[0],
                     reverse=True)
    for (t1, a), (t2, b) in zip(stamped, stamped[1:]):
        if t1 - t2 <= PAIR_WINDOW:
            return b, a
    return None


def when(path: Path) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(taken(path)))


# --------------------------------------------------------------------- keys
def key_name(spec: str | None) -> str:
    """ReShade's "vk,ctrl,shift,alt" -> "Ctrl + Print Screen"."""
    if not spec:
        return DEFAULT_KEY
    parts = [s.strip() for s in spec.split(",")]
    try:
        vk = int(parts[0])
    except ValueError:
        return DEFAULT_KEY
    if vk == 0:
        return "not bound - set one in ReShade's settings tab"
    mods = [name for flag, name in zip(parts[1:4], ("Ctrl", "Shift", "Alt"))
            if flag not in ("", "0")]
    return " + ".join(mods + [_VK.get(vk, f"key {vk}")])


def screenshot_key(install_dir: Path) -> str:
    return key_name(Ini.load(Path(install_dir) / "ReShade.ini")
                    .get("INPUT", "KeyScreenshot"))


# ------------------------------------------------------------------- export
def is_png(path: Path) -> bool:
    return Path(path).suffix.lower() == ".png"


def fit_factor(width: int, limit: int) -> int:
    """Integer subsample factor that brings `width` under `limit`."""
    return max(1, math.ceil(width / limit)) if limit > 0 else 1


def _load_capped(tk, master, path: Path, limit: int):
    img = tk.PhotoImage(master=master, file=str(path))
    f = fit_factor(img.width(), limit)
    if f > 1:
        img = img.subsample(f, f)
    return img


def export_side_by_side(a: Path, b: Path, out: Path, master=None,
                        max_width: int = EXPORT_MAX_W) -> Path:
    """Write `a` and `b` next to each other into one PNG, `a` on the left.

    Tk's photo image can read and write PNG on its own, so no PIL. Each side
    is capped at `max_width` first: tk copies pixels one at a time in C, and
    two uncapped 4K shots would take long enough to look frozen. A hidden
    root is created when no `master` is given so this works from scripts.
    """
    import tkinter as tk
    own_root = None
    if master is None:
        own_root = tk.Tk()
        own_root.withdraw()
        master = own_root
    try:
        ia = _load_capped(tk, master, Path(a), max_width)
        ib = _load_capped(tk, master, Path(b), max_width)
        wa, ha, wb, hb = ia.width(), ia.height(), ib.width(), ib.height()
        combined = tk.PhotoImage(master=master, width=wa + wb, height=max(ha, hb))
        try:
            # The Tcl-level copy is a straight memcpy; tkinter only exposes
            # put(), which would round-trip every row through a string.
            combined.tk.call(str(combined), "copy", str(ia), "-to", 0, 0)
            combined.tk.call(str(combined), "copy", str(ib), "-to", wa, 0)
        except tk.TclError:
            for x0, img, h in ((0, ia, ha), (wa, ib, hb)):
                for y in range(h):
                    row = [img.get(x, y) for x in range(img.width())]
                    combined.put("{" + " ".join(
                        "#%02x%02x%02x" % c for c in row) + "}", to=(x0, y))
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        combined.write(str(out), format="png")
        return out
    finally:
        if own_root is not None:
            own_root.destroy()


def export_name(install_dir: Path) -> Path:
    stamp = time.strftime("%Y-%m-%d %H-%M-%S")
    return Path(install_dir) / f"{OUR_PREFIX}_{stamp}.png"
