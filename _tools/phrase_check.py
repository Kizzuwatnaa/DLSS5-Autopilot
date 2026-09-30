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
        # #309: the output-window rule hangs on the add-on's own two failure lines
        ("native presentation initialization failed", "native presentation"),
        ("native presentation initialization rejected", "native presentation"),
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
        # The 32-bit feed and its 64-bit helper (core/diagnose/helper.py, #252).
        ("frames: feed CPU %.2f ms/frame", "frames: feed CPU"),
        ("pipe write", "pipe write"),
        ("present probe: %lld presents / %lld frames fed",
         "present probe: 0 presents"),
        ("the game kept presenting", "the game kept presenting"),
        ("[host] Present failed 0x%08X", "Present failed"),
        ("[host] neural consumer outcome: ", "neural consumer outcome: "),
        ("did not intercept", "did not intercept"),
        ("consumer intercepted DLSS but feature 18 failed", "feature 18 failed"),
        # 1.16.0-beta.5 reworded "(ReShade.log is unavailable)"
        ("ReShade.log could not be opened", "could not be opened"),
        ("[host] DLSS 5 add-on file: %s", "DLSS 5 add-on file: "),
        ("[host] frame %llu evaluated", "evaluated"),
        # 1.18.0-beta: NGX's own result, the helper mode, NGX's log copy
        ("NGX SuperSampling.FeatureInitResult: 0x%08X (%s)",
         "FeatureInitResult:? "),
        ("would not set DLSS up inside this game's process",
         "would not set DLSS up inside this game's process"),
        ("dlss5-feed.addon64 stands down", "addon64 stands down"),
        ("===== NGX's own log", "===== NGX's own log"),
        ("end of NGX's own log", "end of NGX's own log"),
    ],
    "OptiScaler (DLSS-NR fork)": [
        ("DlssNr_Dx12::Dispatch", "DlssNr_Dx12::Dispatch"),
        ("forwarder loaded", "forwarder loaded"),
        ("Vulkan is creating swapchain", "Vulkan is creating swapchain"),
    ],
    # #311: this fork moved its timing line and nothing here was reading the
    # build that writes it - the default build's phrases were the only ones
    # checked. "running" in core/diagnose/routes.py hangs on these.
    "OptiScaler (wilsjo2's pre-SR fork)": [
        ("DLSS-NR elapsed:", "DLSS-NR elapsed:"),
        ("DLSS-NR finished picture:", "DLSS-NR finished picture:"),
    ],
    # 2.0.8: the runtime the remix swap installs. Its patterns live in
    # core/remix.py and routes._analyse_remix reads them; none of its lines
    # carry Kim2091's "[DLSS-NR]" prefix, which is how every session of it
    # read "never even attempted" before (the 0-of-4 remix record).
    "dxvk-remix-plus-dlssnr (lunks)": [
        ("NVIDIA DLSS-NR snippet loaded from ", "NVIDIA DLSS-NR snippet loaded from "),
        ("NVIDIA DLSS-NR evaluated (count=", "NVIDIA DLSS-NR evaluated"),
        ("NVIDIA DLSS-NR inactive:", "NVIDIA DLSS-NR inactive:"),
        ("NVIDIA DLSS-NR skipped: ", "NVIDIA DLSS-NR skipped: "),
        ("NVIDIA DLSS-NR not available: ", "NVIDIA DLSS-NR not available:"),
        ("NVSDK_NGX_VULKAN_Init_Ext failed for DLSS-NR: ",
         "NVSDK_NGX_VULKAN_Init_Ext failed for DLSS-NR:"),
        ("AllocateParameters failed for DLSS-NR: ", "AllocateParameters failed for DLSS-NR:"),
        ("Failed to create DLSS-NR feature: ", "Failed to create DLSS-NR feature:"),
        ("NVSDK_NGX_VULKAN_EvaluateFeature failed for DLSS-NR: ",
         "NVSDK_NGX_VULKAN_EvaluateFeature failed for DLSS-NR:"),
        ("does not export the full NVSDK_NGX_VULKAN_",
         "does not export the full NVSDK_NGX_VULKAN_* surface"),
    ],
    "renodx-dlss5": [
        # The 616.64+ verdict hangs on this pair: the add-on's own hook
        # failure line, and the name it registers under in ReShade.
        ("EvaluateFeature", "vtable::Hook"),
        ("DLSS 5 Neural Rendering", "DLSS 5 Neural Rendering"),
    ],
    # 2.0.7: core/diagnose/live.py reads the bridge's own dlss5-bridge.log,
    # with the patterns in model.py.
    "dlss5-bridge": [
        ("dlss5-bridge %s (built %s %s) attached.", r"\(built [^)\n]*\) attached\."),
        ("[bridge] frame %llu delivered (%ux%u)", r"frame (\d+) delivered"),
        ("[synth] D3D12 frame %llu delivered", r"(?:D3D12 |Vulkan )?frame (\d+) delivered"),
        ("[bridge] %ld frames delivered so far.", r"(\d+) frames delivered so far\."),
        ("frames: bridge CPU %.2f ms/frame | frame interval %.2f ms (%.1f fps)",
         r"frames: bridge CPU [\d.,]+ ms/frame"),
        ("stopped: %s. The game renders normally", r"stopped: (.+?)\. The game renders normally"),
        ("[bridge] evaluate raised exception 0x%08X -- disabling",
         r"(evaluate raised exception 0x[0-9A-Fa-f]{8}) -- disabling"),
        ("[bridge] evaluate failed with result 0x%08X, %s",
         r"evaluate failed with result (0x[0-9A-Fa-f]{8}), (\S+)"),
        ("### CRASH RECORDED ###", "### CRASH RECORDED ###"),
        ("[bridge]   it faulted in %ls", r"\]\s+it faulted in "),
        ("DLSS 5 Bridge", r'Registered add-on \"DLSS 5 Bridge'),
    ],
}

# A component the tool installs in more than one build: the phrase has to be
# in ONE of them, and which one is worth printing. The feeder is both - the
# dropdown's "newest release" is the stable build and "newest pre-release"
# is the 1.16 beta line, and four host phrases (#252) exist only in the
# betas. Reading the default build alone called them gone.
ALSO = {"DLSS5-Feeder": "DLSS5-Feeder (pre-release)"}


def _files(archive: Path):
    """Every binary inside an archive, as bytes - or the file itself, when
    the component ships as one (the bridge's .addon64)."""
    out = b""
    if archive.suffix.lower() != ".zip":
        try:
            return archive.read_bytes()
        except OSError as e:
            print(f"   !! cannot read {archive.name}: {e}")
            return out
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
    for name, pre in (("DLSS5-Feeder", False),
                      ("DLSS5-Feeder (pre-release)", True)):
        try:
            tag, assets = sources.resolve_feeder(prerelease=pre)
            # "feeder" in the name, the way the installer picks it: a
            # release page can carry an archive that is a different program
            # altogether, which is the whole lesson of #364.
            u = next((v for k, v in assets.items()
                      if k.lower().endswith(".zip") and "feeder" in k.lower()), "")
            if u:
                got[name] = (tag, net.download(u, f"phrasecheck-feeder-{tag}.zip"))
        except Exception as e:
            print(f"   !! feeder{' pre-release' if pre else ''}: {e}")
    try:
        cat = sources.rhi_catalog().get("renodx") or []
        if cat:
            got["renodx-dlss5"] = (cat[0]["label"],
                                   net.download(cat[0]["url"],
                                                f"phrasecheck-renodx-{cat[0]['label']}.zip"))
    except Exception as e:
        print(f"   !! renodx: {e}")
    try:
        tag, u = sources.resolve_bridge()
        # The installer's own cache name, so a build it downloaded is reused.
        got["dlss5-bridge"] = (tag, net.download(u, f"dlss5-bridge-{tag}.addon64"))
    except Exception as e:
        print(f"   !! bridge: {e}")
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
    try:
        tag, u = optiscaler.resolve(optiscaler.PRESR)
        if u.lower().endswith(".zip"):
            got["OptiScaler (wilsjo2's pre-SR fork)"] = (
                tag, net.download(u, f"phrasecheck-presr-{tag}.zip"))
    except Exception as e:
        print(f"   !! optiscaler, wilsjo2: {e}")
    try:
        tag, urls = sources.resolve_remix_runtime()
        u = urls.get(sources.REMIX_RUNTIME_ASSETS[0])
        if u:
            got["dxvk-remix-plus-dlssnr (lunks)"] = (
                tag, net.download(u, f"phrasecheck-remix-{tag}-d3d9.dll"))
    except Exception as e:
        print(f"   !! remix runtime: {e}")
    return got


def main() -> int:
    # A package since 1.9.0: the phrases live in its parts, so read them
    # all rather than one file that is no longer there.
    _dpkg = Path(__file__).resolve().parent.parent / "core" / "diagnose"
    dsrc = _dpkg if _dpkg.is_dir() else _dpkg.with_suffix(".py")
    dtext = ("\n".join(f.read_text(encoding="utf8", errors="replace")
                       for f in sorted(dsrc.glob("*.py")))
             if dsrc.is_dir()
             else dsrc.read_text(encoding="utf8", errors="replace"))
    # the Remix runtime's phrases are kept beside the rest of its handling
    _rx = _dpkg.parent / "remix.py"
    if _rx.is_file():
        dtext += "\n" + _rx.read_text(encoding="utf8", errors="replace")
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
        # The other build of the same component, when the tool offers one.
        other = ALSO.get(comp)
        otag, oblob = ("", b"")
        if other and other in got:
            otag, oarchive = got[other]
            oblob = _files(oarchive)
        print()
        print(f"  {comp}  {tag}  ({len(blob)} bytes of binary)"
              + (f"  +  {otag} ({len(oblob)} bytes)" if otag else ""))
        for frag, phrase in phrases:
            def _has(b: bytes) -> bool:
                return frag.encode() in b or frag.encode("utf-16-le") in b
            here, there = _has(blob), bool(oblob) and _has(oblob)
            in_bin = here or there
            in_code = phrase in dtext
            if in_bin and in_code:
                if not here and otag:
                    # Not an error: the rule fires on the build that writes
                    # it, and the person picked that build on purpose.
                    print(f"     .. {frag!r} is in {otag} only, not in {tag}")
                continue
            bad += 1
            why = []
            if not in_bin:
                why.append(f"{frag!r} is GONE from the build"
                           + (f" and from {otag}" if otag else ""))
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
