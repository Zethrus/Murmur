import {call} from "../../bridge";
import type {PageContext} from "../context";
import {controls, setter} from "../controls";
import {card, row, stack, el, button} from "../widgets/row";
import {select} from "../widgets/select";
import {levelMeter} from "../widgets/level-meter";
export function audio(ctx: PageContext): HTMLElement {
  const a = controls(ctx,"audio"), device = el("div","device-control"), hint = el("p","hint","Loading microphones…"); device.append(hint);
  let live = true; ctx.cleanup(() => { live = false; });
  const load = async (): Promise<void> => {
    try { const devices = await call("list_devices"); if (!live) return;
      device.replaceChildren(select(ctx.config.audio.device,[["default","System default"],...devices.map(d => [d.id,`${d.name}${d.is_default ? " · Default" : ""}`] as const)],setter(ctx,"audio","device"),!devices.length));
      if (!devices.length) device.append(el("p","empty","No input devices found. Connect a microphone, then refresh."));
    } catch(e) { if (live) { hint.textContent = `Could not list microphones: ${String(e)}`; device.replaceChildren(hint); } }
  };
  void load(); const meter = levelMeter(); ctx.subscribe("level",({rms})=>meter.update(rms));
  return stack(card("Input",row("Microphone","Choose the source used for all dictation.",device),
    row("Refresh inputs","Use after connecting or reconnecting a microphone.",button("Refresh",load)),
    row("Live input level","Speak normally. No audio is saved to disk.",meter.element),
    a.slider("gain","Input gain","Increase quiet speech; reduce if the meter stays full.",0,10,0.1,n=>`${n.toFixed(1)}×`)),
    card("Make room for a pause",
      a.slider("silence_threshold","Silence threshold","Levels below this are treated as silence.",0,0.2,0.001,n=>n.toFixed(3)),
      a.slider("silence_timeout_ms","Silence timeout","In toggle mode, stop after this much silence following speech.",100,60000,100,n=>`${(n/1000).toFixed(1)} s`),
      a.slider("max_utterance_s","Maximum utterance","Up to 90 seconds for longer prompts.",1,90,1,n=>`${n} s`),
      a.slider("min_utterance_ms","Minimum utterance","Ignore accidental taps shorter than this.",1,Math.min(2000,ctx.config.audio.max_utterance_s*1000),1,n=>`${n} ms`)));
}
