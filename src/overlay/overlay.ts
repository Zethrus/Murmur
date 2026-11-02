import {call, on, type Config, type DictationState, type Events} from "../bridge";
export async function mountOverlay(root: HTMLElement): Promise<void> {
  root.innerHTML = '<section class="pill" role="status" aria-live="polite"><span class="pill-state"><span class="indicator" aria-hidden="true"></span><span class="bars" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></span><span class="pill-label"></span></span><span class="preview"></span></section>';
  const pill = root.querySelector<HTMLElement>(".pill")!, label = root.querySelector<HTMLElement>(".pill-label")!, preview = root.querySelector<HTMLElement>(".preview")!, indicator = root.querySelector<HTMLElement>(".indicator")!;
  const bars = [...root.querySelectorAll<HTMLElement>(".bars i")];
  let state: DictationState = "idle", config: Config | undefined, receivedState = false, receivedConfig = false, lastError = "Dictation unavailable", disposed = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const disposers: (()=>void)[] = [];
  const scheduleHide = (ms: number): void => { clearTimeout(timer); timer = setTimeout(()=>{pill.hidden = true;},ms); };
  const apply = (next: Config): void => {
    config = next; document.documentElement.dataset.theme = next.general.theme;
    document.documentElement.style.setProperty("--overlay-scale",String(next.overlay.scale)); root.dataset.position = next.overlay.position;
    root.dataset.preview = String(next.overlay.show_preview); render(state);
  };
  const render = (next: DictationState): void => {
    const previous = state; state = next; clearTimeout(timer); root.dataset.state = next;
    pill.hidden = config ? !config.overlay.enabled : true;
    indicator.textContent = next === "done" ? "✓" : next === "transcribing" ? "•••" : "";
    label.textContent = {idle:"mur",listening:"Listening",transcribing:"Finding your words",done:"Done",error:lastError}[next];
    if (next !== "listening") preview.textContent = "";
    root.dataset.expanded = String(next === "listening" && !!preview.textContent && !!config?.overlay.show_preview);
    if (next === "done") scheduleHide(900);
    else if (next === "error") scheduleHide(1600);
    else if (next === "idle" && config?.overlay.idle_hide) {
      if (previous === "done" || previous === "error") pill.hidden = true; else scheduleHide(1500);
    }
    if (next !== "listening") bars.forEach(bar=>bar.style.removeProperty("--level"));
  };
  async function subscribe<K extends keyof Events>(event: K, handler: (data: Events[K])=>void): Promise<void> {
    const off = await on(event,handler); if (disposed) off(); else disposers.push(off);
  }
  window.addEventListener("beforeunload",()=>{disposed = true; clearTimeout(timer); disposers.forEach(fn=>fn());},{once:true});
  await subscribe("state",event=>{receivedState = true;render(event.state);});
  await subscribe("config",next=>{receivedConfig = true;apply(next);});
  await subscribe("partial",event=>{if (state === "listening") {preview.textContent = event.text;root.dataset.expanded = String(!!event.text && !!config?.overlay.show_preview);}});
  await subscribe("level",({rms})=>{if (state !== "listening") return;
    bars.forEach((bar,index)=>bar.style.setProperty("--level",String(Math.max(0.12,Math.min(1,rms*8*[0.55,0.85,1,0.75,0.45][index])))));
  });
  await subscribe("error",({message})=>{lastError = message; if (state === "error") label.textContent = message;});
  const [initial,status] = await Promise.all([call("get_config"),call("get_status")]);
  if (disposed) return; if (!receivedConfig) apply(initial); if (status.error) lastError = status.error; if (!receivedState) render(status.state);
}
