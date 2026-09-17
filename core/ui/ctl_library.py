"""The library: scanning, the saved library, hiding, sorting, filtering, and
what each card says. Logic only - libpage draws it.

Ported from the 1.9 window with its behaviour kept: the quick rescan that
reads only what is new, the saved library that opens at once and re-reads
changed games off the Tk thread, rows that never walk a folder on the Tk
thread, the hidden-games list cached from settings.
"""
from __future__ import annotations

import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from .. import (anticheat, community, components, diagnose, dlss, games, gpu, installer, library, log,
                prefs, sources, update, video)
from . import art, imaging
from . import theme as T

SORTS = (("", "scan order"), ("name", "name"), ("source", "store"), ("api", "graphics api"),
         ("route", "route"), ("status", "status"))
FILTERS = ("all", "installed", "working", "needs a look", "update")
ARCH = (("all", "32 + 64-bit"), ("64", "64-bit only"), ("32", "32-bit only"))


def first_line(e: Exception) -> str:
    return (str(e).splitlines() or [""])[0] or type(e).__name__


class LibraryControl:
    # ------------------------------------------------------------ state
    def _library_init(self) -> None:
        self.all_games: list[games.Game] = []
        self._rows: dict[tuple, object] = {}
        self._recheck: set[str] = set()
        self._recheck_id = 0
        self.stale: dict[str, int] = {}
        self.scanning = False
        self.scan_note = ""
        # why the last scan stopped, drawn under the library's title; "" when it did not
        self.scan_failed = ""
        self.board = ""
        self.query = ""
        self.filter = "all"
        v = prefs.get("games_arch")
        self.arch = v if isinstance(v, str) and v in ("all", "32", "64") else "all"
        self.show_hidden = False
        # a hand-edited settings file can hold anything: [[...]] here raised
        # TypeError (a list is not hashable) and the window never opened
        s = prefs.get("games_sort")
        self.sort = s[0] if isinstance(s, list) and s and isinstance(s[0], str) and s[0] in dict(SORTS) else ""
        self.sort_desc = bool(s[1]) if isinstance(s, list) and len(s) > 1 and isinstance(s[1], (bool, int)) \
            else False
        self._lib_job = None
        self._card_memo: dict | None = None
        self._hidden_cache: set | None = None
        self.covers: dict[str, dict] = {}
        self._cover_busy: set[str] = set()
        # a cover found online lands on the art thread; it reaches the window
        # as an ("art", folder) message, never as a call into Tk from there
        art.listen(lambda folder: self.q.put(("art", folder)))
        self.sm: int | None = None
        v = prefs.get("last_verdicts")
        self.verdicts: dict = v if isinstance(v, dict) else {}
        self.shared = None          # the compatibility list, once fetched

    def _sm(self) -> int | None:
        if self.sm is None:
            try:
                _, self.sm = gpu.detect()
            except Exception:
                self.sm = None
        return self.sm

    def have_library(self) -> bool:
        """The video player alone is not a library."""
        return any(getattr(g, "kind", "game") != "video" for g in self.all_games)

    # ------------------------------------------------------------ rows
    @staticmethod
    def inspect_row(g: games.Game, sm: int | None):
        """The compatibility row for a card. Walks folders: worker threads only."""
        try:
            ok, _ = installer.check_supported(g)
            if not ok:
                return False, "-", installer.EXPERIMENTAL, "-", False, ""
            sup = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0, sm,
                              driver=gpu.driver_version())
            level, _ = installer.reliability(g, sup.recommended)
            outlook = {installer.STABLE: "reliable", installer.BETA: "beta",
                       installer.EXPERIMENTAL: "often fails"}[level]
            ac = anticheat.detect(g.install_dir, g.folder)
            return ok, sup.recommended, level, outlook, ac.present, ac.summary
        except Exception as e:
            log.exception(f"inspecting {g.name}", e)
            return False

    @staticmethod
    def matches(g: games.Game, terms: list[str]) -> bool:
        if not terms:
            return True
        hay = f"{g.name} {g.folder} {g.source}".lower()
        return all(t in hay for t in terms)

    def row_of(self, g):
        """The row, or None while a worker reads it (never read here)."""
        key = (str(g.folder), str(g.exe))
        row = self._rows.get(key)
        if row is None and str(g.folder) not in self._recheck and not self.scanning:
            self._read_rows([g])
        return row

    def _read_rows(self, gs: list) -> None:
        """Rows for games that have none yet (a folder picked by hand, an
        install that changed), on a worker; the page redraws when they land."""
        todo = [g for g in gs if g.exe and str(g.folder) not in self._recheck]
        if not todo:
            return
        for g in todo:
            self._recheck.add(str(g.folder))
        sm = self._sm()
        gen = self._recheck_id

        def work():
            rows = {}
            for g in todo:
                rows[(str(g.folder), str(g.exe))] = self.inspect_row(g, sm)
            self.q.put(("rechecked", (gen, rows, [str(g.folder) for g in todo])))
        threading.Thread(target=work, daemon=True).start()

    def forget_row(self, g) -> None:
        if g is not None and getattr(g, "exe", None):
            self._rows.pop((str(g.folder), str(g.exe)), None)
        else:
            self._rows.clear()

    # ------------------------------------------------------------ hidden / sort / filter
    def hidden(self) -> set:
        if self._hidden_cache is None:
            v = prefs.get("hidden_games")
            self._hidden_cache = {x for x in v if isinstance(x, str)} if isinstance(v, list) else set()
        return set(self._hidden_cache)

    def set_hidden(self, g, hide: bool) -> None:
        hid = self.hidden()
        (hid.add if hide else hid.discard)(str(g.folder))
        prefs.set_("hidden_games", sorted(hid))
        self._hidden_cache = hid
        self.refresh("library")

    def set_sort(self, col: str) -> None:
        if col == self.sort and col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort, self.sort_desc = col, False
        prefs.set_("games_sort", [self.sort, self.sort_desc])
        self.refresh("library")

    def set_arch(self, arch: str) -> None:
        self.arch = arch
        prefs.set_("games_arch", arch)
        self.refresh("library")

    def set_filter(self, f: str) -> None:
        self.filter = f
        self.refresh("library")

    def set_query(self, q: str) -> None:
        self.query = q
        self.refresh("library")

    def verdict_of(self, g) -> dict | None:
        v = self.verdicts.get(str(g.install_dir))
        return v if isinstance(v, dict) and g.installed else None

    @contextmanager
    def card_pass(self):
        """One redraw of the library: counts(), visible() and every card ask
        card() for the same game, and each ask checked about nine files and
        read the install record. Inside a pass each game is worked out once."""
        outer = self._card_memo is not None
        if not outer:
            self._card_memo = {}
        try:
            yield
        finally:
            if not outer:
                self._card_memo = None

    def card(self, g) -> dict:
        """What a game's card says: one short status, its colour, and whether
        the cover is dimmed. The same rules the 1.9 list used, plus the last
        real verdict the tool itself recorded for the folder."""
        memo = self._card_memo
        if memo is not None:
            hit = memo.get(id(g))
            if hit is not None and hit[0] is g:
                return hit[1]
        out = self._card(g)
        if memo is not None:
            memo[id(g)] = (g, out)
        return out

    def _card(self, g) -> dict:
        row = self.row_of(g)
        hid = str(g.folder) in self.hidden()
        out = {"status": "", "colour": T.MUTED, "dot": False, "dim": hid, "kind": "ready",
               "route": "", "level": ""}
        if row is None:
            out.update(status="reading...", colour=T.DIM, kind="reading")
            return out
        if row is False:
            out.update(status="unreadable", colour=T.WARN, kind="unsupported", dim=True)
            return out
        ok, route, level, outlook, ac_present, ac_summary = row
        out.update(route=route, level=level, anticheat=bool(ac_present))
        installed = g.installed
        if not ok:
            label = ("choose architecture / api" if g.exe_warning and not g.error else "unsupported")
            out.update(status=label, colour=T.WARN, kind="unsupported", dim=True)
        elif ac_present and not installed:
            out.update(status=ac_summary, colour=T.DIM, kind="anticheat", dim=True)
        elif installed:
            # Installed comes before anti-cheat: an installed game with an
            # anti-cheat was dropping out of the installed, working and update
            # tabs, while update all still reinstalled it.
            n_stale = self.stale.get(str(g.install_dir), 0)
            man = diagnose._manifest(g.install_dir)
            man_api = man.get("api") or ""
            v = self.verdict_of(g)
            if man_api and man_api != g.api and not man.get("dxvk") \
                    and man.get("proxy") != diagnose.VULKAN_LAYER:
                out.update(status=f"reinstall - was {man_api}", colour=T.AMBER, kind="update", dot=True)
            elif n_stale:
                out.update(status=f"update ({n_stale})", colour=T.AMBER, kind="update", dot=True)
            elif v and v.get("ok"):
                out.update(status="working" + (f"  \u00b7  {v['fps']} fps" if v.get("fps") else ""),
                           colour=T.OK, kind="working", dot=True)
            elif v and not v.get("ok"):
                out.update(status="needs a look", colour=T.WARN, kind="look", dot=True)
            else:
                out.update(status="installed", colour=T.MUTED, kind="installed")
        else:
            out.update(status={"reliable": "", "beta": "beta", "often fails": "experimental"}.get(outlook, ""),
                       colour=T.DIM, kind="ready")
            said = self.shared_line(g)
            if said:
                out.update(status=said, colour=T.AMBER)
        return out

    def shared_line(self, g) -> str:
        """'5 of 7 worked' for a game other people reported on, from the shared
        list - only when a route worked for somebody, never a count of failures."""
        data = self.shared
        if not data:
            return ""
        try:
            entry = community.for_game(data, g)
            routes = (entry or {}).get("routes") or {}
            best = max(((r.get("worked", 0), r.get("worked", 0) + r.get("failed", 0))
                        for r in routes.values() if isinstance(r, dict)), default=(0, 0))
        except Exception:
            return ""
        if not best[0]:
            return ""
        return f"{best[0]} of {best[1]} worked for others"

    def load_shared(self) -> None:
        def work():
            try:
                self.q.put(("shared", community.fetch()))
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _on_shared(self, data) -> None:
        self.shared = data
        if self._community is None:
            self._community = data
        self.refresh("library", soft=True)

    def visible(self) -> list:
        terms = self.query.strip().lower().split()
        hid = self.hidden()
        out = []
        for g in self.all_games:
            if not g.exe:
                continue
            if self.arch != "all" and g.bitness is not None and str(g.bitness) != self.arch:
                continue
            if not self.show_hidden and str(g.folder) in hid:
                continue
            if not self.matches(g, terms):
                continue
            if self.filter != "all":
                kind = self.card(g)["kind"]
                want = {"installed": ("installed", "working", "look", "update"),
                        "working": ("working",), "needs a look": ("look",),
                        "update": ("update",)}[self.filter]
                if kind not in want:
                    continue
            out.append(g)
        if self.sort:
            def key(g):
                if self.sort == "name":
                    return g.name.lower()
                if self.sort == "source":
                    return str(g.source).lower()
                if self.sort == "api":
                    return str(g.api).lower()
                c = self.card(g)
                return (c["route"] if self.sort == "route" else c["status"]).lower()
            out.sort(key=key, reverse=self.sort_desc)
        return out

    def counts(self) -> dict:
        n = {"all": 0, "installed": 0, "working": 0, "needs a look": 0, "update": 0, "hidden": 0,
             "noexe": 0}
        hid = self.hidden()
        for g in self.all_games:
            if not g.exe:
                n["noexe"] += 1
                continue
            # the tabs count what the grid can show: a 32-bit game is not one
            # of "3 installed" while the view shows 64-bit only
            if self.arch != "all" and g.bitness is not None and str(g.bitness) != self.arch:
                continue
            if str(g.folder) in hid:
                n["hidden"] += 1
                if not self.show_hidden:
                    continue
            n["all"] += 1
            kind = self.card(g)["kind"]
            if kind in ("installed", "working", "look", "update"):
                n["installed"] += 1
            if kind == "working":
                n["working"] += 1
            if kind == "look":
                n["needs a look"] += 1
            if kind == "update":
                n["update"] += 1
        return n

    # ------------------------------------------------------------ covers
    def cover(self, g, w: int, h: int):
        """The cover images for a card (five brightness steps), or None while
        they are read; the page is redrawn when they land."""
        key = f"{g.folder}|{w}x{h}"
        hit = self.covers.get(key)
        if hit is not None:
            return hit
        if key in self._cover_busy:
            return None
        self._cover_busy.add(key)
        dim = str(g.folder) in self.hidden()

        def work():
            buf, accent, kind = None, None, "none"
            try:
                a = art.find(g)
                accent = a.accent
                if a.cover:
                    buf = imaging.picture(a.cover, w, h, bg=T.BG)
                    kind = "cover"
                if buf is None and g.exe:
                    size = min(w, h) // 2
                    ib = imaging.exe_icon(g.exe, size, T.SURF)
                    if ib:
                        buf, kind = (ib, size, size), "icon"
            except Exception:
                log.exception(f"cover for {g.name}")
            self.q.put(("cover", (key, buf, w, h, accent, kind)))
        threading.Thread(target=work, daemon=True).start()
        return None

    def _on_cover(self, payload) -> None:
        import tkinter as tk
        key, buf, w, h, accent, kind = payload
        self._cover_busy.discard(key)
        entry = {"accent": accent or T.AMBER, "kind": kind, "levels": []}
        try:
            if kind == "cover" and buf:
                # rest and hover, and the dimmed one: six images a cover held
                # about 1.1 MB a game at 100% scale, most of it never shown
                entry["levels"] = [imaging.photo(tk, imaging.brightness(buf, k), w, h, self.root) for k in (0.9, 1.08)]
                entry["dim"] = imaging.photo(tk, imaging.brightness(buf, 0.35), w, h, self.root)
            elif kind == "icon" and buf:
                ib, iw, ih = buf
                entry["levels"] = [imaging.photo(tk, ib, iw, ih, self.root)]
                entry["dim"] = entry["levels"][0]
        except Exception:
            log.exception("making a cover image")
        self.covers[key] = entry
        if kind != "cover":
            self.ask_online_art()
        self.library_soon()

    def library_soon(self) -> None:
        """One redraw of the library for everything that landed in the next
        100 ms. At start every cover lands on its own, and each redrew the
        whole grid (with every card's file checks): quadratic in games."""
        if self._lib_job is not None:
            return

        def run():
            self._lib_job = None
            self.refresh("library", soft=True)
        try:
            self._lib_job = self.root.after(100, run)
        except Exception:
            self._lib_job = None
            self.refresh("library", soft=True)

    def _on_art(self, folder: str) -> None:
        """New pictures for one game: its cards and backdrops are made again."""
        pre = f"{folder}|"
        for k in [k for k in self.covers if k.startswith(pre)]:
            del self.covers[k]
        page = getattr(self, "game_page", None)
        if page is not None:
            for k in [k for k in page.backdrops if k.startswith(pre)]:
                del page.backdrops[k]
        self.library_soon()
        shown = getattr(self, "game", None)
        if shown is not None and str(shown.folder) == folder:
            self.refresh("game")

    # ------------------------------------------------------------ pictures by hand
    def picture_items(self, g) -> list:
        """The menu entries that change a game's pictures."""
        items = [("choose cover...", lambda: self.choose_picture(g, "cover")),
                 ("choose background...", lambda: self.choose_picture(g, "hero")),
                 ("choose logo...", lambda: self.choose_picture(g, "logo"))]
        known = art.peek(g)
        if known is not None and known.chosen:
            items.append(("use the automatic pictures", lambda: self.reset_pictures(g)))
        return items

    def choose_picture(self, g, kind: str) -> None:
        from tkinter import filedialog
        # only what GDI+ draws: it has no WebP decoder on every Windows
        p = filedialog.askopenfilename(
            title={"cover": "cover for", "hero": "background for", "logo": "logo for"}[kind] + f" {g.name}",
            parent=self.root, filetypes=[("pictures", "*.jpg *.jpeg *.png *.bmp")])
        if not p:
            return

        def work():
            try:
                said = art.choose(g, kind, p)
            except Exception as e:
                log.exception("choosing a picture")
                said = first_line(e)
            self.q.put(("picture", (g.name, kind, said)))
        threading.Thread(target=work, daemon=True).start()

    def reset_pictures(self, g) -> None:
        def work():
            try:
                art.reset(g)
                said = ""
            except Exception as e:
                said = first_line(e)
            self.q.put(("picture", (g.name, "", said)))
        threading.Thread(target=work, daemon=True).start()

    def _on_picture(self, payload) -> None:
        name, kind, said = payload
        if said:
            self.shell.error("picture not used", said)
        else:
            label = {"hero": "background"}.get(kind, kind)
            self.shell.status(f"{name}: {label} set" if kind else f"{name}: automatic pictures")

    def ask_online_art(self) -> None:
        """Once, when a game has no picture on this PC: may its name go to
        Steam's store to find one? A banner, not a dialog - nothing waits on it."""
        from .. import covers
        if not covers.undecided() or getattr(self, "_asked_online_art", False):
            return
        self._asked_online_art = True

        def answer(on):
            self.shell.unbanner("covers")
            self.set_online_art(on)
        self.shell.banner("covers", "some games have no cover on this PC - look them up online? "
                                    "(sends their names to Steam's store)",
                          [("look them up", lambda: answer(True), True), ("no", lambda: answer(False), False)])

    def set_online_art(self, on: bool) -> None:
        prefs.set_("online_art", bool(on))
        if on:
            art.retry()
            self.covers.clear()
            page = getattr(self, "game_page", None)
            if page is not None:
                page.backdrops.clear()
        self.refresh("library", soft=True)

    # ------------------------------------------------------------ the saved library
    def remember_library(self) -> None:
        if self._recheck:
            return
        if self.have_library() and (self._rows or not library.FILE.is_file()):
            library.save(self.all_games, self._rows, update.VERSION, self._sm())

    def load_cached(self) -> bool:
        try:
            got = library.load(update.VERSION, self._sm())
        except Exception:
            log.exception("reading the library cache")
            got = None
        if not got:
            return False
        gs, rows, changed = got
        self.all_games = list(gs)
        kp = video.known()
        if kp and not any(x.install_dir == kp.install_dir for x in gs):
            self.all_games.insert(0, kp)
        self._rows.clear()
        self._rows.update(rows)
        self._recheck = {str(g.folder) for g in changed}
        self._recheck_id += 1
        n = len([g for g in self.all_games if g.exe])
        self.scan_note = (f"{n} games from the last scan"
                          + (f", reading {len(self._recheck)} that changed" if self._recheck else ""))
        if changed:
            self._recheck_changed(changed)
        # 'update (n)' came up only after a scan, an install or an uninstall;
        # a normal start opens here and never asked. Worker thread.
        self.check_stale()
        return True

    def _recheck_changed(self, changed: list) -> None:
        sm = self._sm()
        gen = self._recheck_id

        def work():
            rows = {}
            for g in changed:
                try:
                    games.enrich(g)
                except Exception:
                    log.exception(f"re-reading {g.name}")
                if g.exe:
                    rows[(str(g.folder), str(g.exe))] = self.inspect_row(g, sm)
            self.q.put(("rechecked", (gen, rows, [str(g.folder) for g in changed])))
        threading.Thread(target=work, daemon=True).start()

    def _on_rechecked(self, payload) -> None:
        gen, rows, folders = payload
        if gen != self._recheck_id:
            return
        self._rows.update(rows)
        for f in folders:
            self._recheck.discard(f)
        if not self._recheck:
            self.scan_note = f"{len([g for g in self.all_games if g.exe])} games"
            self.remember_library()
        self.library_soon()

    def scan(self, full: bool = False) -> None:
        if self.busy or self.scanning:
            return
        self.scanning = True
        self._recheck = set()
        self._recheck_id += 1
        self.scan_note = "scanning..."
        self.scan_failed = ""
        self.shell.busy("scanning your stores and game folders...")
        self.refresh("library")

        def rows_for(gs, sm, only=None):
            rows = {}
            todo = [g for g in gs if g.exe and (only is None or g in only)]
            for i, g in enumerate(todo, 1):
                self.q.put(("scan", f"checking compatibility {i}/{len(todo)}: {g.name}"))
                started = time.monotonic()
                rows[(str(g.folder), str(g.exe))] = self.inspect_row(g, sm)
                if time.monotonic() - started >= 1:
                    log.write(f"checked {g.name} in {time.monotonic() - started:.1f}s")
            return rows

        def quick(cached):
            known, rows, changed = cached
            gs, fresh = games.quick_scan(known, progress=lambda m: self.q.put(("scan", m)))
            sm = self._sm()
            for g in changed:
                try:
                    games.enrich(g)
                except Exception:
                    log.exception(f"re-reading {g.name}")
            keep = {(str(g.folder), str(g.exe)) for g in gs if g.exe}
            rows = {k: r for k, r in rows.items() if k in keep}
            rows.update(rows_for(gs, sm, only=fresh + [c for c in changed if c in gs]))
            log.write(f"quick rescan: {len(fresh)} new, {len(changed)} changed, {len(gs)} in all")
            library.save(gs, rows, update.VERSION, sm)
            self.q.put(("scanned", (gs, rows)))

        def work():
            gs = games.scan_all(progress=lambda m: self.q.put(("scan", m)))
            sm = self._sm()
            rows = rows_for(gs, sm)
            library.save(gs, rows, update.VERSION, sm)
            self.q.put(("scanned", (gs, rows)))

        def start():
            try:
                cached = None
                if not full:
                    try:
                        cached = library.load(update.VERSION, self._sm())
                    except Exception:
                        log.exception("reading the library cache")
                (quick if cached else (lambda _c: work()))(cached)
            except (sources.RateLimited, sources.Unavailable) as e:
                self.q.put(("scanfail", str(e)))
            except Exception:
                log.exception("scanning the library")
                self.q.put(("scanfail", traceback.format_exc()))
        threading.Thread(target=start, daemon=True).start()

    def _on_scan(self, text: str) -> None:
        self.shell.busy(text.lower())

    def _on_scanned(self, payload) -> None:
        gs, rows = payload
        self.scanning = False
        self.shell.busy("")
        kept = [g for g in self.all_games if g.source == "Manual" and not any(
            x.folder == g.folder for x in gs)]
        allg = kept + list(gs)
        try:
            # it returns the list with one entry per executable; the answer
            # was dropped, and a game two stores report stayed in twice
            allg = list(games.same_exe_once(allg))
        except Exception:
            log.exception("merging games two stores report")
        kp = video.known()
        if kp and not any(x.install_dir == kp.install_dir for x in allg):
            allg.insert(0, kp)
        self.all_games = allg
        self._rows = dict(rows)
        art.forget()
        n = len([g for g in allg if g.exe])
        noexe = len([g for g in allg if not g.exe])
        self.scan_note = f"{n} games" + (f", {noexe} folders had no executable" if noexe else "")
        self.scan_failed = ""
        self.shell.status(f"scan complete: {n} games")
        self.remember_library()
        self.check_stale()
        self.watch_refresh()
        self.refresh("library")

    def _on_scanfail(self, text: str) -> None:
        self.scanning = False
        self.shell.busy("")
        self.scan_note = "the scan stopped - see the log"
        text = str(text or "")
        # said under the library's title: one line in a closed log was all a
        # rate-limited scan left behind (#163)
        if "Traceback" in text or not text.strip():
            self.scan_failed = "the scan stopped - details in the log"
        else:
            self.scan_failed = "the scan stopped: " + first_line(Exception(text.strip()))
        self.write(text.strip().splitlines()[-1] if text.strip() else "scan failed", "err")
        if "Traceback" in text:
            self.offer_crash_report()
        self.refresh("library")

    def check_stale(self) -> None:
        # which games are installed is a few file checks each: asked on the
        # worker, since this now also runs when the window opens
        todo = [g for g in self.all_games if g.exe]
        if not todo:
            return

        def work():
            try:
                roots = [g.install_dir for g in todo if g.installed]
                if roots:
                    self.q.put(("stale", components.stale_counts(roots)))
            except Exception:
                log.exception("checking installed games for updates")
        threading.Thread(target=work, daemon=True).start()

    def _on_stale(self, counts: dict) -> None:
        self.stale = dict(counts or {})
        self.library_soon()
        self.refresh("game", soft=True)

    def load_board(self) -> None:
        """Nothing draws the component board in 2.0, and it asked GitHub six
        times on every start for it. Kept as a name only while app.py still
        calls it."""
        return

    def _on_board(self, text: str) -> None:
        self.board = text

    def update_targets(self) -> tuple[list, list]:
        """(games update all installs again, installed games with newer parts
        it leaves alone because of their anti-cheat). The scan menu's count
        and update all read this one answer, so they cannot disagree."""
        todo, skipped = [], []
        for g in self.all_games:
            if not g.exe or not self.stale.get(str(g.install_dir)) or not g.installed:
                continue
            row = self._rows.get((str(g.folder), str(g.exe)))
            if isinstance(row, tuple) and len(row) > 4 and row[4]:
                skipped.append(g)
            else:
                todo.append(g)
        return todo, skipped

    def update_all(self) -> None:
        if self.busy:
            return
        targets, skipped = self.update_targets()
        left = (f" {len(skipped)} game{'s' if len(skipped) != 1 else ''} with anti-cheat "
                f"{'are' if len(skipped) != 1 else 'is'} left for you to update from "
                f"{'their' if len(skipped) != 1 else 'its'} own page.") if skipped else ""
        if not targets:
            self.shell.status("nothing to update - every installed game is current" if not skipped
                              else "nothing updated -" + left)
            return
        if not self.shell.ask(f"update {len(targets)} game{'s' if len(targets) != 1 else ''}?",
                              "Each is installed again with the same route and settings, with the "
                              "newest components. Backups and your own files are kept." + left,
                              "update", "cancel"):
            return
        self.busy = True

        def work():
            done = []
            for g in targets:
                self.q.put(("scan", f"updating {g.name}..."))
                try:
                    # inside the try: a malformed record raised here, the
                    # message never came and busy stayed on for good
                    opt = installer.options_from_manifest(g.install_dir)
                    if opt is None:
                        continue
                    # read again here: the row's answer may be older than the folder
                    if anticheat.detect(g.install_dir, g.folder).present:
                        self.q.put(("log", (f"{g.name}: anti-cheat found - not updated", "warn")))
                        continue
                    installer.install(g, opt, on_log=lambda t: log.write(t))
                    done.append(g)
                except Exception as e:
                    log.exception(f"updating {g.name}", e)
                    self.q.put(("log", (f"{g.name}: {first_line(e)}", "err")))
            self.q.put(("updated_all", (len(done), done, len(skipped))))
        threading.Thread(target=work, daemon=True).start()

    def _on_updated_all(self, payload) -> None:
        if isinstance(payload, tuple):
            n, done, skipped = payload
        else:
            n, done, skipped = int(payload or 0), [], 0
        self.watch_refresh()
        self.busy = False
        self.stale = {}
        self._rows.clear()
        self.shell.busy("")
        # what the cards and pages said came from the installs before the
        # update: the verdict, the offer of a next route, the DLSS build read
        for g in done:
            self.verdicts.pop(str(g.install_dir), None)
            try:
                self.dlss_reread(g)
            except Exception:
                log.exception(f"reading the DLSS files of {g.name} again")
        if done:
            prefs.set_("last_verdicts", self.verdicts)
        self.shell.status(f"updated {n} game{'s' if n != 1 else ''}"
                          + (f" - {skipped} with anti-cheat left for you" if skipped else ""))
        self.check_stale()
        self.refresh("library")

    def pick_folder(self) -> None:
        from tkinter import filedialog
        d = filedialog.askdirectory(title="select the game folder", parent=self.root)
        if not d:
            return
        g = games.manual(Path(d))
        if not g.exe:
            self.shell.error("no executable here", f"No executable was found in:\n{d}")
            return
        self.all_games.insert(0, g)
        self.query = ""
        self.filter = "all"
        self._read_rows([g])
        self.root.after(1, self.remember_library)
        self.open_game(g)
