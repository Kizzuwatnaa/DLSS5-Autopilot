## RTX Remix + DLSS 5

A game with an **RTX Remix** mod now gets DLSS 5 inside the Remix runtime,
where it belongs: after Remix's own upscaler, with no ReShade, no feeder and
no add-on anywhere in the folder. Install the mod yourself as you always
would, press rescan, and the game arrives with the **remix** route already
chosen. Tested on GTA IV with xoxor4d's compatibility mod: path tracing and
DLSS 5 neural rendering running together.

The install does at most three things - the matching `nvngx_dlssnr.dll` into
`.trex`, an optional (and experimental) runtime swap for mods whose runtime
has no DLSS 5, and one line in `rtx.conf`. Uninstall reverses exactly those.
The mod's assets and runtime are never touched.

**Which games have a mod** is a card on the first page: every project the
tool knows about, with the ones in your own library marked, and a link to
each. Portal with RTX, Portal Prelude RTX and the Half-Life 2 RTX demo are
free and ship with Remix already inside.

### Two of the mods install themselves

Where a project publishes a *complete* install as a plain `.zip` on its own
GitHub releases - the renderer included - the card offers **download &
install** beside that game, with a percentage as it goes, into the folder
the scan already found. That is **GTA IV** and **NFS Underground 2**. The
file comes from the author's own release page, never a mirror, and a record
is kept so it can be taken out again.

Everything else stays a link, deliberately. Most Remix projects publish a
small proxy whose own instructions then ask for NVIDIA's runtime and a
manual rename; installing that alone would leave the game loading a
`d3d9.dll` with nothing behind it. The tool reads the archive first and
refuses rather than guess - and it never writes over a Remix mod that is
already in the folder.

## RE Engine games (Capcom) no longer just crash and get blamed on setup

Resident Evil 2/3/4, RE7, RE8/Village and Resident Evil Requiem share a
documented problem: ReShade's add-on support - what every route here needs -
can crash the engine outright, worst on Requiem, which also carries Denuvo.
The tool recognises the engine (`re_chunk_000.pak`) and now installs
**REFramework** first, automatically, on any route: a separate mod that
loads before the game's own tamper checks and patches around them, the fix
players report using with ReShade on Requiem itself. It fetches the current
build from praydog/REFramework-nightly, which detects the running game on
its own rather than shipping one file per title. Not guaranteed on every
title or every game update, and never touches a Remix game.

## dgVoodoo2 is gone; DirectX 9 goes through DXVK

DirectX 9 used to be translated to D3D11 by dgVoodoo2. That path carried a
long tail of per-game tuning - VRAM, the captured mouse cursor, a fullscreen
mode that had to agree with the game's own - and it was the least reliable
thing here. It has been removed entirely.

DirectX 9 now takes **DXVK** (D3D9 to Vulkan), the same translation the
D3D11 games that quit on ReShade already used, and it is no longer optional:
the feed needs a D3D11/D3D12 device to build its contract on, and ReShade on
a raw D3D9 device cannot give it one. Proven on Bayonetta - 32-bit, DirectX
9 - where DLSS 5 built at 1920x1080 and delivered frames through the Vulkan
transport.

An install made by an older release still uninstalls completely: dgVoodoo's
files stay on the cleanup list.

## Play a window a few pixels short and nothing happens - now it says so

The one that cost the most to find, on Bayonetta (32-bit, DirectX 9). The
feed builds at whatever size the swap chain reports, and in a **bordered
window that is the client area**: 1920x1071 instead of 1920x1080. The neural
result then never lands on the screen - while every log says success,
`feature ready`, tens of thousands of frames evaluated. No setting changes
anything, because nothing is wrong with the settings.

Three builds at 1920x1071 did nothing; the first build at a true 1920x1080
worked immediately. **"did it work?" now detects exactly this** and says to
use borderless or true fullscreen at the display's own resolution, and the
instructions after an install say it up front.

Two more things it learned from the same session:

- **A minimized game.** Alt-tab out of an exclusive-fullscreen game and
  Windows reports a 160x28 client area, so the swap chain comes back that
  size and every DLSS create against it fails until the window is back.
- **A Vulkan game is no longer called dead.** The "closed before it drew a
  single frame" check only knew the DXGI spelling of a swap chain, so every
  DXVK and native-Vulkan session tripped it, right beside "frames are being
  processed".

The instructions after an install also never said where the DLSS 5 panel is
on a 32-bit game. It is the page in the game's own ReShade overlay - F6
toggles it - and the 64-bit helper's separate window is not somewhere to
alt-tab to while playing: that is what tears the feature down.

## Uninstall no longer leaves the translation layer behind

Installing twice backed up our **own** DXVK as though it were the game's
file. Uninstall then restored that backup and the game was left rendering
through DXVK for good, with nothing on disk admitting it. DXVK and
REFramework now recognise their own binary by content and skip that backup,
so an uninstall really does empty the folder.

## A D3D12 game no longer reads as D3D11

Some D3D12 titles only statically import `d3d11.dll`, loading the real
D3D12 runtime later through the Agility SDK (`D3D12\D3D12Core.dll`) or
carrying DLSS Frame Generation/Ray Reconstruction, both DX12-only. Found on
Resident Evil Requiem, which this mislabelling was steering onto the
`bridge` route instead of the better-fitting `optiscaler`/`native`. Detected
now and labelled correctly.

## Smaller fixes

- Uninstalling a game with no install record could delete a `dxgi.dll`,
  `opengl32.dll` or `D3D9.dll` that belonged to something else entirely -
  SpecialK, an ENB, a separately installed ReShade, or an RTX Remix runtime.
  All three are now removed only after the file is confirmed to be ours.
- A game on a drive that goes unready mid-session (unplugged, asleep, a
  dropped network share) crashed "what will happen?" and install with a raw
  Windows error instead of saying the folder is not there.
- The before/after screenshot window never got the dark title bar the rest of
  the app has, and the title bar was only ever set before a window had
  finished opening - it retries now.
