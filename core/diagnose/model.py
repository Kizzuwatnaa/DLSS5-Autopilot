r"""The shapes and the constants every reader here shares.

This half is data: the Report a diagnosis fills in, the log
names, the phrases the add-ons print. It imports nothing from
the package, so everything else can import it.

The three things the suite and the replay tools patch live here
- STANDALONE_LOG, _layer_state, _user_data_roots - and every use
inside the package goes through `model.`, because a plain import
would bind the value and stop seeing the patch.

Part of core/diagnose; see __init__.py.
"""
from __future__ import annotations
import itertools
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


__all__ = [
    "BAD", "FEED_LOG", "FEED_SHADERS", "Finding",
    "HOST_LOG", "INFO", "LEGACY_MANIFESTS", "MANIFEST",
    "NATIVE_ADDON_NAME", "OK", "OPTI_LOG", "PROVIDER_FX",
    "RESHADE_LOG", "RR_RUNTIME", "Report", "STANDALONE_ADDON_NAME",
    "STANDALONE_LOG", "STANDALONE_PANEL", "UPSTREAM_ADDON_NAME", "UPSTREAM_PANEL",
    "NEVER_RAN_SAID", "drop_never_ran",
    "VULKAN_LAYER", "WARN", "_BACKUP_SUFFIX", "_COMPILER_FIX",
    "_CORE_NAMES", "_CRASH_CAUSES", "_DEPTH_HINT", "_DISPATCH_MS",
    "_FEED_GOT_RUNTIME", "_FEED_SESSION", "_HOOK_ADDRESSES", "_INPUT_NEVER",
    "_INPUT_SEEN", "_INSTALL_FRAMES", "_NGX_IN_STACK", "_OURS_IN_STACK",
    "_SHARED_MODULES", "_install_modules",
    "_OUR_MARKS", "_RAN_CONTAINERS", "_RAN_ENTRIES", "_RAN_LOOK",
    "_RAN_MARGIN", "_RAN_NOT_A_NAME", "_RAN_NOT_EVIDENCE", "_RAN_PARENTS",
    "_RAN_PER_DIR", "_RAN_SECONDS", "_RAN_SKIP", "_RAN_SKIP_PART", "_RAN_SKIP_SUFFIX",
    "_RESHADE_ALSO", "_RESHADE_FIRM", "_RESHADE_GOT_GOING", "_RESHADE_KEEP",
    "_RESHADE_SESSION", "_RESHADE_STARTED", "_SCAN_NOISE", "_STANDALONE_NO_RUNTIME",
    "_STANDALONE_SESSION", "_layer_state", "_user_data_roots"
]

FEED_LOG = "dlss5-feed.log"


HOST_LOG = Path("host64") / "dlss5-feed-host.log"


RESHADE_LOG = "ReShade.log"


MANIFEST = "dlss5-autopilot.json"


OK, WARN, BAD, INFO = "ok", "warn", "bad", "info"


UPSTREAM_ADDON_NAME = "DLSS5 NR Pre-Upscale"


UPSTREAM_PANEL = "'NR Pre-Upscale' tab (neural-upstream)"


NATIVE_ADDON_NAME = "DLSS 5 Neural Rendering"


STANDALONE_ADDON_NAME = "Standalone DLSS-NR + SR"


STANDALONE_PANEL = "'Standalone DLSS-NR + SR' add-on tab"


STANDALONE_LOG = (Path(os.environ.get("LOCALAPPDATA") or Path.home())
                  / "RHI" / "Logs" / "standalone-dlssnr.log")


_STANDALONE_SESSION = " attached; requested profile="


_STANDALONE_NO_RUNTIME = "required private runtime dependency missing"


FEED_SHADERS = ("dlss5_feed.fx", "lumenite_kernel.fx", "lumenite_quantmotion.fx")


_DISPATCH_MS = re.compile(r"\d+(?:[.,]\d+)?\s*ms\b")


_DEPTH_HINT = (
    "In the ReShade overlay open the Add-ons tab and look at the depth "
    "buffer list: one has to be selected. If none is, or it switches when "
    "you change display mode, try 'Use aspect ratio heuristics' set to off "
    "there. Borderless, display scaling and an in-game render scale below "
    "100% are the usual reason the buffer stops matching. If one is selected "
    "and the depth is still flat, tick 'Copy depth buffer before clear "
    "operations' on the same tab - Mass Effect Legendary Edition needs it.")


_COMPILER_FIX = (
    "The game ships its own d3dcompiler_47.dll and it predates Shader Model "
    "5.1, so the neural pass never compiles - frames still flow, nothing "
    "changes on screen. Rename that file to d3dcompiler_47.dll.dlss5-off so "
    "Windows uses the System32 copy; the tool's next install does this by "
    "itself.")


@dataclass
class Finding:
    level: str
    title: str
    detail: str = ""


# Everything the diagnosis says when it believes the game has NOT run since
# the install. Proof that it DID run - a sighting, a Windows fault record -
# makes every one of them false, and a correction that reaches the verdict
# and leaves these on the screen is #171's shape. Two places pruned this
# list, with different fragments and different strength; it lives here so
# they cannot drift apart again (gate 1.9.1). "Own files changed after the
# install" is not on it: that finding is evidence the game DID run.
NEVER_RAN_SAID = (
    "has not been started since the install",
    "is older than the install",
    "predates this install",
    "has not been run since installing",
    "play once and check again",
)


def drop_never_ran(findings) -> list:
    """The findings that survive proof that the game ran."""
    return [f for f in findings
            if not any(s in f"{getattr(f, 'title', '')} "
                         f"{getattr(f, 'detail', '')}"
                       for s in NEVER_RAN_SAID)]


@dataclass
class Report:
    ran: bool = False
    verdict: str = ""
    route: str = ""
    findings: list[Finding] = field(default_factory=list)
    log_time: str = ""
    # This verdict rests on there being no log at all: whoever has better
    # evidence that the game ran (Windows' fault record) must replace it.
    never_ran: bool = False
    # There is no install record in this folder. Nothing below may date
    # anything "since the install", because there is no install to date
    # against - and a log older than it cannot be told from a new one.
    no_record: bool = False

    def add(self, level: str, title: str, detail: str = "") -> None:
        self.findings.append(Finding(level, title, detail))


_RESHADE_SESSION = "Initializing crosire's ReShade"


_FEED_SESSION = re.compile(r"^[\d:.]+\s+dlss5-feed\S*\s[^\n]*attached\.", re.M)


_FEED_GOT_RUNTIME = re.compile(
    r"effect runtime|runtime \w+ initialis|effects:|technique|building:"
    r"|feature ready|session ready|frame \d+", re.I)


_OURS_IN_STACK = ("dlss5-feed.addon64", "dlss5-feed.addon32",
                  "dlss5-feed-host64.exe", "dlss5-feed-host32.exe",
                  "renodx-dlss5.addon64", "renodx-dlss.addon64",
                  "dlss5-bridge.addon64", "dlss5-dx11-bridge.addon64",
                  "nvngx.dll.addon64", "standalone-dlssnr.addon64")


_NGX_IN_STACK = ("nvngx_dlssnr.dll", "_nvngx.dll", "nvngx.dll",
                 "nvngx_dlss.dll", "D3D12Core.dll")


LEGACY_MANIFESTS = ("dlss5kur-kurulum.json", "dlss5-installer.json")


_OUR_MARKS = ("dlss5-feed.addon64", "dlss5-feed.addon32",
              "dlss5-bridge.addon64", "renodx-dlss5.addon64",
              "dlss5-feed.log", "OptiScaler.ini", "nvngx_dlssnr.dll")


# A fallback only. As the answer this hand list rotted: net.py and mfg.py
# were never in it, so a download dying below the installer's frames was
# no crash (the report keeps only a 900-character tail).
_INSTALL_FRAMES = ("installer.py", "optiscaler.py", "remix.py", "dxvk.py",
                   "vulkan.py", "openxr.py", "feedcfg.py", "reshade_ini.py",
                   "emulators.py", "refw.py", "reengine.py", "net.py",
                   "sources.py", "mfg.py")
# Reached by the install, and by the window and the scan as well.
_SHARED_MODULES = frozenset({"games", "gpu", "pe", "update", "log", "prefs",
                             "autotune", "dlss", "anticheat"})
_install_graph: list = []


def _install_modules() -> frozenset:
    """Every core module the installer reaches, read from its bytecode (the
    frozen build has no .py files), imports inside functions included; minus
    the shared modules, the window and the diagnosis."""
    if _install_graph:
        return _install_graph[0]
    import dis, importlib, types  # noqa: E401
    pkg = __name__.split(".")[0]
    seen: set = set()
    stack = ["installer"]
    while stack:
        n = stack.pop()
        # "ui" is the 2.0 window's package (core/ui); "gui" the old module
        if n in seen or n in ("diagnose", "gui", "ui", "log", "prefs"):
            continue
        try:
            mod = importlib.import_module(f"{pkg}.{n}")
            codes = [mod.__loader__.get_code(mod.__name__)]
        except Exception:
            continue
        seen.add(n)
        found = {v.__name__.split(".")[1] for v in list(vars(mod).values())
                 if isinstance(v, types.ModuleType)
                 and v.__name__.startswith(pkg + ".")}
        while codes:
            co, consts = codes.pop(), []
            for ins in dis.get_instructions(co):
                if ins.opname == "LOAD_CONST":
                    consts.append(ins.argval)
                elif ins.opname == "IMPORT_NAME" and len(consts) >= 2 \
                        and consts[-2] == 1:      # a "from ." import
                    if ins.argval:
                        found.add(str(ins.argval).split(".")[0])
                    elif isinstance(consts[-1], tuple):
                        found.update(str(x) for x in consts[-1])
            codes += [c for c in co.co_consts if isinstance(c, types.CodeType)]
        stack.extend(found - seen)
    if "installer" not in seen or len(seen) < 3:
        seen = {f[:-3] for f in _INSTALL_FRAMES}
    _install_graph.append(frozenset(seen - _SHARED_MODULES))
    return _install_graph[0]


_CRASH_CAUSES = (
    # urllib wraps the socket error, so the class name is usually gone by
    # the time this reads the line: match what is left of it (#213's own
    # line is "URLError: <urlopen error [Errno 11001] getaddrinfo failed>").
    ("getaddrinfo", "this PC could not look up the download's address"),
    ("gaierror", "this PC could not look up the download's address"),
    ("ssl", "the secure connection to the download failed"),
    ("timed out", "the download timed out"),
    ("connectionreset", "the connection was reset part way through"),
    ("connectionrefused", "the download server refused the connection"),
    ("connection", "the download could not reach the internet"),
    ("urlerror", "the download could not reach the internet"),
    ("httperror", "the download server answered with an error"),
    ("permissionerror", "Windows refused a file the install had to write"),
    ("filenotfounderror", "a file the install expected was not there"),
    # Windows says "[WinError 112] There is not enough space on the disk";
    # Python's own OSError says "[Errno 28] No space left on device".
    # Neither contains the word this used to look for, so a full drive was
    # answered with "Windows refused a file" and "press INSTALL again".
    ("no space left", "the drive filled up"),
    ("not enough space", "the drive filled up"),
    ("errno 28", "the drive filled up"),
    ("winerror 112", "the drive filled up"),
    ("oserror", "Windows would not let the install finish a file"),
    ("memoryerror", "this PC ran out of memory during the install"),
)


_CORE_NAMES = ("nvngx_dlssnr.dll",)


OPTI_LOG = "OptiScaler.log"


_INPUT_SEEN = ("context created", "hk_ffxFsr2", "hk_ffxFsr3",
               "hk_ffxCreateContext", "hk_xess", "XeSS Version:",
               "libxess.dll found")


_INPUT_NEVER = ("libxess.dll not found!", "disabling FSR2 hooks!")


VULKAN_LAYER = "(vulkan layer)"


def _layer_state(man: dict) -> tuple[bool, bool]:
    """(any ReShade layer active, one this game's architecture can load)."""
    try:
        from .. import vulkan
    except ImportError:                       # not Windows
        return True, True
    x64 = man.get("bitness") != 32
    return (vulkan.existing_registration() is not None,
            vulkan.registered_for(x64) is not None)


_RAN_LOOK = ("", "Saved/Logs", "Saved/SaveGames", "Saved/Config/WindowsClient",
             "Saved/Config/Windows", "Saved", "Logs", "logs", "Config",
             "config", "SavedGames", "profiles", "Profiles", "UserData",
             "savegames")


_RAN_PARENTS = 3


_RAN_ENTRIES = 4000


_RAN_PER_DIR = 400


_RAN_SECONDS = 1.0


_RAN_SKIP = {"reshade.log", "dlss5-feed.log", "optiscaler.log",
             "standalone-dlssnr.log", "dlss5-autopilot.json",
             "dlss5-feed-host64.log", "reshade.ini", "reshadepreset.ini",
             "dlss5-feed.cfg", "dlss5-feed-crash.dmp",
             # the dlss page's record: written by the tool, not the game
             "dlss5-dlss-update.json"}


_RAN_SKIP_SUFFIX = (".dlss5-autopilot-backup", ".tmp")


# Anywhere in the name, not only at its end: the dlss page keeps the game's
# own runtime as `.dlss5-dlss-original`, sets one aside as
# `.dlss5-dlss-displaced-<time>` and copies through `.dlss5-dlss-part`. A
# fresh one of those read as "the game ran and loaded nothing" when the game
# had not been started since the install.
_RAN_SKIP_PART = (".dlss5-dlss-",)


_RAN_NOT_EVIDENCE = (".dll", ".addon64", ".addon32", ".fx", ".fxh", ".asi",
                     ".json", ".7z", ".zip", ".pdb",
                     # An executable's timestamp moves when the store
                     # updates the game, which is not a session: Crimson
                     # Desert answered with its own .exe on this machine.
                     ".exe")


_RAN_MARGIN = 60.0


_RAN_CONTAINERS = ("binaries", "win64", "win32", "wingdk", "winarm64", "bin",
                   "bin64", "x64", "x86", "retail", "shipping", "game")


_RAN_NOT_A_NAME = _RAN_CONTAINERS + (
    "common", "steamapps", "steamlibrary", "steam", "epic games", "gog galaxy",
    "gog games", "ubisoft", "ubisoft game launcher", "origin games", "ea games",
    "ea", "battle.net", "riot games", "amazon games", "xboxgames",
    "program files", "program files (x86)", "games", "program data")


def _user_data_roots() -> list[Path]:
    """Where engines keep per-user game data on Windows."""
    roots: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    app = os.environ.get("APPDATA")
    if local:
        roots.append(Path(local))
        roots.append(Path(local + "Low"))
    if app:
        roots.append(Path(app))
    try:
        home = Path.home()
        roots.append(home / "Documents" / "My Games")
        roots.append(home / "Saved Games")
    except (OSError, RuntimeError):
        pass
    return roots


_RESHADE_KEEP = ("WARN", "ERROR", "Registered add-on", "CreateSwapChain",
                 "Direct3DCreate9", "Exiting", "EvaluateFeature")


_RESHADE_GOT_GOING = ("registered add-on", "redirecting", "initialized runtime",
                      "swap chain", "swapchain", "compiled", "exiting",
                      "effect", "created")


_RESHADE_STARTED = "initializing crosire"


_RESHADE_FIRM = ("EvaluateFeature",)


_RESHADE_ALSO = ("Registered add-on",)


_HOOK_ADDRESSES = re.compile(r" with 0x[0-9A-Fa-f]+ => 0x[0-9A-Fa-f]+")


PROVIDER_FX = {2: "vort_Motion.fx", 3: "lumenite_Kernel.fx",
               4: "lumenite_QuantMotion.fx"}


RR_RUNTIME = "nvngx_dlssd.dll"


_BACKUP_SUFFIX = ".dlss5-autopilot-backup"


_SCAN_NOISE = re.compile(
    r"scan \S+: \d+ found|is not readable yet|an earlier install is recorded "
    r"in|the store names|could not read |no executable under|inspected .+ in "
    r"[\d.]+s|checked .+ in [\d.]+s|scan: .+ is the same executable|stopped "
    r"looking for")
