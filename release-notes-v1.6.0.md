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

## Two ways a Remix mod could have been damaged, closed off

- Uninstalling a game with no install record deleted files by name, and
  `D3D9.dll` was on that list. In a Remix game that file is the runtime.
- Any other route on a Remix game would have written dgVoodoo's `D3D9.dll`
  over the runtime, plus a ReShade DLL that crashes a Remix game at start.

Both are refused now, before anything is written, with the reason said out
loud. A test section proves a Remix install comes out of an install and an
uninstall byte for byte identical.

## RE Engine games (Capcom) are called out

Resident Evil 2/3/4, RE7, RE8/Village and Resident Evil Requiem share a
documented problem: ReShade's add-on support - what every route here needs -
can crash the engine outright, worst on Requiem, which also carries Denuvo.
The tool now recognises the engine (`re_chunk_000.pak`) and says so plainly
before you install, rather than letting it look like a setup mistake. A new
**dinput8.dll** option under `reshade proxy` is the community's partial
workaround - it loads earlier than the usual `dxgi.dll`.

## A D3D12 game no longer reads as D3D11

Some D3D12 titles only statically import `d3d11.dll`, loading the real
D3D12 runtime later through the Agility SDK (`D3D12\D3D12Core.dll`) or
carrying DLSS Frame Generation/Ray Reconstruction, both DX12-only. Found on
Resident Evil Requiem, which this mislabelling was steering onto the
`bridge` route instead of the better-fitting `optiscaler`/`native`. Detected
now and labelled correctly.

## Smaller fixes from testing against real games

- The RTX Remix game list had one wrong Steam page (Portal: Prelude RTX was
  pointing at the wrong game entirely). Every link in the list was checked.
- The "which of my games have a Remix mod?" window used to grab the mouse
  wheel for the whole app the moment it opened, and never let go - it broke
  scrolling in the main window's own log, and once the Remix window was
  closed, every further scroll anywhere in the app raised an error. Scoped
  to the window it belongs to now.
- The before/after screenshot window never got the dark title bar the rest
  of the app has; it does now, and the title bar code retries once a window
  actually finishes opening instead of only trying too early.
- "What will happen?" could describe a normal install (dgVoodoo, ReShade...)
  for a Remix game if a different route was picked by hand, even though
  pressing install would have refused it - the preview now gives the same
  refusal up front.
- Uninstalling with no install record could delete a `dxgi.dll` or
  `opengl32.dll` that belonged to something else entirely (SpecialK, an ENB,
  a separately installed ReShade), the way `D3D9.dll` already was protected
  against - both are now only removed after confirming the file really is
  this tool's ReShade.
- A folder on a drive that goes unready mid-session (unplugged, asleep, a
  dropped network share) could crash "what will happen?" or install instead
  of showing "does not exist" - fixed.
