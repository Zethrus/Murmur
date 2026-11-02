import {el} from "./row";
export function progress(label: string): {element: HTMLElement; update: (value?: number, detail?: string) => void} {
  const element = el("div", "progress-block"), meter = el("progress", "progress"), text = el("span", "hint", label);
  meter.max = 1; meter.setAttribute("aria-label", label); element.append(meter,text);
  return {element, update(value, detail) { if (value === undefined) meter.removeAttribute("value"); else meter.value = Math.max(0,Math.min(1,value));
    text.textContent = detail ?? (value === undefined ? label : `${Math.round(value * 100)}%`); }};
}
