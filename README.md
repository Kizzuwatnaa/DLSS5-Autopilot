# DLSS 5 Kurulum Aracı

DLSS5-Feeder kurulumunun bütün adımlarını tek tıkla halleden Windows aracı.
Oyunlarını tarar, mimarisini kendi tespit eder, gereken her şeyi indirir ve
ReShade ayarlarını doğru sırayla yazar.

**[→ Son sürümü indir](../../releases/latest)** — `dlss5kur.exe`, tek dosya, kurulum gerektirmez.

---

## Ne destekliyor

| | Destek | Yol |
|---|---|---|
| **64-bit DX11 / DX12** | ✅ | ReShade `dxgi.dll` olarak kurulur |
| **64-bit OpenGL** | ✅ | ReShade `opengl32.dll` olarak kurulur |
| **32-bit DX11 / DX12** | ✅ beta | + `host64\` yardımcı süreç |
| **32-bit OpenGL** | ✅ beta | + `host64\` yardımcı süreç |
| **DirectX 9** | ✅ beta | dgVoodoo2 ile DX9 → D3D11, sonrası 32-bit yol |
| **Emülatörler** | ✅ | DuckStation, PCSX2, Dolphin, PPSSPP, Xenia |
| **Vulkan** | ❌ | ReShade'in Vulkan katmanı sistem geneline kaydedilmeli, elle yapılmalı |

Steam, Epic ve GOG kütüphaneleri otomatik taranır. Listede olmayan bir şey için
"Klasör seç…" var.

---

## Nasıl kullanılır

1. `dlss5kur.exe`'yi çalıştır
2. **Adım 1** — mimari seç (bilmiyorsan "Hepsini göster")
3. **Adım 2** — oyununu listeden seç
4. **Adım 3** — **KUR**

Oyunda:

- **Home** → ReShade paneli
- `LUMENITE: Kernel 2.0` ve `DLSS 5 Feed` işaretli olmalı — **Kernel üstte**
- `DLSS 5 Neural Rendering` panelinden neural rendering'i aç
- Oyunun kendi **MSAA/SSAA** ayarını kapat

### Komut satırı

```
dlss5kur.exe "D:\Oyunlar\Oyun"            kur
dlss5kur.exe "D:\Oyunlar\Oyun" --kontrol  sadece tespit et, hiçbir şey yazma
dlss5kur.exe "D:\Oyunlar\Oyun" --kaldir   kaldır
```

---

## Ne indiriyor

Kurulum sırasında her şey otomatik iner — elle dosya indirmene gerek yok.

| Bileşen | Kaynak |
|---|---|
| ReShade (Addon sürümü) | reshade.me |
| Shader başlıkları | crosire/reshade-shaders |
| DLSS5-Feeder | jlrouzies-fr/DLSS5-Feeder |
| LumeniteFX (hareket vektörleri) | umar-afzaal/LumeniteFX |
| renodx-dlss5, nvngx_dlssnr, nvngx_dlss | RankFTW/rhi-repo |
| dgVoodoo2 (yalnızca DX9) | dege-diosg/dgVoodoo2 |

İndirilenler `%LOCALAPPDATA%\dlss5kur\cache` altında saklanır — ilk kurulum
~150 MB, sonraki oyunlar anında.

Araç **yalnızca** şu alan adlarına bağlanır: `reshade.me`,
`raw.githubusercontent.com`, `api.github.com`, `github.com`,
`objects.githubusercontent.com`, `codeload.github.com`.

### Kendi renodx dosyanı kullanmak

Discord'dan daha yeni bir `renodx-*.addon64` indirdiysen:

- exe'nin yanındaki `renodx\` klasörüne koy, **veya**
- İndirilenler/Masaüstü'ne koy — araç kendisi bulur, **veya**
- Adım 3'te "Kendi dosyam…" ile seç (seçim kalıcı olarak hatırlanır)

Yerel dosya bulunursa aynadaki sürüme tercih edilir.

---

## Ekran kartı uyumluluğu

Sızdırılan `nvngx_dlssnr.dll`'in içindeki CUDA kodu belirli mimariler için
derlenmiş. Araç kartını tespit edip **indirdiği dosyanın içinde senin kartın
için gerçekten kod var mı** diye denetler; yoksa kurulumu durdurur.

Ölçülen durum (fatbin kayıtları ayrıştırılarak):

| Sürüm | RTX 20 | RTX 30 | RTX 40 | RTX 50 |
|---|:---:|:---:|:---:|:---:|
| `310.8.0` | – | – | – | ✓ |
| `310.8.0-RTX40` | – | – | ✓ | ✓ |
| `310.8.SF` | ✓ | ✓ | ✓ | ✓ |
| `310.8.SF-v2` | ✓ | ✓ | ✓ | ✓ |

---

## Ayarlar

Adım 3'teki **Kalite / hız** bölümü `dlss5-feed.cfg`'yi yazar:

- **İşleme alanı** (`work_resolution`, %50–100) — performans düğmesi.
  4K'da fps düşerse %70–80 dene.
- **DLSS preset** — alevlerin/saydam nesnelerin etrafında bozulma görürsen
  Preset E veya F (eski CNN).
- **HDR** — otomatik / SDR zorla / HDR zorla

### "DLSS Performance modu" neden yok

Feeder yolu **her zaman DLAA**'dır, mimari olarak başka türlü olamaz:
DLSS5-Feeder oyunun düşük çözünürlüklü render'ını görmez, ReShade zincirinin
sonundaki bitmiş tam çözünürlüklü kareyi görür. Upscale edilecek düşük
çözünürlüklü bir kaynak yoktur. Performans için doğru düğme
`work_resolution`'dır.

---

## Dikkat

- **Online oyunlarda kullanma.** Add-on'lu ReShade anti-cheat'e takılır.
- **Exclusive fullscreen yerine borderless** kullan — alt-tab'da çökme olabiliyor.
- **DLSS 5 add-on'unun kendisi sızdırılmış, kapalı kaynak NVIDIA yazılımıdır.**
  Lisansı yoktur, bu depo onu barındırmaz; araç çalışma anında topluluk
  aynasından indirir. Kendi riskinle kullan.
- Emülatörlerde render arka ucu **Direct3D 11/12** olmalı; Vulkan/OpenGL
  seçiliyse ReShade devreye girmez.

---

## Sorun giderme

Oyun klasöründeki `dlss5-feed.log`:

- `feature ready … DLAA` → sözleşme kuruldu
- `frame N delivered` → kareler işleniyor
- `MV probe … %N non-zero` → hareket varken %0 olmamalı
- `CreateFeature raised exception` → renodx ile nvngx_dlssnr sürümleri
  uyuşmuyor; daha yeni bir renodx dene

Kurulum bozulursa: araç yazdığı her dosyayı `dlss5kur-kurulum.json` içinde
tutar. "Kurulumu kaldır" sadece o listedekileri siler, oyunun kendi
dosyalarına dokunmaz.

---

## Kaynaktan derleme

```
derle.bat
```

Python 3.10+ ve `pip install pyinstaller` yeterli.

### Testler

```
python _test_ini.py        ReShade ini/preset mantığı (teknik sırası dahil)
python _test_kurulum.py    uçtan uca kurulum + kaldırma (geçici klasörlerde)
```

### Dosyalar

```
dlss5kur.py           giriş noktası (GUI + komut satırı)
core/pe.py            PE okuma: mimari, import tablosu, API tespiti, exe bulma
core/games.py         Steam / Epic / GOG / emülatör taraması
core/emulators.py     emülatör profilleri ve arama
core/gpu.py           ekran kartı tespiti + CUDA mimari uyumluluk denetimi
core/sources.py       bütün indirme adresleri — tek yerde
core/net.py           indirme, önbellek, zip açma
core/prefs.py         kalıcı tercihler, yerel renodx bulma
core/reshade_ini.py   ReShade.ini / preset, teknik sıralaması
core/feedcfg.py       dlss5-feed.cfg
core/dgvoodoo.py      DX9 → D3D11 (dgVoodoo2)
core/installer.py     kurulum motoru
core/gui.py           arayüz
```

## Lisans

Aracın kendi kodu MIT. İndirdiği bileşenler kendi lisanslarına tabidir
(ReShade BSD-3, LumeniteFX AGNYA, dgVoodoo2 kendi şartları, NVIDIA
çalışma zamanları tescilli).
