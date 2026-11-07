import {call, type ComponentInfo, type ComponentJob, type ComponentsSnapshot} from "../../bridge";
import {el, button} from "./row";
import {progress} from "./progress";

/** Decimal units, matching what download tools and file managers show. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "unknown";
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`;
  if (bytes >= 1e6) return `${Math.round(bytes / 1e6).toLocaleString()} MB`;
  if (bytes >= 1e3) return `${Math.round(bytes / 1e3)} KB`;
  return `${bytes} bytes`;
}
export function formatEta(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "";
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} s left`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min left`;
  return `${(seconds / 3600).toFixed(1)} h left`;
}
const STATE: Record<ComponentInfo["state"], string> = {
  available: "Not installed", queued: "Queued", installing: "Installing", installed: "Installed", active: "Active",
  paused: "Paused", failed: "Failed", unverified: "Unverified", corrupt: "Damaged",
};
export const stateLabel = (state: ComponentInfo["state"]): string => STATE[state];
const PHASE: Record<ComponentJob["phase"], string> = {
  queued: "Waiting", downloading: "Downloading", verifying: "Verifying SHA-256", extracting: "Unpacking",
  testing: "Testing", activating: "Activating", paused: "Paused", failed: "Failed",
};
export function jobText(job: ComponentJob): string {
  const parts = [PHASE[job.phase]];
  if (job.bytes_total > 0) parts.push(`${formatBytes(job.bytes_done)} of ${formatBytes(job.bytes_total)}`);
  if (job.phase === "downloading" && job.rate > 0) parts.push(`${formatBytes(job.rate)}/s`, formatEta(job.eta));
  return parts.filter(Boolean).join(" · ");
}
export function sizes(info: ComponentInfo): string {
  return `${formatBytes(info.size_bytes)} download · ${formatBytes(info.installed_size_bytes)} on disk`;
}

export interface RowHooks {
  /** Report an action's error near the row. */
  fail(message: string): void;
  /** Extra buttons for this component (e.g. "Use model"). */
  extra?(info: ComponentInfo): HTMLElement[];
  /** Installs (and resuming them) are refused while the trust root or manifest is unavailable or expired. */
  canInstall: boolean;
}

/** One component: name, state, sizes, live progress and the actions its state allows. */
export function componentRow(info: ComponentInfo, hooks: RowHooks): {element: HTMLElement; progress(job: ComponentJob): void} {
  const box = el("article", "component-row"), head = el("div", "component-head"), title = el("h3", "component-title", info.display_name);
  const badge = el("span", "badge", stateLabel(info.state)); badge.dataset.state = info.state;
  box.dataset.component = info.id; box.dataset.state = info.state;
  head.append(title, badge);
  const meta = el("p", "hint", [info.installed_version ? `Version ${info.installed_version}` : `Version ${info.version}`,
    info.update_available ? `update ${info.version} available` : "", info.size_bytes ? sizes(info) : ""].filter(Boolean).join(" · "));
  const description = el("p", "hint", info.description);
  const bar = progress(`${info.display_name} progress`); bar.element.hidden = !info.job || info.job.phase === "paused" || info.job.phase === "failed";
  const error = el("p", "field-error"); error.setAttribute("role", "alert");
  if (info.job?.error) error.textContent = info.job.error;
  const note = el("p", "hint");
  if (info.state === "unverified") note.textContent = `Found an earlier download${info.legacy_path ? ` at ${info.legacy_path}` : ""}. Its files do not match the signed manifest, so Murmur does not load it. Nothing was deleted; Reinstall downloads a verified copy.`;
  if (info.state === "corrupt") note.textContent = "Installed files are missing or changed. Reinstall to repair, or remove.";
  if (info.migrated) note.textContent = "Adopted from an earlier version after its SHA-256 matched the signed manifest.";
  const actions = el("div", "button-group");
  const act = (label: string, run: () => Promise<unknown>, kind = "secondary", aria = `${label} ${info.display_name}`): HTMLButtonElement => {
    const b = button(label, async () => { error.textContent = ""; try { await run(); } catch (e) { error.textContent = String(e); hooks.fail(String(e)); } }, kind);
    b.setAttribute("aria-label", aria); return b;
  };
  const install = (label: string): HTMLButtonElement => { const b = act(label, () => call("components_install", {ids: [info.id]}), "primary"); b.disabled = !hooks.canInstall; return b; };
  const remove = act("Remove", async () => {
    const refusal = info.in_use ?? (info.dependents.length ? `${info.dependents.join(", ")} needs this component. Remove it first.` : null);
    if (refusal) { error.textContent = refusal; return; }
    if (!window.confirm(`Remove ${info.display_name} from this device? You can download it again later.`)) return;
    await call("components_remove", {id: info.id});
  }, "quiet danger");
  switch (info.state) {
    case "available": actions.append(install("Install")); break;
    case "queued": case "installing":
      actions.append(act("Pause", () => call("components_stop", {id: info.id, pause: true})), act("Cancel", () => call("components_stop", {id: info.id, pause: false}), "quiet danger")); break;
    case "paused": case "failed": {
      const resume = act(info.state === "paused" ? "Resume" : "Retry", () => call("components_resume", {id: info.id}), "primary"); resume.disabled = !hooks.canInstall;
      actions.append(resume, act("Discard", () => call("components_discard", {id: info.id}), "quiet danger")); break;
    }
    case "unverified": actions.append(install("Reinstall")); break;
    case "corrupt": actions.append(install("Reinstall"), remove); break;
    default:
      actions.append(...(hooks.extra?.(info) ?? []));
      if (info.update_available) actions.append(install("Update"));
      actions.append(act("Verify", async () => { const {verdict} = await call("components_verify", {id: info.id});
        error.textContent = verdict === "verified" ? "" : "Verification failed: reinstall this component."; note.textContent = verdict === "verified" ? "Files verified." : note.textContent; }), remove);
  }
  box.append(head, description, meta, bar.element, note, error, actions);
  const update = (job: ComponentJob): void => {
    const active = job.phase !== "paused" && job.phase !== "failed";
    bar.element.hidden = !active;
    bar.update(job.bytes_total ? job.bytes_done / job.bytes_total : undefined, jobText(job));
    if (job.error) error.textContent = job.error;
  };
  if (info.job) update(info.job);
  return {element: box, progress: update};
}

export const STALE_NOTICE = "Component list expired — Check for updates. Installed components keep working and can be verified or removed; nothing new is installed until the list is renewed.";
/** Why installs are unavailable, in words a user can act on. */
export function trustNotice(snapshot: ComponentsSnapshot): string | null {
  if (snapshot.trust.ok) return snapshot.stale ? STALE_NOTICE : null;
  const reason = snapshot.trust.reason;
  if (reason === "trust_root_missing") return "This build has no release signing key, so it cannot verify or install components. Dictation needs the speech engine and a speech model; use an official Murmur release.";
  if (reason === "primary_revoked") return snapshot.trust.message ?? "Murmur's release key was revoked. Installs are disabled.";
  if (reason === "clock_error") return snapshot.trust.message ?? "Check the system clock.";
  return `${snapshot.trust.message ?? "No verified component list is available."} Use Check for updates to download the signed list.`;
}

export const needs = "Dictation needs the speech engine and one speech model. GPU acceleration is optional (NVIDIA only). Enhance needs its own model and is optional.";
