r"""Are the words the diagnosis reads still in the programs that write them?

Every verdict this tool gives about somebody else's log is a string match
against a build of theirs. Those strings move: OptiScaler's DLSS-NR line
changed from "cost:" to "elapsed:" between two releases and the tool spent
a week telling people with a working install that neural rendering had
never started (#168). A report is a slow way to find that out.

So: download the current build of each component, look for every phrase the
diagnosis hinges on, and print the ones that are gone. A phrase that is
GONE is either a rule that can no longer fire or one that now fires on
nothing - both are wrong answers waiting to happen.

    python _tools/phrase_check.py

Phrases are format strings in the binaries, so each entry here is the
fixed part of one - the part that cannot change with the values. When a
phrase goes missing, find what replaced it in the new build and match the
SHAPE rather than the words where you can (a dispatch line with a duration
on it, not the word "cost").
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import diagnose, net, optiscaler, sources   # noqa: E402

# component -> [(fragment in the binary, phrase in diagnose.py)]
#
# A log line is a format string: "DLSS5_Feed.fx technique found" is printed
# from "technique %s" and never appears whole in the build. So each entry
# carries the FIXED part that has to be in the binary, and the phrase the
# diagnosis actually matches - both are checked, so this table cannot drift
# from either side.
PHRASES = {
    "standalone-dlssnr (DLSS5-Reshade-AIO)": [
        (" attached; requested profile=", " attached; requested profile="),
        ("required private runtime dependency missing",
         "required private runtime dependency missing"),
        ("NGX core: no _nvngx.dll found", "NGX core: no _nvngx.dll found"),
        ("same-frame VORT optical flow", "same-frame VORT optical flow"),
        ("zero-motion", "zero-motion"),
        ("fallback guides", "fallback guides"),
        ("DLSS-G runtime unavailable", "DLSS-G runtime unavailable"),
        ("falling back to real frames", "falling back to real frames"),
        ("frame generation disabled", "frame generation disabled"),
        ("native presentation", "native presentation"),
        ("waiting for a valid", "waiting for a valid"),
        ("shared frame", "shared frame"),
    ],
    "DLSS5-Feeder": [
        ("host spawned", "host spawned"),
        ("host connected", "host connected"),
        ("feature ready", "feature ready"),
        ("evaluate raised", "evaluate raised"),
        ("CRASH RECORDED", "CRASH RECORDED"),
        ("an external frame pacer is presenting this swapchain:",
         "an external frame pacer is presenting this swapchain:"),
        # printed as "<name> technique %s" and "SuperSampling.Available=%d"
        ("technique %s", "technique (found|MISSING)"),
        ("SuperSampling.Available", "SuperSampling.Available=1"),
        ("has not resolved yet", "has not resolved yet"),
        ("is not loaded", "is not loaded"),
    ],
    "OptiScaler (DLSS-NR fork)": [
        ("DlssNr_Dx12::Dispatch", "DlssNr_Dx12::Dispatch"),
        ("forwarder loaded", "forwarder loaded"),
        ("Vulkan is creating swapchain", "Vulkan is creating swapchain"),
    ],
    "renodx-dlss5": [
        # The 616.64+ verdict hangs on this pair: the add-on's own hook
        # failure line, and the name it registers under in ReShade.
        ("EvaluateFeature", "vtable::Hook"),
        ("DLSS 5 Neural Rendering", "DLSS 5 Neural Rendering"),
    ],
}


def _files(archive: Path):
    """Every binary inside an archive, as bytes."""
    out = b""
    try:
        with zipfile.ZipFile(archive) as z:
            for n in z.namelist():
                if n.lower().endswith((".dll", ".addon64", ".addon32", ".exe",
                                       ".asi")):
                    out += z.read(n)
    except (zipfile.BadZipFile, OSError) as e:
        print(f"   !! cannot read {archive.name}: {e}")
    return out


def _archives() -> dict:
    """{component: downloaded archive}, skipping what cannot be fetched."""
    got: dict = {}
    try:
        tag, urls = sources.resolve_standalone()
        u = urls.get(sources.STANDALONE_ZIP)
        if u:
            got["standalone-dlssnr (DLSS5-Reshade-AIO)"] = (
                tag, net.download(u, f"phrasecheck-aio-{tag}.zip"))
    except Exception as e:
        print(f"   !! standalone: {e}")
    try:
        tag, assets = sources.resolve_feeder()
        u = next((v for k, v in assets.items() if k.lower().endswith(".zip")), "")
        if u:
            got["DLSS5-Feeder"] = (tag, net.download(u, f"phrasecheck-feeder-{tag}.zip"))
    except Exception as e:
        print(f"   !! feeder: {e}")
    try:
        cat = sources.rhi_catalog().get("renodx") or []
        if cat:
            got["renodx-dlss5"] = (cat[0]["label"],
                                   net.download(cat[0]["url"],
                                                f"phrasecheck-renodx-{cat[0]['label']}.zip"))
    except Exception as e:
        print(f"   !! renodx: {e}")
    try:
        tag, u = optiscaler.resolve()
        if u.lower().endswith(".zip"):
            got["OptiScaler (DLSS-NR fork)"] = (
                tag, net.download(u, f"phrasecheck-opti-{tag}.zip"))
        else:
            print("   .. OptiScaler ships a .7z here; unpack it with the "
                  "installer's own path if this needs checking")
    except Exception as e:
        print(f"   !! optiscaler: {e}")
    return got


def main() -> int:
    dsrc = Path(__file__).resolve().parent.parent / "core" / "diagnose.py"
    dtext = dsrc.read_text(encoding="utf8", errors="replace")
    bad = 0
    print("=" * 78)
    print("PHRASES THE DIAGNOSIS READS, IN THE BUILDS THAT WRITE THEM")
    print("=" * 78)
    got = _archives()
    for comp, phrases in PHRASES.items():
        if comp not in got:
            print()
            print(f"  {comp}: not fetched, skipped")
            continue
        tag, archive = got[comp]
        blob = _files(archive)
        print()
        print(f"  {comp}  {tag}  ({len(blob)} bytes of binary)")
        for frag, phrase in phrases:
            in_bin = (frag.encode() in blob
                      or frag.encode("utf-16-le") in blob)
            in_code = phrase in dtext
            if in_bin and in_code:
                continue
            bad += 1
            why = []
            if not in_bin:
                why.append(f"{frag!r} is GONE from the build")
            if not in_code:
                why.append(f"{phrase!r} is no longer read by diagnose.py")
            print(f"     !! {'; '.join(why)}")
    print()
    if bad:
        print(f"{bad} phrase(s) to look at - a rule reads words that are not "
              f"there any more, or this table is out of date.")
        return 1
    print("every phrase the diagnosis hinges on is still in the current builds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
