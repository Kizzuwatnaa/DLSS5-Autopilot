r"""What an RTX Remix install looks like, and how DLSS 5 gets into it.

RTX Remix replaces the game's own d3d9.dll with a small bridge; the real
runtime - a 150-230 MB dxvk-remix build - lives in a `.trex` folder (next to
the executable for GTA IV, under `bin\` for Portal RTX). The frame is path
traced by that runtime, then DLSS upscales it, and only then does the DLSS-NR
snippet run. So on this route there is no ReShade, no feeder and no add-on:
the neural pass is a stage inside the Remix runtime itself.

    game D3D9 -> Remix path tracing -> DLSS SR/RR -> DLSS-NR -> tone mapping

NVIDIA's own Remix runtime has no DLSS-NR stage at all. Two community forks
do, and they name their options differently, which is the whole reason this
module exists:

    rtx.neuralUplift.*      Kim2091's gta4-atmos-dlss5 fork, shipped inside
                            xoxor4d's GTA IV RTX mod
    rtx.neuralRendering.*   lunks/dxvk-remix-plus-dlssnr, a drop-in runtime
                            for any Remix game

Guessing which one is installed would silently write a key the runtime never
reads, so the option name is READ OUT OF THE RUNTIME BINARY instead: both
forks embed their own option prefix as a plain string (the RTX_OPTION macro
registers it by name). Verified on this machine: the GTA IV runtime's
`.trex\d3d9.dll` contains "rtx.neuralUplift" and
"rtx.neuralUplift.bypassCallerCheck" and no "rtx.neuralRendering".
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# The runtime folder and the file inside it that IS the runtime.
TREX = ".trex"
RUNTIME_DLL = "d3d9.dll"
# The caller-identity bridge. Its NAME is load-bearing: the DLSS-NR snippet
# checks that the calling module's path contains "nvngx.dll" and fails with
# 0xbad00002 otherwise, so this file may never be renamed.
REMIX_NVNGX = "remix_nvngx.dll"
DLSSNR = "nvngx_dlssnr.dll"
CONF = "rtx.conf"
LOG = Path("rtx-remix") / "logs" / "remix-dxvk.log"

# The two forks, and the option prefix each one registers.
UPLIFT, NEURAL = "uplift", "neural"
PREFIX = {UPLIFT: "rtx.neuralUplift", NEURAL: "rtx.neuralRendering"}

# Folders that never hold a .trex and can hold hundreds of thousands of
# files. Same idea as dlss._SKIP_DIRS, kept separate so this module has no
# import of its own to trip over (dlss imports this one).
_SKIP_DIRS = {"content", "paks", "saved", "logs", "movies", "sounds", "music",
              "videos", "localization", "shadercache", "derivedcache", "cache",
              "textures", "maps", "levels", "audio", "data", "assets", "mods",
              "screenshots", "steamapps", "redist", "_commonredist",
              "rtx-remix", "rtx_comp", "common", "update", "pc"}
# Remix has to sit where the game loads d3d9.dll from, so `.trex` is beside
# the executable (GTA IV) or one folder down (Portal RTX keeps it in bin\).
# Three levels is generous for that, and the search must stay cheap: this
# runs once per game every time the library is scanned.
_WALK_DEPTH = 3


def find_runtime(folder: Path) -> Path | None:
    """The `.trex` runtime folder under `folder`, or None.

    Directories only, breadth first, to a shallow depth - a game tree holds
    hundreds of thousands of files and none of them are of any interest here.
    Only a `.trex` that actually holds the runtime DLL counts: an empty
    folder of that name is not a Remix install.
    """
    folder = Path(folder)
    level = [folder]
    for _ in range(_WALK_DEPTH):
        nxt: list[Path] = []
        for d in level:
            cand = d / TREX
            if (cand / RUNTIME_DLL).is_file():
                return cand
            try:
                with os.scandir(d) as it:
                    for e in it:
                        # Unlike the DLSS walk a dot-folder is what we are
                        # after, but only the one we want.
                        if (e.is_dir(follow_symlinks=False)
                                and e.name.lower() not in _SKIP_DIRS
                                and not e.name.startswith(".")):
                            nxt.append(Path(e.path))
            except OSError:
                continue
        if not nxt:
            break
        level = nxt
    return None


def is_remix_game(folder: Path) -> bool:
    """Does this folder hold an RTX Remix install?"""
    return find_runtime(folder) is not None


def runtime_flavour(trex: Path) -> str:
    """"uplift", "neural" or "" - which DLSS 5 fork this runtime is, if any.

    Read out of the runtime binary rather than guessed. The file is 150-230 MB
    so it is scanned in chunks, with an overlap so a marker that straddles a
    chunk boundary is still seen, and it is never held in memory twice.
    """
    dll = Path(trex) / RUNTIME_DLL
    marks = {k: v.encode("ascii") for k, v in PREFIX.items()}
    longest = max(len(m) for m in marks.values())
    found: set[str] = set()
    try:
        with open(dll, "rb") as f:
            prev = b""
            while True:
                chunk = f.read(1 << 22)
                if not chunk:
                    break
                buf = prev + chunk
                for flav, m in marks.items():
                    if m in buf:
                        found.add(flav)
                if len(found) == len(marks):
                    break
                prev = buf[-longest:]
    except OSError:
        return ""
    # A runtime carrying both (a future merge) is treated as the newer fork's:
    # neuralRendering is the name the drop-in release ships under.
    if NEURAL in found:
        return NEURAL
    return UPLIFT if UPLIFT in found else ""


def enable_key(flavour: str) -> str:
    """The rtx.conf key that switches the neural pass on for this fork."""
    prefix = PREFIX.get(flavour)
    if not prefix:
        raise ValueError(f"not a DLSS 5 Remix runtime: {flavour!r}")
    return prefix + ".enable"


def conf_path(game_folder: Path, trex: Path) -> Path:
    """The rtx.conf the runtime reads.

    Remix reads it from the game's working directory, which is the folder the
    executable sits in - for GTA IV that is one level above `.trex`. An
    existing file wins over where we would otherwise create one, so a mod's
    own conf is edited rather than shadowed by a second copy.
    """
    for base in (Path(trex).parent, Path(game_folder)):
        p = base / CONF
        if p.is_file():
            return p
    return Path(trex).parent / CONF


def log_path(game_folder: Path) -> Path:
    """Where the runtime writes the log that says whether DLSS-NR ran."""
    return Path(game_folder) / LOG


def _line_re(key: str) -> "re.Pattern[bytes]":
    return re.compile(rb"^[ \t]*" + re.escape(key.encode("ascii")) + rb"[ \t]*=")


def set_option(conf: Path, key: str, value: str = "True") -> bool:
    """Set `key` in rtx.conf, replacing it in place or appending it.

    Byte-safe on purpose: rtx.conf is the user's (or a mod author's) file, it
    is CRLF here, and it may hold anything. Every other line is passed through
    untouched, and the file's own line ending is reused.

    The awkward case, hit by hand on GTA IV: rtx.conf does NOT end with a
    newline, so a plain append produced
    "...skyIndirectRadianceScale = 3rtx.neuralUplift.enable = True" - one
    corrupt line and two options lost. A missing final newline is added first.
    """
    conf = Path(conf)
    try:
        data = conf.read_bytes() if conf.is_file() else b""
    except OSError:
        return False
    nl = b"\r\n" if b"\r\n" in data else b"\n"
    pat = _line_re(key)
    new = f"{key} = {value}".encode("ascii")
    out: list[bytes] = []
    replaced = False
    for line in data.splitlines(keepends=True):
        if pat.match(line):
            if replaced:
                continue                  # a duplicate of a key we just set
            ending = line[len(line.rstrip(b"\r\n")):] or nl
            out.append(new + ending)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and not out[-1].endswith((b"\n", b"\r")):
            out[-1] += nl                 # the no-trailing-newline case
        out.append(new + nl)
    try:
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_bytes(b"".join(out))
    except OSError:
        return False
    return True


def ends_with_newline(conf: Path) -> bool:
    """Did this file end with a line terminator? Asked before we touch it, so
    uninstall can hand the file back in the shape it arrived in."""
    try:
        data = Path(conf).read_bytes()
    except OSError:
        return True
    return (not data) or data.endswith((b"\n", b"\r"))


def remove_option(conf: Path, key: str, had_final_newline: bool = True) -> bool:
    """Take `key` back out of rtx.conf, leaving every other line alone.

    `had_final_newline` is what the file looked like before the install:
    when it did not end with one, set_option added it to append its key, and
    this takes it away again so the file comes out byte for byte as it was.
    """
    conf = Path(conf)
    try:
        data = conf.read_bytes() if conf.is_file() else b""
    except OSError:
        return False
    if not data:
        return False
    pat = _line_re(key)
    kept = [ln for ln in data.splitlines(keepends=True) if not pat.match(ln)]
    if len(kept) == len(data.splitlines(keepends=True)):
        return False
    out = b"".join(kept)
    if not had_final_newline:
        out = out.rstrip(b"\r\n")
    try:
        conf.write_bytes(out)
    except OSError:
        return False
    return True


def option_set(conf: Path, key: str) -> bool:
    """Is `key` present in rtx.conf at all? (The diagnosis asks this.)"""
    try:
        data = Path(conf).read_bytes()
    except OSError:
        return False
    pat = _line_re(key)
    return any(pat.match(ln) for ln in data.splitlines())


# Everything the runtime says about the neural pass, quoted from the fork's
# own source (Kim2091/dxvk-remix, branch gta4-atmos-dlss5,
# src/dxvk/rtx_render/rtx_neural_uplift.cpp and rtx_ngx_wrapper.cpp) and
# verified byte-for-byte against the strings in this machine's runtime.
LOADED = "[DLSS-NR] Loaded "
INITIALISED = "[DLSS-NR] Snippet initialized"
# "[DLSS-NR] Created the Neural Uplift feature (id 18, preset 0) at 1920x1080"
CREATED_RE = re.compile(
    r"\[DLSS-NR\] Created the ([\w ]+) feature \(id (\d+), preset (\d+)\)"
    r"(?: at (\S+))?")

# (phrase in the log, what to do about it). All four are the fork's wording.
FAILURES = (
    ("nvngx_dlssnr.dll could not be loaded",
     "The runtime found nvngx_dlssnr.dll but Windows refused to load it - "
     "usually a build for another card, or antivirus quarantine. Install "
     "again; the tool picks a build that matches your card."),
    ("nvngx_dlssnr.dll was not found",
     "nvngx_dlssnr.dll has to sit INSIDE the .trex folder, beside the "
     "runtime. Install again to put it back."),
    ("nvngx_dlssnr.dll not found",
     "nvngx_dlssnr.dll has to sit INSIDE the .trex folder, beside the "
     "runtime. Install again to put it back."),
    ("nvngx_dlssnr.dll does not export the Vulkan NGX entry points",
     "That nvngx_dlssnr build has no Vulkan entry points, and Remix renders "
     "on Vulkan. Install again and let the tool pick the build."),
    ("snippet failed to load",
     "The snippet did not initialise. Check nvngx_dlssnr.dll is in the .trex "
     "folder and that your driver is 570 or newer."),
    ("the caller check could not be bypassed",
     "The snippet only answers a caller whose module path contains "
     "'nvngx.dll'. remix_nvngx.dll must keep exactly that name in the .trex "
     "folder - it is what satisfies the check."),
    ("[DLSS-NR] Failed to create the Neural Uplift feature",
     "NGX refused the feature. Almost always the nvngx_dlssnr build does not "
     "match the card; install again and let the tool choose."),
)
