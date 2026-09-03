"""Games that already have an RTX Remix mod, and where to get it.

The tool does not install these. They are whole projects - path-traced
remasters with their own asset packs, sometimes several gigabytes, made by
different people under their own terms. What this tool does is add DLSS 5
to a Remix install once it is there: the `remix` route looks for a `.trex`
runtime folder and works with ANY of them, listed here or not.

So this list is a signpost, nothing more. Every entry was checked to exist
on 2026-09-03; a project can move or stop, and the URL is the authority,
not this file. Entries are deliberately short on promises: "a mod exists"
is not "it will run well on your machine".

`match()` is what makes it useful in the interface: the scanned library is
compared against these patterns, so someone sees "you own four games that
have a Remix mod" instead of a list of games they do not have.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RemixMod:
    game: str                       # the game, as people call it
    patterns: tuple                 # lowercase fragments matched against name/exe
    mod: str                        # the project's name
    url: str                        # where it lives
    note: str = ""                  # one honest sentence
    # True only where the project's own .zip release carries the whole thing,
    # renderer included (".trex/d3d9.dll" inside the archive), so it can be
    # fetched and dropped in with nothing left for the person to do. Checked
    # by reading the published archives on 2026-09-03. Most Remix projects
    # publish a small proxy instead and their own INSTALL file then asks for
    # NVIDIA's runtime and a manual rename - those stay a link.
    installable: bool = False


# Community mods, all with a public repository. Checked 2026-09-03.
MODS: tuple = (
    RemixMod("Grand Theft Auto IV", ("grand theft auto iv", "gtaiv", "gta 4"),
             "GTAIV RTX Remix Compatibility Mod (xoxor4d)",
             "https://github.com/xoxor4d/gta4-rtx",
             "the one this tool was tested against; its runtime already "
             "carries DLSS 5, so only the runtime file is needed",
             installable=True),
    RemixMod("Need for Speed: Underground 2", ("underground 2", "nfsu2", "speed2"),
             "NFSU2-RTX-Remix (Ekozmaster)",
             "https://github.com/Ekozmaster/NFSU2-RTX-Remix",
             installable=True),
    RemixMod("Garry's Mod", ("garry", "gmod"),
             "Garry's Mod RTX Remixed (Xenthio)",
             "https://github.com/Xenthio/garrys-mod-rtx-remixed",
             "needs the game in a fixed-function mode; read its own guide"),
    RemixMod("Deus Ex", ("deus ex",),
             "Deus Ex Echelon Renderer (onnoj)",
             "https://github.com/onnoj/DeusExEchelonRenderer",
             "a renderer that gives the game a fixed-function pipeline first"),
    RemixMod("Thief Gold", ("thief gold", "thief"),
             "thief-gold-rtx-remix (Night1099)",
             "https://github.com/Night1099/thief-gold-rtx-remix",
             "NewDark 1.27"),
    RemixMod("The Elder Scrolls III: Morrowind", ("morrowind", "openmw"),
             "Morrowind RTX Remix (BrunchyChineapple)",
             "https://github.com/BrunchyChineapple/Morrowind-RTX-Remix-source",
             "there is a separate set of loose files for OpenMW"),
    RemixMod("Vampire: The Masquerade - Bloodlines", ("bloodlines", "vampire"),
             "VTMB RTX Remix (CattoSalad)",
             "https://github.com/CattoSalad/VTMB-RTX-Remix",
             "a knowledge base rather than a one-click mod"),
    RemixMod("Prince of Persia: The Sands of Time", ("sands of time", "prince of persia"),
             "pop-sot-rtx (kaminoer)",
             "https://github.com/kaminoer/pop-sot-rtx"),
    RemixMod("Saints Row 2", ("saints row 2", "saintsrow2"),
             "sr2-rtx-remix-proxy (BRAGme)",
             "https://github.com/BRAGme/sr2-rtx-remix-proxy"),
    RemixMod("Saints Row: The Third", ("saints row the third", "saintsrowthethird"),
             "Saints Row The Third RTX Remix shim (PurrsianMilkman)",
             "https://github.com/PurrsianMilkman/Saints-Row-The-Third-RTX-REMIX-compatibility-mod",
             "the 2011 DirectX 9 release only"),
    RemixMod("Red Faction", ("red faction",),
             "RedFaction-RTX (BRAGme)",
             "https://github.com/BRAGme/RedFaction-RTX",
             "version 1.20 NA"),
    RemixMod("Total Overdose", ("total overdose",),
             "TotalOverDoseRTXRemix (Utkar5hM)",
             "https://github.com/Utkar5hM/TotalOverDoseRTXRemix"),
    RemixMod("Assassin's Creed II", ("assassin's creed ii", "assassinscreediigame",
                                     "assassin's creed 2"),
             "ac2-rtx (Kamzik123)",
             "https://github.com/Kamzik123/ac2-rtx",
             "later than the era Remix is built for; expect rough edges"),
    RemixMod("Populous: The Beginning", ("populous",),
             "Populous-3-RTX-Remix (xmarre)",
             "https://github.com/xmarre/Populous-3-RTX-Remix",
             "an experiment, in its author's words"),
    RemixMod("Silent Storm", ("silent storm",),
             "silent-storm-rtx (WormSlayer)",
             "https://github.com/WormSlayer/silent-storm-rtx"),
    RemixMod("Dungeon Keeper 2", ("dungeon keeper",),
             "dk2-dxwrapper with path tracing (mencelot)",
             "https://github.com/mencelot/dk2-dxwrapper-with-path-tracing-support"),
    RemixMod("Grand Theft Auto: Vice City", ("vice city",),
             "GTA Vice City RTX Remix ASI (GmanRO)",
             "https://github.com/GmanRO/GTA-VICE-CITY-RTX-REMIX-.ASI-compiled-within-linux-"),
    RemixMod("Cry of Fear", ("cry of fear",),
             "CryofFear_RTX-REMIX (michaelabilliot)",
             "https://github.com/michaelabilliot/CryofFear_RTX-REMIX"),
    RemixMod("Chess Titans", ("chess titans",),
             "Chess-Titans-RTX (Kamilkampfwagen-II)",
             "https://github.com/Kamilkampfwagen-II/Chess-Titans-RTX"),
)

# Finished games rather than mods: NVIDIA and Orbifold shipped these with
# Remix already inside, so there is nothing to install - they simply have a
# .trex folder from the start and the route works on them.
BUILT_IN: tuple = (
    RemixMod("Portal with RTX", ("portal with rtx", "portalrtx"),
             "official, free for owners of Portal",
             "https://store.steampowered.com/app/2012840/"),
    RemixMod("Portal: Prelude RTX", ("prelude rtx",),
             "official, free",
             "https://store.steampowered.com/app/2410180/"),
    RemixMod("Half-Life 2 RTX", ("half-life 2 rtx", "hl2rtx"),
             "official demo, free",
             "https://store.steampowered.com/app/2477290/"),
)

# Where the rest are. Not scraped, just pointed at: many more projects live
# on ModDB and in the Remix Showcase Discord, including ones with no public
# repository at all.
MORE = (
    ("Every Remix project, community list", "https://www.moddb.com/rtx"),
    ("NVIDIA's compatibility notes",
     "https://github.com/NVIDIAGameWorks/rtx-remix/wiki/Compatibility"),
)

RULE_OF_THUMB = ("RTX Remix only reaches DirectX 8 and 9 games with a fixed "
                 "function pipeline - roughly 2000 to 2005. Anything after "
                 "2010 will not work unless someone wrote a shim for it. "
                 "Each game needs its own mod; there is no universal one.")


def _haystack(name: str, exe: str, folder: str) -> str:
    return " ".join(x for x in (name, exe, folder) if x).lower().replace("_", " ")


def match(name: str = "", exe: str = "", folder: str = "") -> RemixMod | None:
    """The Remix project for this game, or None.

    Matching is on the game's name, its executable and its folder, because
    a Steam library folder is often the only place the real title appears.
    """
    hay = _haystack(name, exe, folder)
    if not hay.strip():
        return None
    for entry in BUILT_IN + MODS:
        for p in entry.patterns:
            if p in hay:
                return entry
    return None


def for_library(games) -> list:
    """[(game, RemixMod)] for the games in a scanned library that have one."""
    out = []
    for g in games:
        try:
            entry = match(getattr(g, "name", "") or "",
                          g.exe.name if getattr(g, "exe", None) else "",
                          str(getattr(g, "folder", "") or ""))
        except Exception:
            entry = None
        if entry is not None:
            out.append((g, entry))
    return out
