import {el, feedback} from "./row";
export function hotkeyCapture(value: string, save: (value: string) => Promise<void>, optional = false): HTMLElement {
  const b = el("button", "hotkey"); b.type = "button"; let capturing = false;
  const {element,error} = feedback(b);
  const render = (): void => { b.replaceChildren(); if (!value) b.append(el("span", "hint", "Not set"));
    else value.split("+").forEach(key => b.append(el("kbd", "key-chip", key))); };
  render();
  b.addEventListener("click", () => { capturing = true; b.textContent = "Press a combination…"; error.textContent = "Esc cancels · Backspace clears"; });
  b.addEventListener("blur", () => { capturing = false; render(); });
  b.addEventListener("keydown", async event => {
    if (!capturing) return; event.preventDefault(); event.stopPropagation();
    if (event.key === "Escape") { capturing = false; error.textContent = ""; render(); return; }
    if (["Control", "Alt", "Shift", "Meta"].includes(event.key)) { error.textContent = "Add a non-modifier key."; return; }
    let next = "";
    if (event.key === "Backspace") { if (!optional) { error.textContent = "The primary shortcut cannot be empty."; return; } }
    else {
      const key = /^Key[A-Z]$/.test(event.code) ? event.code.slice(3) : /^Digit[0-9]$/.test(event.code) ? event.code.slice(5) : event.code;
      next = [event.ctrlKey ? "Ctrl" : "", event.altKey ? "Alt" : "", event.shiftKey ? "Shift" : "", event.metaKey ? "Super" : "", key].filter(Boolean).join("+");
    }
    capturing = false; b.disabled = true; error.textContent = "";
    try { await save(next); value = next; } catch(e) { error.textContent = String(e); }
    finally { b.disabled = false; render(); b.focus(); }
  }); return element;
}
