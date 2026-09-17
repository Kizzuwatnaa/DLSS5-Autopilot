r"""Replay a real bug report through the diagnosis, before writing a fix.

Every wrong verdict this tool has given was found the same way: take the
logs out of the report, put them in a folder the way the person had them,
and run `diagnose.analyse()` over it. Reading the rule and reasoning about
it is how the wrong ones got written in the first place.

Two ways in.

**A report pasted into a file.** Save the issue body (the whole thing, with
its ``` blocks) and point at it:

    python _tools\replay_report.py --issue 63 report.txt

It pulls out the version/gpu/route header, the ReShade.log, dlss5-feed.log,
OptiScaler.log and standalone-dlssnr.log blocks, builds a folder that looks
like that install, and prints the verdict the current code would give.

**A log file downloaded from the issue.** GitHub attachments are plain URLs:

    python _tools\replay_report.py --route upstream --reshade ReShade.log
    python _tools\replay_report.py --route feeder --feed dlss5-feed.log \
        --reshade ReShade.log --api DX12 --exe Game.exe

Then the part that matters: **change the code, run it again, and read the
verdict.** If the verdict is right for the wrong reason, it is still wrong -
check which finding fired.

Once it says what it should, copy the log lines into a test in test_all.py.
The reports are the only real inputs this project has; a rule that has never
seen one is a guess.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import diagnose   # noqa: E402
from core import remix     # noqa: E402

# The report template's own headings, so a pasted issue can be taken apart.
BLOCKS = {
    "ReShade.log": "reshade",
    "dlss5-feed.log": "feed",
    "OptiScaler.log": "opti",
    "standalone-dlssnr.log": "standalone",
    # The Remix runtime's own log is the ONLY one that applies on that
    # route, and it was not in this list: both remix reports replayed with
    # no log, no .trex and no rtx.conf, so every rule in _analyse_remix has
    # never seen a real report.
    "remix-dxvk.log": "remix",
    # The 64-bit helper of a 32-bit game, where its DLSS actually runs
    # (#252: the fault was only in this log).
    "dlss5-feed-host.log": "host",
}
ADDONS = ("dlss5-feed.addon64", "renodx-dlss5.addon64")


def _blocks(text: str) -> dict:
    """Every ```-fenced block in the report, keyed by the heading above it."""
    out: dict = {}
    for name, key in BLOCKS.items():
        m = re.search(r"\*\*" + re.escape(name) + r"[^\n]*\*\*\s*\n```[^\n]*\n(.*?)```",
                      text, re.S)
        if m and m.group(1).strip() not in ("(none)", ""):
            out[key] = m.group(1)
    return out


def _header(text: str) -> dict:
    """The head of a report, key by key.

    "work area" is in the list because it is printed there: a key the
    report carries and the replay ignores is a key nobody can reproduce
    from.
    """
    out = {}
    for k in ("version", "gpu", "game", "exe", "arch/api", "route",
              "work area"):
        m = re.search(r"^- " + re.escape(k) + r":\s*(.+)$", text, re.M)
        if m:
            out[k] = m.group(1).strip()
    return out


# The four verdicts diagnose.analyse() can only reach through the
# `complete is False` branch, which returns before anything else is read.
# A report carrying one of them proves the install record said "unfinished";
# a report carrying any OTHER verdict proves it did not.
UNFINISHED = (
    "The install never finished - install again.",
    "The install stopped for a reason of its own - see below.",
    "The drive was full - free up space and install again.",
    "The uninstall left files behind - close the game and uninstall again.",
)


def printed_verdict(text: str) -> str:
    """The verdict the tool printed on the reporter's own machine."""
    m = re.search(r"^\*\*Diagnosis\*\*:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else ""


def finished(text: str) -> dict:
    """Whether the install record in that folder said the install finished.

    Not in the report as a field, and it decides everything: the unfinished
    branch returns before any log is read. Two things in the report settle
    it - the verdict the machine printed (it proves which side of that
    branch it came out of), and, when there is none, whether the report
    carries a traceback out of the installer (#97, #103).
    """
    said = printed_verdict(text)
    if said:
        return {"complete": said not in UNFINISHED}
    crashed = re.search(r"\*\*Last error\*\*(.*?)```", text, re.S) is not None \
        and "installer.py" in text
    return {"complete": not crashed}


def folder_state(text: str) -> dict | None:
    """What the report says was in the folder, from its own file list.

    The list under "**Files in the folder**" is written by
    `diagnose._presence()` on the reporter's machine, so it is the one piece
    of the report that describes the DISK rather than a log. Until 1.8.2
    this was thrown away and every report was replayed against a folder with
    every file in place and a finished install record - so no replay could
    ever reproduce a quarantined DLL, an uninstalled folder, or a folder the
    tool had never installed into (#43, #194). The measurement said "not
    started since the install" as often as the corpus did, and the reason
    those reports got that answer stayed invisible.

    Returns None when the report has no such list (the older template), so
    those keep the old behaviour: a folder with everything in place.

    Otherwise: {"files": {name: present}, "manifest": bool}. No proxy DLL,
    no add-on, no Vulkan layer and no .trex line in the list means
    `_presence` had nothing to name them from - there was no install record
    in that folder at all.
    """
    m = re.search(r"\*\*Files in the folder\*\*\s*\n(.*?)(?:\n\*\*|\Z)", text, re.S)
    if not m:
        return None
    files: dict[str, bool] = {}
    named = False
    layer: bool | None = None
    remix: dict = {"trex": False, "files": {}, "flavour": "", "key": "",
                   "key_set": False, "swapped": False}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        name, _, state = line[2:].partition(":")
        name, state = name.strip(), state.strip()
        low = name.lower()
        # The Remix route first, and before the prefix list below: ".trex"
        # is a prefix of ".trex/d3d9.dll" too, so every runtime file was
        # swallowed by that list and the branch meant for them was dead
        # code. That is why #211 - a folder with a whole Remix runtime in
        # it - replayed as a folder nobody had ever installed into.
        if low == ".trex":
            remix["trex"] = not state.upper().startswith("MISSING")
            named = named or remix["trex"]
            continue
        if "/" in low and low.split("/")[0].endswith(".trex"):
            remix["files"][name.split("/", 1)[1]] = state.startswith("present")
            remix["trex"] = True
            named = True
            continue
        if low == "remix runtime":
            # "swapped by this install" / "the mod's own, left alone"
            remix["swapped"] = state.lower().startswith("swapped")
            named = True
            continue
        if low == "dlss 5 add-on switch":
            # "NeuralUplift=1" / "not written yet (...)"
            if "=" in state:
                files["(addon switch)"] = state.split("=", 1)[1].strip()
            named = True
            continue
        if low == "runtime flavour":
            # "neural", "uplift", or "no DLSS 5 pass" for a runtime with none.
            remix["flavour"] = "" if state.startswith("no ") else state
            named = True
            continue
        if low.startswith("rtx."):
            # The conf key, printed only when the install record carries one.
            remix["key"] = name
            remix["key_set"] = state.lower().startswith("set")
            named = True
            continue
        # Lines that describe a registration or a folder rather than a file
        # beside the game: they answer "is the install recorded", not "is
        # this file on disk", and there is nothing to create for them.
        if name.lower().startswith(("reshade openxr", "reshade ",
                                    "optiscaler build",
                                    "the game's own upscaler")):
            if "vulkan layer" in name.lower():
                # "registered" / "NOT REGISTERED": the layer is a registry
                # entry, so this line is the only place a replay can learn
                # what the loader on that machine had.
                layer = not state.upper().startswith("NOT")
                named = True
            else:
                named = named or "registered" in state.lower()
            continue
        files[name] = state.startswith("present") or state.startswith("not written")
        if re.match(r"(dxgi|d3d9|d3d10|d3d11|d3d12|opengl32|winmm|version|"
                    r"dinput8|nvngx)\.dll$", name, re.I) or ".addon" in name.lower():
            named = True
    if not files and not named:
        return None
    proxy = next((n for n in files if re.match(
        r"(dxgi|d3d9|d3d10|d3d11|d3d12|opengl32|winmm|version|dinput8)\.dll$",
        n, re.I)), "")
    if not proxy and (layer is not None or "(vulkan layer)" in files):
        proxy = "(vulkan layer)"                  # diagnose.VULKAN_LAYER
        if layer is None:
            layer = files.get("(vulkan layer)", False)
        files.pop("(vulkan layer)", None)
    return {"files": files, "manifest": named, "proxy": proxy, "layer": layer,
            "remix": remix if (remix["trex"] or remix["key"]) else None,
            "addon_switch": files.pop("(addon switch)", None)}


def build(route: str, api: str, exe: str, logs: dict, bitness: int = 64,
          extra_manifest: dict | None = None,
          state: dict | None = None, work_area: str = "") -> Path:
    d = Path(tempfile.mkdtemp(prefix="replay_"))
    proxy = "dxgi.dll"
    files = [proxy] + (list(ADDONS) if route == "feeder" else [])
    on_disk = files + ["ReShade.ini", "nvngx_dlssnr.dll",
                       "reshade-shaders/Shaders/DLSS5_Feed.fx",
                       "reshade-shaders/Shaders/lumenite_Kernel.fx"]
    write_manifest = True
    if state is not None:
        # The report's own list has the last word on both questions: which
        # files were there, and whether the install was recorded at all.
        # What the install WROTE stays the default set - a file the report
        # calls MISSING is one that was written and has since gone, which is
        # exactly what `diagnose._missing_core` is there to notice.
        on_disk = [n for n, there in state["files"].items() if there]
        # No proxy in the list means the install did not write one under a
        # name this can see - guessing dxgi.dll there invents a file that is
        # "missing" and answers the wrong question (#16, #19: both Vulkan).
        proxy = state["proxy"]
        files = sorted(set(state["files"]) | ({proxy} if proxy else set()))
        write_manifest = bool(state["manifest"])
    man = {"version": 1, "complete": True, "exe": exe, "bitness": bitness,
           "api": api, "proxy": proxy, "path": route, "files": files}
    # A D3D proxy name AND a registered ReShade Vulkan layer is the DXVK
    # shape: the install record says dxvk and the proxy is the layer. Read
    # as a plain d3d9.dll proxy, #238 replayed with "the proxy is reached".
    if state is not None and state.get("layer") is not None \
            and re.match(r"(d3d9|d3d10|d3d11|dxgi)\.dll$", proxy or "", re.I):
        man["dxvk"] = True
        man["proxy"] = diagnose.VULKAN_LAYER
    rx = (state or {}).get("remix")
    if rx:
        man["remix"] = {"key": rx["key"], "conf": remix.CONF} if rx["key"] else {}
        if rx.get("swapped"):
            comp = dict(man.get("components") or {})
            comp["remix_runtime"] = "swapped"
            man["components"] = comp
    man.update(extra_manifest or {})
    if write_manifest:
        (d / "dlss5-autopilot.json").write_text(json.dumps(man), encoding="utf8")
    for n in on_disk:
        p = d / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"MZ")
    sh = d / "reshade-shaders" / "Shaders"
    sh.mkdir(parents=True, exist_ok=True)
    # The add-on's own switch, as the report recorded it: the diagnosis
    # reads ReShade.ini for it, so the replay has to write one.
    switch = (state or {}).get("addon_switch")
    if switch is not None:
        from core import reshade_ini as _ini
        (d / "ReShade.ini").write_text(
            f"[{_ini.ADDON_SECTION}]\n{_ini.ADDON_SWITCH}={switch}\n",
            encoding="utf8")
    for key, name in (("reshade", "ReShade.log"), ("feed", "dlss5-feed.log"),
                      ("opti", "OptiScaler.log")):
        if logs.get(key):
            (d / name).write_text(logs[key], encoding="utf8")
    if logs.get("host"):
        p = d / diagnose.HOST_LOG
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(logs["host"], encoding="utf8")
    if rx:
        _build_remix(d, rx)
    if logs.get("remix"):
        p = d / remix.LOG
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(logs["remix"], encoding="utf8")
    if work_area:
        digits = re.search(r"(\d+)", work_area)
        if digits:
            n = int(digits.group(1))
            if route == "optiscaler":
                (d / "OptiScaler.ini").write_text(
                    f"[DlssNr]\nWorkingScale={n / 100:.3f}\n", encoding="utf8")
            else:
                cfg = d / "dlss5-feed.cfg"
                was = cfg.read_text(encoding="utf8") if cfg.exists() else ""
                if "work_resolution" not in was:
                    cfg.write_text(was + f"work_resolution={n}\n",
                                   encoding="utf8")
    return d


def _build_remix(d: Path, rx: dict) -> None:
    """Put the Remix runtime back, as the report describes it.

    `runtime_flavour()` reads the fork's option prefix out of the runtime
    binary itself, so the flavour the report printed is written INTO the
    stand-in d3d9.dll - a zero-byte file would be read as a runtime with no
    neural pass, which is a different verdict and the wrong one.
    """
    if not rx["trex"]:
        return
    trex = d / remix.TREX
    trex.mkdir(parents=True, exist_ok=True)
    present = dict(rx["files"])
    # The runtime DLL is what `find_runtime` looks for: no report describes
    # a .trex without one (`_presence` prints nothing else when it is gone).
    present.setdefault(remix.RUNTIME_DLL, True)
    for name, there in present.items():
        if not there:
            continue
        body = b"MZ"
        if name == remix.RUNTIME_DLL and rx["flavour"]:
            body += remix.PREFIX.get(rx["flavour"], "").encode("ascii")
        (trex / name).write_bytes(body)
    if rx["key"] and rx["key_set"]:
        (d / remix.CONF).write_text(f"{rx['key']} = True\n", encoding="utf8")


_LAST_ERROR_RE = re.compile(r"\*\*Last error\*\*\s*```(.*?)```", re.S)


def last_error(text: str) -> str:
    """The tool's own traceback, as the report carries it.

    `analyse()` reads it for a folder nothing arrived in - an install that
    crashed leaves the same empty folder as one that never happened - so a
    replay that does not hand it over answers a different question.
    """
    m = _LAST_ERROR_RE.search(text)
    return m.group(1).strip() if m else ""


@contextmanager
def machine(text: str, d: Path):
    """Make the answer depend on the report, not on the PC replaying it.

    Three things the diagnosis asks the machine for: where the standalone
    add-on's log is (outside the game folder), which Vulkan layers are
    registered (the registry), and the driver version. All three come out
    of the report instead. This used to live in verdict_check only, so the
    same report replayed by hand - through this file, the one whose whole
    job is "replay a report before writing a fix" - got a DIFFERENT verdict
    from the one the corpus measured (#212 read as "no log yet").
    """
    logs = _blocks(text)
    state = folder_state(text)
    sa = d / "standalone-dlssnr.log"
    if logs.get("standalone"):
        sa.write_text(logs["standalone"], encoding="utf8")
    layer = (state or {}).get("layer")
    reg = (True, True) if layer is None else (layer, layer)
    drv = re.search(r"driver\s+([\d.]+)", _header(text).get("gpu", ""))
    from core import gpu as _gpu
    from core import watch as _watch
    from core import vulkan as _vk
    # What the watcher saw on their machine, put where the diagnosis reads
    # it. The block was printed in every 1.9.0 report and the replay never
    # read it, so #238's "another dxgi.dll" answer could not be reproduced.
    rec_file = d / "_sightings.json"
    seen = sighting(text)
    man_file = d / "dlss5-autopilot.json"
    if seen and man_file.is_file():
        # Installed two minutes before it was seen, so the record is read as
        # evidence about this install - on any day the replay is run.
        os.utime(man_file, (seen["at"] - 120, seen["at"] - 120))
    rec_file.write_text(json.dumps(
        {os.path.normcase(str(d)): seen} if seen else {}),
        encoding="utf8")
    with patch.object(diagnose.model, "STANDALONE_LOG", sa), \
            patch.object(diagnose.model, "_layer_state", lambda man: reg), \
            patch.object(_watch, "RECORD", rec_file), \
            patch.object(_watch, "inspect", lambda *a, **k: []), \
            patch.object(_vk, "name_clash", lambda: None), \
            patch.object(_gpu, "driver_version",
                         lambda: drv.group(1) if drv else None):
        yield


def _seen_at(text: str) -> float:
    """When the report says the game was seen, as a fixed timestamp.

    Not the replay's clock: a verdict that prints the time would then move
    on every run, and verdict_check reads that as a changed answer.
    """
    from datetime import datetime
    m = re.search(r"\(seen at (\d{1,2} \w{3} \d{2}:\d{2})\)", text)
    y = re.search(r"^(20\d\d)-\d\d-\d\d \d\d:\d\d", text, re.M)
    try:
        return datetime.strptime(f"{m.group(1)} {y.group(1) if y else 2026}",
                                 "%d %b %H:%M %Y").timestamp()
    except (AttributeError, ValueError):
        return datetime(2026, 1, 1).timestamp()


def sighting(text: str) -> dict:
    """The report's "What ran, and what it loaded" block, as a record."""
    m = re.search(r"\*\*What ran, and what it loaded\*\*[^\n]*\n(.*?)(?:\n\s*\n|\Z)",
                  text, re.S)
    if not m:
        return {}
    rec = {"at": _seen_at(text), "exe": "", "name": "", "refused": "", "modules": 0,
           "ours": [], "elsewhere": [], "missing": []}

    def names(v):
        return [x.strip() for x in v.split(",")
                if x.strip() and x.strip() != "none"]

    for line in m.group(1).splitlines():
        key, _, val = line.strip().lstrip("- ").partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "process":
            rec["name"] = val
        elif key == "dll list":
            rec["refused"] = val.split(" - ", 1)[-1] or "refused"
        elif key == "dlls in the process":
            rec["modules"] = int(re.sub(r"\D", "", val) or 0)
        elif key == "ours, loaded":
            rec["ours"] = names(val)
        elif key == "same name, loaded from elsewhere":
            rec["elsewhere"] = names(val)
        elif key.startswith(("ours, not loaded", "also written")):
            rec["missing"] += names(val)
    return rec


def show(d: Path, label: str, text: str = "") -> None:
    if text:
        with machine(text, d):
            rep = diagnose.analyse(d, last_error(text))
    else:
        rep = diagnose.analyse(d)
    print("=" * 78)
    print(label)
    print("=" * 78)
    print(f"  ran: {rep.ran}   route: {rep.route or '(none)'}")
    print(f"  VERDICT: {rep.verdict}")
    for f in rep.findings:
        print(f"    [{f.level:4}] {f.title}")
        if f.detail:
            print(f"           {f.detail[:150]}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("report", nargs="?", help="a saved issue body")
    p.add_argument("--issue", default="?")
    p.add_argument("--route", default="feeder")
    p.add_argument("--api", default="DX12")
    p.add_argument("--exe", default="Game.exe")
    p.add_argument("--bitness", type=int, default=64)
    p.add_argument("--reshade"), p.add_argument("--feed"), p.add_argument("--opti")
    p.add_argument("--keep", action="store_true", help="leave the folder behind")
    a = p.parse_args()

    logs, route, api, exe, state, text = {}, a.route, a.api, a.exe, None, ""
    if a.report:
        text = Path(a.report).read_text(encoding="utf8", errors="replace")
        logs = _blocks(text)
        state = folder_state(text)
        head = _header(text)
        route = head.get("route", route)
        exe = head.get("exe", exe)
        if "arch/api" in head:
            m = re.search(r"(DX9|DX10|DX11|DX12|Vulkan|OpenGL)", head["arch/api"])
            if m:
                api = m.group(1)
            if "32-bit" in head["arch/api"]:
                a.bitness = 32
        print("report header:", ", ".join(f"{k}={v}" for k, v in head.items()) or "(none)")
        print("log blocks found:", ", ".join(logs) or "(none)")
    for key, path in (("reshade", a.reshade), ("feed", a.feed), ("opti", a.opti)):
        if path:
            logs[key] = Path(path).read_text(encoding="utf8", errors="replace")
    if not logs and state is None:
        print("nothing to replay: pass a saved report, or --reshade/--feed/--opti")
        return 2
    if state is not None:
        gone = [n for n, there in state["files"].items() if not there]
        print(f"folder: {len(state['files']) - len(gone)} file(s) present"
              + (f", gone: {', '.join(gone)}" if gone else "")
              + ("" if state["manifest"] else ", NO install record"))

    # The same folder verdict_check builds, including whether the install
    # record said "finished" - without it a report whose install died
    # replayed one way here and another way in the corpus measurement.
    d = build(route, api, exe, logs, a.bitness, state=state,
              extra_manifest=finished(text) if a.report else None,
              work_area=(_header(text).get("work area", "") if a.report
                         else ""))
    show(d, f"issue #{a.issue}  route={route}  api={api}  exe={exe}",
         text if a.report else "")
    if a.keep:
        print(f"\nfolder kept: {d}")
    else:
        shutil.rmtree(d, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
