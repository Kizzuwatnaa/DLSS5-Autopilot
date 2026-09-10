"""What Windows itself recorded when the game closed.

The largest group of reports by far is "the game closes itself" - twenty
five of the first eighty seven - and it is the hardest to answer, because a
game that dies before ReShade loads writes no log at all. Every answer so
far has been reasoning from what is missing.

Windows was writing it down the whole time. An application that crashes
leaves an "Application Error" event carrying the executable, the faulting
module and the exception code:

    BsgLauncher.exe | faulting module: libcef.dll | 0xc0000005

That single line settles the question the diagnosis has to guess at: our
proxy faulting is our bug, the game's own module faulting is the game's,
and a third party's module is a conflict to name. It is read only for the
executable in question, only for events since the install, and behind a
hard timeout - a diagnosis that hangs is worse than one that says nothing.

Nothing here writes to the event log, and nothing leaves the machine.
"""
from __future__ import annotations

import calendar
import re
import subprocess
import time
from dataclasses import dataclass

# Application Error covers native crashes; .NET Runtime and Application
# Hang are the other two a game can end up in. Windows Error Reporting's
# own provider repeats them, so it is left out.
PROVIDERS = ("Application Error", ".NET Runtime", "Application Hang")
TIMEOUT = 8


@dataclass
class Crash:
    when: str
    exe: str
    module: str
    code: str
    provider: str

    def ours(self, written: tuple[str, ...] = ()) -> bool:
        """Certainly a file this tool put there.

        `written` is the install record's own file list, which is the only
        complete answer: a name list here missed the MFG unlock, the ASI
        loader, REFramework and DXVK - every one of them a file this tool
        wrote - and told the person to close their overlays instead.
        """
        m = self.module.lower()
        if m and m in {str(w).replace("\\", "/").rsplit("/", 1)[-1].lower()
                       for w in written}:
            return True
        return ("reshade" in m or "dlss5" in m or "renodx" in m
                or "optiscaler" in m or m.startswith("nvngx"))

    def ambiguous(self, proxy: str = "") -> bool:
        """The name this install's proxy uses - and also a Windows DLL.

        dxgi.dll in a game folder is ReShade; dxgi.dll in System32 is
        Windows'. The event says which module faulted, not which copy, so
        the two cannot be told apart from here and the wording must not
        pretend otherwise. Only the name this install actually wrote counts:
        a Remix or Vulkan-layer install wrote no proxy at all.
        """
        m = self.module.lower()
        return bool(proxy) and m == proxy.lower() and not self.ours()


def _ps(script: str) -> str:
    """Run one PowerShell command with no profile and a hard timeout.

    Event text is written by whatever produced the event, in whatever the
    machine's code page is, and Python's default decoding falls over on the
    first byte outside it ([[foreign writers]] again - a Turkish cp1254
    console killed this on the first try). Both ends are pinned to UTF-8 and
    anything still undecodable is replaced rather than raised.
    """
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding="
             "[System.Text.Encoding]::UTF8; " + script],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def last_crash(exe_name: str, since: float = 0.0,
               within_days: int = 14) -> Crash | None:
    """The newest crash Windows recorded for `exe_name`, or None.

    `since` is a unix timestamp - normally the install's - so a crash from
    before the install is not offered as evidence about it, the same rule
    every log reader in the diagnosis follows.
    """
    exe = (exe_name or "").strip()
    if not exe or not re.fullmatch(r"[\w .\-()+]{1,120}", exe):
        return None
    days = max(1, min(int(within_days), 60))
    providers = ",".join(f"'{p}'" for p in PROVIDERS)
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"Get-WinEvent -FilterHashtable @{{LogName='Application';"
        f"ProviderName=@({providers});StartTime=(Get-Date).AddDays(-{days})}}"
        " -MaxEvents 60 |"
        " ForEach-Object { $p=$_.Properties;"
        " ($_.TimeCreated.ToUniversalTime().ToString('s')) + '|' +"
        " $_.ProviderName + '|' +"
        # The inner -join needs its own parentheses: without them the whole
        # concatenation is joined instead of the field list, and every field
        # comes back separated by spaces, which parses as one long name.
        " (($p | ForEach-Object { $_.Value }) -join '~') }"
    )
    out = _ps(script)
    best: Crash | None = None
    for line in out.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) != 3:
            continue
        when, provider, rest = parts
        fields = rest.split("~")
        if not any(f.strip().lower() == exe.lower() for f in fields[:2]):
            continue
        if since:
            try:
                # The string is already UTC. time.mktime reads it as local
                # and time.daylight only says the zone HAS daylight saving,
                # not that it is in effect - so the old correction was an
                # hour out all winter in most of Europe, and a crash from
                # before the install could pass as evidence about it.
                stamp = calendar.timegm(time.strptime(when, "%Y-%m-%dT%H:%M:%S"))
                if stamp < since - 60:
                    continue
            except ValueError:
                pass
        # Application Error's own field order: exe, version, stamp, module,
        # version, stamp, exception code, offset, pid, ... The code is bare
        # hex ("c0000005"), not 0x-prefixed, so it is read by position and
        # then checked, rather than searched for by shape.
        # ...and a ReShade add-on is a DLL with its own extension, which is
        # the one case where the answer is "this is ours" - leaving
        # .addon64/.addon32 off this list hid exactly that.
        module = next((f for f in fields[3:6]
                       if f.lower().endswith((".dll", ".exe", ".sys",
                                              ".addon64", ".addon32"))), "")
        # An Application Hang carries no module at all, and taking the first
        # matching event regardless would let one hang hide the crash that
        # actually explains the evening.
        if not module:
            continue
        code = ""
        if len(fields) > 6 and re.fullmatch(r"[0-9A-Fa-f]{8}", fields[6].strip()):
            code = "0x" + fields[6].strip().upper()
        best = Crash(when=when.replace("T", " "), exe=exe, module=module,
                     code=code, provider=provider)
        break          # newest first
    return best


def describe(c: Crash | None, proxy: str = "",
             written: tuple[str, ...] = ()) -> tuple[str, str] | None:
    """(title, detail) for the report, or None when there is nothing to say.

    `proxy` is the file name this install wrote beside the game, out of the
    manifest. Without it a module cannot be called ours on a name alone.
    `written` is the rest of that manifest's file list, so a fault in the
    MFG unlock, the ASI loader, REFramework or DXVK is recognised as ours
    instead of being answered with "close your overlays".
    """
    if c is None or not c.module:
        return None
    where = f"{c.when} UTC" if c.when else "an unrecorded time"
    if c.module.lower() == c.exe.lower():
        return (f"Windows recorded {c.exe} faulting in its own code "
                f"({c.code or 'no code'}).",
                f"The crash was inside the game itself, not in anything this "
                f"tool loaded, at {where}. That does not clear the install - "
                f"a hook can push a game into its own bad path - but it means "
                f"the fault is not in our module. Uninstalling and starting "
                f"the game once is the fastest way to tell the two apart.")
    # The ambiguous case FIRST. The install's own proxy is in `written`,
    # and its name is also a Windows system DLL's: the event names a module,
    # not a path, so "it is in our file list" is not proof it was our copy.
    # Testing ownership first made this branch unreachable and turned the
    # commonest fault of all into "this is ours to fix".
    if c.ambiguous(proxy):
        return (f"Windows recorded {c.exe} faulting in {c.module} "
                f"({c.code or 'no code'}).",
                f"That is the name this install writes beside the game "
                f"({proxy}), and it is also the name of a Windows system "
                f"DLL - the event does not say which copy faulted, at "
                f"{where}. If it was ours, press 'report a bug'. To tell "
                f"them apart: uninstall from here and start the game once. "
                f"A fault in the same place with our file gone is the "
                f"driver's or the game's.")
    if c.ours(written):
        return (f"Windows recorded {c.exe} faulting in {c.module} "
                f"({c.code or 'no code'}).",
                f"That module is part of this install, at {where}. This is "
                f"ours to fix: press 'report a bug' so the module, the "
                f"exception code and the route go in together.")
    return (f"Windows recorded {c.exe} faulting in {c.module} "
            f"({c.code or 'no code'}).",
            f"That module is neither the game's executable nor anything this "
            f"tool installs, at {where} - an overlay, a driver component or "
            f"another mod. Close overlays (RivaTuner, Discord, GeForce "
            f"Experience) and try again; if it persists, that module is the "
            f"one to chase.")
