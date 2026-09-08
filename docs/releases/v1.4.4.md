## Your own LumeniteFX is not duplicated

If `lumenite_Kernel.fx` is already anywhere under `reshade-shaders` (you
installed LumeniteFX yourself), the feeder install uses your copy instead of
dropping a second one next to it - two copies of the same technique was a
red error in ReShade's overlay. The preview says so too. A copy an earlier
install of this tool wrote is still refreshed as before. Requested in
issue #4.
