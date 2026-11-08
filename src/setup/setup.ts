import {call, type ComponentInfo, type ComponentJob, type ComponentsSnapshot, type Config, type ConfigPatch, type Events} from "../bridge";
import {el, button} from "../settings/widgets/row";
import {toggle} from "../settings/widgets/toggle";
import {progress} from "../settings/widgets/progress";
import {STALE_NOTICE, formatBytes, jobText, stateLabel} from "../settings/widgets/component";

export interface SetupHooks {
  config: Config;
  snapshot: ComponentsSnapshot;
  save(patch: ConfigPatch): Promise<void>;
  subscribe<K extends keyof Events>(event: K, handler: (data: Events[K]) => void): void;
  /** "Set up later": persist the choice and return to Settings. */
  later(): Promise<void>;
  /** Setup finished: return to Settings. */
  done(): void;
}
type Language = "en" | "multilingual";
type Compute = "cpu" | "cuda";
export const STEPS = ["Welcome", "Language", "Speech model", "Compute", "Summary", "Install"] as const;
const MARGIN = 100 * 1024 * 1024;

const isReady = (info: ComponentInfo | undefined): boolean => info?.state === "active" || info?.state === "installed";

/** First-run setup. Nothing is downloaded before the explicit "Download and install" click. */
export function setupWizard(hooks: SetupHooks): HTMLElement {
  let snapshot = hooks.snapshot, step = 0;
  const speech = (): ComponentInfo[] => snapshot.components.filter(c => c.kind === "speech-model");
  const find = (id: string): ComponentInfo | undefined => snapshot.components.find(c => c.id === id);
  // "Change setup choices" starts from the current configuration; a first run from the
  // hardware recommendation.
  const reopening = snapshot.engine_installed;
  const configured = reopening ? speech().find(m => m.model === hooks.config.engine.model) : undefined;
  let language: Language = configured?.languages ?? (reopening && hooks.config.general.language !== "en" ? "multilingual" : "en");
  let translate = hooks.config.general.translate;
  const gpuOffered = snapshot.hardware.cuda_offered && snapshot.components.some(c => c.kind === "cuda");
  let compute: Compute = gpuOffered && (!reopening || hooks.config.engine.device !== "cpu")
    && (isReady(find("cuda")) || snapshot.recommendation.compute === "cuda") ? "cuda" : "cpu";
  const recommended = (): string => language === "en" ? snapshot.recommendation.english : snapshot.recommendation.multilingual;
  let model = configured?.model ?? recommended();
  const root = el("section", "setup"), status = el("p", "setup-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
  root.setAttribute("aria-label", "Murmur setup");
  let installing: string[] = [];
  const progressRows = new Map<string, (job: ComponentJob) => void>();

  const choice = (name: string, value: string, checked: boolean, title: string, detail: string, onPick: () => void, badge?: string): HTMLElement => {
    const label = el("label", "choice-card"), input = el("input", "choice-input"), copy = el("span", "choice-copy");
    input.type = "radio"; input.name = name; input.value = value; input.checked = checked;
    input.addEventListener("change", () => { if (input.checked) { onPick(); render(); } });
    label.dataset.selected = String(checked);
    const heading = el("span", "choice-title", title);
    if (badge) heading.append(el("span", "badge", badge));
    copy.append(heading, el("span", "hint", detail)); label.append(input, copy); return label;
  };
  const group = (legend: string, ...items: HTMLElement[]): HTMLElement => {
    const fieldset = el("fieldset", "choice-group"), caption = el("legend", "choice-legend", legend);
    fieldset.append(caption, ...items); return fieldset;
  };
  const selection = (): {ids: string[]; parts: ComponentInfo[]} => {
    const wanted = ["engine", ...(compute === "cuda" ? ["cuda"] : []), `speech-${model}`];
    const parts = wanted.map(find).filter((c): c is ComponentInfo => !!c);
    return {ids: parts.filter(c => !isReady(c) || c.update_available).map(c => c.id), parts};
  };
  const apply = (): Promise<void> => hooks.save({engine: {model, device: compute === "cuda" ? "auto" : "cpu"},
    general: {language: language === "en" ? "en" : "", translate: language === "en" ? false : translate}, setup: {postponed: false}});

  const steps: (() => HTMLElement[])[] = [
    () => [el("p", "setup-lead", "Murmur turns your voice into text on this computer."),
      (() => { const list = el("ul", "setup-points");
        for (const point of ["Speech is processed locally. Audio and text never leave this computer.",
          "Downloads come only from GitHub Releases (engine, GPU add-on) and Hugging Face (speech models), and each file is checked against Murmur's signed list.",
          "No account and no telemetry.",
          "Nothing is downloaded until you confirm on the summary step."]) list.append(el("li", "", point));
        return list; })()],
    () => {
      const items = [group("Which languages will you dictate in?",
        choice("language", "en", language === "en", "English only", "Smaller, faster models tuned for English.", () => { language = "en"; model = recommended(); }),
        choice("language", "multilingual", language === "multilingual", "Many languages", "Multilingual models for 99 languages; larger downloads.", () => { language = "multilingual"; model = recommended(); }))];
      if (language === "multilingual") {
        const row = el("div", "setup-toggle"), label = el("label", "row-label", "Translate speech into English"), control = toggle(translate, async v => { translate = v; });
        const input = control.querySelector<HTMLElement>("input"); if (input) { input.id = "setup-translate"; label.htmlFor = "setup-translate"; }
        row.append(label, control); items.push(row);
      }
      return items;
    },
    () => {
      const models = speech().filter(m => m.languages === language);
      if (!models.some(m => m.model === model)) model = models.find(m => m.model === recommended())?.model ?? models[0]?.model ?? model;
      return [el("p", "hint", snapshot.recommendation.reason), group("Choose a speech model",
        ...models.map(m => choice("model", m.model ?? "", m.model === model, m.display_name,
          `${m.description} Quality: ${m.quality ?? "?"}. Speed: ${m.speed ?? "?"}. About ${((m.memory_mb ?? 0) / 1024).toFixed(1)} GB memory. ${formatBytes(m.size_bytes)} download, ${formatBytes(m.installed_size_bytes)} on disk.${isReady(m) ? " Already installed." : ""}`,
          () => { model = m.model ?? model; }, m.model === recommended() ? "Recommended" : undefined)))];
    },
    () => {
      const gpu = snapshot.hardware.gpus[0], cuda = find("cuda");
      const options = [choice("compute", "cpu", compute === "cpu", "CPU", "Works on every computer. No extra download.", () => { compute = "cpu"; })];
      if (gpuOffered && gpu) options.push(choice("compute", "cuda", compute === "cuda", "NVIDIA GPU (CUDA)",
        `${gpu.name}, ${(gpu.vram_mb / 1024).toFixed(1)} GB video memory, driver ${gpu.driver}. Adds the GPU add-on: ${formatBytes(cuda?.size_bytes)} download, ${formatBytes(cuda?.installed_size_bytes)} on disk. Falls back to the CPU if the GPU fails.`,
        () => { compute = "cuda"; }, snapshot.recommendation.compute === "cuda" ? "Recommended" : undefined));
      const items = [group("Where should speech recognition run?", ...options)];
      if (!gpuOffered) items.push(el("p", "hint", "No supported NVIDIA GPU was detected, so dictation will use the CPU."));
      return items;
    },
    () => {
      const {ids, parts} = selection();
      const pending = parts.filter(c => ids.includes(c.id));
      const sum = (key: "size_bytes" | "installed_size_bytes" | "temp_size_bytes"): number => pending.reduce((n, c) => n + (c[key] ?? 0), 0);
      const download = sum("size_bytes"), installed = sum("installed_size_bytes"), temporary = sum("temp_size_bytes"), free = snapshot.hardware.free_bytes;
      const table = el("dl", "summary-list");
      for (const part of parts) table.append(el("dt", "", part.display_name), el("dd", "", ids.includes(part.id) ? `${formatBytes(part.size_bytes)} download · ${formatBytes(part.installed_size_bytes)} on disk` : "Already installed"));
      const totals = el("dl", "summary-list summary-totals");
      totals.append(el("dt", "", "Download"), el("dd", "", formatBytes(download)), el("dt", "", "Installed size"), el("dd", "", formatBytes(installed)),
        el("dt", "", "Temporary space"), el("dd", "", formatBytes(temporary)), el("dt", "", "Free disk space"), el("dd", "", formatBytes(free)));
      const short = free !== null && free < download + installed + temporary + MARGIN;
      const items: HTMLElement[] = [table, totals];
      if (short) items.push(el("p", "field-error", `Not enough free disk space: about ${formatBytes(download + installed + temporary + MARGIN)} is needed.`));
      if (!snapshot.trust.ok) items.push(el("p", "field-error", snapshot.trust.message ?? "Components cannot be verified, so nothing can be installed."));
      else if (snapshot.stale && ids.length) items.push(el("p", "field-error", `${STALE_NOTICE} Check for updates is in Settings → Components.`));
      items.push(el("p", "hint", ids.length ? `Selecting Download and install downloads ${formatBytes(download)} from GitHub Releases and Hugging Face. Nothing else is sent.` : "Everything you chose is already installed."));
      return items;
    },
    () => {
      const list = el("div", "component-list");
      progressRows.clear();
      for (const id of installing) {
        const info = find(id), row = el("div", "setup-progress"), name = el("span", "row-label", info?.display_name ?? id), bar = progress(`${info?.display_name ?? id} progress`), error = el("p", "field-error");
        error.setAttribute("role", "alert");
        const actions = el("div", "button-group"), label = info?.display_name ?? id;
        const act = (text: string, run: () => Promise<unknown>, kind = "secondary"): HTMLButtonElement => { const b = button(text, async () => { try { await run(); } catch (e) { error.textContent = String(e); } }, kind); b.setAttribute("aria-label", `${text} ${label}`); return b; };
        const ready = isReady(info) && !info?.job;
        if (ready) bar.update(1, "Installed");
        else if (info?.job) bar.update(info.job.bytes_total ? info.job.bytes_done / info.job.bytes_total : undefined, jobText(info.job));
        else bar.update(undefined, info ? stateLabel(info.state) : "Waiting");
        if (info?.state === "failed" || info?.state === "paused") {
          if (info.job?.error) error.textContent = info.job.error;
          actions.append(act(info.state === "failed" ? "Retry" : "Resume", () => call("components_resume", {id}), "primary"));
        } else if (!ready) actions.append(act("Pause", () => call("components_stop", {id, pause: true})), act("Cancel", () => call("components_stop", {id, pause: false}), "quiet danger"));
        row.append(name, bar.element, error, actions); list.append(row);
        progressRows.set(id, job => { bar.update(job.bytes_total ? job.bytes_done / job.bytes_total : undefined, jobText(job)); if (job.error) error.textContent = job.error; });
      }
      const complete = installing.every(id => isReady(find(id)) && !find(id)?.job);
      status.textContent = complete ? "All set. Hold your dictation hotkey and speak." : "Installing. You can keep using your computer; Murmur continues in the background.";
      return [list];
    },
  ];

  const render = (focus = false): void => {
    const body = el("div", "setup-body"), footer = el("div", "setup-footer"), heading = el("h1", "setup-title", STEPS[step]);
    heading.tabIndex = -1;
    const counter = el("p", "eyebrow", `STEP ${step + 1} OF ${STEPS.length}`);
    body.append(...steps[step]());
    const back = button("Back", () => { step--; render(true); }, "quiet"); back.hidden = step === 0 || step === 5;
    const later = button("Set up later", () => hooks.later(), "quiet"); later.setAttribute("aria-label", "Set up later and close setup");
    const next = step < 4 ? button("Next", () => { step++; render(true); }, "primary") : null;
    footer.append(back, later);
    if (next) footer.append(next);
    if (step === 4) {
      const {ids} = selection();
      const go = button(ids.length ? "Download and install" : "Finish", async () => {
        await apply();
        if (!ids.length) { hooks.done(); return; }
        await call("components_install", {ids});
        installing = ids; step = 5; snapshot = await call("components_snapshot"); render(true);
      }, "primary");
      go.disabled = ids.length > 0 && (!snapshot.trust.ok || snapshot.stale);
      footer.append(go);
    }
    if (step === 5) {
      const finished = installing.every(id => isReady(find(id)) && !find(id)?.job);
      const done = button("Done", () => hooks.done(), "primary"); done.disabled = !finished;
      footer.append(done);
    }
    root.replaceChildren(counter, heading, body, status, footer);
    if (step !== 5) status.textContent = "";
    if (focus) heading.focus();
  };
  let pending: ReturnType<typeof setTimeout> | undefined;
  hooks.subscribe("components/progress", job => progressRows.get(job.id)?.(job));
  hooks.subscribe("components/changed", () => {
    if (step !== 5) return;
    clearTimeout(pending);
    pending = setTimeout(() => { void call("components_snapshot").then(next => { snapshot = next; if (step === 5) render(); }); }, 100);
  });
  render();
  return root;
}
