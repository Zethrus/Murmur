import {call, type EnhanceConfig, type EnhanceDownload, type EnhanceModel, type EnhanceStatus} from "../../bridge";
import type {PageContext} from "../context";
import {card, row, el, button} from "../widgets/row";
import {toggle} from "../widgets/toggle";
import {select} from "../widgets/select";
import {hotkeyCapture} from "../widgets/hotkey-capture";
import {progress} from "../widgets/progress";

/** One-click suggestions only; both shortcuts stay unassigned by default. */
export const SUGGESTED_REWRITE = "Ctrl+Alt+Shift+R";
export const SUGGESTED_ACCEPT = "Ctrl+Alt+Shift+A";
const STATEMENT = "Rewrites run locally on your CPU. Dictation audio and text are never uploaded. Internet access is used only when you explicitly download a model. Your original dictation is delivered immediately.";
const ACTIVE_PHASES = new Set(["starting","downloading","verifying"]);
const DEFAULTS: EnhanceConfig = {auto_rewrite:false,rewrite_hotkey:"",accept_hotkey:"",style:"concise",model:"qwen3-1.7b-q4_k_m",accept_mode:"clipboard",rewrite_code:false,threads:0,unload_after_s:120,load_timeout_ms:30000,timeout_ms:15000};

/** Survives page re-renders so a revisited Behavior page paints the last known state before rehydrating from the backend. */
let cache: {model?: EnhanceModel; status?: EnhanceStatus; download?: EnhanceDownload} = {};
export function resetEnhanceCache(): void { cache = {}; }

export function formatBytes(bytes: number): string {
  const gb = bytes / 1e9, gib = bytes / 2 ** 30;
  return `${gb.toFixed(2)} GB (${gib.toFixed(2)} GiB · ${bytes.toLocaleString("en-US")} bytes)`;
}

export function enhanceCard(ctx: PageContext): HTMLElement {
  const section = ctx.config.enhance as EnhanceConfig | undefined, supported = section !== undefined;
  const e: EnhanceConfig = {...DEFAULTS,...(section ?? {})};
  const save = (patch: Partial<EnhanceConfig>): Promise<void> => ctx.save({enhance:patch});
  let live = true, model = cache.model, status = cache.status, download = cache.download ?? status?.download ?? undefined, statusRevision = status?.revision ?? -1;
  ctx.cleanup(()=>{live = false;});

  const modelName = el("p","enhance-model-name"), modelDetail = el("p","hint"), modelState = el("p","hint"), actions = el("div","button-group");
  const modelBox = el("div","enhance-model"), notice = el("p","field-error"), bar = progress("Waiting for download");
  notice.setAttribute("role","alert"); bar.element.hidden = true; modelBox.append(modelName,modelDetail,modelState,actions);
  const consent = el("div","enhance-consent"); consent.hidden = true;

  const installed = (): boolean => !!model?.installed || status?.installation_state === "installed";
  const downloading = (): boolean => !!download && ACTIVE_PHASES.has(download.phase);
  const sizeText = (): string => model ? formatBytes(model.size_bytes) : "Size unavailable";

  const install = async (enable: boolean): Promise<void> => {
    if (!model || downloading()) return; notice.textContent = ""; consent.hidden = true;
    try {
      if (e.model !== model.id) await save({model:model.id});
      const {operation_id} = await call("install_enhance_model",{modelId:model.id,enableAfterInstall:enable});
      if (!live) return;
      // Progress events may already have arrived for this operation; do not regress them.
      if (download?.operation_id !== operation_id) download = {operation_id,model_id:model.id,phase:"starting",received_bytes:0,total_bytes:model.size_bytes,error_code:null};
      cache.download = download; paint();
    } catch(error) { if (live) { notice.textContent = String(error); paint(); } }
  };
  const cancel = async (): Promise<void> => {
    const op = download?.operation_id; if (!op) return;
    try { await call("cancel_enhance_download",{operationId:op}); } catch(error) { if (live) notice.textContent = String(error); }
  };
  const remove = async (): Promise<void> => {
    if (!model || !window.confirm(`Remove ${model.display_name} from this device? Automatic rewriting will be turned off.`)) return;
    notice.textContent = "";
    try { await call("remove_enhance_model",{modelId:model.id}); await refresh(); } catch(error) { if (live) notice.textContent = String(error); }
  };

  /** Inline, explicit download consent. Nothing touches the network until a download button is pressed. */
  const askConsent = (): void => {
    consent.replaceChildren(el("p","hint",`Automatic rewriting needs the ${model?.display_name ?? "Enhance"} model: a one-time ${sizeText()} download from ${model ? `huggingface.co/${model.repo}` : "Hugging Face"}, verified by SHA-256. It runs on your CPU and uses about 1.1–1.4 GiB of memory while warm.`));
    const group = el("div","button-group");
    group.append(button("Download and enable",()=>install(true),"primary"),button("Install for manual rewrite",()=>install(false)),button("Not now",()=>{consent.hidden = true;}));
    consent.append(group); consent.hidden = false;
  };

  const paint = (): void => {
    if (!live) return;
    modelName.textContent = model?.display_name ?? (supported ? "Loading model details…" : "Enhance is unavailable in this build");
    modelDetail.textContent = model ? `${sizeText()} · ${model.repo} · ${model.filename}` : "";
    const st = status?.installation_state, load = status?.load_state;
    modelState.textContent = !supported ? "Update Murmur to a build with Enhance support." : status?.backend_available === false ? "This build has no local rewrite backend." :
      downloading() ? `Downloading… ${download?.phase === "verifying" ? "verifying checksum" : ""}`.trim() :
      st === "installed" ? `Installed · ${load === "loaded" ? "loaded in memory" : load === "loading" ? "loading" : "not loaded"}` :
      st === "corrupt" ? "The installed file failed verification. Remove it and install again." :
      st === "unavailable_model" ? `The configured model “${e.model}” is not in Murmur’s catalogue. Install the supported model to recover.` :
      st === "removing" ? "Removing…" : status?.message ?? (installed() ? "Installed" : "Not installed");
    actions.replaceChildren();
    const offline = !supported || !model || status?.backend_available === false;
    manual.disabled = offline;
    if (downloading()) actions.append(button("Cancel download",cancel,"danger"));
    else if (installed()) { const r = button("Remove",remove,"quiet danger"); r.disabled = st === "removing"; actions.append(r); }
    else { const failed = download?.phase === "error" || download?.phase === "cancelled" || st === "error" || st === "corrupt";
      const b = button(failed ? "Retry" : "Install",()=>install(false),"primary"); b.disabled = offline; actions.append(b); }
    if (download) {
      bar.element.hidden = download.phase === "complete" && installed();
      const total = download.total_bytes || model?.size_bytes || 0, fraction = total ? download.received_bytes / total : undefined;
      const detail = {starting:"Preparing download…",downloading:`${(download.received_bytes/1e6).toFixed(0)} of ${(total/1e6).toFixed(0)} MB`,verifying:"Verifying SHA-256…",
        complete:"Installed and verified.",cancelled:"Download cancelled. Nothing was enabled.",error:`Download failed${download.message ? `: ${download.message}` : download.error_code ? ` (${download.error_code})` : ""}. Retry when ready.`}[download.phase];
      bar.update(download.phase === "verifying" || download.phase === "starting" ? undefined : download.phase === "downloading" ? fraction : download.phase === "complete" ? 1 : 0,detail);
    } else bar.element.hidden = true;
  };

  const adopt = (next: EnhanceStatus): void => {
    if (next.revision < statusRevision) return; statusRevision = next.revision; status = next; cache.status = next;
    if (next.download) { download = next.download; cache.download = download; }
    else { download = undefined; cache.download = undefined; }
    if (model) { model = {...model,installed:next.installation_state === "installed"}; cache.model = model; }
  };
  const refresh = async (): Promise<void> => {
    if (!supported) { paint(); return; }
    try { const [m,s] = await Promise.all([call("get_enhance_model"),call("get_enhance_status")]); if (!live) return;
      model = m; cache.model = m; adopt(s); model = {...m,installed:m.installed || s.installation_state === "installed"};
    } catch(error) { if (live) notice.textContent = `Enhance status unavailable: ${String(error)}`; }
    paint();
  };
  ctx.subscribe("enhance-status",next=>{ adopt(next); paint(); });
  ctx.subscribe("enhance-download",event=>{
    // Only follow the operation this page started or rehydrated; ignore late events from older operations.
    if (!status?.download && (status?.installation_state === "installed" || status?.installation_state === "removing") && ACTIVE_PHASES.has(event.phase)) return;
    if (download && event.operation_id !== download.operation_id && ACTIVE_PHASES.has(download.phase)) return;
    download = event; cache.download = event;
    if (event.phase === "complete" && model) { model = {...model,installed:true}; cache.model = model; }
    paint(); if (event.phase === "complete") void refresh();
  });

  const disabledReason = !supported || status?.backend_available === false;
  const autoToggle = toggle(e.auto_rewrite,async value=>{
    if (value && !installed()) { askConsent(); throw new Error("Download the model first, or choose Install for manual rewrite."); }
    if (value && !ctx.config.overlay.enabled) throw new Error("Automatic rewriting needs the overlay. Use “Enable overlay and auto-rewrite” below.");
    await save({auto_rewrite:value});
  },disabledReason);
  const overlayOff = el("div","button-group"); overlayOff.hidden = ctx.config.overlay.enabled || !supported;
  overlayOff.append(button("Enable overlay and auto-rewrite",async()=>{
    if (!installed()) { askConsent(); return; }
    try { await ctx.save({overlay:{enabled:true},enhance:{auto_rewrite:true}}); } catch(error) { ctx.report(error); }
  }));

  const shortcut = (key: "rewrite_hotkey" | "accept_hotkey", suggestion: string): HTMLElement => {
    const box = el("div","button-group"), capture = hotkeyCapture(e[key],value=>save({[key]:value}),true);
    box.append(capture);
    if (!e[key]) { const use = button(`Use ${suggestion}`,async()=>{ try { await save({[key]:suggestion}); } catch(error) { notice.textContent = String(error); } }); use.disabled = disabledReason; box.append(use); }
    return box;
  };
  const styleSelect = select(e.style,[["concise","Concise"],["thorough","Thorough"],["structured","Structured"]],value=>save({style:value as EnhanceConfig["style"]}),disabledReason);
  const codeToggle = toggle(e.rewrite_code,value=>save({rewrite_code:value}),disabledReason);
  const acceptMode = select("clipboard",[["clipboard","Copy to clipboard"]],async()=>{},true);
  const manual = button("Rewrite last dictation",async()=>{
    try { const reply = await call("rewrite_last_dictation"); if (!live) return;
      const text: Record<string,string> = {queued:"Rewriting your last dictation. The suggestion appears in the overlay.",revealed:"The suggestion is shown in the overlay.",no_dictation:"Dictate something first.",
        busy:"Wait for the current dictation to finish.",already_running:"Already rewriting your last dictation.",model_missing:"Install the model first.",
        code_style:"Code and terminal dictation is not rewritten unless enabled above.",unavailable:"Enhance is unavailable.",expired:"Your last dictation expired. Dictate again."};
      notice.textContent = reply.message ?? text[reply.status] ?? reply.status; notice.dataset.tone = reply.status === "queued" || reply.status === "revealed" ? "ok" : "";
    } catch(error) { if (live) notice.textContent = String(error); }
  });
  manual.disabled = disabledReason;

  const statement = el("p","enhance-statement",STATEMENT);
  const disclosure = el("p","card-note","While warm, the model uses about 1.1–1.4 GiB of memory and is unloaded after two idle minutes. Code and terminal dictation is not rewritten unless you opt in. Accept copies the suggestion to your clipboard; it never replaces text in your app.");
  void refresh(); paint();
  return card("Enhance",statement,disclosure,
    row("Rewrite automatically","Suggest a rewrite in the overlay after each prose dictation. Needs the overlay and the installed model.",autoToggle),
    overlayOff,
    row("Style","Concise tightens wording; Thorough fixes grammar and flow; Structured organises multiple items.",styleSelect),
    row("Rewrite code and terminal dictation","Off by default. Code style keeps its exact deterministic output.",codeToggle),
    row("Rewrite shortcut","Optional. Rewrites your latest dictation on demand, even when automatic rewriting is off.",shortcut("rewrite_hotkey",SUGGESTED_REWRITE)),
    row("Accept shortcut","Optional. Copies the suggestion shown in the overlay.",shortcut("accept_hotkey",SUGGESTED_ACCEPT)),
    row("When you accept","Copied. Select the original dictation and paste to replace it. Direct replacement is not available.",acceptMode),
    row("Rewrite model","Downloads begin only when you choose Install. Stored separately from speech models.",modelBox),
    bar.element,consent,
    row("Try it","Rewrites the latest dictation from this session.",manual),
    notice);
}
