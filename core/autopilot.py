r"""Install, watch the game start, and try the next route when nothing loaded.

The tool has always stopped at the same place: it writes the files, says
what it wrote, and the person is on their own from there. Two classes of
report come straight out of that gap - "the install stopped part way" and
"nothing we wrote ever loaded" are 32 of the 87 reports in the corpus - and
both were answerable at the time, by the machine, in about a minute.

This is that minute. One pass is:

    install the route -> start the game (or ask, when this tool may not) ->
    wait for the process and let it settle -> read its module list

and the module list decides. Our dxgi.dll in the process means the chain is
in and the person can play; our dxgi.dll beside the game while System32's is
the one loaded means this route cannot work here, however many times it is
installed - so the next route is tried without anybody having to know that.

What it deliberately does NOT do:

  * decide whether the picture is better. That needs frames, a log and
    somebody looking at the screen; "did it work?" answers it afterwards.
  * start a game with anti-cheat, or a launcher executable. The first can
    read a started process as tampering, the second starts the wrong thing.
  * keep going forever. Three routes, then it stops and says so - a loop
    that reinstalls all night is not autopilot, it is a fault.

Nothing here writes into a game folder: the installs are `installer.install`
as the window runs it, and the watching is `watch`, which only reads.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import anticheat, child, community, installer, log, pe, watch, wincrash

# Three, and the first one is the route the tool recommended. A fourth try
# has never rescued a game in the corpus, and every attempt costs a download,
# an install and a launch of somebody's game.
MAX_ATTEMPTS = 3

# How long to wait for the game to appear before giving up on this pass.
# A cold start off a hard disk with a launcher in front of it is slow.
START_SECONDS = 300.0

# And how long to wait for it to close again before the next route. The
# installer cannot replace a DLL the running game has mapped in, so this
# loop has to wait for the game it just started to go away.
CLOSE_SECONDS = 600.0


@dataclass
class Attempt:
    """One route, from install to what the process had loaded."""
    route: str
    installed: bool = False
    started: bool = False
    ours: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    elsewhere: list[str] = field(default_factory=list)
    note: str = ""             # advice about the launch, never a reason
    why: str = ""              # why this attempt could not answer
    no_record: bool = False    # installed, but nothing could record it

    @property
    def loaded(self) -> bool:
        """Our files in the process, and none of them shadowed.

        A name loaded from somewhere else while ours sits beside the game is
        the exact fault this pass exists to find - ours being in the process
        as well does not undo it, so it is not "it worked".
        """
        return bool(self.ours) and not self.elsewhere


@dataclass
class Outcome:
    attempts: list[Attempt] = field(default_factory=list)
    route: str = ""
    ok: bool = False
    stopped: str = ""
    installed: str = ""        # the route this pass installed and kept
    left: str = ""             # what the folder's own record says is in it

    @property
    def note(self) -> str:
        """Whatever the last attempt had to say about starting the game."""
        return self.attempts[-1].note if self.attempts else ""

    @property
    def tried(self) -> list[str]:
        return [a.route for a in self.attempts]


def plan(first: str, offer: list[str], data: dict | None = None,
         game=None, limit: int = MAX_ATTEMPTS, klass: str = "") -> list[str]:
    """The routes to try, in order, starting with the recommended one.

    Only routes this game is actually offered: naming one that is not in the
    dropdown is #148's shape, and installing one would be worse. What other
    people's results say goes second, so a route that rescued this game
    elsewhere is tried before the rest of the list.
    """
    out = [first] if first and first in (offer or [first]) else []
    rest = [n for n in (offer or []) if n not in out]
    if data is not None and game is not None:
        try:
            # every remaining route in the order the shared results put them
            # in, not just one promoted by matching a sentence
            rest = [n for n, _why in community.rank_routes(data, game, rest, klass)]
        except Exception:
            pass
    return (out + rest)[:max(1, limit)]


def plan_reasons(offer: list[str], data: dict | None, game=None,
                 klass: str = "") -> list[tuple[str, str]]:
    """(route, why it is in that place) for a plan, for the log to print.

    A pass that reorders itself has to say what reordered it, or the person
    watching the log sees the tool choose and cannot tell why.
    """
    if data is None or game is None:
        return [(n, "") for n in offer]
    try:
        said = dict(community.rank_routes(data, game, offer, klass))
    except Exception:
        said = {}
    return [(n, said.get(n, "")) for n in offer]


def _through_user_layer(game) -> bool:
    """Does the install in this game's folder reach it as a Vulkan layer?

    Read from the install record, never guessed: a folder with no record
    answers False, and so does anything that cannot be read.
    """
    try:
        man = installer._previous_manifest(Path(game.install_dir)) or {}
        return man.get("proxy") == installer.VULKAN_LAYER
    except Exception:
        return False


def _layer_reaches_elevated(game) -> bool:
    """Is a ReShade layer this game can load on the machine's own list?

    False when it cannot be read - the refusal it guards is the safe answer.
    """
    try:
        from . import vulkan
        bits = getattr(game, "bitness", None)
        return bits in (32, 64) and vulkan.reaches_elevated(bits == 64)
    except Exception:
        return False


def may_start(game, check_running: bool = False) -> tuple[bool, str]:
    """Whether this tool may start the game itself, and why not when it may not.

    Never a guess dressed as a yes: when the answer is no the person is told
    what to press, which is what they would have done anyway.

    `check_running` asks the process table as well, which costs a snapshot of
    every process on the machine and a PE read per candidate. start() asks
    for it; the callers that only want the question answered to word a
    dialog do not, because they ask on the Tk thread (#8, #18, #32).
    """
    exe = getattr(game, "exe", None)
    if exe is None:
        return False, "this game has no executable picked"
    # A folder that cannot be read is a refusal, not a pass. The cost of
    # being wrong here is not symmetric: one button press against a ban.
    try:
        found = anticheat.detect(Path(game.install_dir), Path(game.folder))
    except Exception as e:
        log.write(f"autopilot: could not read {game.folder} for anti-cheat:"
                  f" {e}", "warn")
        return False, ("this folder could not be read to check for "
                       "anti-cheat, so start the game yourself")
    if found.present:
        return False, (f"{found.summary} is in this folder ("
                       f"{', '.join(found.evidence[:2])}) - an anti-cheat can "
                       f"read a game started by another program as tampering, "
                       f"so start it yourself the way you always do")
    # Starting a second copy of a running game is never what was meant, and
    # the button's own 10-second lock only covers a double press. THE GAME,
    # though: from_folder returns everything running out of the folder, and
    # this tool writes one of those itself - the 32-bit feeder's
    # host64\dlss5-feed-host64.exe, which outlives the game by a few
    # seconds. Refusing on our own helper, or on a launcher that lives in
    # the folder, is a dead button with a nonsense reason (gate 1.9.1, and
    # the standing rule that our own files are never evidence about a game).
    if check_running:
        want = Path(exe).name.lower()
        host = str(Path(game.install_dir) / installer.HOST_DIR).lower()

        def _is_the_game(p) -> bool:
            path = str(p.path or "").lower()
            if not path or path.startswith(host):
                return False     # our own 64-bit helper, never the game
            if os.path.basename(path) == want:
                return True
            # The picked exe is not always the process: a launcher in the
            # folder starts the real one (#191), and an Unreal game picked
            # as Game.exe runs as Game-Win64-Shipping.exe. Those are still
            # this game running.
            try:
                return pe.looks_like_game(Path(p.path))
            except Exception:
                return False

        try:
            running = [p for p in watch.from_folder(Path(game.install_dir),
                                                    exe=str(exe))
                       if _is_the_game(p)]
        except Exception:
            running = []
        if running:
            return False, (f"{running[0].name} is already running - "
                           f"switch to it rather than starting a second copy")
    try:
        if pe.launcher_like(Path(exe)):
            return False, (f"{Path(exe).name} is a launcher, not the game - "
                           f"starting it would start the wrong thing")
    except Exception:
        pass
    # A program this tool starts inherits its token. Run as administrator,
    # that makes the game elevated too, and an elevated process does not get
    # the Vulkan layers registered per user - which is where ReShade's is.
    # On a route that reaches the game through that layer the start itself
    # would decide the answer: DXVK runs, ReShade never does, and autopilot
    # records the route as failed (#400, #348).
    # ...unless the layer is on the machine's list as well, which an install
    # made as administrator puts there (#562): then the elevated start gets
    # ReShade like any other.
    if _through_user_layer(game) and wincrash.elevated() \
            and not _layer_reaches_elevated(game):
        return False, ("this tool is running as administrator, and a game it "
                       "starts would be too - the Vulkan loader gives an elevated "
                       "game none of the per-user layers ReShade needs on "
                       "this route. Install again from here to put the layer "
                       "where an elevated game looks, or start the game "
                       "yourself the way you always do")
    src = str(getattr(game, "source", "") or "")
    if src and src.lower() not in ("manual", "emulator"):
        return True, (f"{src} game: it starts from the executable here, but "
                      f"if {src} wants to own the launch, start it there "
                      f"instead - the watching is the same either way")
    return True, ""


def start(game) -> tuple[bool, str]:
    """Start the game. (started, what to say about it.)"""
    ok, why = may_start(game, check_running=True)
    if not ok:
        return False, why
    exe = Path(game.exe)
    try:
        child.popen([str(exe)], cwd=str(exe.parent),
                         close_fds=True)
        return True, why
    except Exception as e:                      # a store stub, a permission
        log.write(f"autopilot: could not start {exe.name}: {e}", "warn")
        return False, (f"{exe.name} would not start ({e}) - start it the "
                       f"way you normally do")


class Hooks:
    """Everything here that touches the world, so a test can hand in its own.

    Plain instance attributes rather than a dataclass: a function stored as
    a CLASS attribute is a descriptor, so `hooks.install(...)` would arrive
    with the Hooks object as its first argument.
    """

    def __init__(self, install=None, wait=None, start=None, log=None,
                 stop=None, seconds: float = START_SECONDS, closed=None,
                 options=None):
        self.install = install or installer.install
        # Route by route, because half of Options is route-specific: the
        # OptiScaler dials, frame generation, the Remix runtime swap. Taking
        # the first route's Options for all three installed the second and
        # third as weaker versions of themselves - a remix attempt that
        # could never swap a runtime with no neural pass, which is the only
        # reason that route exists.
        self.options = options or (lambda opt, route: replace(opt, path=route))
        self.wait = wait or watch.wait_for
        self.start = start or globals()["start"]
        self.log = log or (lambda text, kind="": None)
        self.stop = stop or (lambda: False)
        self.seconds = seconds
        self.closed = closed or (lambda folder, exe, hooks: wait_closed(
            folder, exe, hooks))


def _files(root: Path) -> tuple[list[str], str]:
    man = installer._previous_manifest(root) or {}
    return list(man.get("files") or []), str(man.get("exe") or "")


def attempt(game, opt, route: str, hooks: Hooks) -> Attempt:
    """One route: install it, get the game up, read what it loaded."""
    a = Attempt(route=route)
    hooks.log(f"> {route}: installing", "head")
    try:
        rep = hooks.install(game, hooks.options(opt, route))
        a.installed = True
        if installer.record_lost(rep):
            # The next route would take these files for the game's own and
            # back them up; the message says how to undo it by hand.
            a.no_record = True
            a.why = next(w for w in rep.warnings if w.startswith(installer.RECORD_LOST_HEAD))
            hooks.log(f"  {route}: {a.why}", "warn")
            return a
    except Exception as e:
        # An install that stops is this route's answer, not the pass's: a
        # 5xx from one publisher, a route this game refuses, a file the
        # game holds open. The next route is a different download and a
        # different set of files, so it is still worth trying.
        # The cause, not the first line: a failed install opens with what
        # was taken back out (gate 2.0.7), and the lock test below reads this.
        a.why = installer.cause_of(e)
        hooks.log(f"  {route}: the install stopped - {a.why}", "warn")
        log.write(f"autopilot: {route} install failed: {e}", "warn")
        return a
    root = Path(game.install_dir)
    ours, exe = _files(root)

    started, note = hooks.start(game)
    a.note = note                        # advice about the launch, not a reason
    if started:
        hooks.log(f"  started {Path(game.exe).name} - watching", "")
    else:
        # A note here is always the reason the tool did not start it, so
        # it stands on its own - prefixing it produced "start the game now
        # - it is already running" (gate 1.9.1).
        hooks.log(f"  {note}" if note else
                  "  start the game now - watching for it", "warn")

    seen = hooks.wait(root, ours, exe, seconds=hooks.seconds,
                      tick=lambda _s, _p: not hooks.stop())
    if not seen:
        a.why = "the game was never seen running"
        return a
    a.started = True
    # seen[0] is the game (watch.from_folder puts it first and that is the
    # one remembered); the rest are its children and our own helpers, and
    # their module lists are not what "did this install get in" means.
    s = seen[0]
    a.ours, a.missing, a.elsewhere = list(s.ours), list(s.missing), list(s.elsewhere)
    others = [p.proc.name for p in seen[1:]]
    if others:
        hooks.log(f"  (also running from this folder: {', '.join(others[:3])})",
                  "")
    # The module list is the whole point of the pass, and until it is
    # written down it exists only in this object: the report reads it back
    # through watch.last_sighting(), and the summary tells people the report
    # carries it. It has to be true when they press the button.
    try:
        watch.remember(root, seen[0])
    except Exception:
        log.exception("recording what the game had loaded")
    return a


def _exe_name(game, root: Path) -> str:
    """The executable to wait for: the record's, or the game's own.

    A failed install leaves no record, and waiting on "" means waiting for
    any process under the folder - which includes the 64-bit helper this
    tool puts there itself.
    """
    name = _files(root)[1]
    if name:
        return name
    exe = getattr(game, "exe", None)
    return Path(exe).name if exe else ""


def wait_closed(folder: Path, exe: str, hooks: Hooks,
                seconds: float = CLOSE_SECONDS) -> bool:
    """Wait for the game to exit. True when it is gone.

    The installer replaces the very DLLs the running game has mapped in, and
    Windows does not allow that: switching route under a game that is still
    up is a PermissionError, and this loop is what puts the game there in
    the first place. So it asks, and waits.
    """
    end = time.monotonic() + max(1.0, seconds)
    said = False
    while time.monotonic() < end:
        if not watch.from_folder(Path(folder), exe=exe):
            return True
        if hooks.stop():
            return False
        if not said:
            hooks.log("  close the game and this carries on with the next "
                      "route", "warn")
            said = True
        time.sleep(2.0)
    return not watch.from_folder(Path(folder), exe=exe)


def run(game, opt, routes: list[str], hooks: Hooks | None = None) -> Outcome:
    """Try each route in turn until the game has our files in it."""
    hooks = hooks or Hooks()
    out = Outcome()
    todo = routes[:MAX_ATTEMPTS]
    for i, route in enumerate(todo):
        if hooks.stop():
            out.stopped = "you pressed stop"
            break
        a = attempt(game, opt, route, hooks)
        out.attempts.append(a)
        if a.loaded:
            out.ok, out.route = True, route
            hooks.log(f"  {route}: {_names(a.ours)} loaded in the game", "ok")
            out.stopped = "loaded"
            break
        if a.no_record:
            out.stopped = a.why
            break
        if not a.installed:
            # This route could not be put in place. The next one is a
            # different download and a different set of files.
            if i + 1 < len(todo):
                if "refused to write" in a.why or "is running" in a.why \
                        or "being used by another process" in a.why:
                    # Windows refused the file because the game has it open,
                    # and the next route writes into the same folder. If it
                    # never closes, the next route fails the same way.
                    root = Path(game.install_dir)
                    if not hooks.closed(root, _exe_name(game, root), hooks):
                        out.stopped = ("you pressed stop" if hooks.stop() else
                                       "the game is still running - the files "
                                       "it has open cannot be replaced while "
                                       "it is up")
                        break
                continue
            out.stopped = a.why or "the install did not finish"
            break
        if not a.started:
            # Why it could not answer - never the launch advice, which says
            # nothing about what happened ("Steam game: it starts from the
            # executable here..." is not a reason a pass stopped). And a
            # stop arrives here as "nothing came back", so it is asked
            # about first: the person knows why it stopped.
            out.stopped = ("you pressed stop" if hooks.stop()
                           else a.why or "the game was never seen running")
            break                               # nothing to learn from a rerun
        if a.elsewhere:
            hooks.log(f"  {route}: the game loaded {a.elsewhere[0]} from "
                      f"somewhere else, not ours", "warn")
        else:
            hooks.log(f"  {route}: the game ran and loaded none of our files",
                      "warn")
        if i + 1 < len(todo):
            root = Path(game.install_dir)
            if not hooks.closed(root, _exe_name(game, root), hooks):
                out.stopped = ("you pressed stop" if hooks.stop() else
                               "the game is still running - the files it has "
                               "open cannot be replaced while it is up")
                break
    else:
        out.stopped = "every route tried"
    # What is in the folder is what the RECORD says, whatever this pass
    # managed to finish: an install that stopped part way writes its files
    # and its record too, and saying "nothing was left" over that folder
    # hides the uninstall that would clean it up.
    out.installed = next((a.route for a in reversed(out.attempts)
                          if a.installed), "")
    try:
        man = installer._previous_manifest(Path(game.install_dir)) or {}
        # A failed fresh install is taken back out and may leave a record
        # holding only its reason (2.0.6): that route is not an install in
        # the folder.
        rolled = installer.net.ROLLED_BACK_NOTE in (man.get("notes") or [])
        out.left = ("" if rolled else str(man.get("path") or "")) \
            or out.installed
    except Exception:
        out.left = out.installed
    return out


def _names(paths: list[str]) -> str:
    """The file names of what was loaded, without their paths."""
    seen = sorted({os.path.basename(p) for p in paths})
    if len(seen) <= 3:
        return ", ".join(seen)
    return ", ".join(seen[:3]) + f" and {len(seen) - 3} more"


# The two ends of the pass. Anything else in `stopped` is a sentence about
# this person's machine, and saying it beats any summary written in advance.
_LOADED, _EXHAUSTED = "loaded", "every route tried"


def summary(out: Outcome) -> str:
    """One line for the window, and for the person who has to decide."""
    if out.ok:
        last = out.attempts[-1]
        return (f"The {out.route} install is loaded in the game "
                f"({_names(last.ours)}). Play for a few minutes, then press "
                f"'did it work?'.")
    if not out.attempts:
        return "Nothing was tried."
    # What is in the folder NOW is the first thing the person needs: this
    # stops with an install in place, and leaving that unsaid is how
    # somebody ends up with a route they never chose and no idea of it.
    if out.attempts[-1].no_record:
        where = ""      # the stopped text already says what to remove by hand
    elif out.installed:
        where = (f" The {out.installed} route is what is installed in the "
                 f"folder now - 'uninstall' takes it back out.")
    elif out.left:
        where = (f" The folder's record says a {out.left} install is in it - "
                 f"'uninstall' takes that back out.")
    else:
        where = " Nothing of ours is in the folder."
    if out.stopped not in (_LOADED, _EXHAUSTED):
        # It stopped for a reason of this machine's: the game would not
        # close, the install did not finish, nobody started it. That
        # sentence is the answer - a summary about routes is not.
        said = out.stopped.rstrip(". ")
        # The launch advice belongs only where nobody managed to start it.
        advice = out.note if (out.attempts and not out.attempts[-1].started
                              and out.note) else ""
        return f"Stopped: {said}.{where}" + (f" {advice}" if advice else "")
    last = out.attempts[-1]
    if last.elsewhere:
        what = (f"the game loaded {_names(last.elsewhere)} from another "
                f"folder, so the copy beside it is never reached")
    else:
        what = "nothing of ours ended up running in the game"
    return (f"Tried {', '.join(out.tried)} - {what}. Use "
            f"'report a bug' in the help menu: what the game had loaded is recorded and "
            f"goes into the report.{where}")
