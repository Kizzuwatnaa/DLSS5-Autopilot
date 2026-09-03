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
