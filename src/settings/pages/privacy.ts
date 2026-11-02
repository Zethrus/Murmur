import {call} from "../../bridge";
import type {PageContext} from "../context";
import {controls} from "../controls";
import {card, row, stack, el, button} from "../widgets/row";
export function privacy(ctx: PageContext): HTMLElement {
  const p = controls(ctx,"privacy"), search = el("input","text-input"), results = el("div","history-list"), status = el("p","hint","Loading history…");
  search.type = "search"; search.placeholder = "Search your transcripts…"; search.setAttribute("aria-label","Search history");
  let sequence = 0, live = true, timer: ReturnType<typeof setTimeout> | undefined;
  ctx.cleanup(()=>{live = false; clearTimeout(timer);});
  const load = async (): Promise<void> => { const current = ++sequence;
    try { const entries = await call("history_search",{q:search.value,limit:100}); if (!live || current !== sequence) return;
      results.replaceChildren(); status.textContent = entries.length ? `Showing ${entries.length}${entries.length === 100 ? " most recent matching" : ""} entries` : search.value ? "No matching transcripts." : "No history yet. History is off by default.";
      entries.forEach(entry=>{ const item = el("article","history-entry"), meta = el("p","hint",`${new Date(entry.ts*1000).toLocaleString()} · ${entry.app || "Unknown app"} · ${entry.latency_ms} ms`);
        item.append(meta,el("p","history-text",entry.text)); results.append(item); });
    } catch(e) { if (live && current === sequence) status.textContent = `History unavailable: ${String(e)}`; }
  };
  search.addEventListener("input",()=>{clearTimeout(timer); timer = setTimeout(()=>void load(),250);});
  const clear = button("Delete all history",async()=>{ if (!window.confirm("Permanently delete all saved transcripts? This cannot be undone.")) return;
    try { await call("history_clear"); await load(); } catch(e) { ctx.report(e); }
  },"danger"); void load();
  const folders = card("On this device");
  const paths = [
    ["config","Configuration","Linux: $XDG_CONFIG_HOME/murmur (default ~/.config/murmur) · Windows: %APPDATA%/Murmur"],
    ["models","Speech models","Stored in the models folder under local app data."],
    ["data","App data","Linux: $XDG_DATA_HOME/murmur (default ~/.local/share/murmur) · Windows: %LOCALAPPDATA%/Murmur"],
    ["logs","Diagnostic logs","murmur.log in app data. Transcript text is excluded unless debug logging is explicitly enabled."],
    ["history","Transcript history","history.sqlite in app data. Only saved when you opt in."]
  ] as const;
  for (const [kind,title,hint] of paths) folders.append(row(title,hint,button("Open folder",()=>call("open_path",{kind}).catch(ctx.report))));
  return stack(card("Your voice stays yours",el("p","privacy-statement","Local by design. No accounts. No telemetry. No cloud transcription."),
    el("p","card-note","Audio is processed on this device and is never saved to disk. The only network access is an explicit model download. History is optional, and you can remove it at any time.")),
    card("Keep only what you choose",p.toggle("history_enabled","Save transcript history","Opt in to a searchable local record. Turning this off stops new entries; delete existing ones below."),
      p.slider("history_retention_days","Retention","Older entries are pruned on startup and daily.",1,3650,1,n=>`${n} days`,!ctx.config.privacy.history_enabled)),
    card("Transcript history",search,status,results,clear),folders);
}
