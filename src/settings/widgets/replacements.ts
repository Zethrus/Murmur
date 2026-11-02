import {el, button} from "./row";
export function replacements(values: Record<string,string>, save: (values: Record<string,string>) => Promise<void>): HTMLElement {
  const box = el("div","replacement-editor"), table = el("table","replacement-table"), head = el("thead"), tr = el("tr"), body = el("tbody"), error = el("p","field-error");
  ["Spoken phrase","Replace with",""].forEach(x=>tr.append(el("th","",x))); head.append(tr); table.append(head,body); error.setAttribute("role","alert");
  const addRow = (from = "", to = ""): void => {
    const line = el("tr"), left = el("td"), right = el("td"), action = el("td"), a = el("input","text-input"), b = el("input","text-input");
    a.value = from; b.value = to; a.setAttribute("aria-label","Spoken phrase"); b.setAttribute("aria-label","Replacement");
    a.placeholder = "mur mur"; b.placeholder = "Murmur"; left.append(a); right.append(b);
    action.append(button("×",()=>line.remove(),"icon-button")); action.firstElementChild?.setAttribute("aria-label","Remove replacement");
    line.append(left,right,action); body.append(line);
  };
  Object.entries(values).forEach(([a,b])=>addRow(a,b)); if (!Object.keys(values).length) addRow();
  const actions = el("div","button-group"); actions.append(button("Add rule",()=>addRow()),button("Save replacements",async()=>{
    const next: Record<string,string> = Object.create(null); const seen = new Set<string>(); error.textContent = "";
    for (const line of body.rows) { const [a,b] = line.querySelectorAll("input"); const key = a.value.trim();
      if (!key && !b.value) continue;
      if (!key || seen.has(key.toLowerCase())) { error.textContent = "Each phrase must be non-empty and unique."; return; }
      seen.add(key.toLowerCase()); next[key] = b.value;
    }
    try { await save(next); } catch(e) { error.textContent = String(e); }
  },"primary")); box.append(table,actions,error); return box;
}
