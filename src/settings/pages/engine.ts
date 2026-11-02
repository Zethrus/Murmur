import {call, type Model} from "../../bridge";
import type {PageContext} from "../context";
import {controls, setter} from "../controls";
import {card, row, stack, el, button} from "../widgets/row";
import {listEditor} from "../widgets/list-editor";
import {progress} from "../widgets/progress";
export function engine(ctx: PageContext): HTMLElement {
  const c = ctx.config, e = controls(ctx,"engine"), catalog = el("div","model-grid"), notice = el("p","hint","Loading your model library…");
  const download = progress("Waiting for download"), testResult = el("p","test-result","Not tested in this session."); download.element.hidden = true;
  let live = true, busy = false; ctx.cleanup(()=>{live = false;});
  const fail = (error: unknown): void => { notice.textContent = String(error); notice.className = "field-error"; };
  const load = async (): Promise<void> => {
    try { const models = await call("list_models"); if (!live) return; catalog.replaceChildren();
      notice.className = "hint"; notice.textContent = models.some(m=>m.installed) ? "Models stay on this device. Downloads begin only when you choose Install." : "No models installed. Choose a speech model to start dictating.";
      models.forEach(model=>catalog.append(modelCard(model)));
    } catch(error) { if (live) { catalog.replaceChildren(); fail(`Model library unavailable. ${String(error)}`); } }
  };
  const modelCard = (model: Model): HTMLElement => {
    const box = el("article","model-card"), title = el("h3","",model.name), tag = el("span","badge",model.installed ? (model.name === c.engine.model ? "Selected" : "Installed") : "Not installed");
    box.dataset.selected = String(model.installed && model.name === c.engine.model);
    const actions = el("div","button-group"), action = async (kind: "install" | "remove"): Promise<void> => {
      if (busy) return;
      if (kind === "remove" && !window.confirm(`Remove ${model.name} from this device? You can download it again later.`)) return;
      busy = true; catalog.querySelectorAll("button").forEach(b=>b.disabled = true); notice.textContent = "";
      if (kind === "install") { download.element.hidden = false; download.update(undefined,`Preparing ${model.name}…`); }
      try { if (kind === "install") await call("install_model",{name:model.name}); else await call("remove_model",{name:model.name});
        if (live) { await load(); if (kind === "install") download.update(1,`${model.name} is ready. Choose Use model to select it.`); }
      } catch(error) { if (live) { await load(); fail(error); download.element.hidden = true; } }
      finally { busy = false; }
    };
    if (model.installed) {
      const use = button(model.name === c.engine.model ? "In use" : "Use model",()=>ctx.save({engine:{model:model.name}}).catch(ctx.report),"primary");
      use.disabled = model.name === c.engine.model || !ctx.status.sidecar_alive;
      const remove = button("Remove",()=>action("remove"),"quiet danger"); remove.disabled = ctx.status.state !== "idle" || !ctx.status.sidecar_alive; actions.append(use,remove);
    } else { const install = button("Install",()=>action("install"),"primary"); install.disabled = !ctx.status.sidecar_alive; actions.append(install); }
    box.append(tag,title,el("p","hint",`${model.size_mb.toLocaleString()} MB · ${model.accuracy} accuracy`),actions); return box;
  };
  ctx.subscribe("download",event=>{ if (!live) return; download.element.hidden = false;
    download.update(event.progress,`${event.name} · ${event.phase}${event.file ? ` · ${event.file}` : ""}${event.progress === undefined ? "" : ` · ${Math.round(event.progress*100)}%`}`);
    if (event.phase === "error") fail("Download failed. Check your connection and retry Install.");
  });
  void load();
  const test = button("Test 3 s",async()=>{
    testResult.textContent = "Running a local 3-second inference probe…";
    try { const start = performance.now(), result = await call("test_engine"); if (live) testResult.textContent = `${Math.round(result.infer_ms)} ms inference · ${Math.round(performance.now()-start)} ms round trip`; }
    catch(error) { if (live) testResult.textContent = String(error); }
  }); test.disabled = !ctx.status.sidecar_alive || !ctx.status.model_available || ctx.status.state !== "idle";
  return stack(card("Your speech model",el("p","card-note","Small models feel fast. Larger models hear technical vocabulary more accurately."),notice,catalog,download.element,button("Refresh library",load)),
    card("Local inference",
      e.select("device","Compute device","Auto tries CUDA, then falls back to CPU.",[["auto","Automatic"],["cuda","NVIDIA CUDA"],["cpu","CPU"]]),
      row("Detected hardware","Resolved by the local sidecar; may stay unloaded until first use.",el("span","status-value",ctx.status.sidecar_alive ? `${ctx.status.device} · ${ctx.status.compute}` : "Sidecar offline")),
      e.select("compute_type","Precision","Auto chooses an efficient precision for your hardware.",["auto","int8","int8_float16","float16","float32","int8_float32"]),
      e.slider("beam_size","Beam size","Higher values trade speed for recognition accuracy.",1,10,1),
      e.toggle("vad_filter","Voice activity filter","Skip silent regions before speech recognition."),
      row("Engine check","A silent, three-second probe; not a microphone recording. First use may warm the model.",stack(test,testResult))),
    card("Words worth knowing",row("Dictionary","Seeded with coding vocabulary. These words bias recognition; they are not automatic replacements.",listEditor(c.engine.dictionary,setter(ctx,"engine","dictionary"),"dictionary word"))));
}
