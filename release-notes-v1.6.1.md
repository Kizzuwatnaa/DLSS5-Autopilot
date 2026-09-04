## A day of bug reports, fixed

v1.6.0 went out yesterday and four reports came in by this afternoon. Every
one of them is in here. Thank you to the people who filed them - two of
these would have been invisible without your logs.

### DirectX 9 games: "ReShade's (vulkan layer) is missing" was wrong, and hid the real bug

Since 1.6.0 every DirectX 9 game renders through DXVK, so ReShade reaches it
as a **Vulkan layer** - a registry entry, not a file. "did it work?" kept
looking for a *file* called `(vulkan layer)`, never found one, and told
everybody their antivirus had quarantined it. That message is gone.

Underneath it was the actual problem (#10, and Bayonetta in #2): if
ReShade's own installer had ever registered its Vulkan layer on the PC, the
tool said "already registered, reusing it" and stopped - but ReShade's
installer only registers the **64-bit** layer. A 32-bit game (GTA IV,
Bayonetta and most DirectX 9 titles) cannot load that. The game ran on
Vulkan, ReShade was never in it, and there were no logs to say why.

Now the tool checks for a layer **the game's architecture can load**, adds
the missing 32-bit one beside ReShade's own registration when it has to,
and ignores a registration that is present but disabled. The diagnosis
reads the registry and says exactly which layer is missing; the bug report
shows the layer state, whether DXVK is still in the folder, and looks for
`nvngx_dlssnr.dll` in `host64\` where the 32-bit route actually keeps it -
it was reported "MISSING" on every healthy 32-bit install.

If you are one of the people with this problem: **install again** with
1.6.1. Nothing to remove first.

### Downloads through an antivirus or VPN (#7)

`SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC` in the middle of a download is
something sitting inside the TLS connection - HTTPS scanning in an
antivirus, a VPN, a proxy. It used to fail three times in a row instantly
and show the raw traceback. Now it backs off between attempts, resumes the
partial file, and if it still cannot get through says what to turn off.

### The scan no longer gets stuck in `C:\XboxGames` (#8)

The executable search has a hard budget of folders and seconds per game
directory, so a tree with thousands of empty directories and no `.exe`
cannot stall the scan any more. The Xbox app's `GameSave` and
`Minecraft Launcher` folders are skipped outright - neither is a game.

### Red Dead Redemption 2 is not a DirectX 9 game (#12)

`RDR2.exe` imports `d3d9.dll` and nothing from DXGI, so it was labelled
DX9 and offered nothing it could use. A game that ships its own
`nvngx_dlss.dll` (or a D3D12 Agility SDK) cannot be DirectX 9; the label is
now DX12 with the reason spelled out, and the routes that need it appear.
(The second half of that report - the add-on missing NGX modules that load
late - is in neural-upstream itself, not here.)

### Also

- `dxvk.py` carried two definitions of `is_dxvk`; the dead one is gone.
- Version 1.6.1; `test_all.py` sections 33-35 cover the above.
