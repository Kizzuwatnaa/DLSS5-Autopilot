r"""Do the notes I keep still describe this code?

A memory is written the day something was learned and then believed for
months. The code moves: a file is renamed, a constant goes, a tool is
replaced, a check moves into a hook. A note that names something which no
longer exists is worse than no note - it is read as current and acted on.

    python _tools\memory_check.py            what no longer resolves
    python _tools\memory_check.py --list     every claim it checked
    python _tools\memory_check.py --dir X    another memory folder

It checks the mechanical half, which is most of it:

  - every file path a note names exists in the repository;
  - every `module.SYMBOL` and every backticked identifier is still defined
    somewhere in it;
  - every [[link]] points at a memory that exists;
  - every memory is listed in MEMORY.md, and MEMORY.md links nothing that
    is gone;
  - the frontmatter has a name matching the filename and one of the four
    types.

What it cannot check is whether a note is still TRUE - that a rule still
behaves the way the note says. That half needs reading, and the round that
reads it should say so in the handover.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
DEFAULT_MEM = (Path(os.environ.get("USERPROFILE", Path.home()))
               / ".claude" / "projects"
               / "C--Users-Mustafa-Desktop-dlss-5" / "memory")

# A path a note can name: repo-relative, with an extension this repo uses.
PATH_RE = re.compile(r"`([A-Za-z_][\w./\\-]*\.(?:py|json|md|bat|yml|ico|txt))`")
# module.SYMBOL, e.g. installer.OTHER_NGX_HOOKS, gui.ADDON_AUTO, sources.pick
DOTTED_RE = re.compile(r"`([a-z_]+)\.([A-Za-z_]\w+)(?:\(\))?`")
# a bare identifier in backticks that looks like code rather than prose
IDENT_RE = re.compile(r"`(_[A-Za-z]\w+|[a-z][a-z0-9]*_[a-z0-9_]+)(?:\(\))?`")
LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
FRONT_NAME = re.compile(r"^name:\s*(.+)$", re.M)
FRONT_TYPE = re.compile(r"^\s*type:\s*(.+)$", re.M)
TYPES = {"user", "feedback", "project", "reference"}
# Words that look like identifiers but are prose, tool names or other
# projects' files - not things this repository has to define.
SKIP = {
    "dlss5_autopilot", "did_it_work", "report_a_bug", "share_the_result",
    "auto_update", "scan_library", "full_rescan", "game_pass", "no_log",
    "never_ran", "work_area", "model_resolution", "reshade_loads_as",
    "loads_as", "graphics_api", "opti_build", "dlss5_feed", "dlss5_feeder",
    "nvngx_dlssnr", "nvngx_dlss", "nvngx_dlssg", "nvngx_dlssd", "_nvngx",
    "remix_nvngx", "dlssg_sm86", "renodx_dlss5", "dlss5_bridge",
    "steam_autocloud", "cyber_engine_tweaks", "rtx40mfg", "rtx40mfgcore",
    "d3d12core", "reshade_finish_effects", "vkcreateswapchainkhr",
    "webhelper", "user_settings", "player_log", "gameuser_settings",
    "e_g", "i_e",
}


def repo_text() -> str:
    """Everything this repository's own code says, in one string."""
    out = []
    for p in list(SRC.glob("*.py")) + list((SRC / "core").glob("*.py")) \
            + list((SRC / "_tools").glob("*.py")) \
            + list((SRC / "_tools" / "hooks").glob("*.py")):
        try:
            out.append(p.read_text(encoding="utf8", errors="replace"))
        except OSError:
            pass
    for extra in (".claude/skills/issue-triage/SKILL.md",
                  ".claude/skills/release-gate/SKILL.md",
                  ".claude/skills/upstream-watch/SKILL.md",
                  ".claude/settings.json"):
        p = SRC.parent / extra
        try:
            out.append(p.read_text(encoding="utf8", errors="replace"))
        except OSError:
            pass
    return "\n".join(out)


def _resolves(path: str) -> bool:
    """Is this a file of ours - written relative, or named on its own?

    A note says `core/diagnose.py` one line and `diagnose.py` the next, and
    both mean the same file. Only the basename has to be somewhere in the
    repository; a note naming a scratchpad script that dies with the session
    is exactly what this is meant to catch, so nothing outside it counts.
    """
    rel = path.replace("\\", "/")
    if (SRC / rel).exists() or (SRC.parent / rel).exists():
        return True
    base = rel.rsplit("/", 1)[-1]
    if base != rel and rel.split("/", 1)[0] in ("scratchpad", "tmp", "temp"):
        return False          # a session folder: gone by the next round
    for where in (SRC, SRC / "core", SRC / "_tools", SRC / "_tools" / "hooks",
                  SRC / "docs", SRC / ".github" / "workflows",
                  SRC.parent, SRC.parent / ".claude" / "skills"):
        if (where / base).exists():
            return True
    return any(p.name == base for p in SRC.rglob(base) if p.is_file())


def check(mem: Path, show_all: bool) -> int:
    files = sorted(p for p in mem.glob("*.md") if p.name != "MEMORY.md")
    if not files:
        print(f"no memories in {mem}")
        return 2
    code = repo_text()
    names = {p.stem for p in files}
    index = (mem / "MEMORY.md").read_text(encoding="utf8", errors="replace") \
        if (mem / "MEMORY.md").is_file() else ""
    problems: list[str] = []
    checked = 0

    for p in files:
        t = p.read_text(encoding="utf8", errors="replace")
        say = []

        m = FRONT_NAME.search(t)
        if not m:
            say.append("no name: in the frontmatter")
        elif m.group(1).strip().strip('"') != p.stem:
            say.append(f"name: is {m.group(1).strip()!r}, file is {p.stem!r}")
        m = FRONT_TYPE.search(t)
        if not m:
            say.append("no type: in the frontmatter")
        elif m.group(1).strip() not in TYPES:
            say.append(f"type: {m.group(1).strip()!r} is not one of {sorted(TYPES)}")

        if p.name not in index:
            say.append("not listed in MEMORY.md")

        for link in LINK_RE.findall(t):
            checked += 1
            if link.strip() not in names:
                say.append(f"[[{link}]] points at no memory")

        for path in PATH_RE.findall(t):
            checked += 1
            if not _resolves(path):
                say.append(f"names {path}, which is not in the repository")

        for mod, sym in DOTTED_RE.findall(t):
            checked += 1
            if (SRC / "core" / f"{mod}.py").is_file() and sym not in code:
                say.append(f"names {mod}.{sym}, which nothing defines")

        for ident in IDENT_RE.findall(t):
            if ident.lower() in SKIP or len(ident) < 5:
                continue
            checked += 1
            if ident not in code:
                say.append(f"names {ident}, which nothing defines")

        if say:
            problems.append(p.name + "\n    " + "\n    ".join(sorted(set(say))))
        elif show_all:
            print(f"  ok   {p.name}")

    for link in re.findall(r"\(([a-z0-9-]+\.md)\)", index):
        checked += 1
        if not (mem / link).is_file():
            problems.append(f"MEMORY.md\n    links {link}, which is gone")

    print("=" * 78)
    print(f"{len(files)} memories, {checked} claims checked against the code")
    print("=" * 78)
    if not problems:
        print("  every path, symbol and link in them still resolves.")
        print("  (whether each note is still TRUE is a reading job, not this.)")
        return 0
    for pr in problems:
        print("  !! " + pr)
    print()
    print(f"{len(problems)} memory file(s) name something that is not there.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DEFAULT_MEM))
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    return check(Path(a.dir), a.list)


if __name__ == "__main__":
    raise SystemExit(main())
