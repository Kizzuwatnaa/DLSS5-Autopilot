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

    lines = []
    for k, v in cur.items():
        if isinstance(v, float) or k.startswith("mv_scale"):
            lines.append(f"{k}={float(v):.3f}")
        else:
            lines.append(f"{k}={v}")
    p.write_text("\n".join(lines) + "\n", encoding="utf8")
    return p


def describe(settings: dict) -> list[str]:
    """Human-readable summary lines for the log."""
    out = []
    wr = int(settings.get("work_resolution", 100))
    if wr != 100:
        out.append(f"work_resolution={wr}% (smaller neural work area - "
                   f"higher fps, slightly less detail)")
    pr = int(settings.get("preset", 0))
    if pr:
        out.append(f"preset={pr} ({PRESETS.get(pr, '?')})")
    hd = int(settings.get("hdr", -1))
    if hd != -1:
        out.append(f"hdr={hd} ({HDR.get(hd)})")
    di = int(settings.get("depth_inverted", -1))
    if di != -1:
        out.append(f"depth_inverted={di} ({DEPTH.get(di)})")
    for ax in ("x", "y"):
        v = float(settings.get(f"mv_scale_{ax}", 1.0))
        if abs(v - 1.0) > 1e-6:
            out.append(f"mv_scale_{ax}={v:.3f}")
    return out
