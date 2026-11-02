import { getVersion } from "@tauri-apps/api/app";
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

export const appVersion = (): Promise<string> => getVersion();

export type DictationState = "idle" | "listening" | "transcribing" | "done" | "error";
export interface Status {
  state: DictationState; model: string; device: string; compute: string;
  sidecar_alive: boolean; model_available: boolean; error: string | null;
}
export interface Config {
  general: { hotkey_code: string; hotkey: string; hotkey_mode: "hold" | "toggle"; language: string; translate: boolean;
    autostart: boolean; theme: "light" | "dark" | "auto" };
  overlay: { enabled: boolean; position: "top-center" | "bottom-center"; scale: number; show_preview: boolean; idle_hide: boolean };
  sounds: { enabled: boolean; volume: number; muted: boolean };
  engine: { model: string; device: "auto" | "cpu" | "cuda"; compute_type: string; beam_size: number; vad_filter: boolean; dictionary: string[] };
  audio: { device: string; gain: number; silence_threshold: number; silence_timeout_ms: number; max_utterance_s: number; min_utterance_ms: number };
  injection: { prefer_paste: boolean; method: "auto" | "type" | "clipboard"; clipboard_only: boolean; trailing_space: boolean; capitalize: boolean; remove_fillers: boolean; exclude_apps: string[] };
  postproc: { style: "prose" | "code" | "auto"; code_apps: string[]; spoken_punctuation: boolean; allow_newlines: boolean; replacements: Record<string, string>; fillers: string[] };
  privacy: { history_enabled: boolean; history_retention_days: number };
}
export type ConfigPatch = { [K in keyof Config]?: Partial<Config[K]> };
export interface Model { name: string; repo: string; size_mb: number; accuracy: string; installed: boolean }
export interface Device { id: string; name: string; is_default: boolean }
export interface Transcript { text: string; language: string; duration_ms: number; infer_ms: number }
export interface HistoryEntry { id: number; ts: number; text: string; app: string; duration_ms: number; latency_ms: number; model: string }
export interface Download { name: string; phase: "starting" | "downloading" | "complete" | "error"; progress?: number; file?: string }
export interface Events {
  state: { state: DictationState }; level: { rms: number }; partial: { text: string };
  latency: { latency_ms: number; text?: string }; error: { message: string; persistent?: boolean };
  download: Download; config: Config;
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
}
export function call<K extends keyof Commands>(command: K,
  ...args: Commands[K][0] extends undefined ? [] : [Commands[K][0]]): Promise<Commands[K][1]> {
  return invoke<Commands[K][1]>(command, args[0]);
}
export function on<K extends keyof Events>(event: K, handler: (payload: Events[K]) => void): Promise<UnlistenFn> {
  return listen<Events[K]>(event, ({ payload }) => handler(payload));
}
