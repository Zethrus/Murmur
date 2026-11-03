import {call, on, startOverlayDrag, type Config, type DictationState, type Enhancement, type Events, type OverlaySnapshot} from "../bridge";
import {cssLength, isNewer, sizeChanged, windowSize, type Size} from "./layout";

const NONE: Enhancement = {phase:"none",dictation_id:null,suggestion_id:null,original_text:null,proposed_text:null,acceptance_mode:"unavailable",message:null,expires_at:null};
const COPIED = "Copied. Select the original dictation and paste to replace it.";
const NOTICE_MS = 4000;
const active = (e: Enhancement): boolean => e.phase !== "none";

export async function mountOverlay(root: HTMLElement): Promise<void> {
  root.innerHTML = '<section class="pill" role="status" aria-live="polite"><span class="pill-state"><span class="drag-handle" role="presentation" title="Drag to move"><i></i><i></i><i></i></span><span class="indicator" aria-hidden="true"></span><span class="bars" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></span><span class="pill-label"></span></span><span class="preview"></span>'
    + '<div class="suggestion"><p class="suggestion-title">Suggested rewrite</p><p class="suggestion-text"></p><p class="suggestion-message"></p>'
    + '<div class="suggestion-actions"><button type="button" class="overlay-button primary accept-button">Accept — copy</button><button type="button" class="overlay-button discard-button">Discard</button></div></div></section>';
  const pill = root.querySelector<HTMLElement>(".pill")!, label = root.querySelector<HTMLElement>(".pill-label")!, preview = root.querySelector<HTMLElement>(".preview")!, indicator = root.querySelector<HTMLElement>(".indicator")!;
  const handle = root.querySelector<HTMLElement>(".drag-handle")!, panel = root.querySelector<HTMLElement>(".suggestion")!, proposed = root.querySelector<HTMLElement>(".suggestion-text")!;
  const message = root.querySelector<HTMLElement>(".suggestion-message")!, accept = root.querySelector<HTMLButtonElement>(".accept-button")!, discard = root.querySelector<HTMLButtonElement>(".discard-button")!;
  const bars = [...root.querySelectorAll<HTMLElement>(".bars i")];
  let state: DictationState = "idle", config: Config | undefined, receivedState = false, receivedConfig = false, lastError = "Dictation unavailable", disposed = false;
  let enhancement: Enhancement = NONE, revision = -1, busy = false, acting = false, localMessage = "";
  let timer: ReturnType<typeof setTimeout> | undefined, expiry: ReturnType<typeof setTimeout> | undefined, noticeTimer: ReturnType<typeof setTimeout> | undefined;
  const disposers: (()=>void)[] = [];
  panel.hidden = true;
  const scheduleHide = (ms: number): void => { clearTimeout(timer); timer = setTimeout(()=>{ if (!active(enhancement)) pill.hidden = true; },ms); };

  const apply = (next: Config): void => {
    config = next; document.documentElement.dataset.theme = next.general.theme;
    document.documentElement.style.setProperty("--overlay-scale",String(next.overlay.scale)); root.dataset.position = next.overlay.position;
    root.dataset.preview = String(next.overlay.show_preview); render(state);
  };
  /** Suggestion view; never hides the overlay while a rewrite is pending, ready, accepting or reporting. */
  const renderEnhancement = (): void => {
    const e = enhancement, phase = e.phase, unchanged = phase === "ready" && (e.proposed_text === null || e.proposed_text === e.original_text);
    root.dataset.enhance = phase; panel.hidden = !active(e) || state === "listening" || state === "transcribing";
    proposed.textContent = phase === "ready" || phase === "accepting" ? (unchanged ? "" : e.proposed_text ?? "") : "";
    proposed.hidden = !proposed.textContent;
    message.textContent = localMessage || e.message || (unchanged ? "No changes suggested." : (phase === "loading" ? "Loading the local rewrite model…" : phase === "rewriting" ? "Rewriting on this device…" : ""));
    message.hidden = !message.textContent;
    const actionable = phase === "ready" && !unchanged && !!e.suggestion_id && e.acceptance_mode !== "unavailable";
    accept.hidden = !actionable && phase !== "accepting"; accept.disabled = busy || acting || phase !== "ready";
    accept.textContent = phase === "accepting" ? "Copying…" : "Accept — copy";
    const pending = phase === "loading" || phase === "rewriting";
    discard.hidden = phase === "none" || (pending && !e.request_id);
    discard.textContent = pending ? "Cancel" : phase === "notice" || !e.suggestion_id ? "Dismiss" : "Discard";
    discard.disabled = busy || acting || phase === "accepting";
    root.dataset.expanded = String(isExpanded());
    if (!panel.hidden) {
      clearTimeout(timer); pill.hidden = config ? !config.overlay.enabled : true;
      label.textContent = "Enhance";
    }
  };
  const isExpanded = (): boolean => (state === "listening" && !!preview.textContent && !!config?.overlay.show_preview) || !panel.hidden;
  const render = (next: DictationState): void => {
    const previous = state; state = next; clearTimeout(timer); root.dataset.state = next;
    pill.hidden = config ? !config.overlay.enabled : true;
    indicator.textContent = next === "done" ? "✓" : next === "transcribing" ? "•••" : "";
    label.textContent = {idle:"mur",listening:"Listening",transcribing:"Finding your words",done:"Done",error:lastError}[next];
    if (next !== "listening") preview.textContent = "";
    if (next === "listening" && previous !== "listening") { setEnhancement(NONE); }
    renderEnhancement();
    if (active(enhancement) && !panel.hidden) { bars.forEach(bar=>bar.style.removeProperty("--level")); return; }
    if (next === "done") scheduleHide(900);
    else if (next === "error") scheduleHide(1600);
    else if (next === "idle" && config?.overlay.idle_hide) {
      if (previous === "done" || previous === "error") pill.hidden = true; else scheduleHide(1500);
    }
    if (next !== "listening") bars.forEach(bar=>bar.style.removeProperty("--level"));
  };
  /** Replace the enhancement view; clears suggestion text from memory on expiry, new dictation or discard. */
  const setEnhancement = (next: Enhancement): void => {
    const wasActive = active(enhancement), same = next.suggestion_id !== null && next.suggestion_id === enhancement.suggestion_id;
    enhancement = next; if (!same || (next.phase === "notice" && next.message)) localMessage = ""; if (!same) acting = false;
    clearTimeout(expiry); clearTimeout(noticeTimer);
    if (next.expires_at !== null && active(next)) {
      expiry = setTimeout(()=>{ if (enhancement === next) { setEnhancement(NONE); render(state); } },Math.max(0,next.expires_at - Date.now()));
    }
    if (next.phase === "notice") noticeTimer = setTimeout(()=>{ if (enhancement === next) { setEnhancement(NONE); settle(); } },NOTICE_MS);
    if (wasActive && !active(next)) { proposed.textContent = ""; }
  };
  /** After a suggestion closes, fall back to the ordinary idle presentation. */
  const settle = (): void => {
    renderEnhancement(); if (state === "listening" || state === "transcribing") return;
    if (config?.overlay.idle_hide) pill.hidden = true; else render("idle");
  };
  const applySnapshot = (snapshot: OverlaySnapshot): void => {
    if (!isNewer(revision,snapshot.revision)) return; revision = snapshot.revision;
    const wasActive = active(enhancement); busy = snapshot.delivery_busy; root.dataset.busy = String(busy);
    setEnhancement(snapshot.enhancement ?? NONE);
    if (wasActive && !active(enhancement)) settle(); else renderEnhancement();
  };

  const act = async (kind: "accept" | "discard"): Promise<void> => {
    const pending = enhancement.phase === "loading" || enhancement.phase === "rewriting";
    const id = kind === "discard" && pending ? enhancement.request_id : enhancement.suggestion_id;
    if (acting || busy) return;
    if (!id || (kind === "discard" && enhancement.phase === "notice")) { if (kind === "discard") { setEnhancement(NONE); settle(); } return; }
    acting = true; localMessage = ""; renderEnhancement();
    try {
      if (kind === "accept") {
        const reply = await call("accept_enhancement",{suggestionId:id});
        if (enhancement.suggestion_id !== id) return;
        localMessage = reply.outcome === "copied" ? COPIED : reply.outcome === "stale" ? "This suggestion is no longer available." : reply.message || "Could not copy the suggestion.";
        if (reply.outcome !== "failed") { enhancement = {...enhancement,phase:"notice",message:localMessage,original_text:null,proposed_text:null}; clearTimeout(noticeTimer);
          const shown = enhancement; noticeTimer = setTimeout(()=>{ if (enhancement === shown) { setEnhancement(NONE); settle(); } },NOTICE_MS); }
      } else {
        await call("discard_enhancement",{suggestionId:id});
        if ((pending ? enhancement.request_id : enhancement.suggestion_id) === id) { setEnhancement(NONE); acting = false; settle(); return; }
      }
    } catch(error) { if ((pending ? enhancement.request_id : enhancement.suggestion_id) === id) localMessage = String(error); }
    finally { if ((pending ? enhancement.request_id : enhancement.suggestion_id) === id || kind === "discard") acting = false; renderEnhancement(); }
  };
  accept.addEventListener("click",()=>act("accept")); discard.addEventListener("click",()=>act("discard"));
  // Native drag from the dedicated handle only; text, buttons and scrollbars stay interactive and never start a move.
  handle.addEventListener("pointerdown",(event: Event)=>{ const pointer = event as PointerEvent;
    if (pointer.button !== 0 || busy) return; void startOverlayDrag().catch(()=>{}); });

  // Report the rendered surface size; Rust clamps it to the work area and keeps the placement anchor.
  let layoutRevision = 0, reported: Size | undefined;
  const measure = (): void => {
    if (disposed || pill.hidden) return; const box = pill.getBoundingClientRect(); if (!(box.width > 0 && box.height > 0)) return;
    const style = window.getComputedStyle(root);
    const next = windowSize({width:box.width,height:box.height},{top:cssLength(style.paddingTop),right:cssLength(style.paddingRight),bottom:cssLength(style.paddingBottom),left:cssLength(style.paddingLeft)});
    if (!sizeChanged(reported,next)) return; reported = next;
    void call("set_overlay_size",{width:next.width,height:next.height,layoutRevision:++layoutRevision}).catch(()=>{});
  };
  const Observer = (globalThis as {ResizeObserver?: typeof ResizeObserver}).ResizeObserver;
  if (Observer) { const observer = new Observer(()=>measure()); observer.observe(pill); disposers.push(()=>observer.disconnect()); }

  async function subscribe<K extends keyof Events>(event: K, handler: (data: Events[K])=>void): Promise<void> {
    const off = await on(event,handler); if (disposed) off(); else disposers.push(off);
  }
  window.addEventListener("beforeunload",()=>{disposed = true; clearTimeout(timer); clearTimeout(expiry); clearTimeout(noticeTimer); enhancement = NONE; disposers.forEach(fn=>fn());},{once:true});
  await subscribe("state",event=>{receivedState = true;render(event.state);});
  await subscribe("config",next=>{receivedConfig = true;apply(next);});
  await subscribe("partial",event=>{if (state === "listening") {preview.textContent = event.text;root.dataset.expanded = String(isExpanded());}});
  await subscribe("level",({rms})=>{if (state !== "listening") return;
    bars.forEach((bar,index)=>bar.style.setProperty("--level",String(Math.max(0.12,Math.min(1,rms*8*[0.55,0.85,1,0.75,0.45][index])))));
  });
  await subscribe("error",({message: text})=>{lastError = text; if (state === "error") label.textContent = text;});
  await subscribe("overlay-update",applySnapshot);
  const [initial,status,snapshot] = await Promise.all([call("get_config"),call("get_status"),call("get_overlay_snapshot").catch(()=>undefined)]);
  if (disposed) return; if (!receivedConfig) apply(initial); if (status.error) lastError = status.error; if (!receivedState) render(status.state);
  if (snapshot) applySnapshot(snapshot);
  measure();
}
