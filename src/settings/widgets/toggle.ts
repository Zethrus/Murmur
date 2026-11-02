import {el, feedback} from "./row";
export function toggle(value: boolean, save: (value: boolean) => Promise<void>, disabled = false): HTMLElement {
  const input = el("input", "toggle"); input.type = "checkbox"; input.setAttribute("role", "switch"); input.checked = value; input.disabled = disabled;
  const {element, error} = feedback(input);
  input.addEventListener("change", async () => { input.disabled = true; error.textContent = "";
    try { await save(input.checked); value = input.checked; } catch(e) { input.checked = value; error.textContent = String(e); }
    finally { input.disabled = disabled; }
  }); return element;
}
