import {el, feedback} from "./row";
export function slider(value: number, min: number, max: number, step: number, save: (value: number) => Promise<void>, format = (n: number) => String(n), disabled = false): HTMLElement {
  const box = el("div", "slider-control"), input = el("input", "slider"), output = el("output", "numeric", format(value));
  input.type = "range"; input.min = String(min); input.max = String(max); input.step = String(step); input.value = String(value); input.disabled = disabled;
  const display = (): void => { output.value = format(Number(input.value)); input.setAttribute("aria-valuetext", output.value); };
  input.addEventListener("input", display); display(); box.append(input, output); const {element,error} = feedback(box);
  input.addEventListener("change", async () => { input.disabled = true; error.textContent = "";
    try { await save(Number(input.value)); value = Number(input.value); } catch(e) { input.value = String(value); display(); error.textContent = String(e); }
    finally { input.disabled = disabled; }
  }); return element;
}
