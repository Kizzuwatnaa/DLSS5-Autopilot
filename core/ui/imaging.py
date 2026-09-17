"""Pictures without an image library: GDI+ (in every Windows) through ctypes.

Tk can only show PNG/GIF/PPM, and a game's art is JPEG. Pulling in PIL would
add megabytes and one more thing antivirus heuristics look at, so decoding,
scaling, darkening and fading all happen inside gdiplus.dll; Python only
copies the finished pixels out and hands Tk a PPM. A full-window page
backdrop costs about 30 ms, a cover about 5 ms, and a brightness step is a
256-entry lookup through bytes.translate.

Every function returns RGB bytes (top-down, 3 per pixel) or None, and never
raises for a missing or unreadable file - a game without art simply gets the
plain tile. Safe to call from a worker thread; Tk images must still be made
on the Tk thread (`photo`).
"""
from __future__ import annotations

import colorsys
import ctypes
import threading
from ctypes import byref, c_int, c_uint, c_void_p, wintypes

_lock = threading.Lock()
_gp = None

RGB24 = 0x00021808
ARGB32 = 0x0026200A
_WRAP_TILE_FLIP = 3
_UNIT_PIXEL = 2


class _Start(ctypes.Structure):
    _fields_ = [("ver", c_uint), ("cb", c_void_p), ("nothread", wintypes.BOOL),
                ("nocodecs", wintypes.BOOL)]


class _Rect(ctypes.Structure):
    _fields_ = [("x", c_int), ("y", c_int), ("w", c_int), ("h", c_int)]


class _Point(ctypes.Structure):
    _fields_ = [("x", c_int), ("y", c_int)]


class _Data(ctypes.Structure):
    _fields_ = [("w", c_uint), ("h", c_uint), ("stride", c_int), ("fmt", c_int),
                ("scan0", c_void_p), ("res", ctypes.c_size_t)]


def _gdip():
    """gdiplus.dll, started once for the process; None where it is missing."""
    global _gp
    with _lock:
        if _gp is None:
            try:
                gp = ctypes.windll.gdiplus
                tok = ctypes.c_size_t()
                if gp.GdiplusStartup(byref(tok), byref(_Start(1, None, False, False)), None) != 0:
                    raise OSError("GdiplusStartup failed")
                _gp = gp
            except (OSError, AttributeError):
                _gp = False
    return _gp or None


def rgb(c: str) -> tuple[int, int, int]:
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def _argb(c: str, a: int = 255) -> c_uint:
    r, g, b = rgb(c)
    return c_uint((max(0, min(255, a)) << 24) | (r << 16) | (g << 8) | b)


def _load(gp, path):
    img = c_void_p()
    if not path or gp.GdipLoadImageFromFile(ctypes.c_wchar_p(str(path)), byref(img)) != 0:
        return None, 0, 0
    w, h = c_uint(), c_uint()
    gp.GdipGetImageWidth(img, byref(w))
    gp.GdipGetImageHeight(img, byref(h))
    if not w.value or not h.value:
        gp.GdipDisposeImage(img)
        return None, 0, 0
    return img, w.value, h.value


def _canvas(gp, w, h, fmt=RGB24):
    bmp, g = c_void_p(), c_void_p()
    gp.GdipCreateBitmapFromScan0(w, h, 0, fmt, None, byref(bmp))
    gp.GdipGetImageGraphicsContext(bmp, byref(g))
    gp.GdipSetInterpolationMode(g, 7)        # high quality bicubic
    gp.GdipSetPixelOffsetMode(g, 4)          # half pixel: no edge shift
    return bmp, g


def _fill(gp, g, colour, alpha, x, y, w, h):
    br = c_void_p()
    gp.GdipCreateSolidFill(_argb(colour, alpha), byref(br))
    gp.GdipFillRectangleI(g, br, x, y, w, h)
    gp.GdipDeleteBrush(br)


def _gradient(gp, g, colour, p1, p2, a1, a2, rect):
    br = c_void_p()
    gp.GdipCreateLineBrushI(byref(_Point(*p1)), byref(_Point(*p2)),
                            _argb(colour, a1), _argb(colour, a2), _WRAP_TILE_FLIP, byref(br))
    gp.GdipFillRectangleI(g, br, *rect)
    gp.GdipDeleteBrush(br)


def _rgb_out(gp, bmp, w, h) -> bytes:
    d = _Data()
    gp.GdipBitmapLockBits(bmp, byref(_Rect(0, 0, w, h)), 1, RGB24, byref(d))
    stride = abs(d.stride)
    raw = ctypes.string_at(d.scan0, stride * h)
    gp.GdipBitmapUnlockBits(bmp, byref(d))
    if stride != w * 3:
        raw = b"".join(raw[y * stride:y * stride + w * 3] for y in range(h))
    out = bytearray(raw)
    out[0::3], out[2::3] = raw[2::3], raw[0::3]         # BGR -> RGB
    return bytes(out)


def _draw_fitted(gp, g, img, sw, sh, w, h, cover=True, focus_y=0.5):
    if cover:
        s = max(w / sw, h / sh)
        cw, ch = max(1, int(w / s)), max(1, int(h / s))
        gp.GdipDrawImageRectRectI(g, img, 0, 0, w, h, (sw - cw) // 2,
                                  int((sh - ch) * focus_y), cw, ch, _UNIT_PIXEL, None, None, None)
    else:
        s = min(w / sw, h / sh)
        dw, dh = max(1, int(sw * s)), max(1, int(sh * s))
        gp.GdipDrawImageRectRectI(g, img, (w - dw) // 2, (h - dh) // 2, dw, dh,
                                  0, 0, sw, sh, _UNIT_PIXEL, None, None, None)


def picture(path, w: int, h: int, bg: str = "#000000", cover: bool = True,
            focus_y: float = 0.5, dim: int = 0) -> bytes | None:
    """`path` fitted into w x h: cropped to fill (cover) or letterboxed on bg."""
    gp = _gdip()
    if gp is None or w < 1 or h < 1:
        return None
    img, sw, sh = _load(gp, path)
    if img is None:
        return None
    bmp, g = _canvas(gp, w, h)
    try:
        gp.GdipGraphicsClear(g, _argb(bg))
        _draw_fitted(gp, g, img, sw, sh, w, h, cover, focus_y)
        if dim:
            _fill(gp, g, "#000000", dim, 0, 0, w, h)
        gp.GdipDeleteGraphics(g)
        g = None
        return _rgb_out(gp, bmp, w, h)
    finally:
        if g:
            gp.GdipDeleteGraphics(g)
        gp.GdipDisposeImage(bmp)
        gp.GdipDisposeImage(img)


def gradient(w: int, h: int, bg: str, colour: str, alpha_top: int) -> bytes | None:
    """colour at alpha_top over bg, fading to bg at the bottom."""
    gp = _gdip()
    if gp is None:
        return None
    bmp, g = _canvas(gp, w, h)
    try:
        gp.GdipGraphicsClear(g, _argb(bg))
        _gradient(gp, g, colour, (0, -1), (0, h + 1), alpha_top, 0, (0, 0, w, h))
        gp.GdipDeleteGraphics(g)
        return _rgb_out(gp, bmp, w, h)
    finally:
        gp.GdipDisposeImage(bmp)


def backdrop(hero, blur, logo, w: int, h: int, bg: str,
             logo_box: tuple[int, int, int, int] | None = None,
             text_side: float = 0.62) -> tuple[bytes | None, int, bool]:
    """A sharp game page: (rgb, height of the art band, whether a logo was drawn).

    The hero (Steam's is 1920x620) is drawn across the width at its own aspect,
    so a normal window only ever scales it down; the rest of the page takes
    its colour from the blurred copy; the join fades row by row through an
    alpha ramp drawn in one pass; the left side is darkened where the words
    go; the game's transparent logo sits in logo_box.
    """
    gp = _gdip()
    if gp is None:
        return None, 0, False
    himg, hw, hh = _load(gp, hero)
    bimg, bw, bh = _load(gp, blur or hero)
    limg, lw, lh = _load(gp, logo)
    # Epic's key art is 16:9 and a picture chosen by hand can be anything:
    # a hero taller than Steam's 1920x620 is cropped to that shape (from
    # near its top, where the faces and titles are) so the page keeps its
    # layout instead of pushing everything below a screen of picture.
    hy = 0
    if himg and hh * 1920 > hw * 620:
        full = hh
        hh = max(1, hw * 620 // 1920)
        hy = int((full - hh) * 0.3)
    band = int(w * hh / hw) if himg else int(h * 0.45)
    dst, g = _canvas(gp, w, h)
    amb, ga = _canvas(gp, w, h)
    try:
        # 1. ambient: the blurred copy stretched over the page, darkened
        gp.GdipGraphicsClear(ga, _argb(bg))
        if bimg and not blur:
            # no blurred copy (Epic, a picture chosen by hand): the sharp one
            # stretched over the page read as a second, ghost title under the
            # first. Shrunk to a few pixels and scaled back up, it is a blur.
            sw_, sh_ = 32, max(1, 32 * bh // max(1, bw))
            small, gs_ = _canvas(gp, sw_, sh_)
            gp.GdipDrawImageRectRectI(gs_, bimg, 0, 0, sw_, sh_, 0, 0, bw, bh, _UNIT_PIXEL, None, None, None)
            gp.GdipDeleteGraphics(gs_)
            gp.GdipDisposeImage(bimg)
            bimg, bw, bh = small, sw_, sh_
        if bimg:
            gp.GdipDrawImageRectRectI(ga, bimg, -8, -8, w + 16, h + 16, 0, 0, bw, bh,
                                      _UNIT_PIXEL, None, None, None)
        _fill(gp, ga, bg, 205, 0, 0, w, h)
        gp.GdipDeleteGraphics(ga)
        ga = None
        gp.GdipDrawImageRectRectI(g, amb, 0, 0, w, h, 0, 0, w, h, _UNIT_PIXEL, None, None, None)
        if himg:
            # 2. the hero, sharp, at its own aspect
            gp.GdipDrawImageRectRectI(g, himg, 0, 0, w, band, 0, hy, hw, hh,
                                      _UNIT_PIXEL, None, None, None)
            _gradient(gp, g, "#000000", (0, -1), (0, band // 3), 90, 0, (0, 0, w, band // 3))
            # 3. its lower part melts into the ambient
            y0 = int(band * 0.42)
            fh = band - y0 + 2
            ov, go = _canvas(gp, w, fh, ARGB32)
            gp.GdipDrawImageRectRectI(go, amb, 0, 0, w, fh, 0, y0, w, fh, _UNIT_PIXEL, None, None, None)
            gp.GdipDeleteGraphics(go)
            d = _Data()
            gp.GdipBitmapLockBits(ov, byref(_Rect(0, 0, w, fh)), 3, ARGB32, byref(d))
            stride = abs(d.stride)
            raw = bytearray(ctypes.string_at(d.scan0, stride * fh))
            for r in range(fh):
                a = min(255, int(255 * ((r + 1) / fh) ** 1.25))
                o = r * stride
                raw[o + 3:o + w * 4:4] = bytes((a,)) * w
            ctypes.memmove(d.scan0, bytes(raw), len(raw))
            gp.GdipBitmapUnlockBits(ov, byref(d))
            gp.GdipDrawImageRectRectI(g, ov, 0, y0, w, fh, 0, 0, w, fh, _UNIT_PIXEL, None, None, None)
            gp.GdipDisposeImage(ov)
        # 4. the side the words sit on, the full height so there is no seam
        side = int(w * text_side)
        _gradient(gp, g, "#000000", (0, 0), (side + 1, 0), 140, 0, (0, 0, side, h))
        # 4a. the page below the art ends in the plain window colour, so a
        #     page taller than the picture scrolls on without an edge. After
        #     the side shade, not before: shading the faded tail left its
        #     bottom row darker than the window, a hard line under the words
        #     once settings made the page longer than the picture.
        tail = int(h * 0.3)
        _gradient(gp, g, bg, (0, h - tail - 1), (0, h + 1), 0, 255, (0, h - tail, w, tail))
        # 5. the logo, alpha kept
        drawn = False
        if limg and logo_box:
            bx, by, bwid, bht = logo_box
            s = min(bwid / lw, bht / lh)
            dw, dh = max(1, int(lw * s)), max(1, int(lh * s))
            gp.GdipDrawImageRectRectI(g, limg, bx, by + bht - dh, dw, dh, 0, 0, lw, lh,
                                      _UNIT_PIXEL, None, None, None)
            drawn = True
        gp.GdipDeleteGraphics(g)
        g = None
        return _rgb_out(gp, dst, w, h), band, drawn
    finally:
        for gr in (g, ga):
            if gr:
                gp.GdipDeleteGraphics(gr)
        for im in (dst, amb, himg, bimg, limg):
            if im:
                gp.GdipDisposeImage(im)


def exe_icon(exe, size: int, bg: str) -> bytes | None:
    """The executable's own icon on bg, for games without store art."""
    try:
        # Private handles: setting argtypes on ctypes.windll.gdi32 changes
        # the prototypes for every other module in the process (it broke
        # _tools/shot.py's PrintWindow call the first time it ran).
        shell32 = ctypes.WinDLL("shell32")
        user32 = ctypes.WinDLL("user32")
        gdi32 = ctypes.WinDLL("gdi32")
    except (OSError, AttributeError):
        return None
    hicon = c_void_p()
    shell32.SHDefExtractIconW.argtypes = [wintypes.LPCWSTR, c_int, c_uint,
                                          ctypes.POINTER(c_void_p), ctypes.POINTER(c_void_p), c_uint]
    if shell32.SHDefExtractIconW(str(exe), 0, 0, byref(hicon), None, size) != 0 or not hicon.value:
        return None

    class BMIH(ctypes.Structure):
        _fields_ = [("size", c_uint), ("w", c_int), ("h", c_int), ("planes", ctypes.c_ushort),
                    ("bits", ctypes.c_ushort), ("comp", c_uint), ("isize", c_uint),
                    ("xppm", c_int), ("yppm", c_int), ("used", c_uint), ("imp", c_uint)]
    gdi32.CreateCompatibleDC.restype = c_void_p
    gdi32.CreateCompatibleDC.argtypes = [c_void_p]
    gdi32.CreateDIBSection.restype = c_void_p
    gdi32.CreateDIBSection.argtypes = [c_void_p, c_void_p, c_uint, ctypes.POINTER(c_void_p), c_void_p, c_uint]
    gdi32.SelectObject.restype = c_void_p
    gdi32.SelectObject.argtypes = [c_void_p, c_void_p]
    gdi32.DeleteObject.argtypes = [c_void_p]
    gdi32.DeleteDC.argtypes = [c_void_p]
    user32.DrawIconEx.argtypes = [c_void_p, c_int, c_int, c_void_p, c_int, c_int, c_uint, c_void_p, c_uint]
    user32.DestroyIcon.argtypes = [c_void_p]
    dc = gdi32.CreateCompatibleDC(None)
    bits = c_void_p()
    hdr = BMIH(ctypes.sizeof(BMIH), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
    dib = gdi32.CreateDIBSection(dc, byref(hdr), 0, byref(bits), None, 0)
    try:
        if not dib:
            return None
        old = gdi32.SelectObject(dc, dib)
        r, g_, b = rgb(bg)
        ctypes.memmove(bits, bytes((b, g_, r, 255)) * (size * size), size * size * 4)
        user32.DrawIconEx(dc, 0, 0, hicon, size, size, 0, None, 3)
        raw = ctypes.string_at(bits, size * size * 4)
        gdi32.SelectObject(dc, old)
        out = bytearray(size * size * 3)
        out[0::3], out[1::3], out[2::3] = raw[2::4], raw[1::4], raw[0::4]
        return bytes(out)
    finally:
        if dib:
            gdi32.DeleteObject(dib)
        gdi32.DeleteDC(dc)
        user32.DestroyIcon(hicon)


_LUT: dict[float, bytes] = {}


def brightness(buf: bytes, k: float) -> bytes:
    key = round(k, 3)
    lut = _LUT.get(key)
    if lut is None:
        lut = _LUT[key] = bytes(min(255, int(i * k)) for i in range(256))
    return buf.translate(lut)


def photo(tk, buf: bytes, w: int, h: int, master=None):
    """A Tk image from RGB bytes. Tk thread only.

    `master` ties the image to the window that shows it; without it Tk uses
    the first root the process made, and a later window cannot see it."""
    return tk.PhotoImage(master=master, data=b"P6 %d %d 255\n" % (w, h) + buf, format="PPM")


def accent(buf: bytes | None) -> str | None:
    """The colour a small picture is about: its strongest hue, made bright.

    Pass a thumbnail (e.g. picture(path, 24, 36)). None when the picture is
    grey or missing, so the caller keeps the tool's own accent.
    """
    if not buf:
        return None
    buckets: dict[int, list[float]] = {}
    for i in range(0, len(buf) - 2, 3):
        r, g, b = buf[i] / 255, buf[i + 1] / 255, buf[i + 2] / 255
        hue, s, v = colorsys.rgb_to_hsv(r, g, b)
        if s < 0.25 or v < 0.2:
            continue
        wgt = s * s * v
        acc = buckets.setdefault(int(hue * 12) % 12, [0.0, 0.0, 0.0, 0.0])
        acc[0] += wgt
        acc[1] += r * wgt
        acc[2] += g * wgt
        acc[3] += b * wgt
    if not buckets:
        return None
    wt, r, g, b = max(buckets.values(), key=lambda a: a[0])
    if wt < 0.5:
        return None
    hue, s, _v = colorsys.rgb_to_hsv(r / wt, g / wt, b / wt)
    r, g, b = colorsys.hsv_to_rgb(hue, min(0.78, max(0.5, s)), 0.92)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
