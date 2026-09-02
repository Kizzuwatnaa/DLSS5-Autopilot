r"""Named settings profiles: save the dials that worked for one game, apply
them to another with one click.

A profile is a small JSON file under the settings folder:

    %LOCALAPPDATA%\dlss5-autopilot\profiles\<safe-name>.json

Only the GAME-INDEPENDENT fields of installer.Options go in. Three fields are
left out on purpose and are never overwritten by apply():

    native_dlss          - whether THIS game ships DLSS; found per game
    renodx_local         - a path on this machine, meaningless elsewhere
    ignore_gpu_mismatch  - a one-off override, not a preference

Three presets (Quality / Balanced / Performance) are always listed but never
written to disk: they are the two "cost" dials most people actually move, and
the same names the games themselves use, so they need no explanation.
"""
from __future__ import annotations

import json
import re
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path

from . import dlss, feedcfg, optiscaler, prefs, reshade_ini
from .installer import Options

# Next to settings.json, NOT prefs.app_dir(): the app folder is read-only in
# a portable install and gets wiped by a self-update.
DIR = prefs.FILE.parent / "profiles"

# The Options fields a profile carries - everything else is per game or per
# machine. Order matters only for how the JSON reads.
FIELDS = ("path", "provider", "renodx", "dlssnr", "dlss", "keep_game_dlss",
          "feed", "nr", "feeder_prerelease", "feeder_tag", "reshade_proxy",
          "opti_proxy", "dxvk")

# work_resolution is the feeder's only cost dial (the route is always DLAA -
# see feedcfg). OptiScaler's WorkingScale is quadratic in cost, which is why
# its steps are wider than the feeder's for the same three names.
BUILTINS: dict[str, dict] = {
    "Quality":     {"feed": {"work_resolution": 100}, "nr": {"WorkingScale": 1.0, "Preset": 0}},
    "Balanced":    {"feed": {"work_resolution": 85},  "nr": {"WorkingScale": 0.75, "Preset": 0}},
    "Performance": {"feed": {"work_resolution": 70},  "nr": {"WorkingScale": 0.5, "Preset": 0}},
}


def is_builtin(name: str) -> bool:
    return name in BUILTINS


def _safe_name(name: str) -> str:
    """A file name Windows will accept, whatever the user typed.

    Anything outside the ASCII letters/digits/space/-/_ set is dropped, so a
    name made of only odd characters is still given a stable fallback name
    rather than an empty file stem.
    """
    s = re.sub(r"[^A-Za-z0-9 _\-]+", "_", name.strip()).strip(" ._")
    s = re.sub(r"_+", "_", s)
    return (s or "profile")[:60]


def _path(name: str) -> Path:
    return DIR / f"{_safe_name(name)}.json"


def list_profiles() -> list[str]:
    """Display names of every profile, built-ins first, then files by name."""
    names: list[str] = []
    try:
        for p in sorted(DIR.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf8"))
                n = str(data.get("name") or p.stem)
            except (OSError, json.JSONDecodeError, AttributeError):
                n = p.stem
            if n not in names and not is_builtin(n):
                names.append(n)
    except OSError:
        pass
    return list(BUILTINS) + sorted(names, key=str.lower)


def _to_dict(opt: Options) -> dict:
    out = {}
    for f in FIELDS:
        v = getattr(opt, f)
        out[f] = dict(v) if isinstance(v, dict) else v
    return out


def save(name: str, opt: Options) -> Path:
    if is_builtin(name):
        raise ValueError(f"'{name}' is a built-in preset and cannot be replaced.")
    from .update import VERSION
    data = {"name": name.strip(),
            "saved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "app_version": VERSION}
    data.update(_to_dict(opt))
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
    return p


def _from_dict(data: dict) -> Options:
    """Options from a JSON dict: unknown keys ignored, missing keys default.

    Types are coerced because a hand-edited file is the most likely source
    of a wrong one, and a str where an int is expected would only blow up
    much later inside the installer.
    """
    defaults = Options()
    kw: dict = {}
    for f in FIELDS:
        if f not in data:
            continue
        v = data[f]
        d = getattr(defaults, f)
        if isinstance(d, bool):
            kw[f] = bool(v)
        elif isinstance(d, int):
            kw[f] = int(v)
        elif isinstance(d, dict):
            kw[f] = dict(v) if isinstance(v, dict) else {}
        elif f in ("renodx", "dlssnr", "dlss"):
            kw[f] = None if v in (None, "") else str(v)
        else:
            kw[f] = str(v) if v is not None else d
    path = kw.get("path", defaults.path)
    if path not in dlss.ALL_ROUTES:
        raise ValueError(f"The profile asks for route '{path}', which this "
                         f"version does not know. Known routes: "
                         f"{', '.join(dlss.ALL_ROUTES)}.")
    return Options(**kw)


def load(name: str) -> Options:
    if is_builtin(name):
        return Options(**{k: dict(v) for k, v in BUILTINS[name].items()})
    p = _path(name)
    try:
        data = json.loads(p.read_text(encoding="utf8"))
    except OSError:
        raise ValueError(f"No profile named '{name}'.")
    except json.JSONDecodeError as e:
        raise ValueError(f"Profile '{name}' is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ValueError(f"Profile '{name}' does not hold settings.")
    return _from_dict(data)


def delete(name: str) -> None:
    if is_builtin(name):
        raise ValueError(f"'{name}' is a built-in preset and cannot be deleted.")
    try:
        _path(name).unlink()
    except FileNotFoundError:
        pass


def apply(base: Options, prof: Options) -> Options:
    """base with the profile's fields overlaid; per-game fields untouched."""
    kw = {f: getattr(prof, f) for f in FIELDS}
    kw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in kw.items()}
    return replace(base, **kw)


def describe(opt: Options) -> list[str]:
    """Short lines for the GUI: only what differs from a plain install."""
    out = [f"route {opt.path}"]
    if opt.path in (dlss.FEEDER,):
        label = reshade_ini.PROVIDERS.get(opt.provider, ("?",))[0]
        out.append(f"provider {opt.provider} ({label.split(' (')[0]})")
    wr = opt.feed.get("work_resolution")
    if wr is not None:
        out.append(f"work resolution {int(wr)}%")
    pr = int(opt.feed.get("preset", 0) or 0)
    if pr:
        out.append(f"feed preset {feedcfg.PRESETS.get(pr, pr)}".split(" (")[0])
    hd = int(opt.feed.get("hdr", -1))
    if hd != -1:
        out.append(f"hdr {feedcfg.HDR.get(hd, hd)}")
    di = int(opt.feed.get("depth_inverted", -1))
    if di != -1:
        out.append(f"depth {feedcfg.DEPTH.get(di, di)}")
    ws = opt.nr.get("WorkingScale")
    if ws is not None:
        out.append(f"model resolution {int(round(float(ws) * 100))}%")
    pr = opt.nr.get("Preset")
    if pr:
        out.append(f"model preset {optiscaler.NR_PRESETS.get(int(pr), pr)}")
    st = opt.nr.get("Style")
    if st:
        out.append(f"style {optiscaler.NR_STYLES.get(int(st), st)}")
    if opt.dlssnr:
        out.append(f"dlssnr {opt.dlssnr}")
    if opt.dlss:
        out.append(f"dlss {opt.dlss}")
    if opt.renodx:
        out.append(f"renodx {opt.renodx}")
    if not opt.keep_game_dlss:
        out.append("replace the game's own DLSS")
    if opt.feeder_tag:
        out.append(f"feeder {opt.feeder_tag}")
    elif opt.feeder_prerelease:
        out.append("feeder pre-release")
    if opt.reshade_proxy:
        out.append(f"reshade as {opt.reshade_proxy}")
    if opt.opti_proxy:
        out.append(f"optiscaler as {opt.opti_proxy}")
    if opt.dxvk:
        out.append("dxvk")
    return out


# Guard against a field being added to Options without a decision here.
assert set(FIELDS) <= {f.name for f in fields(Options)}
