"""One game: its routes, its settings, and everything done to it.

Logic only - gamepage and setpanel draw it. Ported from the 1.9 window's
install page with every rule kept (route visibility, the install-options
contract, the crash overrides, the autotune order, share consent), and three
of its bugs fixed on the way:

* settings live in `self.settings`, not in widgets, so nothing drifts
  between what the page shows and what the install does;
* a setting whose row is not shown for the route never reaches Options -
  the 1.9 window sent the feeder's preset and HDR to OptiScaler too;
* nvngx_dlss has an "auto" choice, so an untouched page no longer pins the
  first build the catalog happened to list.

The game's detection (dlss.detect, anti-cheat, manifests) runs on a worker
when the page opens; the 1.9 window did it on the Tk thread on every pick.
"""
from __future__ import annotations

import threading
import traceback
import webbrowser
from pathlib import Path

from .. import (anticheat, autopilot, autotune, community, components, diagnose, dlss, dxvk,
                feedcfg, games, gpu, installer, log, net, optiscaler, pe, prefs, profiles,
                reengine, reshade_ini, sources, update, video, watch, wincrash)
from .. import mfg as _mfg
from .ctl_library import first_line

AUTO = "auto"
DLSSD_KEEP = "keep"
F10_CLASH = ("!! the overlay key is F10, which is also the standalone add-on's "
             "before/after key - pick another one under 'overlay key'")


def first_sentence(text: str, limit: int = 130) -> str:
    t = " ".join(str(text or "").split())
    for stop in (". ", "; "):
        i = t.find(stop)
        if 0 < i < limit:
            return t[:i + 1]
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "\u2026"


def event_epoch(when: str) -> float:
    import calendar
    import time as _t
    try:
        return calendar.timegm(_t.strptime(str(when)[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0


def last_log_write(install_dir) -> float:
    """When the game's own logs were last written (the crash-window bound)."""
    newest = 0.0
    d = Path(install_dir)
    names = [d / "ReShade.log", d / "dlss5-feed.log", d / "OptiScaler.log",
             d / "host64" / "dlss5-feed-host.log", d / "dlss5-bridge.log"]
    try:
        extra = diagnose._opti_log(d)
        if extra:
            names.append(extra)
        from .. import remix as _rx
        names.append(d / getattr(_rx, "LOG", "rtx-remix/logs/remix-dxvk.log"))
    except Exception:
        pass
    for f in names:
        try:
            if f.is_file():
                newest = max(newest, f.stat().st_mtime)
        except OSError:
            pass
    if not newest:
        try:
            newest = float(diagnose._installed_at(d) or 0.0)
        except Exception:
            newest = 0.0
    return newest


def session_start(install_dir) -> float:
    """When the last ReShade session began, or 0.0 when the log cannot say.

    ReShade rewrites its log on every launch and stamps each line with the
    local time of day only, so the start is the file's last write minus the
    span between its first and last stamped lines.
    """
    import re as _re
    f = Path(install_dir) / "ReShade.log"
    # Only the first and the last stamped line matter. This runs on the Tk
    # thread (twice per recorded crash), and reading and matching up to 4 MB
    # of log froze the window for as long; two small reads answer the same.
    part = 64 * 1024
    try:
        st = f.stat()
        mtime, size = st.st_mtime, st.st_size
        with f.open("rb") as fh:
            head = fh.read(part)
            tail_at = max(0, size - part)
            if tail_at:
                fh.seek(tail_at)
                tail = fh.read(part)
            else:
                tail = head
    except OSError:
        return 0.0
    stamp = _re.compile(r"^(\d\d):(\d\d):(\d\d):(\d{3})")

    def stamped(blob: bytes, base: int, skip_first: bool):
        """(offset in the file, seconds) of every stamped line in a chunk."""
        out, at = [], base
        lines = blob.split(b"\n")
        for i, raw in enumerate(lines):
            here = at
            at += len(raw) + 1
            if i == 0 and skip_first:
                continue            # a chunk read from the middle starts inside a line
            m = stamp.match(raw.decode("utf8", "replace"))
            if m:
                h, mi, se, ms = (int(x) for x in m.groups())
                out.append((here, h * 3600 + mi * 60 + se + ms / 1000.0))
        return out

    first = stamped(head, 0, False)[:1]
    last = stamped(tail, tail_at, bool(tail_at))[-1:]
    if not first or not last or first[0][0] >= last[0][0]:
        return 0.0
    return mtime - ((last[0][1] - first[0][1]) % 86400)


def crash_is_this_session(crash, install_dir) -> bool:
    when = event_epoch(getattr(crash, "when", ""))
    ran = last_log_write(install_dir)
    if not when or not ran:
        return True
    # A fault from before the session began belongs to an earlier launch:
    # #250's GameWorks crash came 21 seconds before ReShade's first line and
    # the old five-minute window took it for this session.
    start = session_start(install_dir)
    if start and start <= ran:
        return when >= start - 5
    return when >= ran - 300


def fault_in_this_folder(crash, install_dir) -> bool:
    mod = str(getattr(crash, "module", "") or "").replace("/", "\\")
    if not ("\\" in mod or ":" in mod):
        return True
    try:
        here = str(Path(install_dir).resolve()).replace("/", "\\").lower()
    except (OSError, ValueError, TypeError):
        return True
    return here.rstrip("\\") in mod.lower()


class GameControl:
    # ================================================================ state
    def _game_init(self) -> None:
        self.game = None
        self.support = None
        self.route = ""
        self.route_fit: dict = {}
        self.notes: list[tuple[str, str]] = []
        self.catalog: dict = {}
        self.feeder_tags: list[tuple[str, bool]] = []
        self.entering = False
        self.action = ""            # installing / autopilot / diagnosing / uninstalling / preview ...
        self.progress: tuple | None = None
        self.result: dict | None = None     # what the page shows after an action
        self.steps: list[tuple[str, str]] = []
        self.seen_exe = None
        self.profile = "(none)"
        self.profile_extra = None
        self._auto_stop = False
        self._community = None
        self._watcher = None
        self._noted_routes: set = set()
        self._f10_said = None
        self._why_full = self._tier_full = ""
        self.settings: dict = {}
        self.target_fps = prefs.get("target_fps") or ""
        # The game a running job belongs to. A person may open another game
        # while an autopilot pass waits for the first one to close; what the
        # job says when it ends belongs to its own game, not the page on screen.
        self.job_game = None
        self._prog_job = None
        self._seed_from_record = False
        self._route_notes: list[tuple[str, str]] = []
        self._defaults_for = None
        self._forget_last_session()

    def _forget_last_session(self) -> None:
        self._last_diag = None
        self._last_crash = None
        self._tune = None
        self._measured = None
        self._measured_rows = None
        self._noted_routes = set()
        self.result = None
        self.seen_exe = None

    def default_settings(self, g) -> dict:
        try:
            prev = installer._previous_manifest(g.install_dir) or {}
            swapped = bool((prev.get("components") or {}).get("remix_runtime"))
        except Exception:
            swapped = False
        return {
            "provider": 3, "renodx": AUTO, "dlssnr": AUTO, "dlss": AUTO, "dlssd": DLSSD_KEEP,
            "keep_dlss": True, "workres": 100, "preset": 0, "hdr": -1, "nr_preset": 0,
            "nr_style": 0, "fg": False, "feeder": "stable", "opti_build": "",
            "dxvk": False, "mfg": False, "vr": False, "remix_swap": swapped,
            "opti_proxy": "", "reshade_proxy": "",
        }

    def _defaults(self) -> dict:
        """default_settings for the game on the page, read once per entry.

        changed() asks it for every row on every redraw, and each ask read
        the install record from disk on the Tk thread."""
        g = self.game
        hit = self._defaults_for
        if hit is None or hit[0] is not g:
            hit = (g, self.default_settings(g) if g is not None else {})
            self._defaults_for = hit
        return hit[1]

    # ================================================================ entering a game
    def enter_game(self, g) -> None:
        if self.game is not g:
            self._forget_last_session()
            self.settings = self.default_settings(g)
            self.profile, self.profile_extra = "(none)", None
            # A game that is installed opens on what it was installed with,
            # once per entry: re-entering after an api or exe change keeps
            # what the person has set since.
            self._seed_from_record = True
        self.game = g
        self._defaults_for = None
        self.support = None
        self.entry = {}
        self.entering = True
        self.watch_this_game(g)
        sm = self._sm()

        def work():
            try:
                drv = gpu.driver_version()
                sup = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0, sm, driver=drv)
                fit = {o: dlss.fit(o, g.api, sup.native_dlss, sm, upscaler=getattr(sup, "upscaler", ""))
                       for o in sup.options}
                ac = anticheat.detect(g.install_dir, g.folder)
                extra = {
                    "ac": ac,
                    "reengine": sup.recommended != dlss.REMIX and reengine.detected(g.install_dir),
                    "shared": games._recorded_exe(g.install_dir),
                    "dxvk": bool(installer.wants_dxvk(g)),
                    "ok": installer.check_supported(g),
                    "seen": self._seen_other_exe_for(g),
                }
                extra.update(self._entry_reads(g, sm))
                self.q.put(("entered", (g, sup, fit, extra)))
            except Exception:
                log.exception(f"reading {g.name}")
                self.q.put(("enterfail", (g, traceback.format_exc())))
        threading.Thread(target=work, daemon=True).start()
        if not self.catalog:
            self.load_catalog()

    @staticmethod
    def _entry_reads(g, sm) -> dict:
        """What the settings rows ask on every redraw and cannot ask the disk
        there: the multi-frame-generation answer (a bounded walk of the whole
        game folder for DLSS-G), the renodx builds in Downloads/Desktop, the
        install record. Worker thread only - read once per entry."""
        out: dict = {}
        try:
            out["mfg"] = bool(_mfg.applies(sm, g.api, g.install_dir, g.folder)[0])
        except Exception:
            out["mfg"] = False
        try:
            out["renodx_local"] = {sf: prefs.find_renodx(sf=sf)[0] for sf in (False, True)}
        except Exception:
            out["renodx_local"] = {}
        try:
            out["dlss_beside"] = (Path(g.install_dir) / "nvngx_dlss.dll").is_file()
        except OSError:
            out["dlss_beside"] = False
        try:
            out["manifest"] = installer._previous_manifest(g.install_dir) or {}
            out["installed_opt"] = installer.options_from_manifest(g.install_dir) if out["manifest"] else None
        except Exception:
            out["manifest"], out["installed_opt"] = {}, None
        return out

    def _on_entered(self, payload) -> None:
        g, sup, fit, extra = payload
        if g is not self.game:
            return
        self.entering = False
        self.support, self.route_fit = sup, fit
        self.entry = extra
        self._defaults_for = None
        self.settings["dxvk"] = extra.get("dxvk", False)
        self.seen_exe = extra.get("seen")
        self._why_full = str(sup.reason or "")
        tier = gpu.tier_note(self._sm())
        self._tier_full = str(tier or "")
        self.write("")
        self.write(f"=== {g.name} ===", "head")
        if extra["dxvk"]:
            self.write(f"> {g.exe.name if g.exe else g.name} closes itself when ReShade loads inside it - it "
                       f"runs through DXVK (Vulkan) instead, with ReShade as a Vulkan layer.", "ok")
        if sup.native_dlss:
            self.write(f"> this game ships its own dlss ({', '.join(sup.evidence[:3])})", "ok")
        elif getattr(sup, "upscaler", ""):
            self.write(f"> no dlss, but the game ships {sup.upscaler.upper()} "
                       f"({', '.join(sup.upscaler_evidence[:2])}) - optiscaler can redirect those calls "
                       f"into dlss, then neural rendering", "ok")
        elif g.api in ("DX11", "DX12", "Unknown") and g.bitness == 64:
            self.write(f"> no dlss files found under {g.folder} - the native and optiscaler routes need "
                       f"the game's own dlss. if this game does have dlss, use 'report a bug' under help "
                       f"and say where the nvngx_dlss.dll is.", "warn")
        if self._why_full:
            self.write(f"> {first_sentence(self._why_full)}")
        if tier:
            self.write(f"> {first_sentence(tier)}")
        self._gpu_note()
        cands = g.candidates or ([g.exe] if g.exe else [])
        if len(cands) > 1:
            self.write(f"!! this folder has {len(cands)} executables; selected {g.exe.name} - if the game "
                       f"launches a different one, change 'target exe' in settings", "warn")
        if not extra.get("ok", (True, ""))[0] and getattr(g, "exe_warning", ""):
            try:
                self.game_page.settings_open = True
            except Exception:
                pass
        route = sup.recommended
        # The route and settings it was installed with, not the tool's pick
        # for a new install: 'update (n)' and 'install again' send opts(), and
        # a game set up on another route (autopilot, try <route>, by hand)
        # was being moved to the recommended one with every setting reset.
        seed = extra.get("installed_opt") if self._seed_from_record else None
        self._seed_from_record = False
        if seed is not None and seed.path not in sup.options:
            self.write(f"> installed on the {seed.path} route, which this game is not offered now - "
                       f"showing {route}", "warn")
            seed = None
        if seed is not None:
            route = seed.path
        elif self.profile_extra is not None and self.profile_extra.path in sup.options:
            route = self.profile_extra.path
        self.apply_route(route)
        if seed is not None:
            self._settings_from(seed, extra.get("manifest") or {})
        self.refresh("game")

    def _on_enterfail(self, payload) -> None:
        g, text = payload
        if g is not self.game:
            return
        self.entering = False
        self.write(text.strip().splitlines()[-1], "err")
        self.refresh("game")

    def _gpu_note(self) -> None:
        try:
            card, _sm = gpu.detect()
        except Exception:
            card = None
        if card:
            return
        vendor = None
        try:
            vendor = gpu.other_vendor()
        except Exception:
            pass
        self.write("!! no NVIDIA card detected - dlss 5 will not run here", "warn")
        if vendor == "AMD":
            for line in gpu.AMD_ANSWER.splitlines():
                self.write("   " + line.strip())

    def watch_this_game(self, g) -> None:
        if g is None:
            return
        try:
            man = installer._previous_manifest(g.install_dir) or {}
            if not man:
                return
            if self._watcher is None:
                self._watcher = watch.Recorder()
            self._watcher.add(g.install_dir, list(man.get("files") or []), exe=str(man.get("exe") or ""))
        except Exception:
            pass

    # ================================================================ routes
    def route_label(self, o: str) -> str:
        usable, note = self.route_fit.get(o, (True, ""))
        label = dlss.LABELS[o].split(" - ")[0]
        if not usable:
            return f"{label}  (not for this pc)"
        if self.support and o == self.support.recommended:
            return f"{label}  (recommended)"
        return label

    def level(self, route: str | None = None) -> str:
        if not self.game:
            return ""
        try:
            return installer.reliability(self.game, route or self.route)[0]
        except Exception:
            return ""

    def apply_route(self, path: str) -> None:
        """Choose a route: its notes, and the settings it cannot use set back."""
        g = self.game
        self.route = path
        s = self.settings
        usable, note = self.route_fit.get(path, (True, ""))
        parts: list[tuple[str, str]] = []
        if g and g.exe_warning:
            parts.append(("warn", games.XBOX_EXE_CHOSEN))
        if not usable:
            parts.append(("bad", f"not for this pc - {note}."))
        try:
            foreign = installer.other_ngx_hooks(g.install_dir, path) if g else []
        except Exception:
            foreign = []
        for kind, line in getattr(dlss, "CONFLICTS", {}).get(path, ()):
            if kind == "folder" and foreign:
                parts.append(("warn", f"{', '.join(foreign[:3])} in this folder - {line}"))
        for line in dlss.quirks(g.exe if g else None, g.api if g else ""):
            parts.append(("warn", line))
        try:
            warn = dlss.driver_warning(path, gpu.driver_version(),
                                       offered=getattr(self.support, "options", None))
        except Exception:
            warn = None
        if warn:
            parts.append(("driver", first_sentence(warn, 150)))
        ac = getattr(self, "entry", {}).get("ac")
        if ac is not None and ac.present:
            parts.append(("bad", f"{ac.summary} is installed here ({ac.found}) - an online game's anti-cheat "
                                 f"can refuse to start, remove the files or ban the account"))
        if getattr(self, "entry", {}).get("reengine"):
            parts.append(("note", "RE Engine (Capcom) game - REFramework goes in first so ReShade survives"))
        shared = getattr(self, "entry", {}).get("shared")
        if shared and g and g.exe and shared.lower() != g.exe.name.lower():
            parts.append(("note", f"this folder is already set up for {shared}; both executables use the "
                                  f"same files"))
        if getattr(g, "emu", None):
            parts.append(("note", g.emu.renderer_hint))
        self._route_notes = parts
        # what this route cannot use goes back to its default
        if not self.shown_setting("fg"):
            s["fg"] = False
        if not self.shown_setting("dxvk"):
            s["dxvk"] = False
        if not self.shown_setting("mfg"):
            s["mfg"] = False
        if not self.shown_setting("vr"):
            s["vr"] = False
        if not self.shown_setting("remix_swap"):
            s["remix_swap"] = False
        if not self.shown_setting("dlssd"):
            s["dlssd"] = DLSSD_KEEP
        if self.work_applies():
            if path == dlss.OPTI:
                if s["workres"] == 100 or s["workres"] < 50:
                    s["workres"] = optiscaler.NR_SCALE_DEFAULT
            elif s["workres"] < 50:
                s["workres"] = 100
        else:
            s["workres"] = 100
        if g and path == dlss.STANDALONE and reshade_ini.overlay_key_name() == "F10":
            k = (str(g.install_dir), path)
            if self._f10_said != k:
                self._f10_said = k
                self.write(F10_CLASH, "warn")
        self._refresh_notes()
        if g:
            level, why = installer.reliability(g, path)
            self.write(f"> route: {dlss.LABELS[path]}  [{level}]", "head")
            self.write(f"  {first_sentence(why)}")
            if not usable:
                self.write(f"  !! not for this pc: {note}", "warn")
            try:
                self.write(f"  plan: {' -> '.join(installer.plan(g, self.opts()))}")
            except Exception:
                pass
            if path not in self._noted_routes:
                self._noted_routes.add(path)
                self.community_note()
                self.hdr_note()

    def _refresh_notes(self) -> None:
        """The notes on the page: what the route says, and before those what
        the current settings put at risk. The runtime swap (#225) and the F10
        clash (#214) were written to the log only, and the log is closed."""
        g, s = self.game, self.settings
        mine: list[tuple[str, str]] = []
        if g is not None and s:
            if self.shown_setting("keep_dlss") and not s.get("keep_dlss", True) and (
                    getattr(self.support, "native_dlss", False) or (getattr(self, "entry", {}) or {}).get(
                        "dlss_beside")):
                mine.append(("bad", self._swap_note("nvngx_dlss.dll")))
            if self.shown_setting("dlssd") and s.get("dlssd", DLSSD_KEEP) != DLSSD_KEEP:
                mine.append(("bad", self._swap_note("nvngx_dlssd.dll")))
            if self.route == dlss.STANDALONE and reshade_ini.overlay_key_name() == "F10":
                mine.append(("warn", F10_CLASH[3:] if F10_CLASH.startswith("!! ") else F10_CLASH))
        self.notes = mine + list(self._route_notes)

    @staticmethod
    def _swap_note(name: str) -> str:
        return (f"{name} is swapped: the game's own file is replaced - an online game's anti-cheat can treat "
                f"that as tampering, and a launcher that verifies its files puts its own back. Keep the "
                f"game's own for anything played online.")

    def has_rr(self) -> bool:
        sup = self.support
        if sup is None or self.game is None:
            return False
        return any(str(e).lower().endswith("nvngx_dlssd.dll") for e in (getattr(sup, "evidence", None) or []))

    def work_applies(self, route: str | None = None) -> bool:
        """OptiScaler always; the feeder on the 64-bit D3D11 path only - and not
        through DXVK, where the add-on runs on Vulkan and ignores the value."""
        g = self.game
        route = route or self.route or dlss.FEEDER
        if route == dlss.OPTI:
            return True
        if route != dlss.FEEDER:
            return False
        return bool(g and g.bitness == 64 and g.api == "DX11" and not self.settings.get("dxvk"))

    def shown_setting(self, key: str) -> bool:
        """Is this setting on the page for the route? The one rule the page,
        Options and the reset in apply_route all read.

        Taken from what the installer reads per route, not from what the 1.9
        window showed: Remix reads only dlssnr and the runtime swap, OptiScaler
        returns before ReShade, the add-on and the feed configs, DXVK is refused
        on renodx/upstream/standalone/optiscaler/remix and forced on DX9, and
        preset/hdr are the feeder's alone.
        """
        g, r = self.game, self.route
        opti, feeder, remix = r == dlss.OPTI, r == dlss.FEEDER, r == dlss.REMIX
        api = g.api if g else ""
        reshade_routes = r in (dlss.NATIVE, dlss.BRIDGE, dlss.FEEDER, dlss.RENODX, dlss.UPSTREAM, dlss.STANDALONE)
        # Only the rule asked is evaluated. This is asked for every row on
        # every redraw (and by apply_route and opts()); building the whole
        # table each time ran every rule, the disk-reading ones included,
        # about 26 times a redraw.
        rules = {
            # a protected Xbox executable cannot be read: its architecture is
            # asked, like its graphics api (#157)
            "bits": lambda: bool(g) and bool(getattr(g, "exe_warning", "")),
            "exe": lambda: bool(g and len(g.candidates or []) > 1),
            "route": lambda: True,
            "api": lambda: bool(g) and getattr(g, "kind", "") != "video",
            "opti_proxy": lambda: opti,
            "provider": lambda: feeder,
            "reshade_proxy": lambda: reshade_routes and api != "Vulkan"
            and not (api == "DX9" or bool(self.settings.get("dxvk"))),
            "renodx": lambda: r in (dlss.NATIVE, dlss.BRIDGE, dlss.FEEDER, dlss.RENODX),
            "dlssnr": lambda: True,
            # With 'keep the game's own' on and the game's file beside the
            # executable, the install leaves that file alone and never reads
            # this choice; a game with none still gets the build picked here.
            "dlss": lambda: r not in (dlss.UPSTREAM, dlss.REMIX) and not (
                bool(self.settings.get("keep_dlss", True)) and self.shown_setting("keep_dlss")
                and bool((getattr(self, "entry", {}) or {}).get("dlss_beside"))),
            "keep_dlss": lambda: r not in (dlss.UPSTREAM, dlss.REMIX) and (g is None or g.bitness != 32),
            "dlssd": lambda: self.has_rr() and reshade_routes,
            "workres": lambda: self.work_applies(),
            "preset": lambda: feeder,
            "hdr": lambda: feeder,
            "nr_preset": lambda: opti,
            "nr_style": lambda: opti,
            "fg": lambda: opti and api == "DX12",
            "feeder": lambda: feeder,
            "opti_build": lambda: opti,
            "dxvk": lambda: bool(g) and r in (dlss.NATIVE, dlss.BRIDGE, dlss.FEEDER) and api == "DX11",
            "mfg": lambda: bool(g) and reshade_routes and self._mfg_applies(),
            "overlay_key": lambda: bool(g) and not remix,
            "vr": lambda: bool(g) and reshade_routes and (g.bitness or 64) == 64,
            "remix_swap": lambda: bool(g) and remix,
        }
        rule = rules.get(key)
        return bool(rule()) if rule is not None else False

    def _mfg_applies(self) -> bool:
        """Read once when the game is entered (a walk of the whole game folder
        for DLSS-G - never on the Tk thread); False until that read lands."""
        return bool((getattr(self, "entry", {}) or {}).get("mfg"))

    def _renodx_local(self, sf: bool):
        """The renodx build of your own for this family, as read on entry."""
        return ((getattr(self, "entry", {}) or {}).get("renodx_local") or {}).get(bool(sf))

    # ================================================================ settings
    def set_setting(self, key: str, value) -> None:
        s = self.settings
        old = s.get(key)
        s[key] = value
        g = self.game
        if key == "route":
            self.apply_route(value)
        elif key == "keep_dlss" and not value and g is not None and (
                (g.install_dir / "nvngx_dlss.dll").is_file() or getattr(self.support, "native_dlss", False)):
            self.warn_swap("nvngx_dlss.dll")
        elif key == "dlssd" and value != DLSSD_KEEP:
            self.warn_swap("nvngx_dlssd.dll")
        elif key == "opti_proxy" and value and g is not None:
            p = g.install_dir / value
            if p.is_file() and not optiscaler.is_optiscaler(p):
                self.write(f"  note: this game already has its own {value}. It is backed up and restored "
                           f"on uninstall, but another name is safer.")
        elif key == "feeder" and value not in ("stable", "pre"):
            self.write(f"> feeder pinned to {value} - the matching DLSS 5 add-on build is chosen for it")
            self.hdr_note(value)
        elif key == "overlay_key":
            vk = reshade_ini.OVERLAY_KEYS.get(value, 0)
            prefs.set_("overlay_key", 0 if "default" in value else vk)
            s.pop("overlay_key", None)
            if "default" not in value:
                self.write(f"> the overlay will open on {value} - press install to write it into this game")
            if value == "F10" and self.route == dlss.STANDALONE:
                self.write(F10_CLASH, "warn")
        elif key == "target_fps":
            s.pop("target_fps", None)
            try:
                v = int(float(value)) if value else 0
            except (TypeError, ValueError, OverflowError):
                v = 0
            if v and not 20 <= v <= 480:
                v = 0
            self.target_fps = v or ""
            prefs.set_("target_fps", self.target_fps)
        if key in ("keep_dlss", "dlssd", "overlay_key", "dxvk"):
            self._refresh_notes()
        if old != value:
            self.refresh("game", soft=True)

    def warn_swap(self, name: str) -> None:
        self.write("")
        for line in anticheat.swap_message(name).splitlines():
            self.write(f"!! {line}" if line.strip() else "", "warn")

    def overlay_key(self) -> str:
        try:
            vk = int(prefs.get("overlay_key") or 0)
        except (TypeError, ValueError):
            vk = 0
        for name, code in reshade_ini.OVERLAY_KEYS.items():
            if code == vk and (vk or "default" in name):
                return name
        return "route default"

    def set_api(self, chosen: str) -> None:
        g = self.game
        if not g:
            return
        games.set_api_override(g.folder, chosen or None)
        detected = getattr(g, "api_detected", "") or g.api
        if chosen:
            g.api, g.api_why = chosen, f"set by hand (detected {detected})"
        else:
            try:
                g.api, g.api_why = pe.detect_api(g.exe)
            except Exception:
                g.api, g.api_why = detected, "detected from the executable"
        self.write(f"> graphics api: {g.api}" + ("" if chosen else " (auto)"))
        self.forget_row(g)
        self.root.after(1, self.remember_library)
        self.enter_game(g)
        self.refresh("game")

    def set_bitness(self, bits) -> None:
        g = self.game
        if not g or not g.exe_warning:
            return
        games.set_bitness_override(g.folder, bits)
        g.bitness = bits
        self.forget_row(g)
        self.remember_library()
        self.enter_game(g)
        self.refresh("game")

    def set_exe(self, exe) -> None:
        g = self.game
        if not g or exe == g.exe:
            return
        self._forget_last_session()
        if exe not in (g.candidates or []):
            g.candidates = list(g.candidates or []) + [exe]
        g.exe = exe
        g.emu = None
        games.enrich(g, chosen=True)
        self.write(f"> target exe -> {g.exe.name}  ({g.bit_label} {g.api}); installing into {g.install_dir}",
                   "head")
        ok, why = installer.check_supported(g)
        if not ok:
            self.write(f"  not supported: {why}", "err")
        self.forget_row(g)
        self.remember_library()
        self.enter_game(g)
        self.refresh("game")

    def reset_settings(self) -> None:
        if not self.game:
            return
        keep_route = self.route
        self.settings = dict(self._defaults())
        self.settings["dxvk"] = bool(getattr(self, "entry", {}).get("dxvk"))
        self.profile, self.profile_extra = "(none)", None
        if self.support:
            self.apply_route(self.support.recommended if keep_route not in self.support.options
                             else self.support.recommended)
        self.write("> every setting is back on auto")
        self.refresh("game", soft=True)

    def changed(self, key: str) -> bool:
        """Did the person change this one? Only then is it drawn as changed."""
        if key == "route":
            return bool(self.support) and self.route != self.support.recommended
        if key == "overlay_key":
            return "default" not in self.overlay_key()
        if key == "api":
            return bool(self.game) and bool(games.api_override(self.game.folder))
        if key in ("exe", "bits"):
            return False
        d = self._defaults() if self.game else {}
        if key == "dxvk":
            return self.settings.get("dxvk") != bool(getattr(self, "entry", {}).get("dxvk"))
        if key == "workres":
            base = optiscaler.NR_SCALE_DEFAULT if self.route == dlss.OPTI else 100
            return self.settings.get("workres") != base
        return key in d and self.settings.get(key) != d[key]

    # ================================================================ catalog
    def load_catalog(self) -> None:
        def work():
            try:
                self.q.put(("catalog", sources.rhi_catalog()))
            except Exception as e:
                self.q.put(("log", (f"!! could not fetch the version list: {e}", "warn")))
            try:
                self.q.put(("feeders", sources.feeder_releases()))
            except Exception as e:
                log.write(f"feeder release list: {e}", "warn")
        threading.Thread(target=work, daemon=True).start()

    def _on_catalog(self, cat: dict) -> None:
        self.catalog = cat or {}
        if sources.last_fallback:
            self.write(f"!! {sources.last_fallback}", "warn")
        counts = {k: len(self.catalog.get(k, [])) for k in ("renodx", "dlssnr", "dlss", "dlssd")}
        self.write(f"> versions: renodx {counts['renodx']}, dlssnr {counts['dlssnr']}, dlss {counts['dlss']}"
                   + (f", ray reconstruction {counts['dlssd']}" if counts["dlssd"] else ""))
        self.refresh("game", soft=True)

    def _on_feeders(self, rels) -> None:
        self.feeder_tags = list(rels or [])
        self.refresh("game", soft=True)

    def choices(self, key: str) -> list[tuple[str, str]]:
        """(value, label) for a dropdown setting."""
        g = self.game
        if key == "route":
            return [(o, self.route_label(o)) for o in (self.support.options if self.support else [])]
        if key == "bits":
            return [("", "not set"), (64, "64-bit"), (32, "32-bit")]
        if key == "exe":
            out = []
            for c in (g.candidates or []):
                try:
                    out.append((c, str(c.relative_to(g.folder))))
                except ValueError:
                    out.append((c, str(c)))
            return out
        if key == "api":
            detected = getattr(g, "api_detected", "") or g.api
            return [("", "auto")] + [(a, _api_label(a)) for a in games.APIS]
        if key == "renodx":
            fam = "renodx_sf" if self.route == dlss.RENODX else "renodx"
            out = [(AUTO, "auto")] + [(e["label"], e["label"]) for e in self.catalog.get(fam, [])]
            # read on entry: find_renodx opens every add-on in Downloads and
            # on the Desktop, and this list is built on every redraw
            found = self._renodx_local(self.route == dlss.RENODX)
            if found:
                out.insert(1, ("local", f"your file: {found.name}"))
            return out
        if key == "dlssnr":
            return [(AUTO, "auto")] + [(e["label"], e["label"]) for e in self.catalog.get("dlssnr", [])]
        if key == "dlss":
            return [(AUTO, "auto")] + [(e["label"], e["label"]) for e in self.catalog.get("dlss", [])]
        if key == "dlssd":
            return [(DLSSD_KEEP, "keep the game's own")] + [(e["label"], e["label"])
                                                           for e in self.catalog.get("dlssd", [])]
        if key == "opti_proxy":
            return [("", "auto")] + [(n, n) for n in optiscaler.PROXY_NAMES]
        if key == "reshade_proxy":
            return [("", "auto")] + [(n, n) for n in installer.RESHADE_PROXIES]
        if key == "provider":
            # On OpenGL the installer swaps LumeniteFX for VORT (it reads 0%
            # motion there), so those two are not offered as if they worked.
            gl = bool(g) and g.api == "OpenGL"
            return [(k, v[0]) for k, v in reshade_ini.PROVIDERS.items() if not (gl and k in (3, 4))]
        if key == "preset":
            return [(k, "auto" if k == 0 else v) for k, v in feedcfg.PRESETS.items()]
        if key == "hdr":
            return [(k, "auto" if k == -1 else v.lower()) for k, v in feedcfg.HDR.items()]
        if key == "nr_preset":
            return [(k, "auto" if k == 0 else v.lower()) for k, v in optiscaler.NR_PRESETS.items()]
        if key == "nr_style":
            return [(k, v.lower()) for k, v in optiscaler.NR_STYLES.items()]
        if key == "feeder":
            return [("stable", "newest release"), ("pre", "newest pre-release")] + [
                (t, t + ("  (pre-release)" if pre or "beta" in t.lower() else "")) for t, pre in self.feeder_tags]
        if key == "opti_build":
            return [(k, v.split("  -  ")[0]) for k, v in optiscaler.BUILDS.items()]
        if key == "overlay_key":
            return [(k, k) for k in reshade_ini.OVERLAY_KEYS]
        return []

    def auto_says(self, key: str) -> str:
        """What 'auto' resolves to, shown dim beside it."""
        g = self.game
        if key == "api":
            detected = getattr(g, "api_detected", "") or (g.api if g else "")
            return _api_label(detected)
        if key == "reshade_proxy":
            return installer._proxy_name(g.api if g else "", "") if g else ""
        if key == "renodx":
            return "newest that works here"
        if key == "dlssnr":
            return "matches your card"
        if key == "dlss":
            return "newest"
        if key == "opti_proxy":
            return "a free name"
        if key == "overlay_key":
            return "Insert" if self.route == dlss.OPTI else "Home"
        return ""

    # ================================================================ the install contract
    def opts(self, route: str | None = None) -> installer.Options:
        """The settings, for the route shown or for another one (autopilot).

        A dial belongs to the route it is shown for: another route gets the
        tool's own defaults for it, never this route's values - and a setting
        whose row the route does not show is never sent at all.
        """
        shown = self.route
        route = route or shown or dlss.FEEDER
        s = self.settings
        other = bool(route and shown and route != shown)
        saved = self.route
        self.route = route           # visibility is asked about the route being built
        try:
            vis = self.shown_setting
            local = None
            renodx = None
            if s.get("renodx") == "local":
                local = self._renodx_local(route == dlss.RENODX)
            elif s.get("renodx") not in (None, AUTO):
                renodx = s["renodx"]
            feed: dict = {}
            if self.work_applies(route) and not other and s["workres"] != 100 and route == dlss.FEEDER:
                feed["work_resolution"] = s["workres"]
            if vis("preset") and not other and s["preset"]:
                feed["preset"] = s["preset"]
            if vis("hdr") and not other and s["hdr"] != -1:
                feed["hdr"] = s["hdr"]
            nr: dict = {}
            if route == dlss.OPTI and other:
                nr["WorkingScale"] = round(optiscaler.NR_SCALE_DEFAULT / 100, 2)
            elif route == dlss.OPTI:
                nr["WorkingScale"] = round(s["workres"] / 100, 2)
                if s["nr_preset"]:
                    nr["Preset"] = s["nr_preset"]
                if s["nr_style"]:
                    nr["Style"] = s["nr_style"]
            extra = self.profile_extra
            # A profile's own feed and model dials (depth, say) go to the
            # route that reads them, and to the route shown only: the feed
            # config is the feeder's, [DlssNr] OptiScaler's.
            if extra is not None and not other:
                if route == dlss.FEEDER:
                    feed = {**(extra.feed or {}), **feed}
                if route == dlss.OPTI:
                    nr = {**(extra.nr or {}), **nr}
            feeder = s.get("feeder", "stable")
            return installer.Options(
                provider=s["provider"],
                renodx=renodx if vis("renodx") else None,
                renodx_local=local if vis("renodx") else None,
                dlssnr=None if s["dlssnr"] == AUTO else s["dlssnr"],
                dlss=None if s["dlss"] == AUTO or not vis("dlss") else s["dlss"],
                dlssd="" if s["dlssd"] == DLSSD_KEEP or not vis("dlssd") else s["dlssd"],
                keep_game_dlss=s["keep_dlss"],
                feed=feed,
                nr=nr,
                feeder_prerelease=route == dlss.FEEDER and feeder == "pre",
                feeder_tag=feeder if route == dlss.FEEDER and feeder not in ("stable", "pre") else "",
                dxvk=bool(s["dxvk"]) and vis("dxvk") and not other,
                fg=bool(s["fg"]) and route == dlss.OPTI and vis("fg") and not other,
                mfg=bool(s["mfg"]) and vis("mfg") and not other,
                vr=bool(s["vr"]) and vis("vr") and not other,
                remix_swap=bool(s["remix_swap"]) and route == dlss.REMIX,
                path=route,
                native_dlss=bool(self.support and self.support.native_dlss),
                upscaler=str(getattr(self.support, "upscaler", "") or ""),
                opti_proxy=s["opti_proxy"] if route == dlss.OPTI else "",
                opti_build=s["opti_build"] if route == dlss.OPTI else "",
                reshade_proxy=s["reshade_proxy"] if vis("reshade_proxy") else "",
            )
        finally:
            self.route = saved

    # ================================================================ actions
    def _anticheat_ok(self, verb: str) -> bool:
        g = self.game
        try:
            ac = anticheat.detect(g.install_dir, g.folder)
        except Exception:
            return True
        if not ac.present:
            return True
        return self.shell.ask(f"{ac.summary} is in this game",
                              f"{ac.found}.\n\nReShade add-ons and anti-cheat do not coexist: the game may "
                              f"refuse to start, delete the files, or ban the account. Nobody but you "
                              f"carries that risk.", verb, "cancel", danger=True)

    def install(self) -> None:
        g = self.game
        if self.busy or not g or self.support is None or not installer.check_supported(g)[0]:
            return
        if not self._anticheat_ok("install anyway"):
            return
        self.busy, self.action, self.progress = True, "installing", (0, "starting")
        self.job_game = g
        self.result = None
        opt = self.opts()
        route = self.route
        self.write("")
        self.write(f"=== {g.name}: install ({self.route}) ===", "head")
        self.refresh("game", soft=True)

        def work():
            # every answer carries its game: the page may show another by then
            try:
                rep = installer.install(
                    g, opt,
                    on_step=lambda i, n, name: self.q.put(("step", (i, n, name))),
                    on_prog=lambda p, m: self.q.put(("prog", (p, m))),
                    on_log=lambda t: self.q.put(("log", t)))
                self.q.put(("installed", (g, rep, route)))
            except installer.InstallError as e:
                self.q.put(("fail", (g, str(e))))
            except Exception as e:
                log.exception("installing", e)
                if net.is_disk_full(e):
                    self.q.put(("fail", (g, net.disk_full_message(e, g.install_dir, net.CACHE))))
                else:
                    self.q.put(("fail", (g, traceback.format_exc())))
        threading.Thread(target=work, daemon=True).start()

    def _job_of(self, payload, size: int) -> tuple:
        """(game, *rest) from a job's answer. An answer without its game (the
        shape before 2.0.0, still sent by a few checks) belongs to the job's
        game, or the page's when no job is recorded."""
        if isinstance(payload, tuple) and len(payload) == size and (
                payload[0] is None or hasattr(payload[0], "install_dir")):
            return payload
        return (self.job_game or self.game, payload) + (None,) * (size - 2)

    def _on_step(self, payload) -> None:
        i, n, name = payload
        self.progress = (int(i * 100 / max(1, n)), name)
        self.shell.busy(f"[{i + 1}/{n}] {name}")
        self.refresh("game", soft=True)

    def _on_prog(self, payload) -> None:
        """A download sends one of these per 256 KB chunk - about 650 for the
        largest component - and the queue hands them over all at once. Each
        one redrew the whole page, and the window stopped answering. The
        value is kept; the button is repainted at most every 100 ms, in place."""
        p, m = payload
        self.progress = (p, m)
        if self._prog_job is not None:
            return
        try:
            self._prog_job = self.root.after(100, self._paint_progress)
        except Exception:
            self._prog_job = None

    def _paint_progress(self) -> None:
        self._prog_job = None
        page = getattr(self, "game_page", None)
        try:
            if page is not None and page.paint_progress():
                return
        except Exception:
            pass
        self.refresh("game", soft=True)

    def _idle(self) -> None:
        self.busy = False
        self.action = ""
        self.progress = None
        self.job_game = None
        if self._prog_job is not None:
            try:
                self.root.after_cancel(self._prog_job)
            except Exception:
                pass
            self._prog_job = None
        self.shell.busy("")

    def _on_installed(self, payload) -> None:
        g, rep, route = self._job_of(payload, 3)
        here = g is not None and self.game is g
        route = route or self.route
        self._idle()
        self.forget_row(g)
        self.dlss_reread(g)
        self.remember_library()
        self.write("")
        self.write(f"> done{'' if here or g is None else f' ({g.name})'} - {len(rep.written)} files written",
                   "ok")
        self.watch_this_game(g)
        for n in rep.notes:
            self.write(f"    {n}")
        for w in rep.warnings:
            self.write(f"!!  {w}", "warn")
        if rep.skipped:
            self.write(f"    left untouched: {', '.join(rep.skipped)}")
        if g is not None:
            self.verdicts.pop(str(g.install_dir), None)
            prefs.set_("last_verdicts", self.verdicts)
        self.watch_refresh()
        self.check_stale()
        if not here:
            # the page shows another game: its steps, result and route are its own
            self.shell.status(f"{g.name if g is not None else 'the game'}: install complete")
            self.refresh("library", soft=True)
            return
        self._defaults_for = None
        if getattr(g, "kind", "") == "video":
            import re as _re
            self.steps = [("warn" if line.startswith("!") else "step", _re.sub(r"^(\d+\.|!)\s*", "", line))
                          for line in video.CHECKLIST]
            self.steps.append(("warn", "neural rendering re-draws everything in the window, menus and "
                                       "subtitles included - use the player fullscreen"))
        else:
            self.steps = self.route_steps(route)
        self._write_steps(self.steps)
        self.result = {"kind": "installed", "title": "installed", "warnings": list(rep.warnings)}
        self.shell.status("install complete")
        self.refresh("game")

    def _write_steps(self, steps) -> None:
        self.write("> now launch the game and:", "head")
        n = 0
        for kind, text in steps:
            if kind == "step":
                n += 1
                self.write(f"   {n}. {text}")
            else:
                self.write(f"   !  {text}", "warn" if kind == "warn" else "")
        self.write("")
        if self.watch_on():
            self.write("> play, then close the game - the answer comes up here by itself.", "head")
        else:
            self.write("> played it? come back and press 'did it work?' - it reads the logs and tells you what "
                       "happened.", "head")

    def _on_fail(self, payload) -> None:
        g, text = self._job_of(payload, 2)
        here = g is None or self.game is g
        self._idle()
        text = str(text)
        self.write(text, "err")
        self.shell.status("failed" if here else f"{g.name}: failed")
        last = text.strip().splitlines()[-1] if text.strip() else "it stopped"
        if here:
            self.result = {"kind": "failed", "title": "it stopped", "detail": last}
        if "Traceback" in text:
            self.offer_crash_report()
        self.refresh("game")

    def autopilot(self, routes: list | None = None, ask: bool = True) -> None:
        """Install and test. `routes` and ask=False continue a pass the person
        already agreed to - the "try <route>" a watched game offers after the
        route it had did not work."""
        g = self.game
        if self.busy or not g or self.support is None or not installer.check_supported(g)[0]:
            return
        if not self._anticheat_ok("go on anyway"):
            return
        opt = self.opts()
        offer = list(self.support.options or [self.route])
        planned = autopilot.plan(self.route or opt.path, offer, self._community, g)
        if routes:
            routes = [r for r in routes if r in offer] or planned
        else:
            routes = planned
        if not routes:
            return
        # a hand-edited settings file can hold anything under a game's key
        kept = self.plans.get(str(g.install_dir)) if isinstance(self.plans, dict) else None
        kept = kept.get("routes") if isinstance(kept, dict) else None
        kept = [r for r in kept if isinstance(r, str)] if isinstance(kept, list) else []
        full_plan = list(dict.fromkeys(kept + planned)) if not ask else list(planned)
        self._pass_plan = (full_plan, [r for r in full_plan if r not in routes])
        if not ask:
            self._autopilot_run(g, opt, routes)
            return
        may, why = autopilot.may_start(g)
        start = f"start {g.exe.name if g.exe else 'the game'}" if may else f"ask you to start the game - {why}"
        if not self.shell.ask(
                "autopilot",
                f"This is experimental. It will:\n\n"
                f"1. install the {routes[0]} route\n"
                f"2. {start}\n"
                f"3. read which DLLs the running game loaded (it reads the process, it writes nothing "
                f"into it)\n"
                f"4. if ours are not in it, wait for you to close the game, install the next route and "
                f"go round again\n\n"
                f"Routes it may try, in order: {', '.join(routes)}. It installs the next one without "
                f"asking again, and stops after those or when you press stop.",
                "go ahead", "cancel"):
            return
        self._autopilot_run(g, opt, routes)

    def _autopilot_run(self, g, opt, routes) -> None:
        self.busy, self.action, self._auto_stop = True, "autopilot", False
        self.job_game = g
        self.result = None
        self.write("")
        self.write(f"=== {g.name}: autopilot ({', '.join(routes)}) ===", "head")
        per_route = {r: self.opts(r) for r in routes}
        pass_plan = getattr(self, "_pass_plan", None)
        offered = list(getattr(self.support, "options", None) or [])

        def work():
            try:
                hooks = autopilot.Hooks(
                    install=lambda gg, oo: installer.install(gg, oo, on_log=lambda t: self.q.put(("log", t))),
                    options=lambda base, route: per_route.get(route, base),
                    log=lambda t, kind="": self.q.put(("log", (t, kind))),
                    stop=lambda: self._auto_stop)
                self.q.put(("autopilot", (g, autopilot.run(g, opt, routes, hooks), pass_plan, offered)))
            except installer.InstallError as e:
                self.q.put(("autofail", (g, str(e), pass_plan, offered)))
            except Exception:
                log.exception("the autopilot pass")
                self.q.put(("autofail", (g, traceback.format_exc(), pass_plan, offered)))
        threading.Thread(target=work, daemon=True).start()
        self.refresh("game", soft=True)

    def stop_autopilot(self) -> None:
        self._auto_stop = True
        self.write("> stopping when this install finishes", "warn")
        self.action = "stopping"
        self.refresh("game", soft=True)

    def _on_autofail(self, payload) -> None:
        g, text, plan, offered = self._job_of(payload, 4)
        self._on_autopilot((g, autopilot.Outcome(stopped=str(text)), plan, offered))
        self.write(str(text), "err")

    def _on_autopilot(self, payload) -> None:
        g, out, pass_plan, offered = self._job_of(payload, 4)
        # The pass waits for the person to close the game, so another game's
        # page is often open by the time it ends. Its plan, row, verdict and
        # watch are its own game's; the page's route and result only when
        # that game is the one on screen.
        here = g is not None and self.game is g
        self._auto_stop = False
        self._idle()
        self.write("")
        self.write(f"> {'' if here or g is None else g.name + ': '}{autopilot.summary(out)}",
                   "ok" if out.ok else "warn")
        left = out.installed or getattr(out, "left", "")
        options = list(offered if offered is not None else (getattr(self.support, "options", None) or []))
        if left and options and left not in options:
            self.write(f"> the folder's record says a {left} install is in it - 'uninstall' takes that out.",
                       "warn")
            left = ""
        tries = []
        for a in getattr(out, "attempts", []) or []:
            tries.append({"route": getattr(a, "route", ""), "ok": bool(getattr(a, "loaded", False)),
                          "note": getattr(a, "note", "") or ("loaded" if getattr(a, "loaded", False) else "")})
        # The pass is remembered: when the game closes and its logs say the
        # route that loaded still did not work, the watcher offers the next.
        plan, before = pass_plan or getattr(self, "_pass_plan", None) or (list(out.tried), [])
        if g is not None:
            self.remember_plan(g, plan, list(before) + list(out.tried))
            # what the card and page said came from the install before this
            # pass (and its offer of a next route): it describes files that
            # are gone now
            self.verdicts.pop(str(g.install_dir), None)
            prefs.set_("last_verdicts", self.verdicts)
            self.dlss_reread(g)
        self.watch_refresh()
        if left and g is not None:
            self.forget_row(g)
            self.watch_this_game(g)
            self.remember_library()
        if here:
            self.result = {"kind": "autopilot", "ok": bool(out.ok), "title": autopilot.summary(out),
                           "tries": tries}
            if left and left in options:
                self.apply_route(left)
            if out.ok and getattr(g, "kind", "") != "video" and left:
                self.steps = self.route_steps(left, running=True)
                self._write_steps(self.steps)
        self.check_stale()
        self.refresh("game")
        self.refresh("library", soft=True)

    def uninstall(self) -> None:
        g = self.game
        if self.busy or not g:
            return
        if not self.shell.ask("uninstall", f"Remove everything this tool put in {g.name}? The game's own "
                                           f"files are restored.", "uninstall", "keep", danger=True):
            return
        self.busy, self.action = True, "uninstalling"
        self.job_game = g
        self.write("")
        self.write("=== uninstalling ===", "head")

        def work():
            try:
                self.q.put(("removed", (g, installer.uninstall(g, on_log=lambda t: self.q.put(("log", t))))))
            except (sources.RateLimited, sources.Unavailable) as e:
                self.q.put(("fail", (g, str(e))))
            except Exception:
                log.exception("uninstalling")
                self.q.put(("fail", (g, traceback.format_exc())))
        threading.Thread(target=work, daemon=True).start()
        self.refresh("game", soft=True)

    def _on_removed(self, payload) -> None:
        g, items = self._job_of(payload, 2)
        here = g is not None and self.game is g
        self._idle()
        self.forget_row(g)
        self.dlss_reread(g)
        self.remember_library()
        if g is not None:
            self.verdicts.pop(str(g.install_dir), None)
            prefs.set_("last_verdicts", self.verdicts)
            self.plans.pop(str(g.install_dir), None)
            prefs.set_("autopilot_plans", self.plans)
        self.write(f"> uninstalled{'' if here or g is None else f' {g.name}'} ({len(items)} items)", "ok")
        self.watch_refresh()
        if here:
            self._defaults_for = None
            self.result = {"kind": "removed", "title": "uninstalled - the game's own files are back"}
            self.steps = []
        self.check_stale()
        self.refresh("game")
        self.refresh("library", soft=True)

    def start_game(self) -> None:
        g = self.game
        if g is None or self.busy:
            return
        if not g.installed:
            self.write("> nothing of ours is installed in this folder yet - the game starts as it always does",
                       "warn")
        self.launching = True
        self.watch_this_game(g)

        def work():
            try:
                ok, said = autopilot.start(g)
            except Exception as ex:
                log.exception("starting the game")
                ok, said = False, f"it could not be started from here ({ex})"
            self.q.put(("launched", (g, ok, said)))
        threading.Thread(target=work, daemon=True).start()
        self.refresh("game", soft=True)

    def _on_launched(self, payload) -> None:
        g, ok, said = payload
        name = g.exe.name if g.exe else g.name

        def again():
            self.launching = False
            self.refresh("game", soft=True)
        self.root.after(10000 if ok else 0, again)
        if ok:
            self.write(f"> started {name}", "ok")
            if said:
                self.write(f"   {said}")
            if g.installed:
                self.write("   play, then close the game - the answer comes up here by itself"
                           if self.watch_on() else
                           "   the tool is watching for it - play, close the game, then press 'did it work?'")
            self.shell.status(f"started {name}")
        else:
            self.write(f"> {said or 'it did not start from here'}", "warn")
            self.shell.status(said or "it did not start from here")

    # ---------------------------------------------------------------- checks
    def preview(self) -> None:
        g = self.game
        if not g or self.busy:
            return
        opt = self.opts()
        self.busy, self.action = True, "preview"
        self.write("")
        self.write("=== what will happen ===", "head")
        self.shell.toggle_log(True)

        def work():
            try:
                self.q.put(("preview", installer.preview(g, opt)))
            except Exception as e:
                log.exception("preview")
                self.q.put(("preview", e))
        threading.Thread(target=work, daemon=True).start()

    def _on_preview(self, pv) -> None:
        self._idle()
        if isinstance(pv, Exception):
            self.write(f"!! preview failed: {pv}", "err")
            return
        for line in installer.preview_lines(pv):
            tag = "err" if line.startswith("cannot") else "warn" if line.startswith(("warning", "outside")) else ""
            self.write(f"   {line}", tag)
        self.write("   nothing is downloaded or written by this preview")

    def check_versions(self) -> None:
        g = self.game
        if self.busy or not g:
            return
        if not g.installed:
            self.write("> nothing is installed in this folder yet", "warn")
            return
        self.busy, self.action = True, "checking"
        self.write("")
        self.write("=== component versions ===", "head")
        self.shell.toggle_log(True)

        def work():
            try:
                self.q.put(("components", components.check(g.install_dir)))
            except Exception:
                log.exception("checking component versions")
                self.q.put(("components", []))
        threading.Thread(target=work, daemon=True).start()

    def _on_components(self, items) -> None:
        self._idle()
        if not items:
            self.write("  could not read any recorded versions - install again to record them", "warn")
            return
        for it in items:
            if getattr(it, "note", ""):
                self.write(f"[!!]   {it.name}: {it.installed} - {it.note}", "warn")
            elif it.outdated:
                self.write(f"[!!]   {it.name}: {it.installed} -> {it.latest}", "warn")
            elif it.installed != it.latest:
                self.write(f"[--]   {it.name}: {it.installed} (current is {it.latest}, a different build)")
            else:
                self.write(f"[ok]   {it.name}: {it.installed}", "ok")
        stale = [i for i in items if i.outdated]
        self.write(f"> {components.summary(items)}", "warn" if stale else "ok")
        if stale:
            self.write("  press update - your settings and backups are kept")

    def compare(self) -> None:
        g = self.game
        if not g:
            return
        try:
            from .. import compareui
            compareui.show(self.root, g.install_dir, g.name)
        except Exception as e:
            log.exception("compare window")
            self.shell.error("before / after", f"could not open the comparison:\n{e}")

    def open_folder(self) -> None:
        if self.game:
            webbrowser.open(str(self.game.install_dir))

    def pick_renodx(self) -> None:
        from tkinter import filedialog
        p = filedialog.askopenfilename(title="select the renodx add-on you downloaded", parent=self.root,
                                       filetypes=[("reshade add-on", "*.addon64 *.addon"), ("all files", "*.*")])
        if not p:
            return
        prefs.remember_renodx(Path(p))
        # the list reads the file found on entry; the one just chosen replaces
        # it for its own family (is_renodx_sf reads one file, not a folder)
        try:
            sf = prefs.is_renodx_sf(Path(p))
            if isinstance(getattr(self, "entry", None), dict):
                self.entry.setdefault("renodx_local", {})[bool(sf)] = Path(p)
        except Exception:
            pass
        self.settings["renodx"] = "local"
        self.write(f"> dlss5 add-on: your own file {Path(p).name}", "ok")
        self.refresh("game", soft=True)

    # ---------------------------------------------------------------- profiles
    def profile_names(self) -> list[str]:
        try:
            return ["(none)"] + profiles.list_profiles()
        except Exception:
            return ["(none)"]

    def load_profile(self, name: str) -> None:
        self.profile, self.profile_extra = name, None
        if name == "(none)":
            self.refresh("game", soft=True)
            return
        try:
            opt = profiles.load(name)
        except Exception as e:
            self.shell.error("profile", str(e))
            return
        self.profile_extra = opt
        # A built-in preset is three work-area steps for any route; it carries
        # no route of its own (Options' default would have moved every game
        # it was picked on to the feeder).
        builtin = profiles.is_builtin(name)
        if builtin:
            pass
        elif self.support and opt.path in self.support.options:
            self.apply_route(opt.path)
        elif opt.path != self.route:
            self.write(f"!! profile wants the {opt.path} route, which this game does not offer - keeping "
                       f"{self.route}", "warn")
        self._settings_from(opt, dials_only=builtin)
        self.write(f"> profile '{name}': " + ", ".join(profiles.describe(opt)), "ok")
        self.refresh("game", soft=True)

    def _settings_from(self, opt, man: dict | None = None, dials_only: bool = False) -> None:
        """Options - a profile, or the record of the install in this folder -
        into the page's settings, for the rows the route shows.

        What each value may be is checked against the dropdown it lands in:
        a record or a profile is a file another version (or a person) wrote.
        Called after the route is set, so visibility is the route's."""
        s, vis = self.settings, self.shown_setting
        feed = opt.feed if isinstance(getattr(opt, "feed", None), dict) else {}
        nr = opt.nr if isinstance(getattr(opt, "nr", None), dict) else {}

        def number(v, default=None):
            try:
                return int(round(float(v)))
            except (TypeError, ValueError, OverflowError):
                return default

        if self.route == dlss.OPTI and nr.get("WorkingScale") is not None:
            try:
                ws = int(round(float(nr["WorkingScale"]) * 100))
            except (TypeError, ValueError, OverflowError):
                ws = None
            if ws is not None and optiscaler.NR_SCALE_MIN <= ws <= optiscaler.NR_SCALE_MAX:
                s["workres"] = ws
        elif self.route == dlss.FEEDER and vis("workres") and feed.get("work_resolution") is not None:
            wr = number(feed["work_resolution"])
            if wr is not None and 50 <= wr <= 100:
                s["workres"] = wr
        if vis("preset") and number(feed.get("preset", 0), 0) in feedcfg.PRESETS:
            s["preset"] = number(feed.get("preset", 0), 0)
        if vis("nr_preset") and number(nr.get("Preset", 0), 0) in optiscaler.NR_PRESETS:
            s["nr_preset"] = number(nr.get("Preset", 0), 0)
        if dials_only:
            self._refresh_notes()
            return
        if vis("hdr") and number(feed.get("hdr", -1), -1) in feedcfg.HDR:
            s["hdr"] = number(feed.get("hdr", -1), -1)
        if vis("nr_style") and number(nr.get("Style", 0), 0) in optiscaler.NR_STYLES:
            s["nr_style"] = number(nr.get("Style", 0), 0)
        if vis("provider") and opt.provider in reshade_ini.PROVIDERS:
            s["provider"] = opt.provider
        # dxvk before the proxy and keep_dlss before dlss: each decides
        # whether the other's row is shown
        if vis("dxvk"):
            s["dxvk"] = bool(opt.dxvk) or bool((man or {}).get("dxvk")) or bool(
                (getattr(self, "entry", {}) or {}).get("dxvk"))
        if vis("keep_dlss"):
            s["keep_dlss"] = bool(opt.keep_game_dlss)
        for key, value in (("fg", opt.fg), ("mfg", opt.mfg), ("vr", opt.vr), ("remix_swap", opt.remix_swap)):
            if vis(key):
                s[key] = bool(value)
        if vis("feeder"):
            tag = str(opt.feeder_tag or "")
            s["feeder"] = tag if tag and tag not in ("stable", "pre") else ("pre" if opt.feeder_prerelease
                                                                            else "stable")
        if vis("opti_build") and opt.opti_build in optiscaler.BUILDS:
            s["opti_build"] = opt.opti_build
        if vis("opti_proxy"):
            s["opti_proxy"] = opt.opti_proxy if opt.opti_proxy in optiscaler.PROXY_NAMES else ""
        proxy = str(opt.reshade_proxy or "")
        g = self.game
        if man is not None and g is not None and proxy == installer._proxy_name(g.api, ""):
            # the record keeps the name ReShade went in under; the name auto
            # picks anyway stays auto, and is not drawn as a change
            proxy = ""
        if vis("reshade_proxy"):
            s["reshade_proxy"] = proxy if proxy in installer.RESHADE_PROXIES else ""
        if vis("dlssd"):
            s["dlssd"] = str(opt.dlssd) if getattr(opt, "dlssd", "") else DLSSD_KEEP
        # Builds by label: None is auto. The install record keeps what went
        # in, not whether it was asked for, so an update stays on auto and
        # fetches the newest - which is what update means.
        for key in ("renodx", "dlssnr", "dlss"):
            v = getattr(opt, key, None)
            if vis(key):
                s[key] = str(v) if isinstance(v, str) and v else AUTO
        self._refresh_notes()

    def save_profile(self) -> None:
        name = self.shell.ask_text("save as a profile", "A name for these settings - pick it on any game.")
        if not name:
            return
        try:
            p = profiles.save(name.strip(), self.opts())
        except Exception as e:
            self.shell.error("profile", str(e))
            return
        self.profile = name.strip()
        self.write(f"> profile saved: {p.name} - pick it from 'profile' on any game", "ok")
        self.refresh("game", soft=True)

    def delete_profile(self) -> None:
        name = self.profile
        if name == "(none)" or profiles.is_builtin(name):
            self.shell.info("profile", "pick one of your own profiles to delete")
            return
        if self.shell.ask("delete profile", f"Delete the profile '{name}'?", "delete", "keep", danger=True):
            profiles.delete(name)
            self.profile, self.profile_extra = "(none)", None
            self.refresh("game", soft=True)

    # ---------------------------------------------------------------- notes before the install
    def community_note(self) -> None:
        g = self.game
        if g is None:
            return
        route, applies = self.route, self.work_applies()

        def work():
            try:
                data = community.fetch()
                entry = community.for_game(data, g)
                lines = community.advice(entry, route, gpu.driver_version() or "")
                rows = _route_rows(entry)
            except Exception:
                return
            try:
                said = community.measured_note(entry, route) if applies else ""
                if said:
                    lines.append(said)
            except Exception:
                pass
            self.q.put(("community", (g.install_dir, data, lines, rows)))
        threading.Thread(target=work, daemon=True).start()

    def _on_community(self, payload) -> None:
        where, data, lines, rows = payload
        if data is not None:
            self._community = data
        if self.game is None or self.game.install_dir != where:
            return
        if rows is not None:
            self.others, self.others_for = rows, str(where)
        if lines:
            self.write("")
            self.write("=== what other people found ===", "head")
            for ln in lines:
                self.write(f"> {ln}")
        self.refresh("game", soft=True)

    def hdr_note(self, tag: str = "") -> None:
        if self.route not in ("", dlss.FEEDER):
            return
        try:
            if gpu.hdr_on() is not True:
                return
            if tag and sources.feeder_key(tag) >= sources.feeder_key(sources.FEEDER_HDR_MIN):
                return
        except Exception:
            return
        if tag:
            self.write(f"!! this display is in HDR, and feeder {tag} is older than {sources.FEEDER_HDR_MIN} - "
                       f"its highlights come out blown or flat. Set 'feeder build' back to the newest "
                       f"release, or turn HDR off in Windows while you play.", "warn")
        else:
            self.write(f"> this display is in HDR: feeder {sources.FEEDER_HDR_MIN} or newer handles it, and "
                       f"the newest release will normally be that.")

    # ================================================================ diagnosis
    def diagnose(self) -> None:
        g = self.game
        if self.busy or not g:
            return
        self.busy, self.action = True, "diagnosing"
        self.job_game = g
        route, fallback, applies = self.route or "", self.settings.get("workres", 100), self.work_applies()

        def work():
            try:
                rep = diagnose.analyse(g.install_dir, installer.last_failure(g.install_dir))
                m = self._measure_session(g.install_dir, route, fallback) if applies and rep.ran else None
                self.q.put(("diagnosed", (g.install_dir, rep, m)))
            except Exception:
                log.exception("reading the logs back")
                self.q.put(("diagfail", traceback.format_exc()))
        threading.Thread(target=work, daemon=True).start()
        self.refresh("game", soft=True)

    @staticmethod
    def _measure_session(where, route: str, fallback: int):
        try:
            d = Path(where)
            feed_txt = diagnose._last_run(diagnose._tail(d / diagnose.FEED_LOG, 100_000))
            opti_p = diagnose._opti_log(d)
            opti_txt = diagnose._last_run(diagnose._tail(opti_p, 100_000)) if opti_p else ""
            exact = autotune.ran_at_exact(d, route)
            log_path = opti_p if route == "optiscaler" else d / diagnose.FEED_LOG
            if exact is not None and log_path is not None and autotune.written_after(d, route, log_path):
                exact = None
            m = autotune.measure(feed_txt, opti_txt, route, fallback if exact is None else exact)
            if m is not None and exact is None:
                m.from_config = False
            return m
        except Exception:
            return None

    def _on_diagfail(self, text) -> None:
        self._idle()
        self.write(str(text), "err")
        self.refresh("game", soft=True)

    def _on_diagnosed(self, payload) -> None:
        where, rep, measured = payload
        self._idle()
        g = self.game
        if g is None or Path(g.install_dir) != Path(where):
            return
        self._last_diag = rep
        self.write("")
        self.write(f"=== diagnosis{f' :: log {rep.log_time}' if rep.log_time else ''} ===", "head")
        self.write(f"> {rep.verdict}", "ok" if rep.verdict.startswith("Working") else ("warn" if rep.ran else "err"))
        for f_ in rep.findings:
            mark = {"ok": "[ok]  ", "warn": "[!!]  ", "bad": "[fail]", "info": "[--]  "}[f_.level]
            tag = {"ok": "ok", "warn": "warn", "bad": "err", "info": ""}[f_.level]
            self.write(f"{mark} {f_.title}", tag)
            if f_.detail:
                self.write(f"        {f_.detail}")
        self.seen_exe = self._seen_other_exe_for(g)
        working = rep.verdict.startswith("Working")
        self.result = {"kind": "diagnosis", "ok": working, "ran": bool(rep.ran), "title": rep.verdict,
                       "findings": [(f.level, f.title) for f in rep.findings if f.level in ("bad", "warn")][:3]}
        self._record_verdict(rep, measured)
        if not working:
            self.what_next(rep)
            self.write("")
            self.write("> stuck? 'report a bug' is under help - the diagnosis above and the log tail go into the "
                       "report, you post it.", "head")
        self._autotune(rep, measured)
        self._windows_crash(rep)
        self.refresh("game")

    def _record_verdict(self, rep, measured) -> None:
        """What the card says afterwards: only the tool's own reading of a session."""
        g = self.game
        if g is None or not g.installed:
            return
        fps = getattr(measured, "fps", None) if measured is not None else None
        self.verdicts[str(g.install_dir)] = {
            "ok": rep.verdict.startswith("Working") and self._last_crash is None,
            "said": first_sentence(rep.verdict, 90),
            "fps": int(round(fps)) if isinstance(fps, (int, float)) and fps else None,
        }
        prefs.set_("last_verdicts", self.verdicts)

    def what_next(self, rep) -> None:
        g = self.game
        if g is None:
            return
        tried = str(getattr(rep, "route", "") or self.route or "")
        offer = list(getattr(self.support, "options", None) or [])

        def work():
            try:
                data = community.fetch()
                lines = [x for x in (community.next_route(data, g, tried, offer),
                                     community.driver_note(data, gpu.driver_version() or "")) if x]
            except Exception:
                return
            if lines:
                self.q.put(("community", (g.install_dir, data, lines, None)))
        threading.Thread(target=work, daemon=True).start()

    def _windows_crash(self, rep) -> None:
        g = self.game
        if g is None:
            return
        self._last_crash = None
        exe = getattr(getattr(g, "exe", None), "name", "") or ""
        if not exe:
            return
        where = g.install_dir
        try:
            since = diagnose._installed_at(where) or 0.0
            man = diagnose._manifest(where) or {}
            proxy = str(man.get("proxy") or "")
            written = tuple(str(f) for f in (man.get("files") or []) if isinstance(f, str))
        except Exception:
            since, proxy, written = 0.0, "", ()

        def work():
            try:
                c = wincrash.last_crash(exe, since=since)
                said = wincrash.describe(c, proxy, written)
            except Exception:
                return
            if said:
                self.q.put(("wincrash", (where, c, said)))
        threading.Thread(target=work, daemon=True).start()

    def _on_wincrash(self, payload) -> None:
        where, crash, said = payload
        g = self.game
        if g is None or g.install_dir != where:
            return
        self._last_crash = crash if crash_is_this_session(crash, g.install_dir) else None
        self.write("")
        self.write("=== what Windows recorded ===", "head")
        self.write(f"[fail] {said[0]}", "err")
        self.write(f"        {said[1]}")
        self.crash_overrides(crash)
        if self._last_diag is not None:
            self._record_verdict(self._last_diag, self._measured)
        self.refresh("game", soft=True)

    def crash_overrides(self, crash) -> None:
        """A recorded fault outranks a log that stopped in a good place (#98, #171)."""
        d = self._last_diag
        g = self.game
        if d is None or g is None:
            return
        working = str(getattr(d, "verdict", "")).startswith("Working")
        never_ran = bool(getattr(d, "never_ran", False))
        if not working and not never_ran:
            return
        if not crash_is_this_session(crash, g.install_dir):
            return
        mod = str(getattr(crash, "module", "") or "")
        if never_ran:
            if not fault_in_this_folder(crash, g.install_dir):
                return
            d.verdict = ("It started, and nothing here recorded the session - Windows recorded the fault"
                         + (f" in {mod}." if mod else "."))
            d.never_ran = False
            d.findings = diagnose.drop_never_ran(
                [f for f in d.findings if not f.title.startswith("If you DID start it")
                 and not f.title.startswith("The likeliest reason:")])
            d.add(diagnose.BAD, f"Windows recorded {getattr(crash, 'exe', 'the game')} faulting"
                  + (f" in {mod}." if mod else "."),
                  "So it was started. Nothing here wrote a line that describes this session - a fault on "
                  "record, and no session of our own in the logs.")
            self._reprint_verdict(d)
            route = str(getattr(d, "route", ""))
            drop = "'loads as'" if route == "optiscaler" else "" if route == "remix" else "'reshade loads as'"
            after = (f"and if it faults the same way, try another name in the {drop} setting." if drop else
                     "and if it faults the same way, say so in an issue with this report - a game that dies "
                     "before anything loads is worth a look.")
            self.write("> so it WAS started: nothing here recorded the session, and Windows recorded the game "
                       "faulting - which is what a game that dies before the add-ons load looks like. "
                       f"Uninstall, check the game starts on its own, then install again - {after}", "warn")
            return
        d.verdict = "It ran, and then the game crashed - Windows recorded the fault" + (f" in {mod}." if mod else ".")
        self._reprint_verdict(d)
        self.write("")
        self.write("> the neural pass did run, so the install is right - but Windows recorded this game faulting "
                   "afterwards, and a session that ends in a crash is not a working one.", "warn")
        if str(getattr(d, "route", "")) == "optiscaler":
            self.write("> the test that splits it in two: open OptiScaler.ini beside the game, set Enabled=false "
                       "under [DlssNr], and start it again. Survives = the neural pass is what crashes it "
                       "(Dragon's Dogma 2, #98) - set it back to true and try another 'optiscaler build', or "
                       "switch the route to feeder. Still crashes = the fault is OptiScaler's or the game's "
                       "own, and neural rendering was never the reason.", "warn")
        else:
            self.write("> the test that splits it in two: uninstall (the game's own files go back) and play the "
                       "same spot again. Still crashes = the game or another mod, and this was never it. "
                       "Survives = it is what we installed - say so in an issue with this report, and the "
                       "module Windows named above says whose it is.", "warn")

    def _reprint_verdict(self, rep) -> None:
        self.write("")
        self.write(f"> {rep.verdict}", "ok" if str(rep.verdict).startswith("Working") else "err")
        for f_ in getattr(rep, "findings", [])[-1:]:
            if f_.level == diagnose.BAD and "faulting" in f_.title:
                self.write(f"[fail] {f_.title}", "err")
                if f_.detail:
                    self.write(f"        {f_.detail}")
        if self.result and self.result.get("kind") == "diagnosis":
            self.result.update(ok=str(rep.verdict).startswith("Working"), title=rep.verdict,
                               findings=[(f.level, f.title) for f in rep.findings
                                         if f.level in ("bad", "warn")][:3])

    # ---------------------------------------------------------------- tuning and sharing
    def target(self) -> int:
        # a hand-edited settings file can hold anything: 1e999 overflows int()
        try:
            v = int(float(str(self.target_fps or 0).strip() or 0))
        except (TypeError, ValueError, OverflowError):
            return 0
        return v if 20 <= v <= 480 else 0

    def _autotune(self, rep, measured) -> None:
        self._tune = self._measured = self._measured_rows = None
        g, target = self.game, self.target()
        if g is None or not rep.ran or measured is None:
            return
        try:
            m = measured
            if getattr(m, "from_config", True):
                autotune.remember(g.install_dir, m)
            self._measured = m
            rows = autotune.history(g.install_dir)
            self._measured_rows = rows
            cost = autotune.cost_lines(rows, m)
            if cost:
                self.write("")
                self.write("=== what the work area costs here ===", "head")
                for ln in cost:
                    self.write(f"> {ln}")
            if not target:
                if cost:
                    self.write('> set "aim for" in settings and the next session turns this into a setting, '
                               'not just a number to read.', "head")
                return
            sug = autotune.suggest(rows, target, m.resolution, self.route or "", m)
            if sug is None:
                return
            self.write("")
            self.write(f"=== aiming for {target} fps ===", "head")
            for ln in sug.lines:
                self.write(f"> {ln}")
            if sug.resolution != m.resolution:
                self._tune = sug
        except Exception:
            log.exception("working out what the session cost")

    def measured_for(self, route: str) -> dict:
        m, g = self._measured, self.game
        if m is None or g is None or getattr(m, "route", "") != route:
            return {}
        try:
            rows = self._measured_rows
            return autotune.shared(rows if rows is not None else autotune.history(g.install_dir), m)
        except Exception:
            return {}

    def apply_tune(self) -> None:
        sug, g = self._tune, self.game
        if sug is None or g is None:
            return
        d, route = g.install_dir, self.route or ""
        self.settings["workres"] = sug.resolution
        try:
            if route == dlss.OPTI:
                optiscaler.enable_nr(d, log=lambda t: self.write(t),
                                     settings={"WorkingScale": round(sug.resolution / 100.0, 3)})
            else:
                feedcfg.write(d, {"work_resolution": sug.resolution})
                self.write(f"      dlss5-feed.cfg: work_resolution={sug.resolution}%")
        except Exception as e:
            self.write(f"[fail] could not write the setting ({e}) - press install instead, it writes the "
                       f"same value.", "err")
            return
        self.write(f"> set to {sug.resolution}%. It is read when the game starts, so it applies to the next run "
                   f"- play again and press 'did it work?' to see where it landed.", "ok")
        self._tune = None
        self.refresh("game", soft=True)

    def share_result(self) -> None:
        rep, g = self._last_diag, self.game
        if rep is None or g is None:
            return
        route = self.route or ""
        worked = str(getattr(rep, "verdict", "")).startswith("Working") and self._last_crash is None
        try:
            name, sm = gpu.detect()
        except Exception:
            name, sm = "unknown", None
        try:
            man = diagnose._manifest(g.install_dir) or {}
        except Exception:
            man = {}
        rec = community.record(
            g, route or str(man.get("path") or ""), "worked" if worked else "failed",
            api=str(man.get("api") or getattr(g, "api", "") or ""), build=str(man.get("opti_build") or ""),
            gpu_sm=sm, gpu_name=name or "", driver=gpu.driver_version() or "", version=update.VERSION,
            measured=self.measured_for(route))
        carried = "your card and driver, this tool's version, whether it worked, and the one-line verdict"
        if rec.get("res"):
            carried = ("your card and driver, this tool's version, whether it worked, the one-line verdict, and "
                       f"what it cost ({rec['res']}% work area"
                       + ((f", {rec['ms']} ms a frame for the model and feed together"
                           if (rec.get("route") or route) == dlss.FEEDER else f", {rec['ms']} ms of model a frame")
                          if rec.get("ms") else "")
                       + (f", {rec['fps']} fps" if rec.get("fps") else "") + ")")
        if not self.shell.ask("share the result",
                              "A browser window opens with the result in it - nothing is sent unless you post "
                              f"it. It carries the game's name and executable, the route and build, the graphics "
                              f"api, {carried}. No paths, no user name, nothing else.",
                              "open it", "cancel"):
            return
        try:
            webbrowser.open(community.issue_url(rec, str(getattr(rep, "verdict", ""))))
        except Exception as e:
            self.write(f"[fail] could not open the browser ({e})", "err")

    def _seen_other_exe_for(self, g):
        if g is None:
            return None
        try:
            seen = watch.last_sighting(g.install_dir, diagnose._installed_at(g.install_dir))
            other = Path(str(seen.get("exe") or ""))
            recorded = str((installer._previous_manifest(g.install_dir) or {}).get("exe") or "")
        except Exception:
            return None
        if not seen or not other.name or not recorded:
            return None
        if other.name.lower() == Path(recorded).name.lower():
            return None
        try:
            if not other.is_file() or g.folder not in other.parents:
                return None
        except OSError:
            return None
        return other

    def use_seen_exe(self) -> None:
        g, other = self.game, self.seen_exe
        if g is None or other is None:
            return
        self.set_exe(other)
        self.write("  press install to set it up beside the executable that actually runs. the files beside the "
                   "old one stay until you uninstall there.", "warn")

    # ================================================================ what to do in the game
    def route_steps(self, route: str, running: bool = False) -> list[tuple[str, str]]:
        """What to press in the game, for the route that was installed: (kind, text)
        with kind step / warn / note. Written to the log and shown on the page."""
        g = self.game
        s = self.settings
        out: list[tuple[str, str]] = []
        for kind, line in getattr(dlss, "CONFLICTS", {}).get(route, ()):
            if kind == "ingame":
                out.append(("warn", line))
        key = reshade_ini.overlay_key_name()
        if route == dlss.OPTI:
            out += [("step", f"press {reshade_ini.overlay_key_name('Insert')} to open the optiscaler overlay"),
                    ("step", "neural rendering is switched on already; if the overlay says it refused, it tells "
                             "you why right there"),
                    ("step", f"model resolution is set to {s.get('workres', 100)}% - the slider in the overlay "
                             f"changes it live")]
            if g and g.api == "DX11":
                out.append(("step", "on d3d11 the upscaler is FSR on D3D12 - leave it, dlss cannot be the upscaler "
                                    "on this route"))
            out.append(("note", "set the game's dlss quality mode as you like - it still applies"))
        elif route == dlss.RENODX:
            out += [("note", "reshade's overlay will say 'no .fx files found' - normal on this route"),
                    ("step", f"press {key} to open reshade, then the RenoDX DLSS tab"),
                    ("step", "neural rendering is enabled; the tab shows its status and lets you tune it"),
                    ("step", "turn OFF the game's own MSAA/SSAA")]
        elif route == dlss.REMIX:
            out += [("note", "there is no reshade here and Home does nothing - everything lives in the remix menu"),
                    ("step", "press Alt+X in game, then 'Developer Settings Menu'"),
                    ("step", "open the Post-Processing tab and tick 'Enable Neural Uplift (DLSS-NR)'"),
                    ("step", "style, intensity and the structure sliders are right under it"),
                    ("note", "leave the game's own dlss and ray reconstruction as they are")]
        elif route == dlss.STANDALONE:
            out += [("step", "in the game turn OFF its own DLSS, frame generation and anti-aliasing - this add-on "
                             "brings all three"),
                    ("step", "it shows the result in its own window on top; set resolution and display mode "
                             "BEFORE starting"),
                    ("step", f"press {key} for reshade, then the 'Standalone DLSS-NR + SR' tab"),
                    ("step", "F10 flips between the processed and the original picture"),
                    ("note", "a lower in-game resolution than your monitor = DLSS super resolution; same "
                             "resolution = DLAA")]
        elif route == dlss.UPSTREAM:
            out += [("note", "reshade's overlay will say 'no .fx files found' - normal on this route"),
                    ("step", "keep the game's own DLSS ON - the network runs before it"),
                    ("step", f"press {key} to open reshade, then the 'NR Pre-Upscale' tab"),
                    ("step", "using DLSS Frame Generation? set cadence to Quality (every frame)"),
                    ("step", "turn OFF the game's own MSAA/SSAA")]
        elif route in (dlss.NATIVE, dlss.BRIDGE):
            out += [("note", "reshade's overlay will say 'no .fx files found' - normal on this route"),
                    ("step", f"press {key} to open reshade, then the DLSS 5 tab"),
                    ("step", "turn on neural rendering there (F5 toggles it in the 4.6+ builds)"),
                    ("step", "keep the game's dlss ON - the add-on hooks it"),
                    ("step", "turn OFF the game's own MSAA/SSAA")]
        else:
            out.append(("step", f"press {key} to open reshade"))
            p = reshade_ini.PROVIDERS.get(s.get("provider", 3), reshade_ini.PROVIDERS[3])
            out.append(("step", f"tick '{p[0]}' and 'DLSS 5 Feed', provider ABOVE the feed" if p[1]
                        else "put your provider's technique ABOVE DLSS 5 Feed"))
            if g and g.bitness == 32:
                out.append(("step", "turn on neural rendering in the DLSS 5 page of this overlay - it drives the "
                                    "64-bit helper for you"))
                out.append(("warn", "do NOT alt-tab to the helper's own window while playing - it minimizes the "
                                    "game and DLSS is rebuilt every time"))
            else:
                out.append(("step", "turn on neural rendering in the DLSS 5 panel"))
            out.append(("step", "turn OFF the game's own MSAA/SSAA"))
            out.append(("warn", "play BORDERLESS or true fullscreen at your display's own resolution - a bordered "
                                "window presents a few pixels short and the result never lands (Bayonetta)"))
            out.append(("step", "NVIDIA Smooth Motion and this feeder do not mix - turn it off for this game"))
        if route != dlss.REMIX and "default" in self.overlay_key():
            out.append(("note", "no such key on your keyboard? change 'overlay key' in settings"))
        out.append(("warn", "set your resolution BEFORE enabling neural rendering, and use borderless"))
        if self.settings.get("dxvk"):
            out.append(("warn", "dxvk: the game renders on vulkan now - set it to borderless/windowed first, "
                                "then enable neural rendering"))
        if self.sm is not None and self.sm < 89:
            out.append(("warn", "RTX 20/30: the pass is heavy on your card - lower the work area or turn v-sync "
                                "off if the drop is too much"))
        return out


def _api_label(api: str) -> str:
    return {"DX9": "DirectX 9", "DX10": "DirectX 10", "DX11": "DirectX 11", "DX12": "DirectX 12"}.get(api, api)


def _route_rows(entry) -> list[tuple[str, int, int]] | None:
    """(route, worked, reports) from a compatibility entry, most reports first."""
    if not entry:
        return None
    rows = []
    try:
        routes = entry.get("routes") if isinstance(entry, dict) else None
        if isinstance(routes, dict):
            for r, v in routes.items():
                if isinstance(v, dict):
                    ok = int(v.get("worked", 0) or 0)
                    n = ok + int(v.get("failed", 0) or 0)
                    if n:
                        rows.append((r, ok, n))
    except Exception:
        return None
    rows.sort(key=lambda x: (-x[1], -x[2]))
    return rows or None
