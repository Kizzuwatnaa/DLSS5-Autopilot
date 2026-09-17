"""Spotting anti-cheat before it wastes the user's time - or gets them banned.

ReShade with add-ons injects a DLL and detours graphics entry points. Every
kernel-level anti-cheat treats that as tampering. The result is one of:

  * the game refuses to start
  * ReShade is silently prevented from loading, so nothing happens and the
    user assumes the tool is broken
  * an account ban

Arma 3 and Arma Reforger are the common report: both ship BattlEye, both do
nothing when set up, and neither is a tool bug. Saying so up front is more
useful than letting someone install and wonder.

Detection is by file, not by name list, so it covers games nobody has told us
about.

A marker matches a whole word of a file or folder name, never the middle of
one: "vanguard" inside some asset name told the owner of Rise of the Tomb
Raider - a single-player game - that Riot Vanguard was installed (#187). And
every warning names the file it rests on, so a wrong one can be checked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# (marker, product). Matched case-insensitively against file and directory
# names in and around the install folder, as a whole word: the character
# before it and after it (digits after it aside, mhyprot3.sys) must not be a
# letter or digit.
MARKERS: tuple[tuple[str, str], ...] = (
    ("beservice", "BattlEye"),
    ("beclient", "BattlEye"),
    ("battleye", "BattlEye"),
    ("easyanticheat", "Easy Anti-Cheat"),
    ("eac_launcher", "Easy Anti-Cheat"),
    # Riot Vanguard installs under Program Files, never into a game folder;
    # only its own two files are evidence, not the word.
    ("vgk.sys", "Riot Vanguard"),
    ("vgc.exe", "Riot Vanguard"),
    ("gameguard", "nProtect GameGuard"),
    ("xigncode", "XIGNCODE3"),
    # Denuvo alone is the copy protection most single-player games carry;
    # only its anti-cheat product is anti-cheat.
    ("denuvo-anti-cheat", "Denuvo Anti-Cheat"),
    ("denuvoanticheat", "Denuvo Anti-Cheat"),
    ("punkbuster", "PunkBuster"),
    ("faceit", "FACEIT AC"),
    ("ricochet", "Ricochet"),
    # EA's own anti-cheat (EA SPORTS FC, Battlefield): the service launcher
    # sits beside the game.
    ("eaanticheat", "EA Javelin"),
    ("ea_anticheat", "EA Javelin"),
    # HoYoverse titles: the driver installs system-wide, so the executable
    # name is the evidence. Online games; the anti-cheat closes the game
    # the moment a proxy DLL is seen (#29).
    ("zenlesszonezero", "HoYoverse anti-cheat"),
    ("genshinimpact", "HoYoverse anti-cheat"),
    ("starrail", "HoYoverse anti-cheat"),
    ("mhyprot", "HoYoverse anti-cheat"),
    # Elytra: a kernel anti-cheat that new Unreal shooters ship as a folder
    # of its own beside the game, with its installer in it (WARDOGS). No
    # file next to the executable, so the folder name is the evidence.
    ("elytra", "Elytra Anti-Cheat"),
    ("ace-base", "ACE (Anti-Cheat Expert)"),
    ("anticheatexpert", "ACE (Anti-Cheat Expert)"),
)


_PATTERNS = tuple(
    (re.compile(r"(?<![a-z0-9])" + re.escape(frag) + r"\d*(?![a-z0-9])"), product)
    for frag, product in MARKERS)


def match(name: str) -> str | None:
    """The product a file or folder name belongs to, or None."""
    low = name.lower()
    for pat, product in _PATTERNS:
        if pat.search(low):
            return product
    return None


@dataclass
class Finding:
    products: list[str]
    evidence: list[str]

    @property
    def present(self) -> bool:
        return bool(self.products)

    @property
    def summary(self) -> str:
        return ", ".join(sorted(set(self.products)))

    @property
    def found(self) -> str:
        """The files the warning rests on, for the person to check."""
        return "found: " + ", ".join(self.evidence) if self.evidence else ""


def detect(install_dir: Path, folder: Path) -> Finding:
    """Look for anti-cheat in and just below the game folder."""
    products: list[str] = []
    evidence: list[str] = []

    def look(p: Path) -> None:
        product = match(p.name)
        if product:
            products.append(product)
            evidence.append(p.name)

    for d in {install_dir, folder}:
        if not d.is_dir():
            continue
        try:
            for entry in d.iterdir():
                look(entry)
                # Anti-cheat usually lives one level down in its own folder
                if entry.is_dir() and not entry.name.startswith("."):
                    try:
                        for sub in list(entry.iterdir())[:80]:
                            look(sub)
                    except OSError:
                        continue
        except OSError:
            continue

    return Finding(products=sorted(set(products)), evidence=sorted(set(evidence))[:4])


WARNING = (
    "{product} is installed with this game.\n\n"
    "Anti-cheat and ReShade add-ons do not coexist. Expect one of: the game "
    "refuses to start, ReShade is blocked so nothing happens at all, or your "
    "account is banned. This is not something the tool can work around - it is "
    "the anti-cheat doing its job.\n\n"
    "If you play this game online, do not install here."
)


# Replacing a runtime the game ships is not the same act as injecting a
# DLL, and it carries its own risk: a launcher that verifies its files puts
# the old one back (harmless), and an online game's anti-cheat may treat a
# changed file as tampering (not harmless). Said before the swap, not after.
SWAP_WARNING = (
    "Swapping {name} replaces a file the game itself ships.\n\n"
    "The one that is there is backed up and comes back when you uninstall, "
    "so nothing is lost. Two things to know before you do it: a launcher "
    "that verifies its files will simply put its own copy back, and in an "
    "online game an anti-cheat can treat a changed file as tampering.\n\n"
    "For a single-player game this is the usual way to move off an old "
    "build. For anything you play online, keep the game's own file."
)


def swap_message(name: str) -> str:
    """What to say before replacing a runtime the game shipped."""
    return SWAP_WARNING.format(name=name)


def message(f: Finding) -> str:
    return WARNING.format(product=f.summary) + (f"\n\n({f.found})" if f.found else "")
