import {el, feedback} from "./row";
export type Option = string | readonly [string, string];
export function select(value: string, options: readonly Option[], save: (value: string) => Promise<void>, disabled = false): HTMLElement {
  const input = el("select", "select");
  const choices = options.map(o => typeof o === "string" ? [o,o] : o);
  if (!choices.some(o => o[0] === value)) choices.unshift([value, `${value || "None"} (unavailable)`]);
  choices.forEach(([key,label]) => { const option = el("option", "", label); option.value = key; input.append(option); });
  input.value = value; input.disabled = disabled; const {element,error} = feedback(input);
  input.addEventListener("change", async () => { input.disabled = true; error.textContent = "";
    try { await save(input.value); value = input.value; } catch(e) { input.value = value; error.textContent = String(e); }
    finally { input.disabled = disabled; }
  }); return element;
}
