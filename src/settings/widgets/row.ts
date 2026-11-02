export function el<K extends keyof HTMLElementTagNameMap>(tag: K, className = "", text = ""): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag); node.className = className; node.textContent = text; return node;
}
export function stack(...nodes: HTMLElement[]): HTMLElement { const box = el("div", "stack"); box.append(...nodes); return box; }
export function row(title: string, hint: string, control: HTMLElement): HTMLElement {
  const box = el("div", "setting-row"), copy = el("div", "row-copy"), label = el("label", "row-label", title);
  const id = `field-${title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  const input = control.matches("input,select,button,textarea") ? control : control.querySelector<HTMLElement>("input,select,button,textarea");
  if (input) { input.id = id; label.htmlFor = id; input.setAttribute("aria-describedby", `${id}-hint`); }
  const description = el("p", "hint", hint); description.id = `${id}-hint`;
  copy.append(label, description); control.classList.add("row-control"); box.append(copy, control); return box;
}
export function card(title: string, ...rows: HTMLElement[]): HTMLElement {
  const section = el("section", "card"), heading = el("h2", "card-title", title); section.append(heading, ...rows); return section;
}
export function button(label: string, action: () => Promise<unknown> | void, kind = "secondary"): HTMLButtonElement {
  const b = el("button", `button ${kind}`, label); b.type = "button";
  b.addEventListener("click", async () => { b.disabled = true; b.setAttribute("aria-busy", "true");
    try { await action(); } finally { b.disabled = false; b.removeAttribute("aria-busy"); }
  }); return b;
}
export function feedback(control: HTMLElement): { element: HTMLElement; error: HTMLElement } {
  const error = el("p", "field-error"); error.setAttribute("role", "alert");
  const element = el("div", "field"); element.append(control, error); return { element, error };
}
export function textField(value: string, save: (value: string) => Promise<void>, placeholder = ""): HTMLElement {
  const input = el("input", "text-input"); input.type = "text"; input.value = value; input.placeholder = placeholder;
  const {element, error} = feedback(input);
  input.addEventListener("change", async () => { input.disabled = true; error.textContent = "";
    try { await save(input.value.trim()); value = input.value; } catch (e) { input.value = value; error.textContent = String(e); }
    finally { input.disabled = false; }
  }); return element;
}
