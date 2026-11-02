import type {Config, ConfigPatch} from "../bridge";
import type {PageContext} from "./context";
import {row} from "./widgets/row";
import {toggle} from "./widgets/toggle";
import {select, type Option} from "./widgets/select";
import {slider} from "./widgets/slider";
export function setter<S extends keyof Config, K extends keyof Config[S]>(ctx: PageContext, section: S, key: K): (v: Config[S][K]) => Promise<void> {
  return value => ctx.save({[section]: {[key]:value}} as ConfigPatch);
}
export function controls<S extends keyof Config>(ctx: PageContext, section: S) {
  const values = ctx.config[section];
  return {
    toggle<K extends keyof Config[S]>(key: K, title: string, hint: string, disabled = false): HTMLElement {
      return row(title,hint,toggle(Boolean(values[key]), v => setter(ctx,section,key)(v as Config[S][K]),disabled));
    },
    select<K extends keyof Config[S]>(key: K, title: string, hint: string, options: readonly Option[], disabled = false): HTMLElement {
      return row(title,hint,select(String(values[key]),options,v => setter(ctx,section,key)(v as Config[S][K]),disabled));
    },
    slider<K extends keyof Config[S]>(key: K, title: string, hint: string, min: number, max: number, step: number, format?: (n:number)=>string, disabled = false): HTMLElement {
      return row(title,hint,slider(Number(values[key]),min,max,step,v => setter(ctx,section,key)(v as Config[S][K]),format,disabled));
    }
  };
}
