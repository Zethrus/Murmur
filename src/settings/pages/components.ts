import {call, type ComponentInfo, type ComponentsSnapshot} from "../../bridge";
import type {PageContext} from "../context";
import {controls, setter} from "../controls";
import {card, row, stack, el, button} from "../widgets/row";
import {listEditor} from "../widgets/list-editor";
import {componentRow, formatBytes, needs, trustNotice} from "../widgets/component";

/** Settings → Components: engine, GPU add-on, speech models and Enhance, plus local inference. */
export function components(ctx: PageContext): HTMLElement {
  const c = ctx.config, e = controls(ctx, "engine");
  const engineBox = el("div", "component-list"), gpuBox = el("div", "component-list"), modelBox = el("div", "model-grid"), enhanceBox = el("div", "component-list");
  const free = el("span", "status-value", "…");
  const notice = el("p", "hint", "Loading components…"), updates = el("p", "hint"), problem = el("p", "field-error"); problem.setAttribute("role", "alert");
  const rows = new Map<string, (job: import("../../bridge").ComponentJob) => void>();
  const verified = new Map<string, string>();
  let live = true; ctx.cleanup(() => { live = false; });
  const fail = (message: string): void => { problem.textContent = message; };
  const render = (snapshot: ComponentsSnapshot): void => {
    rows.clear(); problem.textContent = "";
    const trust = trustNotice(snapshot);
    notice.textContent = trust ?? (snapshot.migrating ? "Checking models from an earlier version…" : "Downloads start only when you choose Install. Files come from GitHub Releases and Hugging Face and are verified against Murmur's signed list.");
    notice.className = trust ? "field-error" : "hint";
    free.textContent = formatBytes(snapshot.hardware.free_bytes);
    const m = snapshot.manifest;
    updates.textContent = m ? `Component list ${m.serial}, issued ${m.issued_at.slice(0, 10)}, ${m.stale ? "expired" : "valid until"} ${m.expires_at.slice(0, 10)}${snapshot.trust.development ? " · development signing key" : ""}.` : "No verified component list yet.";
    const hooks = {fail, canInstall: snapshot.trust.ok && !snapshot.stale,
      verified: (info: ComponentInfo): boolean => verified.get(info.id) === info.installed_version &&
        (info.state === "active" || info.state === "installed"),
      onVerify: (info: ComponentInfo, verdict: string): void => {
        if (verdict === "verified" && info.installed_version) verified.set(info.id, info.installed_version);
        else verified.delete(info.id);
        void load(); // a components/changed event may already have replaced the button's row
      },
      extra: (info: ComponentInfo): HTMLElement[] => info.kind === "speech-model" && info.state === "installed" && info.model
        ? [button("Use model", () => ctx.save({engine: {model: info.model ?? ""}}), "primary")] : []};
    const place = (box: HTMLElement, list: ComponentInfo[], empty: string): void => {
      box.replaceChildren(...(list.length ? list.map(info => { const r = componentRow(info, hooks); rows.set(info.id, r.progress); return r.element; }) : [el("p", "empty", empty)]));
    };
    const of = (kind: ComponentInfo["kind"]): ComponentInfo[] => snapshot.components.filter(x => x.kind === kind);
    place(engineBox, of("engine"), "The speech engine is not in the component list.");
    const gpu = snapshot.hardware.gpus[0];
    place(gpuBox, snapshot.hardware.cuda_offered || of("cuda").some(x => x.state !== "available") ? of("cuda") : [],
      gpu ? "GPU acceleration is not offered on this platform yet." : "No NVIDIA GPU detected. Dictation runs on the CPU.");
    place(modelBox, of("speech-model"), "No speech models are listed.");
    place(enhanceBox, of("enhance-model"), "No Enhance model is listed.");
    if (gpu) gpuBox.prepend(el("p", "hint", `${gpu.name} · ${(gpu.vram_mb / 1024).toFixed(1)} GB video memory · driver ${gpu.driver}`));
  };
  let pending: ReturnType<typeof setTimeout> | undefined;
  const load = async (): Promise<void> => {
    try { const snapshot = await call("components_snapshot"); if (live) render(snapshot); }
    catch (error) { if (live) { notice.textContent = `Components unavailable. ${String(error)}`; notice.className = "field-error"; } }
  };
  ctx.subscribe("components/changed", () => { clearTimeout(pending); pending = setTimeout(() => { void load(); }, 100); });
  ctx.subscribe("components/progress", job => { rows.get(job.id)?.(job); });
  ctx.cleanup(() => clearTimeout(pending));
  void load();
  const check = button("Check for updates", async () => {
    updates.textContent = "Checking for updates…";
    try { await call("components_check_updates"); await load(); } catch (error) { updates.textContent = `Update check failed: ${String(error)}`; }
  });
  const reopen = button("Change setup choices", () => ctx.openSetup(), "quiet");
  const test = button("Test 3 s", async () => {
    testResult.textContent = "Running a local 3-second inference probe…";
    try { const start = performance.now(), result = await call("test_engine"); if (live) testResult.textContent = `${Math.round(result.infer_ms)} ms inference · ${Math.round(performance.now() - start)} ms round trip`; }
    catch (error) { if (live) testResult.textContent = String(error); }
  });
  const testResult = el("p", "test-result", "Not tested in this session.");
  test.disabled = !ctx.status.sidecar_alive || !ctx.status.model_available || ctx.status.state !== "idle";
  const actions = el("div", "button-group");
  actions.append(check, reopen, button("Open folder", () => call("open_path", {kind: "components"}), "quiet"));
  return stack(
    card("What you have", el("p", "card-note", needs), notice, problem, row("Free disk space", "Components are stored in your Murmur data folder.", free), updates, actions),
    card("Speech engine", el("p", "card-note", "Required for dictation. Runs entirely on this computer."), engineBox),
    card("GPU acceleration", el("p", "card-note", "Optional NVIDIA CUDA add-on. Without it, dictation uses the CPU."), gpuBox),
    card("Speech models", el("p", "card-note", "Small models feel fast. Larger models hear technical vocabulary more accurately."), modelBox),
    card("Enhance", el("p", "card-note", "Optional local rewriting model. Turn on automatic rewrites in Behavior."), enhanceBox),
    card("Local inference",
      e.select("device", "Compute device", "Auto tries CUDA, then falls back to CPU.", [["auto", "Automatic"], ["cuda", "NVIDIA CUDA"], ["cpu", "CPU"]]),
      row("Detected hardware", "Resolved by the local engine; may stay unloaded until first use.", el("span", "status-value", ctx.status.sidecar_alive ? `${ctx.status.device} · ${ctx.status.compute}` : "Engine offline")),
      ...(c.engine.device === "auto" && ctx.status.fallback_reason ? [el("p", "field-error", `Using CPU because GPU acceleration is unavailable. ${ctx.status.fallback_reason}`)] : []),
      e.select("compute_type", "Precision", "Auto chooses an efficient precision for your hardware.", ["auto", "int8", "int8_float16", "float16", "float32", "int8_float32"]),
      e.slider("beam_size", "Beam size", "Higher values trade speed for recognition accuracy.", 1, 10, 1),
      e.toggle("vad_filter", "Voice activity filter", "Skip silent regions before speech recognition."),
      row("Engine check", "A silent, three-second inference probe; not a microphone recording. First use may warm the model.", stack(test, testResult))),
    card("Words worth knowing", row("Dictionary", "Seeded with coding vocabulary. These words bias recognition; they are not automatic replacements.", listEditor(c.engine.dictionary, setter(ctx, "engine", "dictionary"), "dictionary word"))));
}
