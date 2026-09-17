r"""The smallest honest Windows DLL that carries a version resource.

The DLSS update page reads a runtime's version through Windows' own
GetFileVersionInfo, and a test that fakes that call proves nothing about
the reading. So the tests write files Windows itself accepts: a PE32+ (or
PE32) image with one .rsrc section holding a VS_VERSIONINFO, padded to a
size the download check accepts as a runtime.

    from fake_pe import dll
    Path("nvngx_dlss.dll").write_bytes(dll("3.7.20"))
    Path("x86.dll").write_bytes(dll("310.2.1", machine=0x14C))
"""
from __future__ import annotations

import struct


def _utf16z(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\0\0"


def _align(b: bytes, n: int = 4) -> bytes:
    return b + b"\0" * ((-len(b)) % n)


def _version_info(version: str) -> bytes:
    nums = [int(x) for x in version.split(".")] + [0, 0, 0, 0]
    ms = (nums[0] << 16) | nums[1]
    ls = (nums[2] << 16) | nums[3]
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x00010000, ms, ls, ms, ls,
                        0x3F, 0, 0x40004, 2, 0, 0, 0)
    body = _align(struct.pack("<HHH", 0, len(fixed), 0) + _utf16z("VS_VERSION_INFO"))
    body = _align(body + fixed)
    return body[:0] + struct.pack("<H", len(body)) + body[2:]


def _resources(version: str, rva: int) -> bytes:
    """type 16 (RT_VERSION) -> id 1 -> language 0x409 -> the data."""
    def directory(entry_id: int, offset: int, leaf: bool) -> bytes:
        return struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack(
            "<II", entry_id, offset | (0 if leaf else 0x80000000))
    d0 = directory(16, 0x18, False)          # 0x00, entries at 0x10
    d1 = directory(1, 0x30, False)           # 0x18
    d2 = directory(0x409, 0x48, True)        # 0x30 -> data entry at 0x48
    info = _version_info(version)
    data_at = 0x58
    entry = struct.pack("<IIII", rva + data_at, len(info), 0, 0)
    return _align(d0 + d1 + d2 + entry + info, 8)


def dll(version: str = "", machine: int = 0x8664, size: int = 300_000) -> bytes:
    """A DLL image with `version` stamped in it ("" stamps none)."""
    pe32plus = machine == 0x8664
    file_align, sect_align, rva = 0x200, 0x1000, 0x1000
    rsrc = _resources(version, rva) if version else b""
    raw = rsrc + b"\0" * ((-len(rsrc)) % file_align)
    opt_size = 0xF0 if pe32plus else 0xE0
    out = bytearray(0x40)
    out[0:2] = b"MZ"
    struct.pack_into("<I", out, 0x3C, 0x40)
    nsect = 1 if version else 0
    out += b"PE\0\0" + struct.pack("<HHIIIHH", machine, nsect, 0, 0, 0, opt_size,
                                    0x2022 if pe32plus else 0x2102)
    image = rva + max(sect_align, (len(rsrc) + sect_align - 1) // sect_align * sect_align)
    if pe32plus:
        opt = struct.pack("<HBBIIIII", 0x20B, 14, 0, 0, 0, 0, 0, 0x1000)
        opt += struct.pack("<QIIHHHHHHIIIIHHQQQQII", 0x180000000, sect_align, file_align,
                           6, 0, 0, 0, 6, 0, 0, image, 0x200, 0, 2, 0x160,
                           0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    else:
        opt = struct.pack("<HBBIIIIII", 0x10B, 14, 0, 0, 0, 0, 0, 0x1000, 0x1000)
        opt += struct.pack("<IIIHHHHHHIIIIHHIIIIII", 0x10000000, sect_align, file_align,
                           6, 0, 0, 0, 6, 0, 0, image, 0x200, 0, 2, 0x140,
                           0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    dirs = [(0, 0)] * 16
    if version:
        dirs[2] = (rva, len(rsrc))
    opt += b"".join(struct.pack("<II", a, b) for a, b in dirs)
    out += opt
    if version:
        out += struct.pack("<8sIIIIIIHHI", b".rsrc", len(rsrc), rva, len(raw), 0x200,
                           0, 0, 0, 0, 0x40000040)
    out += b"\0" * (0x200 - len(out))
    out += raw
    if len(out) < size:
        out += b"\0" * (size - len(out))
    return bytes(out)
