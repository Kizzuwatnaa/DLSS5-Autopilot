r"""ReShade .ini okuma/yazma.

ReShade coklu degerleri virgulle ayirir ve gercek bir virgulu ",," olarak
kacirir. Anahtar adlari crosire/reshade kaynagindaki runtime.cpp ile ayni:
  ReShade.ini [GENERAL] : EffectSearchPaths, TextureSearchPaths,
                          PreprocessorDefinitions, PresetPath
  ReShade.ini [ADDON]   : AddonPath
  Preset koku (bolumsuz): Techniques, TechniqueSorting, PreprocessorDefinitions
Teknik girdileri "TeknikAdi@Dosya.fx" bicimindedir.

ONEMLI: hareket vektoru saglayicisinin teknigi DLSS5_Feed'in USTUNDE olmak
zorunda, yoksa besleme calismaz.
"""
from __future__ import annotations

from pathlib import Path

# Saglayici numarasi -> (etiket, teknik girdisi ya da None, gereken shader dosyalari)
PROVIDERS = {
    3: ("LumeniteFX Kernel 2.0 (önerilen)", "Lumenite_Kernel@lumenite_Kernel.fx", True),
    4: ("LumeniteFX QuantMotion", "Lumenite_QuantMotion@lumenite_QuantMotion.fx", True),
    0: ("Genel texMotionVectors (qUINT vb. - kendin kurmalısın)", None, False),
    1: ("iMMERSE Launchpad (kendin kurmalısın)", None, False),
    2: ("VORT (kendin kurmalısın)", None, False),
}

FEED_TECHNIQUE = "DLSS5_Feed@DLSS5_Feed.fx"


class Ini:
    """Sirali bolumler; ilki her zaman kok ("")."""

    def __init__(self) -> None:
        self.sections: list[tuple[str, list[list[str]]]] = [("", [])]

    @classmethod
    def parse(cls, text: str) -> "Ini":
        ini = cls()
        cur = 0
        for line in text.splitlines():
            s = line.strip()
            if not s or s[0] in ";#":
                continue
            if s.startswith("[") and s.endswith("]"):
                cur = ini._index(s[1:-1])
                continue
            if "=" in s:
                k, v = s.split("=", 1)
                ini.sections[cur][1].append([k.strip(), v.strip()])
        return ini

    @classmethod
    def load(cls, path: Path) -> "Ini":
        try:
            return cls.parse(path.read_text(encoding="utf8", errors="replace"))
        except OSError:
            return cls()

    def _index(self, name: str) -> int:
        for i, (n, _) in enumerate(self.sections):
            if n.lower() == name.lower():
                return i
        self.sections.append((name, []))
        return len(self.sections) - 1

    def get(self, section: str, key: str) -> str | None:
        for n, kv in self.sections:
            if n.lower() == section.lower():
                for k, v in kv:
                    if k.lower() == key.lower():
                        return v
        return None

    def set(self, section: str, key: str, value: str) -> None:
        kv = self.sections[self._index(section)][1]
        for e in kv:
            if e[0].lower() == key.lower():
                e[1] = value
                return
        kv.append([key, value])

    def set_default(self, section: str, key: str, value: str) -> None:
        if self.get(section, key) is None:
            self.set(section, key, value)

    def dump(self) -> str:
        out: list[str] = []
        for name, kv in self.sections:
            if not kv and not name:
                continue
            if name:
                if out:
                    out.append("")
                out.append(f"[{name}]")
            out += [f"{k}={v}" for k, v in kv]
        return "\n".join(out) + "\n"

    def save(self, path: Path) -> None:
        path.write_text(self.dump(), encoding="utf8")


def split_list(raw: str) -> list[str]:
    """Tek virgulle bol; ",," kacirilmis virguldur."""
    items, cur, i = [], "", 0
    while i < len(raw):
        if raw[i] == ",":
            if i + 1 < len(raw) and raw[i + 1] == ",":
                cur += ","
                i += 2
                continue
            items.append(cur)
            cur = ""
        else:
            cur += raw[i]
        i += 1
    if cur:
        items.append(cur)
    return [s for s in items if s]


def join_list(items: list[str]) -> str:
    return ",".join(s.replace(",", ",,") for s in items)


def _ensure_define(raw: str, define: str) -> str:
    name = define.split("=", 1)[0]
    kept = [d for d in split_list(raw) if d.split("=", 1)[0] != name]
    kept.append(define)
    return join_list(kept)


def write_reshade_ini(game_dir: Path, provider: int = 3) -> None:
    """ReShade.ini olustur/guncelle. Kullanicinin mevcut ayarlarina dokunmaz."""
    p = game_dir / "ReShade.ini"
    ini = Ini.load(p)
    ini.set_default("GENERAL", "EffectSearchPaths", r".\reshade-shaders\Shaders\**")
    ini.set_default("GENERAL", "TextureSearchPaths", r".\reshade-shaders\Textures\**")
    ini.set_default("GENERAL", "PresetPath", r".\ReShadePreset.ini")
    ini.set("GENERAL", "PreprocessorDefinitions",
            _ensure_define(ini.get("GENERAL", "PreprocessorDefinitions") or "",
                           f"DLSS5_MV_PROVIDER={provider}"))
    # Eklentiler oyun exesinin yaninda; ReShade'e acikca soyluyoruz.
    ini.set_default("ADDON", "AddonPath", ".\\")
    ini.save(p)


def write_addon_only_ini(dir_: Path) -> None:
    r"""host64\ klasoru icin: sadece eklenti yukleme, shader yok."""
    p = dir_ / "ReShade.ini"
    ini = Ini.load(p)
    ini.set_default("ADDON", "AddonPath", ".\\")
    ini.save(p)


def write_preset(game_dir: Path, provider: int = 3) -> None:
    """Preset'te saglayici teknigini DLSS5_Feed'in USTUNE koyar."""
    p = game_dir / "ReShadePreset.ini"
    ini = Ini.load(p)
    tech = PROVIDERS.get(provider, (None, None, False))[1]
    ours = ([tech] if tech else []) + [FEED_TECHNIQUE]
    for key in ("Techniques", "TechniqueSorting"):
        if key == "TechniqueSorting" and ini.get("", key) is None:
            continue
        rest = [t for t in split_list(ini.get("", key) or "") if t not in ours]
        ini.set("", key, join_list(ours + rest))
    ini.set("", "PreprocessorDefinitions",
            _ensure_define(ini.get("", "PreprocessorDefinitions") or "",
                           f"DLSS5_MV_PROVIDER={provider}"))
    ini.save(p)


def remove_our_techniques(game_dir: Path) -> None:
    p = game_dir / "ReShadePreset.ini"
    if not p.is_file():
        return
    ours = {FEED_TECHNIQUE} | {v[1] for v in PROVIDERS.values() if v[1]}
    ini = Ini.load(p)
    for key in ("Techniques", "TechniqueSorting"):
        raw = ini.get("", key)
        if raw is not None:
            ini.set("", key, join_list([t for t in split_list(raw) if t not in ours]))
    ini.save(p)
