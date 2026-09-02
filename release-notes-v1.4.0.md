## Video and YouTube through DLSS 5

A new card on the first page sets up a portable video player (MPC-HC) in a
folder of your choice and feeds DLSS 5 into it, the same way it does for a
game. Play any file, or **File > Open URL** with a YouTube link: yt-dlp sits
next to the player, so the stream plays live and nothing is downloaded.
**F6** switches neural rendering on and off while it plays; the tool has a
button for it too. Tested at 60 fps with the feed costing about 5% of the
frame. There is no depth buffer in a video and the feed does not need one.

Paste a link into the **link** box and press **play**; a link on the
clipboard is picked up by itself. **download, then play** saves it first
(up to 1440p, or 4K when ticked; the first download fetches ffmpeg once,
170 MB, because YouTube only serves video and audio apart).

Command line: `dlss5-autopilot.exe --video ["D:\DLSS5 Player"]`.

## Profiles

Save the settings you liked as a named profile and pick it on any other
game. **Quality / Balanced / Performance** are built in.

## What will happen?

A button on the install page lists what INSTALL would write, back up,
clean up and whether anything goes outside the game folder - without
downloading or writing a thing.

## Before / after

A side-by-side viewer for the last two ReShade screenshots (toggle with
F6, shoot, toggle, shoot), with a PNG export.

## Emulators switch themselves to Direct3D

DuckStation, PCSX2, Dolphin, PPSSPP and Xenia get their render backend set
to D3D11/D3D12 by the install (the config is backed up and restored on
uninstall). Emulators with no DXGI backend say so instead.

## Xbox / Game Pass

Games under `C:\XboxGames` that Windows keeps unreadable are listed with
the fix (Xbox app > Manage > Files > Enable mods) instead of failing on
every scan.

## Feeder version list

The pre-release tick box became a list: stable, newest pre-release, or any
exact release when the newest one breaks a game.

## The diagnosis reads the folder, not just the logs

"Not run yet, or ReShade never loaded" used to be the answer to everything.
It now says which it is: ReShade's DLL gone (antivirus), an add-on
quarantined, the game not started since the install, or a log older than
the install. Shader failures that the feed does not use are no longer
reported as failures, and a flat depth buffer is explained instead of
implied.

## Old d3dcompiler_47.dll no longer fakes a working install

Some games (and MPC-HC) ship a `d3dcompiler_47.dll` too old for the neural
pass. The feed kept reporting frames while neural rendering silently did
nothing. The installer now moves that file aside (`.dlss5-off`), Windows
uses its own, and uninstall puts it back. The diagnosis names it when it
sees one in an existing install.

## Bug reports carry what matters

**Report a bug** now includes which files are in the folder, the tails of
`ReShade.log` and `dlss5-feed.log`, and the diagnosis - not just the tool's
own scan log.
