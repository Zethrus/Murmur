import { call, on, type Config, type DictationState } from "./bridge";
import "./styles/tokens.css";
import "./styles/settings.css";
import "./styles/overlay.css";

async function main(): Promise<void> {
  const root = document.querySelector<HTMLElement>("#app");
  if (!root) throw new Error("Missing app root");
  const overlay = new URLSearchParams(location.search).get("window") === "overlay";
  document.body.dataset.window = overlay ? "overlay" : "settings";
  const disposers: (() => void)[] = [];
  let disposed = false;
  const subscribe = async <K extends Parameters<typeof on>[0]>(event: K,
    handler: Parameters<typeof on<K>>[1]): Promise<void> => {
    const unlisten = await on(event, handler);
    if (disposed) unlisten(); else disposers.push(unlisten);
  };
  window.addEventListener("beforeunload", () => { disposed = true; disposers.forEach(dispose => dispose()); }, { once: true });
  const applyConfig = (config: Config): void => {
    document.documentElement.dataset.theme = config.general.theme;
    root.style.setProperty("--overlay-scale", String(config.overlay.scale));
    root.dataset.preview = String(config.overlay.show_preview);
    root.dataset.position = config.overlay.position;
  };
  if (overlay) {
    root.innerHTML = '<section class="pill" role="status" aria-live="polite"><span class="indicator" aria-hidden="true"></span>' +
      '<span class="bars" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></span>' +
      '<span class="pill-content"><span class="pill-label">mur</span><span class="preview"></span></span></section>';
    const label = root.querySelector<HTMLElement>(".pill-label")!;
    const preview = root.querySelector<HTMLElement>(".preview")!;
    const bars = [...root.querySelectorAll<HTMLElement>(".bars i")];
    let state: DictationState = "idle";
    let receivedState = false;
    let lastError = "Dictation unavailable";
    const indicator = root.querySelector<HTMLElement>(".indicator")!;
    const render = (next: DictationState): void => {
      state = next; root.dataset.state = next;
      indicator.textContent = next === "transcribing" ? "•••" : "";
      label.textContent = { idle: "mur", listening: "Listening", transcribing: "Transcribing…", done: "✓", error: lastError }[next];
      if (next !== "listening") preview.textContent = "";
    };
    await subscribe("state", payload => { receivedState = true; render(payload.state); });
    await subscribe("level", ({ rms }) => {
      if (state !== "listening") return;
      bars.forEach((bar, i) => bar.style.setProperty("--level", String(Math.max(0.12, Math.min(1, rms * 8 * [0.55, 0.85, 1, 0.75, 0.45][i])))));
    });
    await subscribe("partial", ({ text }) => { if (state === "listening") preview.textContent = text; });
    await subscribe("error", ({ message }) => { lastError = message; label.textContent = message; });
    await subscribe("config", applyConfig);
    const [config, status] = await Promise.all([call("get_config"), call("get_status")]);
    applyConfig(config); if (!receivedState) render(status.state);
  } else {
    root.innerHTML = '<section class="status-card"><h1>Murmur</h1><p>Local, private dictation · Phase 1 backend</p>' +
      '<p class="banner" hidden>Choose a speech model to start dictating</p>' +
      '<label class="theme-control">Theme <select aria-label="Theme"><option value="auto">Auto</option><option value="light">Light</option><option value="dark">Dark</option></select></label>' +
      '<pre aria-label="get_status JSON"></pre><p class="error" role="alert"></p>' +
      '<p>The full settings pages are outside phase 1. Configuration lives in config.toml.</p></section>';
    const output = root.querySelector("pre")!;
    const banner = root.querySelector<HTMLElement>(".banner")!;
    const error = root.querySelector<HTMLElement>(".error")!;
    const select = root.querySelector("select")!;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = async (): Promise<void> => {
      try { const status = await call("get_status"); output.textContent = JSON.stringify(status, null, 2);
        banner.hidden = status.model_available; error.textContent = status.error ?? "";
      } catch (e) { error.textContent = String(e); }
    };
    await subscribe("state", () => {
      if (timer !== undefined) return;
      timer = setTimeout(() => { timer = undefined; void refresh(); }, 250);
    });
    disposers.push(() => clearTimeout(timer));
    await subscribe("error", ({ message }) => { error.textContent = message; });
    await subscribe("config", config => { applyConfig(config); select.value = config.general.theme; });
    const config = await call("get_config"); applyConfig(config); select.value = config.general.theme;
    select.addEventListener("change", () => {
      void call("set_config", { patch: { general: { theme: select.value as Config["general"]["theme"] } } })
        .catch(e => { error.textContent = String(e); });
    });
    await refresh();
  }
}
void main().catch(error => {
  const root = document.querySelector("#app");
  if (root) root.textContent = `Murmur backend unavailable: ${String(error)}. Run with npm run tauri dev, not only Vite.`;
});
