r"""A picture of OUR window, and nothing else on the screen.

The owner judges the tool by how it looks, and a scrollbar that reads as
broken makes the rest read as broken too - no suite sees that. So every
round that touches the window looks at one of these.

    python _tools\shot.py                    the tool's own window, if it runs
    python _tools\shot.py --out shots        where to write them
    python _tools\shot.py --title "DLSS 5"   match another window's title

It captures with Win32 `PrintWindow(PW_RENDERFULLCONTENT)` into a DIB, so
it reads THAT WINDOW's own pixels rather than the screen: an early round of
this project took a full-desktop screenshot and caught the owner's private
browsing in it. Nothing outside the window can be in the file. PNG is
written here with zlib, so there is nothing to install.
"""
from __future__ import annotations

import argparse
import ctypes
import struct
import sys
import zlib
from ctypes import wintypes
from pathlib import Path

PW_RENDERFULLCONTENT = 0x00000002
DEFAULT_TITLE = "DLSS 5 Autopilot"


def _windows(title_part: str):
    """(hwnd, title) for every visible top-level window whose title matches."""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if title_part.lower() in buf.value.lower():
            found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return found


def capture(hwnd: int, path: Path) -> tuple[int, int]:
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise RuntimeError("the window has no size")
    src = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(src)
    bmp = gdi32.CreateCompatibleBitmap(src, w, h)
    gdi32.SelectObject(mem, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT):
            raise RuntimeError("PrintWindow refused this window")
        info = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, w * h * 4,
                           0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        binfo = ctypes.create_string_buffer(info)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, binfo, 0)
        # Negative height above means the rows arrive top-down already.
        rows = bytearray()
        stride = w * 4
        raw = buf.raw
        for y in range(h):
            rows.append(0)
            line = raw[y * stride:(y + 1) * stride]
            for i in range(0, stride, 4):
                rows += bytes((line[i + 2], line[i + 1], line[i], 255))

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        path.write_bytes(b"\x89PNG\r\n\x1a\n"
                         + chunk(b"IHDR",
                                 struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                         + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
                         + chunk(b"IEND", b""))
        return w, h
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, src)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default=DEFAULT_TITLE)
    ap.add_argument("--out", default=".")
    a = ap.parse_args()
    if sys.platform != "win32":
        print("this is a Win32 capture; there is nothing to do here")
        return 2
    wins = _windows(a.title)
    if not wins:
        print(f"no visible window whose title contains {a.title!r} - "
              f"start the tool first")
        return 2
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for i, (hwnd, title) in enumerate(wins, 1):
        safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in title)
        p = out / f"{safe.strip() or 'window'}{'' if i == 1 else f'-{i}'}.png"
        try:
            w, h = capture(hwnd, p)
            print(f"  {p}  {w}x{h}")
        except Exception as e:
            print(f"  !! {title}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
