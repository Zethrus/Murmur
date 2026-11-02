import type { Config, ConfigPatch, Events, Status } from "../bridge";
export interface PageContext {
  config: Config;
  status: Status;
  lastTranscript: string;
  save(patch: ConfigPatch): Promise<void>;
  report(error: unknown): void;
  subscribe<K extends keyof Events>(event: K, handler: (data: Events[K]) => void): void;
  cleanup(dispose: () => void): void;
}
