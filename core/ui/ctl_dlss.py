"""Keeping games' DLSS current: the scan, the update and the restore behind
the dlss page. Logic only - dlsspage draws it.

Every folder walk, download and file swap runs on a worker; the page reads
the last scan from %LOCALAPPDATA%\\dlss5-autopilot\\dlss-scan.json, so it opens
with its rows at once and is usable while a new scan runs.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .. import dlssupdate as du
from .. import log, watch

# A scan older than this is run again when the page opens: NVIDIA publishes
# a few builds a year, and a launcher can put an old file back any day.
RESCAN_AFTER = 24 * 3600


def _first_line(text: str) -> str:
    return (str(text).strip().splitlines() or [""])[0]


class DlssControl:
    # ------------------------------------------------------------ state
    def _dlss_init(self) -> None:
        self.dlss_data: dict | None = None     # the last scan, loaded on first use
        self.dlss_scanning = False
        self.dlss_seen = (0, 0, "")            # (games read, of, the one being read)
        self.dlss_job: str | None = None       # folder being updated or restored
        self.dlss_pct = -1                     # download progress of that job
        self.dlss_queue: list[str] = []        # folders "update all" still has to do
        self.dlss_results: dict[str, tuple[bool, str]] = {}
        self.dlss_pending: str | None = None    # a game page's update waiting for a scan to end
        self.dlss_batch: dict | None = None     # what "update all" did so far
        self.dlss_reread_q: list = []           # games dlss_reread still has to read
        self._dlss_rereading = False            # the one worker draining that queue
        self._dlss_rq_lock = threading.Lock()
        self._dlss_late: dict[str, dict] = {}   # rows re-read while a scan of every game ran
        self._dlss_waiting = False              # a read waits for the library scan to end
        self._dlss_wait_fresh = False

    def dlss_state(self) -> dict:
        if self.dlss_data is None:
            self.dlss_data = du.load_cache()
        return self.dlss_data

    def _dlss_games(self) -> list:
        # A game hidden in the library is one the person set aside: it is not
        # listed here and "update all" does not change its files.
        try:
            hid = self.hidden()
        except Exception:
            hid = set()
        return [g for g in self.all_games if getattr(g, "kind", "game") != "video" and g.exe
                and str(g.folder) not in hid]

    @staticmethod
    def _dlss_stamp(g) -> float:
        """The newest mtime of the game's folder and install folder. Worker only.

        Cheap enough to take for every game, unlike the walk: a game whose
        stamp did not move since its last read is not read again when a read
        of the new games runs."""
        t = 0.0
        for d in {str(g.folder), str(g.install_dir)}:
            try:
                t = max(t, os.stat(d).st_mtime)
            except OSError:
                pass
        return t

    @staticmethod
    def _dlss_entry(folder: str, d: dict) -> du.Entry:
        return du.Entry(path=Path(folder) / str(d.get("rel", "")), rel=str(d.get("rel", "")),
                        family=d.get("family") if d.get("family") in du.FILE_OF else "dlss",
                        version=str(d.get("version") or ""), state=str(d.get("state") or du.ORIGINAL),
                        original=str(d.get("original") or ""), label=str(d.get("label") or ""),
                        backup=Path("backup") if d.get("backup") else None)

    def dlss_newest(self) -> dict:
        n = self.dlss_state().get("newest")
        return n if isinstance(n, dict) else {}

    def dlss_row(self, g) -> dict | None:
        """What the last scan said about this game, with the page's verdicts added."""
        info = (self.dlss_state().get("games") or {}).get(str(g.folder))
        if not isinstance(info, dict) or not info.get("entries"):
            return None
        news = self.dlss_newest()
        entries = [self._dlss_entry(str(g.folder), d) for d in info["entries"] if isinstance(d, dict)]
        behind = [e for e in entries if du.outdated(e, news.get(e.family))]
        return {"g": g, "entries": entries, "behind": behind, "news": news,
                "anticheat": str(info.get("anticheat") or ""), "running": str(info.get("running") or ""),
                "anticheat_found": str(info.get("anticheat_found") or ""),
                "restorable": any(e.backup is not None or e.state == du.UPDATED for e in entries)}

    @staticmethod
    def _dlss_rank(r) -> tuple:
        # what can be updated now first, then what is behind but needs a look
        # (anti-cheat, running), then the rest
        now = bool(r["behind"] and not r["anticheat"] and not r["running"])
        return (not now, not r["behind"], r["g"].name.lower())

    def dlss_rows(self) -> list[dict]:
        rows = [r for r in (self.dlss_row(g) for g in self._dlss_games()) if r is not None]
        # Ordered once per scan, then kept: a row that jumps down the moment
        # its update ends (and again for every game of "update all") loses
        # the person's place. New rows go after the ordered ones.
        order = {f: i for i, f in enumerate(self.dlss_state().get("order") or [])}
        rows.sort(key=lambda r: (order.get(str(r["g"].folder), len(order)), self._dlss_rank(r)))
        return rows

    def dlss_todo(self) -> list[dict]:
        """What "update all" takes: behind, no anti-cheat, not running."""
        return [r for r in self.dlss_rows() if r["behind"] and not r["anticheat"] and not r["running"]]

    def dlss_line(self, g) -> str:
        """The game page's line, when this game's DLSS is behind: "dlss 3.7.20 -> 310.9.1"."""
        try:
            r = self.dlss_row(g)
        except Exception:
            return ""
        if not r or not r["behind"]:
            return ""
        e = r["behind"][0]
        new = r["news"].get(e.family, {}).get("version", "")
        n = len({x.family for x in r["behind"]}) - 1
        more = f" and {n} more" if n > 0 else ""
        name = {"dlss": "dlss", "dlssg": "frame generation", "dlssd": "ray reconstruction"}[e.family]
        held = f" - {r['anticheat'].lower()}" if r["anticheat"] else ""
        return f"{name} {e.version} -> {new}{more}{held}"

    def dlss_line_quiet(self, g) -> bool:
        """An anti-cheat game's line is drawn without the accent: not a suggestion."""
        r = self.dlss_row(g)
        return bool(r and r["anticheat"])

    def dlss_background(self) -> None:
        """At start: read the games quietly when the last read is old, so a
        game page can point out an older DLSS without the dlss page ever
        having been opened."""
        try:
            if self.scanning or self.busy:
                # the library is still being read, or an install runs: later
                self.root.after(15000, self.dlss_background)
            elif self.dlss_due():
                self.dlss_scan()
        except Exception:
            log.exception("starting the dlss read")

    def dlss_recheck_running(self) -> None:
        """Which listed games are running now - one process snapshot, on a worker."""
        rows = [r for r in self.dlss_rows()]
        if not rows or self.dlss_scanning:
            return

        def work():
            try:
                procs = watch.procs()
                now = {str(r["g"].folder): du.running(r["g"], procs) for r in rows}
            except Exception:
                return
            self.q.put(("dlssrunning", now))
        threading.Thread(target=work, daemon=True).start()

    def _on_dlssrunning(self, now: dict) -> None:
        games_ = self.dlss_state().get("games") or {}
        changed = False
        for folder, name in now.items():
            info = games_.get(folder)
            if isinstance(info, dict) and (info.get("running") or "") != name:
                info["running"] = name
                changed = True
        if changed:
            self.refresh("dlss")

    def dlss_due(self) -> bool:
        """Should opening the page start a scan by itself?"""
        if self.dlss_scanning or not self.have_library():
            return False
        data = self.dlss_state()
        if not data.get("at") or time.time() - float(data.get("at") or 0) > RESCAN_AFTER:
            return True
        seen = set(data.get("seen") or [])
        # A game whose bitness is not read yet (the library is enriching it)
        # cannot be read either; it becomes due once it is known.
        return any(str(g.folder) not in seen for g in self._dlss_games()
                   if getattr(g, "bitness", None) is not None)

    # ------------------------------------------------------------ scan
    def _dlss_read(self, g, fresh: bool, procs=None) -> dict:
        # No settle() here: a scan runs beside installs and other jobs, and
        # settle changes files. update() and restore() settle under busy.
        notes: list[str] = []
        stamp = self._dlss_stamp(g)
        entries = du.scan(g, fresh=fresh)
        ac, found = du.anticheat_info(g) if entries else ("", "")
        return {"name": g.name, "at": time.time(), "entries": [e.to_json() for e in entries],
                "anticheat": ac, "anticheat_found": found, "stamp": stamp,
                "running": du.running(g, procs) if entries else "", "notes": notes}

    def dlss_reread(self, g) -> None:
        """One game read again after something changed its files (a DLSS 5
        install or uninstall, autopilot, a library-wide update), so its page
        and row do not show the build from before.

        Called for many games in a row: they queue, and one worker reads them
        one after another instead of a thread and a walk per call at once. A
        game already queued is not queued twice."""
        if g is None or getattr(g, "bitness", None) != 64 or not self.dlss_state().get("games"):
            return
        with self._dlss_rq_lock:
            if any(str(x.folder) == str(g.folder) for x in self.dlss_reread_q):
                return
            self.dlss_reread_q.append(g)
            if self._dlss_rereading:
                return
            self._dlss_rereading = True

        def work():
            while True:
                # the queue and the flag change together, so a game queued
                # while the worker is finishing is never left unread
                with self._dlss_rq_lock:
                    if not self.dlss_reread_q:
                        self._dlss_rereading = False
                        return
                    one = self.dlss_reread_q.pop(0)
                try:
                    row = self._dlss_read(one, True)
                except Exception:
                    log.exception(f"reading the DLSS files of {one.name} again")
                    continue
                self.q.put(("dlssone", (str(one.folder), row)))
        threading.Thread(target=work, daemon=True).start()

    def _on_dlssone(self, payload) -> None:
        folder, row = payload
        data = self.dlss_state()
        if not isinstance(data.get("games"), dict):
            return
        if self.dlss_scanning:
            # the scan's answer replaces the whole table and may have read
            # this game before its files changed: this row goes on top of it
            self._dlss_late[folder] = row
        data["games"][folder] = row
        try:
            du.save_cache(data)
        except Exception:
            log.exception("saving the dlss read")
        self.refresh("dlss")
        self.refresh("game", soft=True)

    def dlss_scan(self, fresh: bool = False) -> None:
        if self.dlss_scanning or self.dlss_job:
            return
        if self.busy:
            self.shell.status("another job is running - the games are read when it ends")
            return
        if getattr(self, "scanning", False):
            # The library scan re-reads games while this would read them: a
            # game mid-enrich has no bitness yet, and was counted "32-bit
            # skipped" for a day. Read once the library is done.
            self.shell.status("the library is being read - the dlss files are read when it ends")
            self._dlss_after_library(fresh)
            return
        gs = self._dlss_games()
        if not gs:
            return
        data = self.dlss_state()
        # Only the day-old read and "check again" read every game. A read
        # started because the library has new games reads those, and the
        # games whose folder changed since their last read.
        partial = (not fresh and bool(data.get("at")) and isinstance(data.get("games"), dict)
                   and time.time() - float(data.get("at") or 0) <= RESCAN_AFTER)
        seen_before = set(data.get("seen") or []) if partial else set()
        stamps = {f: (v.get("stamp") if isinstance(v, dict) else None)
                  for f, v in (data.get("games") or {}).items()} if partial else {}
        self.dlss_scanning = True
        self._dlss_late = {}
        self.dlss_seen = (0, len(gs), "")
        self.refresh("dlss")

        def work():
            news = {}
            try:
                news = {f: {"label": e.get("label", ""), "version": e.get("version", "")}
                        for f, e in du.newest(refresh=fresh).items()}
            except Exception:
                log.exception("reading NVIDIA's newest DLSS builds")
            try:
                procs = watch.procs()
            except Exception:
                procs = []
            known = [g for g in gs if getattr(g, "bitness", None) is not None]
            x86 = len([g for g in known if g.bitness != 64])
            todo = [g for g in known if g.bitness == 64]
            if partial:
                todo = [g for g in todo if str(g.folder) not in seen_before
                        or stamps.get(str(g.folder)) != self._dlss_stamp(g)]
            out = {}
            for i, g in enumerate(todo, 1):
                self.q.put(("dlssseen", (i - 1, len(todo), g.name)))
                try:
                    out[str(g.folder)] = self._dlss_read(g, fresh, procs)
                except Exception:
                    log.exception(f"reading the DLSS files of {g.name}")
            # an unread bitness is neither checked nor skipped: not in `seen`,
            # so the game is read once the library knows it
            self.q.put(("dlssscanned", {"at": time.time(), "newest": news, "games": out, "x86": x86,
                                        "seen": [str(g.folder) for g in known], "partial": partial}))
        threading.Thread(target=work, daemon=True).start()

    def _dlss_after_library(self, fresh: bool) -> None:
        """One read, started when the library scan ends - however often asked."""
        self._dlss_wait_fresh = self._dlss_wait_fresh or fresh
        if self._dlss_waiting:
            return
        self._dlss_waiting = True

        def again():
            if getattr(self, "scanning", False):
                self.root.after(2000, again)
                return
            self._dlss_waiting = False
            f, self._dlss_wait_fresh = self._dlss_wait_fresh, False
            try:
                if f or self.dlss_due():
                    self.dlss_scan(fresh=f)
            except Exception:
                log.exception("reading the dlss files after the library scan")
        self.root.after(2000, again)

    def _on_dlssseen(self, payload) -> None:
        self.dlss_seen = payload
        page = self.shell.pages.get("dlss")
        if self.shell.page is page and hasattr(page, "progress"):
            page.progress()

    def _on_dlssscanned(self, data: dict) -> None:
        old = self.dlss_state()
        if not data.get("newest"):
            # offline: the rows are new, the newest builds are what was known
            data["newest"] = old.get("newest") or {}
            data["offline"] = True
        if data.pop("partial", False):
            # the games read now go over the last full read, which keeps its
            # time: the day-old read of every game still comes when it is due
            rows = dict(old.get("games") or {})
            rows.update(data["games"])
            data["games"] = rows
            data["at"] = old.get("at") or data["at"]
        # rows a re-read took while this ran are newer than what it read
        data["games"].update(self._dlss_late)
        self._dlss_late = {}
        self.dlss_data = data
        self.dlss_scanning = False
        data["order"] = []
        data["order"] = [str(r["g"].folder) for r in sorted(self.dlss_rows(), key=self._dlss_rank)]
        du.save_cache(data)
        n = len([1 for v in data["games"].values() if v.get("entries")])
        behind = len([r for r in self.dlss_rows() if r["behind"]])
        self.shell.status(f"dlss: {n} game{'s' if n != 1 else ''} ship it"
                          + (f", {behind} behind nvidia's newest" if behind else ""))
        self.refresh("dlss")
        self.refresh("game")
        # a game page's "update" pressed while this read was running
        pending, self.dlss_pending = self.dlss_pending, None
        g = next((x for x in self._dlss_games() if str(x.folder) == pending), None) if pending else None
        if g is not None:
            self.dlss_update(g)

    # ------------------------------------------------------------ update / restore
    def _dlss_can_start(self) -> bool:
        if self.dlss_job or self.dlss_scanning:
            return False
        if self.busy:
            self.shell.status("another job is running - the dlss files wait for it")
            return False
        return True

    def dlss_update(self, g, families=None) -> None:
        if not self._dlss_can_start():
            return
        r = self.dlss_row(g) or {}
        allow = False
        if r.get("anticheat"):
            if not self.shell.ask(
                    f"{r['anticheat']} - update anyway?",
                    f"{g.name} ships {r['anticheat']}"
                    + (f" ({r['anticheat_found']})" if r.get("anticheat_found") else "")
                    + ". An anti-cheat can read a changed DLSS file as "
                    f"tampering, and online that can mean a ban. The game's own files are kept either way.",
                    "update anyway", "cancel", danger=True):
                return
            allow = True
            # the question is modal, but timers are not: a job may have begun meanwhile
            if not self._dlss_can_start():
                return
        self._dlss_start(g, "update", families, allow)

    def dlss_restore(self, g) -> None:
        if not self._dlss_can_start():
            return
        self._dlss_start(g, "restore", None, False)

    def dlss_update_from_game(self, g) -> None:
        """The game page's link: the dlss page, with this game's update running on it.

        Opening the page can start a read of every game (a day-old read, a
        new game); the update then waits for it instead of being dropped.
        """
        self.shell.show("dlss")
        if self.dlss_scanning:
            self.dlss_pending = str(g.folder)
            self.shell.status(f"{g.name}: the update starts when the games are read")
            return
        self.dlss_update(g)

    def dlss_update_all(self) -> None:
        if not self._dlss_can_start():
            return
        todo = self.dlss_todo()
        if not todo:
            return
        names = ", ".join(r["g"].name for r in todo[:4]) + (f" and {len(todo) - 4} more" if len(todo) > 4 else "")
        if not self.shell.ask(f"update dlss in {len(todo)} game{'s' if len(todo) != 1 else ''}?",
                              f"{names}.\n\nEach game's own files are kept beside the new ones; "
                              f"restore original puts them back.", "update", "cancel"):
            return
        if not self._dlss_can_start():
            return
        self.dlss_queue = [str(r["g"].folder) for r in todo[1:]]
        self.dlss_batch = {"done": 0, "failed": []}
        self._dlss_start(todo[0]["g"], "update", None, False)

    def _dlss_start(self, g, what: str, families, allow: bool) -> None:
        folder = str(g.folder)
        self.dlss_job, self.dlss_pct = folder, -1
        # The same flag install, autopilot and uninstall wait for: two writers
        # of one runtime at once is how the game's own file gets lost.
        self.busy, self.action = True, "dlss"
        self.dlss_results.pop(folder, None)
        self.shell.busy(f"{'updating' if what == 'update' else 'restoring'} dlss in {g.name}...")
        self.refresh("dlss")
        last = [0.0]

        def prog(done, total):
            now = time.monotonic()
            if total and (now - last[0] > 0.15 or done >= total):
                last[0] = now
                self.q.put(("dlssprog", (folder, int(done * 100 / total))))

        def work():
            try:
                if what == "update":
                    rep = du.update(g, families, allow_anticheat=allow, progress=prog)
                else:
                    rep = du.restore(g)
            except Exception as e:
                log.exception(f"dlss {what} in {g.name}", e)
                rep = du.Report(error=_first_line(e) or type(e).__name__)
            try:
                row = self._dlss_read(g, fresh=False)
            except Exception:
                row = None
            self.q.put(("dlssdone", (folder, what, rep, row)))
        threading.Thread(target=work, daemon=True).start()

    def _on_dlssprog(self, payload) -> None:
        folder, pct = payload
        if folder == self.dlss_job:
            self.dlss_pct = pct
            page = self.shell.pages.get("dlss")
            if self.shell.page is page and hasattr(page, "job_progress"):
                page.job_progress()

    def _on_dlssdone(self, payload) -> None:
        try:
            self._dlss_done(payload)
        except Exception:
            # whatever went wrong drawing the answer, the job is over: a
            # "working" row and a stuck flag would block every later job
            self.dlss_job, self.dlss_pct, self.dlss_queue, self.dlss_batch = None, -1, [], None
            self.busy, self.action = False, ""
            self.shell.busy("")
            raise

    def _dlss_done(self, payload) -> None:
        folder, what, rep, row = payload
        data = self.dlss_state()
        if row is not None:
            if rep.refused == du.R_RUNNING:
                row["running"] = row.get("running") or rep.error.split(" is running")[0]
            data.setdefault("games", {})[folder] = row
            du.save_cache(data)
        name = (row or {}).get("name") or Path(folder).name
        if rep.ok:
            # The row's own lines already show the versions; what the row adds
            # is a note the person has to know (a launcher put its file back,
            # a backup was gone), else one word.
            done = ("updated" if what == "update" else "the game's own files are back") if rep.done else ""
            if done:
                text = done
            elif rep.notes:
                text = rep.notes[0]
            else:
                text = (rep.skipped or ["nothing to change"])[0]
            self.write(f"{name}: " + "; ".join(rep.done + rep.notes + rep.skipped), "ok")
        else:
            # the row names the file inside the game; the log keeps the whole path
            text = rep.error.replace(folder + os.sep, "").replace(folder + "/", "").replace(folder, "the game folder")
            if rep.done:
                # part of it went through: say so, above the part that did not
                moved = ", ".join(line.split("  ")[0] for line in rep.done)
                text = f"{moved} updated; {text}"
            self.write(f"{name}: " + "; ".join(rep.done + [rep.error]), "err")
        self.dlss_results[folder] = (rep.ok, text)
        self.busy, self.action = False, ""
        if self.dlss_batch is not None:
            if rep.ok and rep.done:
                self.dlss_batch["done"] += 1
            elif not rep.ok:
                self.dlss_batch["failed"].append(name)
        self.dlss_job, self.dlss_pct = None, -1
        nxt = None
        while self.dlss_queue and nxt is None:
            f = self.dlss_queue.pop(0)
            nxt = next((g for g in self._dlss_games() if str(g.folder) == f), None)
        if nxt is not None:
            self._dlss_start(nxt, "update", None, False)
            return
        self.shell.busy("")
        batch, self.dlss_batch = self.dlss_batch, None
        if batch is not None:
            failed = batch["failed"]
            self.shell.status(f"dlss: {batch['done']} updated"
                              + (f", {len(failed)} failed - {', '.join(failed[:3])}" if failed else ""))
        else:
            self.shell.status(f"{name}: {_first_line(text)}")
        self.refresh("dlss")
        self.refresh("game")
