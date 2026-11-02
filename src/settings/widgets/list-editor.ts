import {el, button} from "./row";
export function listEditor(values: string[], save: (values: string[]) => Promise<void>, label: string): HTMLElement {
  const box = el("div", "list-editor"), chips = el("div", "chips"), form = el("form", "inline-form"), input = el("input", "text-input"), error = el("p", "field-error");
  error.setAttribute("role", "alert"); input.placeholder = `Add ${label.toLowerCase()}…`; input.setAttribute("aria-label", `Add ${label}`);
  const commit = async (next: string[]): Promise<void> => { error.textContent = "";
    box.querySelectorAll<HTMLInputElement | HTMLButtonElement>("input,button").forEach(e => e.disabled = true);
    try { await save(next); values = next; render(); input.value = ""; } catch(e) { error.textContent = String(e); }
    finally { box.querySelectorAll<HTMLInputElement | HTMLButtonElement>("input,button").forEach(e => e.disabled = false); }
  };
  const render = (): void => { chips.replaceChildren(); if (!values.length) chips.append(el("p", "empty", "No entries yet."));
    values.forEach((value,index) => { const chip = el("span", "chip"), remove = button("×", () => commit(values.filter((_,i) => i !== index)), "icon-button");
      remove.setAttribute("aria-label", `Remove ${value}`); chip.append(el("span", "", value), remove); chips.append(chip); }); };
  const add = el("button", "button secondary", "Add"); add.type = "submit";
  form.addEventListener("submit", event => { event.preventDefault(); const v = input.value.trim(); if (!v) { error.textContent = "Enter a value first."; return; }
    if (values.some(x => x.toLowerCase() === v.toLowerCase())) { error.textContent = "Already in the list."; return; } void commit([...values,v]); });
  form.append(input,add); box.append(chips,form,error); render(); return box;
}
