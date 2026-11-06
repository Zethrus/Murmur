import {call} from "../../bridge";
import type {PageContext} from "../context";
import {controls, setter} from "../controls";
import {card, row, stack, el, button} from "../widgets/row";
import {listEditor} from "../widgets/list-editor";
import {hotkeyCapture} from "../widgets/hotkey-capture";
import {replacements} from "../widgets/replacements";
import {enhanceCard} from "../cards/enhance";
export function behavior(ctx: PageContext): HTMLElement {
  const c = ctx.config, p = controls(ctx,"postproc"), i = controls(ctx,"injection"), transcript = el("pre","transcript",ctx.lastTranscript || "Your next completed dictation will appear here. Nothing is saved unless history is enabled.");
  ctx.subscribe("latency",event=>{ if (event.text !== undefined) transcript.textContent = event.text; });
  const exclusions = stack(listEditor(c.injection.exclude_apps,setter(ctx,"injection","exclude_apps"),"app class"),button("Pick a focused app…",async()=>{
    try { const app = await call("pick_exclude_app"); if (app && !c.injection.exclude_apps.includes(app)) await setter(ctx,"injection","exclude_apps")([...c.injection.exclude_apps,app]); } catch(e) { ctx.report(e); }
  }));
  return stack(card("Speak in your own style",
    p.select("style","Dictation style","Auto uses code style in the apps below; prose everywhere else.",[["auto","Auto · by focused app"],["prose","Prose"],["code","Code"]]),
    row("Code apps","Case-insensitive window-class matches. Code style never capitalises identifiers.",listEditor(c.postproc.code_apps,setter(ctx,"postproc","code_apps"),"code app")),
    p.toggle("spoken_punctuation","Spoken punctuation","Say “comma”, “double equals”, “quote … unquote” and more."),
    p.toggle("allow_newlines","Allow newlines","Say “newline” or “next line”. Terminals always receive a single paragraph.")),
    card("Put words where you need them",
      i.select("method","Injection method","Auto types short ASCII prose and pastes other text.",[["auto","Automatic"],["type","Type text"],["clipboard","Clipboard paste"]]),
      i.toggle("prefer_paste","Prefer paste for code","Overrides typing for code style or text longer than 200 characters."),
      i.toggle("clipboard_only","Clipboard only","Copy without typing or pasting into the focused app."),
      row("Excluded apps","Matched apps receive clipboard-only output. The picker hides Settings for 3 seconds; focus your target app.",exclusions),
      i.select("focus_change","When focus changes","If you switch windows between pressing the shortcut and delivery. Tabs inside one window count as the same window.",[["hold","Hold for review in the overlay"],["refocus","Return to the original window, then insert"],["copy","Copy to the clipboard"],["insert","Insert into the current window"]]),
      row("Insert held text shortcut","Optional. Types held text into the window focused when you press it.",hotkeyCapture(c.injection.insert_held_hotkey,setter(ctx,"injection","insert_held_hotkey"),true))),
    card("Finishing touches",
      i.toggle("trailing_space","Trailing space","Leave one space after a completed dictation."),
      i.toggle("capitalize","Capitalise sentences","Prose only. Identifiers and mixed-case tokens stay untouched.",c.postproc.style === "code"),
      i.toggle("remove_fillers","Remove fillers","Only removes the complete words listed below, including in code style."),
      row("Filler words","Edit the words removed when filler removal is enabled.",listEditor(c.postproc.fillers,setter(ctx,"postproc","fillers"),"filler word")),
      ...(c.postproc.remove_duplicate_words === undefined ? [] : [p.toggle("remove_duplicate_words","Remove repeated words","Prose only. “the the” becomes “the”; deliberate pairs such as “had had” stay.")]),
      row("Replacements","Case-insensitive, longest phrase first. Protected code tokens are never rewritten.",replacements(c.postproc.replacements,setter(ctx,"postproc","replacements")))),
    enhanceCard(ctx),
    card("Last transcript",el("p","card-note","Original delivery, exactly as sent to your app. Rewrite suggestions appear only in the overlay."),transcript));
}
