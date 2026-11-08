import {call, type MicrophoneAccess, type MicrophoneBlock} from "../../bridge";
import type {PageContext} from "../context";
import {controls, setter} from "../controls";
import {card, row, stack, el, button} from "../widgets/row";
import {select} from "../widgets/select";
import {levelMeter} from "../widgets/level-meter";

const BLOCKED: Record<MicrophoneBlock, string> = {
  device: "Windows has microphone access turned off for this device.",
  apps: "Windows has “Let apps access your microphone” turned off.",
  desktop_apps: "Windows has “Let desktop apps access your microphone” turned off.",
};

/** Windows privacy explanation with a link to the right Settings page; hidden elsewhere. */
function privacyNotice(): {element: HTMLElement; update(access: MicrophoneAccess | undefined, noDevices: boolean): void} {
  const element = el("div","model-banner mic-privacy"), text = el("span",""), status = el("p","hint");
  const open = button("Open Windows privacy settings",async()=>{
    status.textContent = "";
    try { await call("open_microphone_settings"); } catch(e) { status.textContent = `Could not open Settings: ${String(e)}`; }
  },"primary");
  const body = el("div",""); body.append(text,status); element.append(body,open); element.hidden = true;
  return {element, update(access, noDevices) {
    const blocked = access?.blocked ?? null;
    element.hidden = !access?.settings_available || (!blocked && !noDevices);
    text.textContent = blocked
      ? `${BLOCKED[blocked]} Murmur hears silence until you allow it, then press Refresh.`
      : "No microphone found. If one is connected, Windows privacy settings may be hiding it from desktop apps.";
  }};
}

export function audio(ctx: PageContext): HTMLElement {
  const a = controls(ctx,"audio"), device = el("div","device-control"), hint = el("p","hint","Loading microphones…"); device.append(hint);
  const privacy = privacyNotice();
  let live = true; ctx.cleanup(() => { live = false; });
  const load = async (): Promise<void> => {
    const access = call("microphone_access").catch((): undefined => undefined);
    try { const devices = await call("list_devices"); if (!live) return;
      device.replaceChildren(select(ctx.config.audio.device,[["default","System default"],...devices.map(d => [d.id,`${d.name}${d.is_default ? " · Default" : ""}`] as const)],setter(ctx,"audio","device"),!devices.length));
      if (!devices.length) device.append(el("p","empty","No input devices found. Connect a microphone, then refresh."));
      const state = await access; if (live) privacy.update(state,!devices.length);
    } catch(e) { if (live) { hint.textContent = `Could not list microphones: ${String(e)}`; device.replaceChildren(hint); privacy.update(await access,true); } }
  };
  void load(); const meter = levelMeter(); ctx.subscribe("level",({rms})=>meter.update(rms));
  return stack(privacy.element,card("Input",row("Microphone","Choose the source used for all dictation.",device),
    row("Refresh inputs","Use after connecting or reconnecting a microphone.",button("Refresh",load)),
    row("Live input level","Speak normally. No audio is saved to disk.",meter.element),
    a.slider("gain","Input gain","Increase quiet speech; reduce if the meter stays full.",0,10,0.1,n=>`${n.toFixed(1)}×`)),
    card("Make room for a pause",
      a.slider("silence_threshold","Silence threshold","Levels below this are treated as silence.",0,0.2,0.001,n=>n.toFixed(3)),
      a.slider("silence_timeout_ms","Silence timeout","In toggle mode, stop after this much silence following speech.",100,60000,100,n=>`${(n/1000).toFixed(1)} s`),
      a.slider("max_utterance_s","Maximum utterance","Up to 90 seconds for longer prompts.",1,90,1,n=>`${n} s`),
      a.slider("min_utterance_ms","Minimum utterance","Ignore accidental taps shorter than this.",1,Math.min(2000,ctx.config.audio.max_utterance_s*1000),1,n=>`${n} ms`)));
}
