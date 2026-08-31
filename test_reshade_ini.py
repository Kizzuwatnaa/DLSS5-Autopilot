import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from core import reshade_ini as R

BS = chr(92)          # single backslash
d = Path(tempfile.mkdtemp())

# 1) Sifirdan kurulum
R.write_reshade_ini(d, 3)
R.write_preset(d, 3)
ini = R.Ini.load(d / "ReShade.ini")
pre = R.Ini.load(d / "ReShadePreset.ini")
assert ini.get("GENERAL", "EffectSearchPaths") == r".\reshade-shaders\Shaders\**"
assert ini.get("GENERAL", "TextureSearchPaths") == r".\reshade-shaders\Textures\**"
assert ini.get("GENERAL", "PreprocessorDefinitions") == "DLSS5_MV_PROVIDER=3"
assert ini.get("ADDON", "AddonPath") == BS.join([".", ""]), repr(ini.get("ADDON", "AddonPath"))
techs = R.split_list(pre.get("", "Techniques"))
assert techs == ["Lumenite_Kernel@lumenite_Kernel.fx", "DLSS5_Feed@DLSS5_Feed.fx"], techs
print("1) fresh install OK ->", techs)
print("   AddonPath =", repr(ini.get("ADDON", "AddonPath")))

# 2) Kullanicinin mevcut ayarlari korunuyor mu + saglayici hep ustte mi
(d / "ReShade.ini").write_text(
    "[GENERAL]\n"
    "EffectSearchPaths=." + BS + "ozel" + BS + "**\n"
    "PreprocessorDefinitions=FOO=1,DLSS5_MV_PROVIDER=9\n"
    "[INPUT]\nKeyOverlay=36,0,0,0\n", encoding="utf8")
(d / "ReShadePreset.ini").write_text(
    "Techniques=DLSS5_Feed@DLSS5_Feed.fx,Clarity@Clarity.fx\n"
    "TechniqueSorting=Clarity@Clarity.fx,DLSS5_Feed@DLSS5_Feed.fx\n"
    "[Clarity.fx]\nStrength=0.5\n", encoding="utf8")
R.write_reshade_ini(d, 3)
R.write_preset(d, 3)
ini = R.Ini.load(d / "ReShade.ini")
pre = R.Ini.load(d / "ReShadePreset.ini")
assert ini.get("GENERAL", "EffectSearchPaths") == "." + BS + "ozel" + BS + "**"   # untouched
assert ini.get("INPUT", "KeyOverlay") == "36,0,0,0"                              # preserved
assert R.split_list(ini.get("GENERAL", "PreprocessorDefinitions")) == ["FOO=1", "DLSS5_MV_PROVIDER=3"]
techs = R.split_list(pre.get("", "Techniques"))
assert techs[0] == "Lumenite_Kernel@lumenite_Kernel.fx", techs   # PROVIDER ON TOP
assert techs[1] == "DLSS5_Feed@DLSS5_Feed.fx", techs
assert "Clarity@Clarity.fx" in techs                             # the user's own is still there
sort = R.split_list(pre.get("", "TechniqueSorting"))
assert sort[:2] == ["Lumenite_Kernel@lumenite_Kernel.fx", "DLSS5_Feed@DLSS5_Feed.fx"], sort
assert pre.get("Clarity.fx", "Strength") == "0.5"
print("2) existing settings preserved + provider on top OK ->", techs)

# 3) Virgul kacirma
raw = R.join_list(["A=1", "B=x,y", "C"])
assert raw == "A=1,B=x,,y,C" and R.split_list(raw) == ["A=1", "B=x,y", "C"]
print("3) comma escaping OK ->", raw)

# 4) Kaldirma bizimkileri siliyor, kullanicininkine dokunmuyor
R.remove_our_techniques(d)
techs = R.split_list(R.Ini.load(d / "ReShadePreset.ini").get("", "Techniques"))
assert techs == ["Clarity@Clarity.fx"], techs
print("4) removal OK ->", techs)

# 5) QuantMotion saglayicisi
R.write_preset(d, 4)
techs = R.split_list(R.Ini.load(d / "ReShadePreset.ini").get("", "Techniques"))
assert techs[0] == "Lumenite_QuantMotion@lumenite_QuantMotion.fx", techs
print("5) provider 4 OK ->", techs)

# 6) Saglayici 0 (kullanici kendi shaderini kurar) -> sadece feed eklenir
R.remove_our_techniques(d)
R.write_preset(d, 0)
techs = R.split_list(R.Ini.load(d / "ReShadePreset.ini").get("", "Techniques"))
assert techs[0] == "DLSS5_Feed@DLSS5_Feed.fx", techs
print("6) provider 0 OK ->", techs)

print()
print("ALL INI TESTS PASSED")
