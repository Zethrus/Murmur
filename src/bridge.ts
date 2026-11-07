import { getVersion } from "@tauri-apps/api/app";
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

export const appVersion = (): Promise<string> => getVersion();
/** Native move; resolves after release/settling so the view can defer hiding. */
export const startOverlayDrag = (): Promise<void> => invoke("start_overlay_drag");

export type DictationState = "idle" | "listening" | "transcribing" | "done" | "error";
export interface Status {
  state: DictationState; model: string; device: string; compute: string;
  source?: "local" | "phone" | null;
  sidecar_alive: boolean; model_available: boolean; fallback_reason: string | null; error: string | null;
}
export type OverlayPosition = "top-center" | "bottom-center" | "custom";
export type IdleView = "icon" | "pill" | "hidden";
export interface OverlayPlacement {
  monitor_name: string; monitor_x: number; monitor_y: number; monitor_width: number; monitor_height: number;
  x_fraction: number; y_fraction: number;
}
export type CompanionVerb = "dictate_toggle" | "enhance" | "accept" | "dismiss" | "cancel" | "none";
export type CompanionTransport = "auto" | "usb" | "ble";
export interface CompanionButtons {
  ok: CompanionVerb; up: CompanionVerb; down: CompanionVerb; back: CompanionVerb;
  left: CompanionVerb; right: CompanionVerb;
  ok_long: CompanionVerb; up_long: CompanionVerb; down_long: CompanionVerb;
}
export interface CompanionConfig {
  enabled: boolean; ptt: boolean; transport: CompanionTransport; usb_port: string; ble_address: string;
  source: "auto" | "local" | "phone";
  network: { enabled: boolean; bind: string; port: number; mdns: boolean };
  buttons: CompanionButtons;
}
export interface CompanionNetworkStatus {
  enabled: boolean; fingerprint: string | null; error: string | null; rejected_peers: number; dropped_audio_frames: number;
  devices: { id: string; name: string; created_at: number; last_seen: number | null }[];
}
export interface CompanionPairing { code: string; expires_at: number }
/** Live link state, mirroring `companion::Status` in Rust. */
export interface CompanionStatus {
  enabled: boolean; connected: boolean; transport: string; port: string;
  device: string; protocol: number; last_button: string; error: string | null;
  rssi: number | null; battery: number | null; latency_ms: number;
}
export type EnhanceStyle = "concise" | "thorough" | "structured";
export interface EnhanceConfig {
  auto_rewrite: boolean; rewrite_hotkey: string; accept_hotkey: string; style: EnhanceStyle; model: string;
  /** Phase 1 accepts by copying only. */
  accept_mode: "clipboard";
  /** Opt-in: also rewrite code-style and terminal dictation. Default false. */
  rewrite_code: boolean;
  threads: number; unload_after_s: number; load_timeout_ms: number; timeout_ms: number;
}
export interface Config {
  general: { hotkey_code: string; hotkey: string; hotkey_mode: "hold" | "toggle"; language: string; translate: boolean;
    autostart: boolean; theme: "light" | "dark" | "auto" };
  overlay: { enabled: boolean; position: OverlayPosition; scale: number; show_preview: boolean; idle_view: IdleView; placement?: OverlayPlacement };
  sounds: { enabled: boolean; volume: number; muted: boolean };
  engine: { model: string; device: "auto" | "cpu" | "cuda"; compute_type: string; beam_size: number; vad_filter: boolean; dictionary: string[] };
  audio: { device: string; gain: number; silence_threshold: number; silence_timeout_ms: number; max_utterance_s: number; min_utterance_ms: number };
  injection: { prefer_paste: boolean; method: "auto" | "type" | "clipboard"; clipboard_only: boolean; trailing_space: boolean; capitalize: boolean; remove_fillers: boolean; exclude_apps: string[];
    /** What happens when focus changes between the dictation hotkey and delivery. */
    focus_change: FocusChange; insert_held_hotkey: string };
  postproc: { style: "prose" | "code" | "auto"; code_apps: string[]; spoken_punctuation: boolean; allow_newlines: boolean; replacements: Record<string, string>; fillers: string[]; remove_duplicate_words?: boolean };
  privacy: { history_enabled: boolean; history_retention_days: number };
  /** The Settings card tolerates a backend that omits this section (see cards/enhance.ts). */
  enhance: EnhanceConfig;
  /** Flipper Zero companion link. Defaults are applied by the backend. */
  companion: CompanionConfig;
}
export type ConfigPatch = { [K in keyof Config]?: Partial<Config[K]> };
export interface Model { name: string; repo: string; size_mb: number; accuracy: string; installed: boolean }
export interface Device { id: string; name: string; is_default: boolean }
export interface Transcript { text: string; language: string; duration_ms: number; infer_ms: number }
export interface HistoryEntry { id: number; ts: number; text: string; app: string; duration_ms: number; latency_ms: number; model: string }
export interface Download { name: string; phase: "starting" | "downloading" | "complete" | "error"; progress?: number; file?: string }

export type EnhanceInstallState = "not_installed" | "downloading" | "verifying" | "installed" | "removing" | "corrupt" | "unavailable_model" | "error";
export type EnhanceLoadState = "unloaded" | "loading" | "loaded" | "unavailable";
export type EnhanceDownloadPhase = "starting" | "downloading" | "verifying" | "complete" | "cancelled" | "error";
/** Operation, request, dictation and suggestion IDs are u64 values serialised as decimal strings. */
export interface EnhanceDownload {
  operation_id: string; model_id: string; phase: EnhanceDownloadPhase;
  received_bytes: number; total_bytes: number; error_code: string | null; message?: string | null;
}
export interface EnhanceStatus {
  revision: number; model_id: string; installation_state: EnhanceInstallState; load_state: EnhanceLoadState;
  backend_available: boolean; effective_threads?: number; request_id: string | null;
  error_code: string | null; message: string | null;
  /** Active install operation, if any, so a revisited Settings page can rehydrate it. */
  download?: EnhanceDownload | null;
}
export interface EnhanceModel {
  id: string; display_name: string; repo: string; revision: string; filename: string;
  size_bytes: number; sha256: string; installed: boolean;
}
export type EnhancementPhase = "none" | "loading" | "rewriting" | "ready" | "accepting" | "notice";
export interface Enhancement {
  phase: EnhancementPhase; dictation_id: string | null; suggestion_id: string | null;
  request_id?: string | null;
  original_text: string | null; proposed_text: string | null;
  acceptance_mode: "clipboard" | "unavailable"; message: string | null;
  /** Unix epoch milliseconds. */
  expires_at: number | null;
}
export type FocusChange = "hold" | "refocus" | "copy" | "insert";
/** Transcript held because focus changed during dictation, or a short notice. IDs are decimal strings. */
export interface HeldView {
  kind: "held" | "notice"; id: string | null; text: string | null; origin_app: string | null; message: string | null;
  /** Unix epoch milliseconds. */
  expires_at: number | null;
}
export interface OverlaySnapshot {
  revision: number; dictation_state: DictationState; delivery_busy: boolean; partial_text: string; enhancement: Enhancement;
  held?: HeldView | null;
}
export type RewriteStatus = "queued" | "revealed" | "no_dictation" | "busy" | "already_running" | "model_missing" | "code_style" | "unavailable" | "expired";
export interface RewriteReply { status: RewriteStatus; message?: string | null }
/** Phase 1 never replaces text in the target app; accept only copies. */
export interface AcceptReply { outcome: "copied" | "stale" | "failed"; message?: string | null }

export interface Events {
  state: { state: DictationState; source?: "local" | "phone" | null }; level: { rms: number }; partial: { text: string };
  latency: { latency_ms: number; text?: string }; error: { message: string; persistent?: boolean };
  download: Download; config: Config;
  "enhance-status": EnhanceStatus; "enhance-download": EnhanceDownload; "overlay-update": OverlaySnapshot;
  companion: CompanionStatus;
}
interface Commands {
  get_config: [undefined, Config]; set_config: [{ patch: ConfigPatch }, Config]; get_status: [undefined, Status];
  list_models: [undefined, Model[]]; install_model: [{ name: string }, { name: string; installed: boolean }];
  remove_model: [{ name: string }, { name: string; installed: boolean }]; test_engine: [undefined, Transcript];
  list_devices: [undefined, Device[]]; set_hotkey: [{ accel: string }, Config];
  preview_sound: [{ kind: "start" | "stop" | "error" }, void]; start_dictation: [undefined, void];
  stop_dictation: [undefined, void]; cancel_dictation: [undefined, void]; open_settings: [undefined, void];
  quit_app: [undefined, void]; history_search: [{ q: string; limit: number }, HistoryEntry[]];
  history_clear: [undefined, void]; pick_exclude_app: [undefined, string];
  open_path: [{ kind: "config" | "data" | "models" | "logs" | "history" }, void];
  apply_overlay_prefs: [undefined, void];
  // Enhance (contract: .cache/enhance-api-contract.txt). Tauri maps camelCase argument keys onto snake_case Rust parameters.
  get_enhance_status: [undefined, EnhanceStatus]; get_enhance_model: [undefined, EnhanceModel];
  install_enhance_model: [{ modelId: string; enableAfterInstall: boolean }, { operation_id: string }];
  cancel_enhance_download: [{ operationId: string }, void];
  remove_enhance_model: [{ modelId: string }, void];
  rewrite_last_dictation: [undefined, RewriteReply];
  accept_enhancement: [{ suggestionId: string }, AcceptReply];
  discard_enhancement: [{ suggestionId: string }, void];
  insert_held: [{ heldId: string }, void]; copy_held: [{ heldId: string }, void]; discard_held: [{ heldId: string }, void];
  get_overlay_snapshot: [undefined, OverlaySnapshot];
  reset_overlay_position: [undefined, void];
  set_overlay_size: [{ width: number; height: number; layoutRevision: number; layoutInstance: string }, void];
  // Flipper companion (see companion/protocol.md).
  companion_status: [undefined, CompanionStatus]; companion_resync: [undefined, void];
  companion_ports: [undefined, string[]];
  companion_network_status: [undefined, CompanionNetworkStatus];
  companion_pairing_code: [undefined, CompanionPairing];
  companion_revoke_device: [{id: string}, boolean];
}
export function call<K extends keyof Commands>(command: K,
  ...args: Commands[K][0] extends undefined ? [] : [Commands[K][0]]): Promise<Commands[K][1]> {
  return invoke<Commands[K][1]>(command, args[0]);
}
export function on<K extends keyof Events>(event: K, handler: (payload: Events[K]) => void): Promise<UnlistenFn> {
  return listen<Events[K]>(event, ({ payload }) => handler(payload));
}
