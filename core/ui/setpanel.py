"""The settings of one game, in place on its page.

Every row asks the controller whether it is shown for the route
(`shown_setting`) - the same rule Options is built with - so a setting is
on the page exactly when it does something. "auto" is drawn dim with what
it resolves to; only a setting the person changed is drawn in the accent.
Labels are the names the diagnosis and the docs use for these controls.
"""
from __future__ import annotations

from .. import dlss, optiscaler
from . import theme as T

FPS_CHOICES = [("", "off")] + [(str(v), f"{v} fps") for v in (30, 40, 45, 50, 60, 72, 90, 100, 120, 144, 165, 240)]

# key, label, kind, tip
GROUPS = (
    ("setup", (
        ("route", "route", "dd", "which way DLSS 5 gets into this game"),
        ("bits", "architecture", "dd", "this executable is protected and cannot be read - pick 64 or 32-bit"),
        ("exe", "target exe", "dd", "the executable the game really starts"),
        ("api", "graphics api", "dd", "what the game renders with - auto reads the executable"),
        ("opti_proxy", "loads as", "dd", "the name OptiScaler is loaded under; try another if nothing loads"),
        ("reshade_proxy", "reshade loads as", "dd", "the name ReShade is loaded under; try d3d11.dll if dxgi does nothing"),
        ("provider", "motion vectors", "dd", "where the feeder gets motion from"),
        ("renodx", "dlss5 add-on", "dd", "auto picks the newest build that works on this driver and route"),
        ("dlssnr", "nvngx_dlssnr", "dd", "the neural runtime - auto matches your card"),
        ("dlss", "nvngx_dlss", "dd", "the build that goes in where the game has none of its own beside the "
                                     "executable, or with 'keep the game's own nvngx_dlss' off"),
        ("dlssd", "ray reconstruction", "dd", "this game ships one; a swap is backed up and comes back on uninstall"),
        ("optibuild_row", "", "", ""),
    )),
    ("picture", (
        ("workres", "work area", "slider", ""),
        ("target_fps", "aim for", "dd", "after a session, 'did it work?' suggests the work area for this frame rate"),
        ("preset", "dlss preset", "dd", "the feeder path is always DLAA"),
        ("nr_preset", "model preset", "dd", "the rest of the model's dials are in OptiScaler's overlay"),
        ("nr_style", "style", "dd", ""),
        ("hdr", "hdr", "dd", ""),
        ("feeder", "feeder build", "dd", "pin a feeder build; the matching add-on is chosen for it"),
        ("opti_build", "optiscaler build", "dd", "which OptiScaler build goes in"),
    )),
    ("extras", (
        ("keep_dlss", "keep the game's own nvngx_dlss", "toggle",
         "leave the DLSS file the game ships alone - the safe choice for anything played online"),
        ("fg", "frame generation  (FSR 3.1, 2x)", "toggle", "D3D12; turn the game's own frame generation off"),
        ("mfg", "multi-frame generation 3x/4x  (RTX 40)", "toggle",
         "dashdogy's RTX40MFG-Unlock with the Ultimate ASI Loader - research software"),
        ("dxvk", "run through DXVK (Vulkan)", "toggle",
         "for games that close when ReShade loads inside them; DirectX 9 always goes through DXVK"),
        ("vr", "VR headset (OpenXR layer)", "toggle", "registers ReShade's OpenXR layer so the pass runs on what the "
                                                      "headset shows"),
        ("remix_swap", "swap the Remix runtime", "toggle",
         "only needed when this mod's runtime has no DLSS 5 pass; the mod's own comes back on uninstall"),
        ("overlay_key", "overlay key", "dd", "the key that opens the overlay in the game"),
    )),
)


class SettingsSection:
    def __init__(self, page):
        self.page = page

    def draw(self, x0, y, w, accent) -> int:
        page = self.page
        a, c, k = page.app, page.c, page.kit
        tags = ("page",)
        top = y
        c.create_text(x0, y + T.px(18), text="settings", font=T.mono(14, True), fill=T.TEXT, anchor="w", tags=tags)
        c.create_text(x0 + T.px(120), y + T.px(19), text="auto picks what fits this game and your card",
                      font=T.mono(9), fill=T.DIM, anchor="w", tags=tags)
        k.link(x0 + w, y + T.px(18), "close", page.toggle_settings, glyph="close", colour=T.MUTED, anchor="e",
               tags=tags)
        y += T.px(56)
        cols = 3 if w >= T.px(1060) else 2 if w >= T.px(680) else 1
        gap = T.px(32)
        colw = (w - gap * (cols - 1)) / cols
        for group, rows in GROUPS:
            visible = [r for r in rows if r[2] and self._shown(r[0])]
            if not visible:
                continue
            c.create_text(x0, y, text=group, font=T.mono(9), fill=T.DIM, anchor="w", tags=tags)
            c.create_line(x0 + T.width(group, T.mono(9)) + T.px(12), y, x0 + w, y, fill=T.LINE, tags=tags)
            y += T.px(26)
            col = 0
            row_y = y
            row_h = 0
            for key, label, kind, tip in visible:
                if kind == "slider" or (kind == "toggle" and cols == 1):
                    span = cols if kind == "slider" else 1
                else:
                    span = 1
                if col + span > cols:
                    row_y += row_h
                    col, row_h = 0, 0
                cx = x0 + col * (colw + gap)
                cw = colw * span + gap * (span - 1)
                used = self._control(key, label, kind, tip, cx, row_y, cw, accent)
                row_h = max(row_h, used)
                col += span
                if col >= cols:
                    row_y += row_h
                    col, row_h = 0, 0
            y = row_y + row_h + T.px(14)
        y = self._profile_and_actions(x0, y, w, accent)
        return y

    def _shown(self, key) -> bool:
        a = self.page.app
        if key in ("target_fps",):
            return a.shown_setting("workres")
        if key == "overlay_key":
            return a.shown_setting("overlay_key")
        return a.shown_setting(key)

    def _value(self, key):
        a = self.page.app
        if key == "route":
            return a.route
        if key == "exe":
            return a.game.exe
        if key == "bits":
            return a.game.bitness if a.game.bitness in (32, 64) else ""
        if key == "api":
            from .. import games
            return games.api_override(a.game.folder) or ""
        if key == "overlay_key":
            return a.overlay_key()
        if key == "target_fps":
            return str(a.target_fps or "")
        return a.settings.get(key)

    def _control(self, key, label, kind, tip, x, y, w, accent) -> int:
        page = self.page
        a, k = page.app, page.kit
        tags = ("page",)
        changed = a.changed(key) if key not in ("target_fps",) else bool(a.target_fps)
        busy = a.busy
        if kind == "dd":
            options = a.choices(key) if key != "target_fps" else FPS_CHOICES
            if key == "renodx" and not a.catalog:
                options = [("auto", "auto")]
            value = self._value(key)

            def pick(v, key=key):
                if key == "api":
                    a.set_api(v)
                elif key == "bits":
                    a.set_bitness(v or None)
                elif key == "exe":
                    a.set_exe(v)
                elif key == "route":
                    a.set_setting("route", v)
                else:
                    a.set_setting(key, v)
            k.dropdown(x, y, w, value, options, pick, label=label, tags=tags, enabled=not busy,
                       changed=changed, accent=accent, tip=tip, auto_hint=self._auto_hint(key, value))
            if key == "renodx":
                k.link(x + w, y, "use my file", a.pick_renodx, glyph="file", colour=T.DIM, anchor="e", tags=tags,
                       size=8)
            return T.px(74)
        if kind == "toggle":
            t = k.toggle(x, y + T.px(14), label, bool(a.settings.get(key)),
                         lambda v, key=key: a.set_setting(key, v), tags=tags, enabled=not busy, accent=accent,
                         tip=tip, width=w)
            return max(T.px(46), t.bottom - y + T.px(16))
        if kind == "slider":
            route = a.route
            lo, hi, step = ((optiscaler.NR_SCALE_MIN, optiscaler.NR_SCALE_MAX, 5) if route == dlss.OPTI
                            else (50, 100, 5))
            v = a.settings.get("workres", 100)
            page.c.create_text(x, y, text=label, font=T.mono(9), fill=T.DIM, anchor="w", tags=tags)
            hint = page.c.create_text(x + w, y, text=self._work_hint(v), font=T.mono(9), fill=T.MUTED,
                                      anchor="e", tags=tags)

            def moved(val, done):
                page.c.itemconfigure(hint, text=self._work_hint(val))
                if done:
                    a.set_setting("workres", val)
                else:
                    a.settings["workres"] = val
            k.slider(x + T.px(10), y + T.px(34), w - T.px(20), v, lo, hi, step, moved, tags=tags, enabled=not busy,
                     accent=accent)
            return T.px(66)
        return 0

    def _auto_hint(self, key, value):
        a = self.page.app
        if key == "bits":
            return ""
        if value in ("auto", "", None) or (key == "overlay_key" and "default" in str(value)):
            said = a.auto_says(key)
            return f"auto  \u00b7  {said}" if said else "auto"
        return ""

    def _work_hint(self, v):
        a = self.page.app
        if a.route == dlss.OPTI:
            cost = int(round(v * v / 100))
            if v == 100:
                return "100%  \u00b7  full size, about half your fps"
            return f"{v}%  \u00b7  about {cost}% of the full-size cost"
        if v == 100:
            return "100%  \u00b7  full quality"
        return f"{v}%  \u00b7  " + ("a little faster" if v >= 80 else "faster, softer")

    def _profile_and_actions(self, x0, y, w, accent) -> int:
        page = self.page
        a, c, k = page.app, page.c, page.kit
        tags = ("page",)
        c.create_text(x0, y, text="profile", font=T.mono(9), fill=T.DIM, anchor="w", tags=tags)
        c.create_line(x0 + T.width("profile", T.mono(9)) + T.px(12), y, x0 + w, y, fill=T.LINE, tags=tags)
        y += T.px(26)
        dw = min(T.px(320), w)
        names = a.profile_names()
        k.dropdown(x0, y, dw, a.profile, [(n, n) for n in names], a.load_profile, tags=tags,
                   changed=a.profile != "(none)", accent=accent, tip="settings saved under a name, for any game")
        lx, ly = x0 + dw + T.px(20), y + T.px(19)
        if lx + T.px(260) > x0 + w:
            # no room beside the dropdown: the two links go under it
            lx, ly = x0, y + T.px(62)
            y += T.px(36)
        tag, wid = k.link(lx, ly, "save as...", a.save_profile, glyph="save", colour=T.MUTED, tags=tags)
        box = c.bbox(tag)
        lx = (box[2] if box else lx + wid) + T.px(26)
        k.link(lx, ly, "delete", a.delete_profile, glyph="trash", colour=T.MUTED, tags=tags)
        y += T.px(62)
        c.create_line(x0, y, x0 + w, y, fill=T.LINE, tags=tags)
        y += T.px(26)
        right = x0 + w
        if a.game is not None and a.game.installed:
            tag, wid = k.link(x0 + w, y, "uninstall", a.uninstall, glyph="trash", colour=T.WARN, hot=T.TEXT,
                              anchor="e", tags=tags)
            box = c.bbox(tag)
            right = (box[0] if box else x0 + w - wid) - T.px(30)
        x = x0
        for label, glyph, cmd in (("what will happen?", "info", a.preview),
                                  ("check versions", "update", a.check_versions),
                                  ("before / after", "compare", a.compare),
                                  ("open folder", "folder", a.open_folder),
                                  ("reset to auto", "refresh", a.reset_settings)):
            tag, wid = k.link(x, y, label, cmd, glyph=glyph, colour=T.MUTED, tags=tags)
            box = c.bbox(tag)
            if box and box[2] > right and x > x0:
                # no room left on this row: the rest go on the next one
                y += T.px(36)
                right = x0 + w
                c.move(tag, x0 - box[0], T.px(36))
                box = c.bbox(tag)
                x = x0
            x = (box[2] if box else x + wid) + T.px(30)
        return y + T.px(40)
