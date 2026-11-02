import {el} from "./row";
export function levelMeter(): {element: HTMLElement; update: (level: number) => void} {
  const element = el("div", "level-meter"); element.setAttribute("role", "meter"); element.setAttribute("aria-label", "Microphone level"); element.setAttribute("aria-valuemin", "0"); element.setAttribute("aria-valuemax", "100");
  const bars = Array.from({length:24}, () => el("i")); element.append(...bars);
  const update = (level: number): void => { const value = Math.max(0, Math.min(1, level * 5)); element.setAttribute("aria-valuenow", String(Math.round(value * 100)));
    bars.forEach((bar,i) => bar.classList.toggle("active", i < Math.round(value * bars.length))); };
  update(0); return {element,update};
}
