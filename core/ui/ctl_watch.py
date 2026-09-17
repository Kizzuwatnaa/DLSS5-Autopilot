"""The watcher in the window: games starting and closing, the answer waiting
when they close, and the next route offered when this one did not work.

Opt-in beyond the window: with "keep watching in the tray" on, closing the
window leaves an icon in the notification area and the answers arrive as
Windows notifications. Off (the default), closing the window quits, as it
always did.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .. import autopilot, diagnose, dlss, gpu, installer, log, prefs, verdicts
from ..lookout import Lookout
from . import theme as T
from . import win
from .ctl_game import crash_is_this_session, fault_in_this_folder


class WatchControl:
    def _watch_init(self) -> None:
        self.lookout = Lookout(lambda ev: self.q.put(("look", ev)))
        self.tray = None
        self.hidden_to_tray = False
        plans = prefs.get("autopilot_plans")
        # a hand-edited entry that is not a plan is dropped, not raised on
        self.plans: dict = ({k: v for k, v in plans.items() if isinstance(v, dict)}
                            if isinstance(plans, dict) else {})
        self._try_token = None

    def watch_on(self) -> bool:
        return bool(prefs.get("watch_games", True))

    def watch_refresh(self) -> None:
        """Watch every installed game in the library (after a scan, install, uninstall)."""
        if not self.watch_on():
            self.lookout.stop()
            self._paint_watch()
            return
        n = self.lookout.set_games(self.all_games)
        if n:
            self.lookout.start()
        self._paint_watch()

    def _paint_watch(self) -> None:
        n = self.lookout.count() if self.watch_on() else 0
        self.shell.watching = (bool(n), f"watching {n}" if n else "watch off")
        self.shell.draw_rail()

    def toggle_watch(self) -> None:
        top = self.shell.kit.top()
        if top is not None and getattr(self, "_watch_menu", None) is top:
            self.shell.kit.pop()
            return
        on, bg = self.watch_on(), bool(prefs.get("watch_background", False))
        n = self.lookout.count()
        items = [
            (("\u2022 " if on else "  ") + f"watch installed games ({n}) while the tool is open",
             lambda: (prefs.set_("watch_games", not on), self.watch_refresh())),
            (("\u2022 " if bg else "  ") + "keep watching in the tray when the window is closed",
             lambda: prefs.set_("watch_background", not bg)),
            None,
            ("when a watched game closes, its logs are read and the answer shown", lambda: None, False),
        ]
        self._watch_menu = self.shell.rail_menu(items, "nav_watch", T.px(560), max_rows=8)

    # ------------------------------------------------------------ events
    def _game_at(self, folder):
        key = Path(folder)
        for g in self.all_games:
            try:
                if g.exe and Path(g.install_dir) == key:
                    return g
            except Exception:
                continue
        return None

    def _job_on(self, folder) -> str:
        """What is rewriting this folder right now, or "". An install,
        uninstall or autopilot pass on this game, a DLSS update in the folder
        it sits in, or an "update all" (busy with no action of one game)."""
        def norm(p):
            try:
                return os.path.normcase(os.path.abspath(str(p)))
            except Exception:
                return ""
        key = norm(folder)
        if self.busy:
            g = self.game
            action = str(getattr(self, "action", "") or "")
            if not action:
                return "update all"
            if g is not None and key in (norm(g.install_dir), norm(g.folder)):
                return action
        job = getattr(self, "dlss_job", None)
        if job:
            j = norm(job)
            if key == j or key.startswith(j.rstrip(os.sep) + os.sep):
                return "the dlss update"
        return ""

    def _on_look(self, ev) -> None:
        kind, folder, extra = ev
        g = self._game_at(folder)
        name = g.name if g else Path(folder).name
        if kind == "started":
            self.shell.status(f"{name} started - watching")
            self.write(f"> {name} started - the watcher read what it loaded", "ok")
            return
        # A pass that waits for the game to close is rewriting this folder:
        # reading it now answered from half an install, overwrote the result
        # and offered the very route being installed.
        job = self._job_on(folder)
        if job:
            self.write(f"> {name} closed while {job} runs on it - that job gives the answer")
            return
        # closed: read the logs back, off the Tk thread
        self.shell.status(f"{name} closed - reading its logs")
        sm = self._sm()

        def work():
            try:
                rep = diagnose.analyse(Path(folder), installer.last_failure(Path(folder)))
            except Exception:
                log.exception("reading a watched game's logs")
                return
            # Windows' fault record, as "did it work?" reads it: without it the
            # watcher said "working" about a session that ended in a crash.
            crash = None
            exe = getattr(getattr(g, "exe", None), "name", "") if g is not None else ""
            if exe:
                try:
                    crash, _said = diagnose.windows_crash(Path(folder), exe)
                except Exception:
                    crash = None
            self.q.put(("lookdiag", (folder, rep, self.offered_routes(g, rep, sm), crash)))
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def ran(rep, seen: bool = True) -> bool:
        """Did the game run with this install? The diagnosis says so itself
        (`ran`), or its answer does not rest on there being no log
        (`never_ran` False: "DXVK ran and ReShade did not", "loaded nothing
        from this folder"), or the watcher saw the game start and close -
        which refutes "Not started since the install". Every "nothing of
        ours loaded" verdict has ran False, so asking for `ran` alone offered
        no route for the very kind that needs one."""
        return bool(getattr(rep, "ran", False) or seen or not getattr(rep, "never_ran", False))

    @staticmethod
    def offered_routes(g, rep, sm, seen: bool = True) -> list:
        """The routes this game is offered, in the order autopilot would
        try them - only worth the folder walk when the answer is one another
        route could change. Worker thread. `seen`: the watcher saw it run."""
        if g is None or not WatchControl.ran(rep, seen) or not verdicts.route_failed(rep.verdict):
            return []
        try:
            sup = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0, sm, driver=gpu.driver_version())
            return autopilot.plan(sup.recommended, list(sup.options or []), None, g)
        except Exception:
            log.exception(f"routes for {g.name}")
            return []

    def _on_lookdiag(self, payload) -> None:
        folder, rep, offered = payload[:3]
        crash = payload[3] if len(payload) > 3 else None
        g = self._game_at(folder)
        if g is None:
            return
        job = self._job_on(folder)
        if job:
            # a job started while the logs were being read
            self.write(f"> {g.name} closed while {job} runs on it - that job gives the answer")
            return
        if crash is not None and not crash_is_this_session(crash, g.install_dir):
            crash = None                         # an earlier launch's fault
        mod = str(getattr(crash, "module", "") or "") if crash is not None else ""
        said = str(rep.verdict)
        if crash is not None:
            # the game page's wording (crash_overrides), so the two answers agree
            if said.startswith("Working"):
                said = "It ran, and then the game crashed - Windows recorded the fault" + (
                    f" in {mod}." if mod else ".")
            elif getattr(rep, "never_ran", False) and fault_in_this_folder(crash, g.install_dir):
                said = ("It started, and nothing here recorded the session - Windows recorded the fault"
                        + (f" in {mod}." if mod else "."))
        working = said.startswith("Working") and crash is None
        nxt, why = self.next_route(g, rep, offered)
        entry = {"ok": working, "said": said[:90], "fps": None}
        if nxt:
            # Kept with the answer, so "try <route>" outlives a restart. Every
            # other writer of this entry (install, uninstall, autopilot, did
            # it work?) replaces or drops it, and the offer with it.
            entry["next"], entry["why"] = nxt, why
        self.verdicts[str(g.install_dir)] = entry
        prefs.set_("last_verdicts", self.verdicts)
        if self.game is g:
            self._last_diag = rep
            self._last_crash = crash
            self.result = {"kind": "diagnosis", "ok": working, "ran": bool(rep.ran), "title": said,
                           "findings": [(f.level, f.title) for f in rep.findings if f.level in ("bad", "warn")][:3]}
        self.write("")
        self.write(f"=== {g.name} closed: {said} ===", "ok" if working else "warn")
        title = f"{g.name} closed"
        if crash is not None:
            text = "it crashed" + (f" in {mod}" if mod else "") + (f" - {why}" if why else "")
        else:
            text = "working" if working else (why or first_words(rep.verdict))
        actions = [("open", lambda g=g: self.open_game(g), not nxt)]
        if nxt:
            actions.insert(0, (f"try {nxt}", lambda g=g, nxt=nxt: self.try_next(g, nxt), True))
        toast = dict(title=title, text=text, actions=actions, accent=T.OK if working else T.WARN,
                     glyph="check" if working else "warn", timeout=15000)
        if self.hidden_to_tray and self.tray is not None:
            self.tray.notify(title, text + (f" - open the tool to try {nxt}" if nxt else ""), warn=not working)
            # opening the window from the tray lands on this game with the same answer and offer
            self._pending_open = (g, toast)
        else:
            self.shell.toast(**toast)
        self.refresh("library", soft=True)
        self.refresh("game", soft=True)

    def next_route(self, g, rep, offered=None, seen: bool = True):
        """(route, why) when this verdict is one another route could change and
        a route is left to try: the rest of this game's autopilot pass,
        or - for a game that was installed by hand - the routes it is offered.
        `seen`: the watcher saw the game run (its close is why we are here)."""
        if not self.ran(rep, seen) or not verdicts.route_failed(rep.verdict):
            return "", ""
        man = diagnose._manifest(g.install_dir) or {}
        route = str(man.get("path") or getattr(rep, "route", "") or "")
        plan = self.plans.get(str(g.install_dir))
        if not isinstance(plan, dict) or not plan:
            if not offered:
                return "", ""
            # Kept, so "try <route>" continues from here and a later close
            # does not offer the route that just failed again.
            self.remember_plan(g, offered, [route] if route else [])
            plan = self.plans[str(g.install_dir)]
        elif route and route not in (plan.get("tried") or []):
            self.remember_plan(g, plan.get("routes") or [], list(plan.get("tried") or []) + [route])
            plan = self.plans[str(g.install_dir)]
        tried = set(plan.get("tried") or []) | ({route} if route else set())
        left = [r for r in plan.get("routes") or [] if r not in tried]
        if not left:
            return "", ""
        return left[0], verdicts.why_next(rep.verdict, route)

    TRY_WAIT = 10.0          # seconds "try <route>" waits for the game's page to be read

    def try_next(self, g, route) -> None:
        """Open the game and continue its pass from `route`. Waits for the
        page to be read, at most TRY_WAIT, and only while this game stays
        open: the first version polled forever, and a later visit to the game
        installed with no question asked."""
        if self.busy:
            self.shell.status(f"busy with {getattr(self, 'action', '') or 'another job'} - "
                              f"try {route} when it ends")
            return
        plan = self.plans.get(str(g.install_dir))
        plan = plan if isinstance(plan, dict) else {}
        routes = [r for r in plan.get("routes") or [] if r not in set(plan.get("tried") or [])]
        if route in routes:
            routes = routes[routes.index(route):]
        self.open_game(g)
        token = object()
        self._try_token = token
        deadline = time.monotonic() + self.TRY_WAIT

        def go():
            if self._try_token is not token:
                return                          # a newer "try" replaced this one
            if self.game is not g:
                self._try_token = None          # another game was opened: not this one's install
                return
            if self.support is None:
                if time.monotonic() < deadline:
                    self.root.after(200, go)
                    return
                self._try_token = None
                self.shell.status(f"{g.name} could not be read in time - press autopilot on its page "
                                  f"to try {route}")
                return
            self._try_token = None
            if self.busy:
                self.shell.status(f"busy with {getattr(self, 'action', '') or 'another job'} - "
                                  f"try {route} when it ends")
                return
            self.autopilot(routes=routes, ask=False)
        self.root.after(200, go)

    def remember_plan(self, g, routes, tried) -> None:
        self.plans[str(g.install_dir)] = {"routes": list(routes), "tried": list(tried)}
        prefs.set_("autopilot_plans", self.plans)

    # ------------------------------------------------------------ the tray
    def on_close(self) -> None:
        if prefs.get("watch_background", False) and self.watch_on() and self.lookout.count():
            from .tray import Tray
            if self.tray is None:
                self.tray = Tray("DLSS 5 Autopilot - watching your games", str(win.ico_path()),
                                 lambda ev: self.q.put(("tray", ev)))
            if self.tray.show():
                self.hidden_to_tray = True
                self.root.withdraw()
                self.tray.notify("still watching", f"{self.lookout.count()} installed games - the answer comes "
                                                   f"here when one closes")
                return
        self.quit()

    def _on_tray(self, ev) -> None:
        if ev == "open":
            self.hidden_to_tray = False
            self.root.deiconify()
            self.root.lift()
            try:
                self.root.focus_force()
            except Exception:
                pass
            pending = getattr(self, "_pending_open", None)
            if pending is not None:
                self._pending_open = None
                g, toast = pending
                self.open_game(g)
                self.root.after(400, lambda: self.shell.toast(**toast))
        elif ev == "quit":
            self.quit()

    def quit(self) -> None:
        try:
            self.lookout.stop()
            if self.tray is not None:
                self.tray.hide()
        except Exception:
            pass
        self.root.destroy()


def first_words(text: str, n: int = 60) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[:n - 1].rstrip() + "\u2026"
