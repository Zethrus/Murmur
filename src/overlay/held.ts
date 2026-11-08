import {call, type HeldView} from "../bridge";
import {pressWithoutFocus} from "./no-focus";

/** Review panel for a transcript held back because focus changed during dictation. */
export interface HeldPanel {
  readonly element: HTMLElement;
  /** Apply the backend view; `busy` is true while a delivery is in flight. */
  update(view: HeldView | null, busy: boolean): void;
  /** True when the backend holds text or a notice, whether or not the panel is shown. */
  active(): boolean;
  /** Show or hide the panel (hidden while dictating). */
  show(visible: boolean): void;
  visible(): boolean;
}

type Command = "insert_held" | "copy_held" | "discard_held";

function node<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text = ""): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag); element.className = className; element.textContent = text; return element;
}

export function heldPanel(): HeldPanel {
  const element = node("div","held"), title = node("p","held-title","Not inserted: focus changed"), origin = node("p","held-origin");
  const text = node("p","held-text"), message = node("p","held-message"), actions = node("div","held-actions");
  const insert = node("button","overlay-button primary held-insert","Insert here"), copy = node("button","overlay-button held-copy","Copy");
  const discard = node("button","overlay-button held-discard","Discard");
  for (const button of [insert,copy,discard]) button.setAttribute("type","button");
  pressWithoutFocus(insert,copy,discard);
  actions.append(insert,copy,discard); element.append(title,origin,text,message,actions); element.hidden = true;
  let view: HeldView | null = null, busy = false, acting = false, shown = false, localMessage = "";

  const render = (): void => {
    const held = view?.kind === "held";
    element.hidden = !shown || !view; element.dataset.kind = view?.kind ?? "none";
    title.hidden = !held; actions.hidden = !held;
    origin.textContent = held ? (view?.origin_app ? `Started in ${view.origin_app}. Focus where it belongs, then insert.` : "Started outside a known window. Focus where it belongs, then insert.") : "";
    origin.hidden = !origin.textContent;
    text.textContent = held ? view?.text ?? "" : ""; text.hidden = !text.textContent;
    message.textContent = localMessage || view?.message || ""; message.hidden = !message.textContent;
    for (const button of [insert,copy,discard]) button.disabled = busy || acting;
  };
  const act = async (command: Command): Promise<void> => {
    const id = view?.kind === "held" ? view.id : null;
    if (!id || busy || acting) return;
    acting = true; localMessage = ""; render();
    try { await call(command,{heldId:id}); }
    catch (error) { if (view?.id === id) localMessage = String(error); }
    finally { acting = false; render(); }
  };
  insert.addEventListener("click",()=>act("insert_held"));
  copy.addEventListener("click",()=>act("copy_held"));
  discard.addEventListener("click",()=>act("discard_held"));

  return {
    element,
    update(next, nextBusy) {
      if (next?.id !== view?.id || next?.kind !== view?.kind) { localMessage = ""; acting = false; }
      view = next; busy = nextBusy; render();
    },
    active: () => view !== null,
    show(visible) { shown = visible; render(); },
    visible: () => shown && view !== null,
  };
}
