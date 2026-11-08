import {call} from "../../bridge";
import type {PageContext} from "../context";
import {controls, setter} from "../controls";
import {card, row, stack, el, button, textField} from "../widgets/row";
import {hotkeyCapture} from "../widgets/hotkey-capture";
export function general(ctx: PageContext): HTMLElement {
  const c = ctx.config, g = controls(ctx,"general"), o = controls(ctx,"overlay"), s = controls(ctx,"sounds");
  const previews = el("div", "button-group");
  for (const kind of ["start","stop","error"] as const) {
    const b = button(kind[0].toUpperCase()+kind.slice(1), () => call("preview_sound",{kind}).catch(ctx.report));
    b.disabled = !c.sounds.enabled || c.sounds.muted; previews.append(b);
  }
  const resetPosition = button("Reset position",()=>call("reset_overlay_position").catch(ctx.report));
  resetPosition.disabled = !c.overlay.enabled;
  return stack(
    card("Your shortcut to a thought",
      row("Dictation shortcut","Click to capture keys. Escape cancels recording.",hotkeyCapture(c.general.hotkey,setter(ctx,"general","hotkey"))),
      row("Code shortcut","Optional second shortcut. Always preserves code-style casing.",hotkeyCapture(c.general.hotkey_code,setter(ctx,"general","hotkey_code"),true)),
      g.select("hotkey_mode","Activation","Toggle is recommended for longer coding prompts.",[["hold","Hold to talk"],["toggle","Press to start / stop"]]),
      row("Language","Language code such as en, fr or ja. Leave empty to auto-detect.",textField(c.general.language,setter(ctx,"general","language"),"Auto-detect")),
      g.toggle("translate","Translate to English","Transcribe speech into English rather than its original language.")),
    card("A little presence",
      o.toggle("enabled","Show overlay","A quiet pill above your other windows. Drag its handle to move it; clicking it never takes focus from your app."),
      o.select("position","Position",c.overlay.position === "custom" ? "Placed where you dragged it. Choose a preset or reset to return it to the bottom centre." : "Centred on the primary display, or drag the overlay anywhere.",
        [["bottom-center","Bottom centre"],["top-center","Top centre"],...(c.overlay.position === "custom" ? [["custom","Where you dragged it"] as const] : [])],!c.overlay.enabled),
      row("Reset position","Return the overlay to the bottom centre of the primary display.",resetPosition),
      o.slider("scale","Overlay size","Scale the pill and its text together.",0.8,1.6,0.1,n=>`${Math.round(n*100)}%`,!c.overlay.enabled),
      o.toggle("show_preview","Live preview","See two lines of your words before they land.",!c.overlay.enabled),
      o.select("idle_view","When idle","What stays on screen between dictations. The small icon marks where the overlay is; drag it to move.",
        [["icon","Small app icon"],["pill","Full pill"],["hidden","Nothing"]],!c.overlay.enabled)),
    card("Sound cues",
      s.toggle("enabled","Play cues","Distinct sounds for starting, stopping and errors."),
      s.toggle("muted","Mute sounds","Temporarily silence every cue.",!c.sounds.enabled),
      s.slider("volume","Cue volume","Independent of microphone gain.",0,1,0.05,n=>`${Math.round(n*100)}%`,!c.sounds.enabled || c.sounds.muted),
      row("Preview cues","Uses your current volume and mute settings.",previews)),
    card("Make yourself at home",
      g.select("theme","Appearance","The overlay follows the same colour theme.",[["auto","System"],["light","Light"],["dark","Dark"]]),
      g.toggle("autostart","Start at login","Keep Murmur ready in your system tray."))
  );
}
