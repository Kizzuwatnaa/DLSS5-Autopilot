r"""What is actually running, and what it has loaded.

Every other evidence source in this tool reads a folder or a log after the
fact and reasons backwards: no ReShade.log, so *probably* the proxy was not
loaded, so *probably* the game launches a different executable, or ignores
that DLL name, or was never started at all. Three guesses, offered as a
list, to somebody who wanted an answer - and 34 of the first 84 reports got
that list.

While the game runs, none of it has to be guessed. Windows will say which
process started, which executable it came from, who started it, and which
DLLs are mapped into it - so "our dxgi.dll is not in the module list, the
one in System32 is" is a fact, and so is "the launcher started a second
executable in another folder, and that is what draws".

    procs()                     every process, with its parent and path
    from_folder(folder)         the ones running out of a game folder
    modules(pid)                the DLLs mapped into one, or why not
    inspect(folder, ours)       the whole answer for one game folder

Nothing here writes anything, opens a process for anything but reading, or
touches a process it was not asked about. A protected game (anti-cheat, and
some store builds) refuses the module list: that is reported as refused,
never as "nothing of ours is loaded" - absence of evidence has cost this
project three wrong verdicts already.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# Read-only rights. QUERY_INFORMATION rather than its LIMITED sibling
# because EnumProcessModulesEx refuses the limited one, and VM_READ because
# the module list is read out of the process's own memory.
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_LIST_MODULES_ALL = 0x03
_TH32CS_SNAPPROCESS = 0x00000002
_ERROR_ACCESS_DENIED = 5
_ERROR_PARTIAL_COPY = 299          # the process is starting, or is 32-bit
_MAX_MODULES = 4096

_WINDOWS = os.name == "nt"


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                ("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                ("th32ParentProcessID", w.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


_API = None                    # built once: see _api()


def _api():
    """kernel32/psapi with their argument types declared.

    Declared, because the defaults truncate: a HANDLE came back as a signed
    int and the second OpenProcess of a session raised "int too long to
    convert" instead of reading anything.

    Built once. This is called from procs() and again for every process in
    _image_path(), and the Recorder asks for a snapshot every few seconds
    for as long as the window is open: on a 300-process machine that was
    600 WinDLL constructions per snapshot, all of them identical.
    """
    global _API
    if _API is not None:
        return _API
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    k32.OpenProcess.restype = w.HANDLE
    k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k32.CloseHandle.argtypes = [w.HANDLE]
    k32.CreateToolhelp32Snapshot.restype = w.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k32.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
    k32.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
    k32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR,
                                               ctypes.POINTER(w.DWORD)]
    psapi.EnumProcessModulesEx.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD,
                                           ctypes.POINTER(w.DWORD), w.DWORD]
    psapi.GetModuleFileNameExW.argtypes = [w.HANDLE, w.LPVOID, w.LPWSTR, w.DWORD]
    _API = (k32, psapi)
    return _API


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str
    path: str = ""             # "" when the executable's path cannot be read


@dataclass
class Loaded:
    """The DLLs one running process has mapped in - or why they are unknown."""
    pid: int
    exe: str = ""
    paths: list[str] = field(default_factory=list)
    refused: str = ""          # non-empty means the list is UNKNOWN, not empty

    @property
    def known(self) -> bool:
        return not self.refused

    def by_name(self, name: str) -> list[str]:
        low = name.lower()
        return [p for p in self.paths if os.path.basename(p).lower() == low]


def procs() -> list[Proc]:
    """Every process this account can see, with its parent."""
    if not _WINDOWS:
        return []
    k32, _ = _api()
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == w.HANDLE(-1).value:
        return []
    out: list[Proc] = []
    try:
        e = _PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(e)
        if not k32.Process32FirstW(snap, ctypes.byref(e)):
            return []
        while True:
            out.append(Proc(int(e.th32ProcessID), int(e.th32ParentProcessID),
                            str(e.szExeFile)))
            if not k32.Process32NextW(snap, ctypes.byref(e)):
                break
    finally:
        k32.CloseHandle(snap)
    for p in out:
        p.path = _image_path(p.pid)
    return out


def _image_path(pid: int) -> str:
    """The executable a running process was started from, or ""."""
    if not _WINDOWS:
        return ""
    k32, _ = _api()
    # LIMITED_INFORMATION is enough for the path and is granted for
    # processes whose module list is refused, which is the point: a
    # protected game still tells us WHICH executable is running.
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        size = w.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k32.CloseHandle(h)


def modules(pid: int) -> Loaded:
    """Every DLL mapped into one process, or the reason there is no list."""
    got = Loaded(pid=pid, exe=_image_path(pid))
    if not _WINDOWS:
        got.refused = "not Windows"
        return got
    k32, psapi = _api()
    h = k32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not h:
        err = ctypes.get_last_error()
        got.refused = ("the process is protected (anti-cheat, or a store "
                       "build) and will not be opened"
                       if err == _ERROR_ACCESS_DENIED else
                       f"could not open the process (error {err})")
        return got
    try:
        arr = (ctypes.c_void_p * _MAX_MODULES)()
        need = w.DWORD()
        if not psapi.EnumProcessModulesEx(h, arr, ctypes.sizeof(arr),
                                          ctypes.byref(need), _LIST_MODULES_ALL):
            err = ctypes.get_last_error()
            got.refused = (
                "the process is protected (anti-cheat, or a store build) "
                "and will not list its DLLs" if err == _ERROR_ACCESS_DENIED
                else "the process was still starting - try again in a moment"
                if err == _ERROR_PARTIAL_COPY
                else f"the DLL list could not be read (error {err})")
            return got
        buf = ctypes.create_unicode_buffer(1024)
        count = min(need.value // ctypes.sizeof(ctypes.c_void_p), _MAX_MODULES)
        for i in range(count):
            if psapi.GetModuleFileNameExW(h, arr[i], buf, 1024):
                got.paths.append(buf.value)
    finally:
        k32.CloseHandle(h)
    return got


def _under(path: str, folder: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(folder).resolve())
        return True
    except (ValueError, OSError):
        return False


def from_folder(folder: Path, all_procs: list[Proc] | None = None,
                exe: str = "") -> list[Proc]:
    """The processes running out of this game folder, the game first.

    Also the children of anything in it: a launcher in the folder starting
    the game somewhere else is the case this exists to see, and the child
    keeps the launcher as its parent (#191).

    Ordered, because the caller reads the first one: the executable the
    install was made for, then anything that looks like a game, then the
    helpers - a Sentry crash handler in the folder is a process of this
    game and is emphatically not the thing that draws.
    """
    ps = all_procs if all_procs is not None else procs()
    mine = [p for p in ps if p.path and _under(p.path, folder)]
    pids = {p.pid for p in mine}
    for p in ps:
        if p.ppid in pids and p.pid not in pids and p.path:
            mine.append(p)
            pids.add(p.pid)
    try:
        from . import pe
        helper = lambda p: not pe.looks_like_game(Path(p.path))
    except Exception:                                   # pragma: no cover
        helper = lambda p: False
    want = os.path.basename(exe).lower() if exe else ""
    mine.sort(key=lambda p: (p.name.lower() != want, helper(p),
                             -len(Path(p.path).parts)))
    return mine


@dataclass
class Sighting:
    """What one running game says about an install beside it."""
    proc: Proc
    loaded: Loaded
    ours: list[str] = field(default_factory=list)      # loaded from our folder
    elsewhere: list[str] = field(default_factory=list)  # same name, other folder
    missing: list[str] = field(default_factory=list)    # ours, not loaded


def inspect(folder: Path, ours: list[str], exe: str = "") -> list[Sighting]:
    """Every process of this game, and what became of the files we wrote.

    `ours` is the install's own file list (manifest "files"): the names that
    have to end up in the process for anything to work. A name that is
    loaded from somewhere else - System32's dxgi.dll while ours sits unused
    beside the game - is the answer to "everything is in place and nothing
    happens", and it cannot be seen any other way.
    """
    folder = Path(folder)
    want = {}
    names, sub = process_names(ours)
    for rel in ours or []:
        # ReShade loads an add-on the same way Windows loads any DLL, so it
        # is in the module list under its own name - which is how "the
        # add-on loaded" stops being a question for the person to answer in
        # an overlay. The runtime (nvngx_dlssnr.dll) likewise.
        # A list per name: an install can hold two copies of one runtime
        # (nvngx_dlss.dll beside the exe and the game's own, swapped, under
        # Plugins - #225), and either one loaded is ours.
        if isinstance(rel, str) and os.path.basename(rel).lower() in names \
                and not _other_process(rel):
            want.setdefault(os.path.basename(rel).lower(), []).append(
                folder / rel)
    out: list[Sighting] = []
    for p in from_folder(folder, exe=exe):
        got = modules(p.pid)
        s = Sighting(proc=p, loaded=got)
        if got.known:
            for name, mine in want.items():
                hits = got.by_name(name)
                mine_hits = [h for h in hits
                             if any(_same_file(h, m) for m in mine)]
                if mine_hits:
                    # Ours is in. A same-named copy beside it is the one ours
                    # forwards to - ReShade's dxgi.dll loads System32's - and
                    # was reported as "ours is never reached" (#232, #238).
                    s.ours.extend(mine_hits)
                    continue
                if name in sub:
                    # A file we keep in a subfolder is optional to the game
                    # (OptiScaler's D3D12Core.dll): Windows' own copy of it
                    # is not a conflict. And when that copy was the only
                    # hit, the name belongs in NEITHER list - calling it
                    # missing put "not in the process: D3D12Core.dll" under
                    # a verdict about our own files (gate 1.9.1).
                    hits = [h for h in hits if not _windows_file(h)]
                    if not hits:
                        continue
                if hits:
                    s.elsewhere.extend(hits)
                else:
                    s.missing.append(name)
        out.append(s)
    return out


# The 32-bit feeder's 64-bit helper runs as a process of its own out of this
# folder, so what is in it is never in the game. Listed as the game's, its
# dxgi.dll matched System32's in the game and #238 was told another dxgi.dll
# had taken ours' place in a DX9 game. installer.HOST_DIR, kept literal here
# so watch imports nothing heavy.
_OTHER_PROCESS_DIRS = ("host64",)

# What the game loads by name when the chain is working. The rest of an
# install is loaded on demand (amd_fidelityfx_vk.dll in a DX12 game) or
# never by name (OptiScaler's nvngx.dll_dlssnr.dll), and "not loaded"
# about those read as a fault (#225, #231). nvngx_dlssnr.dll itself
# arrives when the model first runs, so its absence beside a "no neural
# frame yet" verdict is pending, not a fault (#232).
ESSENTIAL = ("nvngx_dlssnr.dll",)


def _other_process(rel: str) -> bool:
    first = rel.replace("\\", "/").lstrip("/").split("/", 1)
    return len(first) == 2 and first[0].lower() in _OTHER_PROCESS_DIRS


def _windows_file(path: str) -> bool:
    win = os.environ.get("SystemRoot") or os.environ.get("windir") \
        or r"C:\Windows"
    try:
        return os.path.normcase(str(path)).startswith(
            os.path.normcase(win.rstrip("\\/")) + os.sep)
    except Exception:
        return False


def process_names(files) -> tuple[set, set]:
    """(names the game process can load, those kept only in a subfolder).

    From an install's file list; anything that is not a string, not a DLL
    or add-on, or belongs to the helper process is left out.
    """
    names: set[str] = set()
    root: set[str] = set()
    for rel in files if isinstance(files, (list, tuple)) else []:
        if not isinstance(rel, str) \
                or not rel.lower().endswith((".dll", ".addon64", ".addon32")) \
                or _other_process(rel):
            continue
        norm = rel.replace("\\", "/").strip("/")
        name = os.path.basename(norm).lower()
        names.add(name)
        # bin/ is where a Source game loads its d3d9.dll (#224): a file the
        # game needs, so Windows' copy there IS the conflict, as beside the
        # exe.
        if "/" not in norm or norm.lower().startswith(("bin/", "bin64/")):
            root.add(name)
    return names, names - root


def essential(name: str, proxy: str = "", also=()) -> bool:
    """`also`: names this install cannot work without beyond the usual -
    DXVK's DLLs on a DXVK install, where the proxy is a Vulkan layer and
    DXVK not loading is the fault itself (#224)."""
    low = str(name).lower()
    return low in ESSENTIAL or low.endswith((".addon64", ".addon32")) \
        or (bool(proxy) and low == str(proxy).lower()) \
        or low in {str(a).lower() for a in also or ()}


def settle(rec: dict, files) -> dict:
    """A remembered sighting, read with the rules inspect() uses today.

    Records written by 1.9.0 listed the helper's files as the game's and
    System32's copy of a loaded proxy as a conflict. They sit in people's
    sightings.json, so the reader applies the rule as well as the writer.
    With no file list there is nothing to read it against: returned as is.
    """
    if not isinstance(rec, dict) or not rec:
        return {}
    names, sub = process_names(files)
    if not names:
        return rec
    out = dict(rec)

    def strs(key):
        v = rec.get(key)
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

    # "ours" is filtered like the other two, and for the same reason: a
    # 1.9.0 record counted the 64-bit helper's own files as the game's, so
    # an unfiltered list still reads host64\renodx-dlss5.addon64 as an
    # add-on the game loaded - and a loaded add-on suppresses the verdict
    # this release wrote for exactly that case (#238).
    ours = [n for n in strs("ours") if os.path.basename(n).lower() in names]
    have = {os.path.basename(n).lower() for n in ours}
    out["ours"] = ours
    out["elsewhere"] = [
        p for p in strs("elsewhere")
        if os.path.basename(p).lower() in names
        and os.path.basename(p).lower() not in have
        and not (os.path.basename(p).lower() in sub and _windows_file(p))]
    out["missing"] = [n for n in strs("missing")
                      if n.lower() in names and n.lower() not in have]
    return out


def _same_file(a: str, b: Path) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == \
            os.path.normcase(os.path.realpath(str(b)))
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


# Where a sighting is kept until the person comes back to the window. Beside
# the tool's own log, never in the game folder: this is our record, and a
# file written into a game folder is one more thing an uninstall has to take
# back out and one more thing a launcher's file check can object to.
RECORD = Path(os.environ.get("LOCALAPPDATA", Path.home())) \
    / "dlss5-autopilot" / "sightings.json"
_RECORD_KEEP = 40


def _load_records() -> dict:
    try:
        import json
        data = json.loads(RECORD.read_text(encoding="utf8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def remember(folder: Path, s: "Sighting") -> None:
    """Keep what one sighting saw, so it can be read after the game closes.

    The window is open while people play - they install, launch, play, come
    back and press "did it work?" with the game already shut. The process is
    gone by then and every question it could have answered goes back to
    being a guess, so the answer is written down at the moment it is true.
    """
    import json
    rec = _load_records()
    rec[os.path.normcase(str(Path(folder)))] = {
        "at": time.time(),
        "exe": s.proc.path,
        "name": s.proc.name,
        "refused": s.loaded.refused,
        "modules": len(s.loaded.paths),
        "ours": sorted({os.path.basename(p) for p in s.ours}),
        "elsewhere": sorted(set(s.elsewhere))[:8],
        "missing": sorted(set(s.missing)),
    }
    if len(rec) > _RECORD_KEEP:
        for k in sorted(rec, key=lambda k: rec[k].get("at", 0))[:-_RECORD_KEEP]:
            rec.pop(k, None)
    try:
        RECORD.parent.mkdir(parents=True, exist_ok=True)
        RECORD.write_text(json.dumps(rec, indent=1), encoding="utf8")
    except OSError:
        pass


def last_sighting(folder: Path, since: float = 0.0) -> dict:
    """What was seen the last time this game ran, or {}.

    `since` is the install time: a sighting from before the install
    describes an install that is no longer there and must not be read as
    evidence about this one.
    """
    got = _load_records().get(os.path.normcase(str(Path(folder))), {})
    if not isinstance(got, dict) or got.get("at", 0) < since - 60:
        return {}
    return got


class Recorder:
    r"""Watch the folders we have installed into, and write down what starts.

    One thread, one snapshot of the process table every few seconds. It
    reads; it never writes anything into a game folder and never touches a
    process it was not asked about.

    A folder is watched until its game has been seen once, and the thread
    stops with the last one - so a game that is started keeps this alive for
    a few seconds, and a game that is never started keeps one four-second
    poll alive until the window is closed.
    """

    def __init__(self, every: float = 4.0, settle: float = 8.0):
        self.every = max(1.0, every)
        self.settle = max(0.0, settle)
        self._want: dict[str, dict] = {}
        self._thread = None
        self._stop = False
        self._seen: dict[str, float] = {}

    def add(self, folder: Path, ours: list[str] | None = None,
            exe: str = "") -> None:
        """Watch this folder from now on (an install just finished here)."""
        key = os.path.normcase(str(Path(folder)))
        self._want[key] = {"folder": Path(folder), "ours": list(ours or []),
                           "exe": exe or ""}
        self.start()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        import threading
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="dlss5-watch",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def _run(self) -> None:
        pending: dict[str, float] = {}          # folder -> when to read it
        while not self._stop and self._want:
            try:
                ps = procs()
                now = time.monotonic()
                for key, job in list(self._want.items()):
                    if key in pending:
                        if now < pending[key]:
                            continue
                        pending.pop(key)
                        # Settled: read it, write it down, stop watching it.
                        found = inspect(job["folder"], job["ours"],
                                        exe=job["exe"])
                        if found:
                            remember(job["folder"], found[0])
                            self._seen[key] = time.time()
                            self._want.pop(key, None)
                        continue
                    if from_folder(job["folder"], ps, exe=job["exe"]):
                        # Wait before reading: the graphics DLLs are loaded
                        # when the device is created, and a proxy read three
                        # seconds in reads as missing when it is merely late.
                        pending[key] = now + self.settle
            except Exception:
                # A watcher must never take the window down with it.
                pass
            time.sleep(self.every if not pending else min(self.every, 1.0))
        self._thread = None

    def saw(self, folder: Path) -> float:
        return self._seen.get(os.path.normcase(str(Path(folder))), 0.0)


def wait_for(folder: Path, ours: list[str] | None = None, exe: str = "",
             seconds: float = 300.0, settle: float = 6.0,
             tick=None) -> list[Sighting]:
    """Wait for this game to start, let it settle, then read it.

    `settle` because the module list of a process three seconds old is not
    the one it will render with: the graphics DLLs are loaded when the
    device is created, and a proxy that loads late would read as missing.
    `tick(seconds_waited, proc_or_None)` is called about once a second so a
    window can say what it is waiting for; return False from it to stop.
    """
    deadline = time.monotonic() + max(1.0, seconds)
    seen: Proc | None = None
    while time.monotonic() < deadline:
        found = from_folder(folder, exe=exe)
        if found:
            seen = found[0]
            break
        if tick is not None and tick(0.0, None) is False:
            return []
        time.sleep(1.0)
    if seen is None:
        return []
    end = time.monotonic() + settle
    while time.monotonic() < end:
        if tick is not None and tick(settle, seen) is False:
            break
        time.sleep(0.5)
    return inspect(folder, ours or [], exe=exe)
