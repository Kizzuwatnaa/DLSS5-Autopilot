"""Writing dlss5-feed.cfg.

The add-on creates this file itself, but writing it up front means the first
launch already uses the right settings. Keys were verified against the
dlss5-feed.addon64 binary and the DLSS5-Feeder documentation.

ABOUT "DLSS Performance mode":
    The feeder path is always DLAA and cannot be anything else. The reason is
    architectural: DLSS5-Feeder never sees the game's low-resolution render,
    it sees the FINISHED full-resolution frame at the end of the ReShade
    chain. There is no low-resolution source to upscale from, so Quality /
    Balanced / Performance are meaningless here - which is why the log always
    says "DLAA".

    The real performance knob is work_resolution (below): it shrinks the area
    the neural pass runs over, between 50% and 100%.
"""
from __future__ import annotations

from pathlib import Path

NAME = "dlss5-feed.cfg"

# DLSS preset hint. Per the DLSS5-Feeder troubleshooting table: if you see
# warping around flames or transparent objects, try 5 or 6 (the older CNN).
PRESETS = {
    0:  "Default (let the add-on decide)",
    5:  "Preset E - legacy CNN (helps with flame/transparency warping)",
    6:  "Preset F - legacy CNN",
    10: "Preset J - transformer",
    11: "Preset K - transformer (newest)",
}

HDR = {-1: "Auto", 0: "Force SDR", 1: "Force HDR"}
DEPTH = {-1: "Follow ReShade", 0: "Force non-inverted", 1: "Force inverted"}
MODE = {2: "Full DLSS (normal)", 1: "Transport test only", 0: "Off"}


def defaults() -> dict:
    return {
        "enabled": 1,
        "mode": 2,
        "hdr": -1,
        "depth_inverted": -1,
        "flags": -1,
        "reset_every": 0,
        "warmup_rebuild": 180,
        "rebuild": 0,
        "log_frames": 3,
        "create_delay": 60,
        "preset": 0,
        "work_resolution": 100,
        "mv_scale_x": 1.0,
        "mv_scale_y": 1.0,
    }


def number(v, fallback: float = 0.0) -> float:
    """A number out of a config value, whatever locale wrote it.

    This file is written by the add-on as well as by us, and the add-on is
    C++: its printf follows whatever locale the host process has set. A game
    or emulator that calls setlocale - PCSX2 does, through Qt - makes it
    write `mv_scale_x=1,000`, and reading that back with float() raised
    ValueError out of the middle of an install, so nothing was installed at
    all (issue #69). A comma where a full stop belongs is not a reason to
    fail an install.
    """
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    for attempt in (s, s.replace(",", ".")):
        try:
            return float(attempt)
        except ValueError:
            continue
    return fallback


def read(path: Path) -> dict:
    out: dict = {}
    try:
        for line in path.read_text(encoding="utf8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")) or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def write(dir_: Path, settings: dict | None = None, host_window: bool | None = None) -> Path:
    """Create/update dlss5-feed.cfg, preserving keys we do not manage."""
    p = dir_ / NAME
    cur = defaults()
    cur.update({k: v for k, v in read(p).items()})      # the user's existing values
    if settings:
        cur.update(settings)
    if host_window is not None:
        cur["host_window"] = 1 if host_window else 0

    base = defaults()
    lines = []
    for k, v in cur.items():
        if isinstance(v, float) or k.startswith("mv_scale"):
            lines.append(f"{k}={number(v, float(base.get(k) or 0)):.3f}")
        else:
            lines.append(f"{k}={v}")
    p.write_text("\n".join(lines) + "\n", encoding="utf8")
    return p


def describe(settings: dict) -> list[str]:
    """Human-readable summary lines for the log."""
    out = []
    # Every value here can have come back out of the .cfg the add-on writes,
    # so none of them is trusted to be a number this locale can parse (#69).
    wr = int(number(settings.get("work_resolution", 100), 100))
    if wr != 100:
        out.append(f"work_resolution={wr}% (smaller neural work area - "
                   f"higher fps, slightly less detail)")
    pr = int(number(settings.get("preset", 0)))
    if pr:
        out.append(f"preset={pr} ({PRESETS.get(pr, '?')})")
    hd = int(number(settings.get("hdr", -1), -1))
    if hd != -1:
        out.append(f"hdr={hd} ({HDR.get(hd)})")
    di = int(number(settings.get("depth_inverted", -1), -1))
    if di != -1:
        out.append(f"depth_inverted={di} ({DEPTH.get(di)})")
    for ax in ("x", "y"):
        v = number(settings.get(f"mv_scale_{ax}", 1.0), 1.0)
        if abs(v - 1.0) > 1e-6:
            out.append(f"mv_scale_{ax}={v:.3f}")
    return out


# --------------------------------------------------------------- bridge cfg

BRIDGE_NAME = "dlss5-bridge.cfg"

# The bridge's own cost knobs. Its synthetic contract runs the NVIDIA driver's
# hardware optical flow engine instead of a ReShade shader, so it is already
# cheaper than the feeder - but the grid size and performance level move it
# further either way.
OFA_GRID = {
    2: "2 - default, balanced",
    4: "4 - coarser grid, cheapest",
    1: "1 - finest grid, most expensive",
    0: "0 - optical flow off (no motion vectors)",
}
OFA_PERF = {
    20: "fast - default",
    10: "medium",
    5:  "slow - best quality, most expensive",
}


def bridge_defaults(native_dlss: bool) -> dict:
    """Sensible starting point. synth_after only matters without native DLSS."""
    # Said either way: a folder first installed as a game without DLSS keeps
    # its synth_after through a later install that found the game's DLSS,
    # and the substitute then costs the mirror the whole session.
    return {"vk_mirror": 1, "synth_after": 0 if native_dlss else 3}


# From 1.4.0 the bridge replaces, at attach and before reading it, any
# settings file whose first line is not "# dlss5-bridge <its own version>"
# or "# dlss5-bridge keep" - with its built-in defaults, synth_after=0 among
# them. An unstamped file from here was therefore replaced the first time
# the bridge ran after an install, and a game with no DLSS of its own never got the substitute
# contract synth_after asks for (#127). "keep" is the bridge's own way of
# saying the file is meant to outlive a version.
BRIDGE_STAMP = "# dlss5-bridge keep"
# What survives from a file the bridge wrote itself (stamped with its own
# version): the choices a person makes in its panel or by hand. The rest of such a file
# is that version's full set of defaults, and "keep" would freeze them
# across every later bridge - which is what its regeneration exists to stop.
# Carried only when they differ from the bridge's own default: a dump
# holds every one of them, and a default carried under "keep" is a default
# frozen.
BRIDGE_CARRY = {"ofa_grid": "2", "ofa_perf": "20", "mv_sign_x": "0",
                "mv_sign_y": "0", "source": "auto", "synth": "0",
                # set by hand, and the bridge's README names unwrap=2 as the
                # value to try when a session that should work does not
                "unwrap": "1", "vk_sync": "0", "vk_present": "0"}


def _chosen(k: str, v) -> bool:
    """A panel value that is not the bridge's default."""
    base = BRIDGE_CARRY[k]
    if k == "source":
        return str(v).strip().lower() != base
    return number(v, float(base)) != float(base)


def write_bridge(dir_: Path, settings: dict | None = None) -> Path:
    """Create dlss5-bridge.cfg, preserving anything already in it."""
    p = dir_ / BRIDGE_NAME
    cur: dict = {}
    old = read(p)
    try:
        first = p.read_text(encoding="utf8", errors="replace").split("\n", 1)[0].strip()
    except OSError:
        first = ""
    if first.startswith("# dlss5-bridge ") and first != BRIDGE_STAMP:
        old = {k: v for k, v in old.items()
               if k in BRIDGE_CARRY and _chosen(k, v)}
    cur.update(old)
    if settings:
        cur.update(settings)
    p.write_text(BRIDGE_STAMP + "\n"
                 + "# written by dlss5-autopilot; the line above keeps it "
                   "across bridge versions\n"
                 + "\n".join(f"{k}={v}" for k, v in cur.items()) + "\n",
                 encoding="utf8")
    return p


def describe_bridge(settings: dict) -> list[str]:
    out = []
    if int(number(settings.get("synth_after", 0))):
        out.append(f"synth_after={settings['synth_after']} (synthetic contract "
                   f"armed - the game has no DLSS of its own)")
    g = settings.get("ofa_grid")
    if g is not None and int(number(g, 2)) != 2:
        out.append(f"ofa_grid={g} ({OFA_GRID.get(int(number(g, 2)), '?')})")
    pf = settings.get("ofa_perf")
    if pf is not None and int(number(pf, 20)) != 20:
        out.append(f"ofa_perf={pf} ({OFA_PERF.get(int(number(pf, 20)), '?')})")
    return out
