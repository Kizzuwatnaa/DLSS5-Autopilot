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
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# (marker fragment, product). Matched case-insensitively against file and
# directory names in and around the install folder.
MARKERS: tuple[tuple[str, str], ...] = (
    ("beservice", "BattlEye"),
    ("beclient", "BattlEye"),
    ("battleye", "BattlEye"),
    ("easyanticheat", "Easy Anti-Cheat"),
    ("eac_launcher", "Easy Anti-Cheat"),
    ("vgk.sys", "Riot Vanguard"),
    ("vanguard", "Riot Vanguard"),
    ("gameguard", "nProtect GameGuard"),
    ("xigncode", "XIGNCODE3"),
    ("denuvo", "Denuvo Anti-Cheat"),
    ("punkbuster", "PunkBuster"),
    ("faceit", "FACEIT AC"),
    ("ricochet", "Ricochet"),
)


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


def detect(install_dir: Path, folder: Path) -> Finding:
    """Look for anti-cheat in and just below the game folder."""
    products: list[str] = []
    evidence: list[str] = []

    def look(p: Path) -> None:
        low = p.name.lower()
        for frag, product in MARKERS:
            if frag in low:
                products.append(product)
                evidence.append(p.name)
                return

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


def message(f: Finding) -> str:
    return WARNING.format(product=f.summary)
