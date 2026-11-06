import test from "node:test";
import assert from "node:assert/strict";
import {harness,settle} from "./ui-harness.mjs";
const plain = value => JSON.parse(JSON.stringify(value)); // args cross a vm realm
const button = (root,label) => root.querySelectorAll("button").find(b=>b.textContent===label||b.dataset.page===label);
async function settings(overrides){const h=await harness(overrides);await (await h.module("settings/app.ts")).mountSettings(h.root);await settle();return h;}
test("settings mounts six pages, status/version, and releases all listeners",async()=>{
  const h=await settings();assert.equal(h.root.querySelectorAll(".nav-button").length,6);assert.match(h.root.textContent,/Version 0.1.0/);
  for(const page of ["General","Engine","Audio","Behavior","Privacy","Companion"]){await button(h.root,page).dispatch("click");await settle();assert.equal(h.root.querySelector("h1").textContent,page);assert.ok(h.root.querySelectorAll(".card").length>=2);}
  h.unload();assert.equal([...h.events.values()].reduce((n,s)=>n+s.size,0),0);
});
test("general saves coding hotkey and theme through set_config",async()=>{
  const h=await settings(),capture=h.document.getElementById("field-code-shortcut");await capture.dispatch("click");await capture.dispatch("keydown",{key:"K",code:"KeyK",ctrlKey:true,altKey:true});
  assert.equal(h.config.general.hotkey_code,"Ctrl+Alt+K");const theme=h.document.getElementById("field-appearance");theme.value="dark";await theme.dispatch("change");
  assert.equal(h.document.documentElement.dataset.theme,"dark");assert.ok(h.calls.some(c=>c.name==="set_config"));h.unload();
});
test("hotkey modifiers, Backspace and conflicts are handled inline",async()=>{
  const h=await harness(),{hotkeyCapture}=await h.module("settings/widgets/hotkey-capture.ts");let calls=0;
  const field=hotkeyCapture("Ctrl+D",async()=>{calls++;throw new Error("Shortcut conflict");},true),b=field.querySelector("button");
  await b.dispatch("click");await b.dispatch("keydown",{key:"Control",code:"ControlLeft"});assert.equal(calls,0);assert.match(field.textContent,/non-modifier/);
  await b.dispatch("keydown",{key:"Backspace",code:"Backspace"});assert.equal(calls,1);assert.match(field.textContent,/Shortcut conflict/);assert.match(b.textContent,/CtrlD/);
});
test("failed settings writes restore controls",async()=>{
  const h=await settings({set_config:async()=>{throw new Error("Write failed");}}),input=h.document.getElementById("field-translate-to-english");
  input.checked=true;await input.dispatch("change");assert.equal(input.checked,false);assert.match(h.root.textContent,/Write failed/);h.unload();
});
test("audio renders 24 live bars and saves gain",async()=>{
  const h=await settings();await button(h.root,"Audio").dispatch("click");await settle();assert.equal(h.root.querySelectorAll(".level-meter i").length,24);
  h.emit("level",{rms:0.2});assert.equal(h.root.querySelectorAll(".level-meter i.active").length,24);
  const gain=h.document.getElementById("field-input-gain");gain.value="1.5";await gain.dispatch("change");assert.equal(h.config.audio.gain,1.5);h.unload();
});
test("behavior saves style, replacements and safe transcript text",async()=>{
  const h=await settings();await button(h.root,"Behavior").dispatch("click");await settle();const style=h.document.getElementById("field-dictation-style");style.value="code";await style.dispatch("change");assert.equal(h.config.postproc.style,"code");
  const table=h.root.querySelector(".replacement-table");await table.querySelector("button").dispatch("click");await button(h.root,"Save replacements").dispatch("click");assert.equal(Object.keys(h.config.postproc.replacements).length,0);
  h.emit("latency",{latency_ms:14,text:"<script>not markup</script>"});assert.equal(h.root.querySelector(".transcript").textContent,"<script>not markup</script>");assert.equal(h.root.querySelector("script"),null);h.unload();
});
test("model install is explicit, progress completes, selection and test are wired",async()=>{
  const h=await settings();await button(h.root,"Engine").dispatch("click");await settle();assert.ok(!h.calls.some(c=>c.name==="install_model"));
  await button(h.root,"Install").dispatch("click");await settle();assert.ok(h.calls.some(c=>c.name==="install_model"&&c.args.name==="tiny.en"));assert.equal(h.root.querySelector("progress").value,1);
  await button(h.root,"Use model").dispatch("click");await settle();assert.equal(h.config.engine.model,"tiny.en");await button(h.root,"Test 3 s").dispatch("click");assert.match(h.root.textContent,/12 ms inference/);h.unload();
});
test("privacy debounces search, clears history and opens folders",async()=>{
  const h=await settings();await button(h.root,"Privacy").dispatch("click");await settle();const input=h.root.querySelectorAll("input").find(i=>i.type==="search");input.value="hello";await input.dispatch("input");h.timers.tick(250);await settle();
  assert.ok(h.calls.some(c=>c.name==="history_search"&&c.args.q==="hello"));await button(h.root,"Delete all history").dispatch("click");assert.ok(h.calls.some(c=>c.name==="history_clear"));
  await button(h.root,"Open folder").dispatch("click");assert.ok(h.calls.some(c=>c.name==="open_path"&&c.args.kind==="config"));h.unload();
});
test("empty and disconnected states remain usable",async()=>{
  const h=await settings({get_status:async()=>({state:"idle",sidecar_alive:false,model_available:false,device:"unloaded",compute:"unloaded",model:"",error:null}),list_models:async()=>{throw new Error("offline");},list_devices:async()=>[]});
  assert.equal(h.root.querySelector(".model-banner").hidden,false);await button(h.root,"Engine").dispatch("click");await settle();assert.match(h.root.textContent,/Model library unavailable/);assert.equal(button(h.root,"Test 3 s").disabled,true);
  await button(h.root,"Audio").dispatch("click");await settle();assert.match(h.root.textContent,/No input devices/);h.unload();
});
test("sidecar errors refresh the footer without a polling loop",async()=>{
  const h=await settings();h.status.sidecar_alive=false;h.emit("error",{message:"Sidecar stopped"});h.timers.tick(250);await settle();
  assert.match(h.root.querySelector(".sidebar-footer").textContent,/Engine offline/);h.unload();
});
test("GPU fallback warning refreshes and clears in Engine settings",async()=>{
  const h=await settings();await button(h.root,"Engine").dispatch("click");await settle();
  h.status.fallback_reason="A required CUDA library could not be loaded.";h.emit("state",{state:"idle"});h.timers.tick(250);await settle();
  assert.match(h.root.textContent,/Using CPU because GPU acceleration is unavailable/);
  h.status.fallback_reason=null;h.emit("state",{state:"idle"});h.timers.tick(250);await settle();
  assert.doesNotMatch(h.root.textContent,/Using CPU because GPU acceleration is unavailable/);h.unload();
});
test("overlay renders all states, five bars, previews and timed hiding",async()=>{
  const h=await harness();await (await h.module("overlay/overlay.ts")).mountOverlay(h.root);const pill=h.root.querySelector(".pill");
  assert.equal(h.root.dataset.state,"idle");h.timers.tick(1500);assert.equal(pill.hidden,true);
  h.emit("state",{state:"listening"});assert.equal(pill.hidden,false);h.emit("partial",{text:"foo underscore bar"});assert.equal(h.root.dataset.expanded,"true");
  h.emit("level",{rms:0.2});assert.equal(h.root.querySelectorAll(".bars i").length,5);assert.ok(h.root.querySelector(".bars i").style.values.has("--level"));
  h.emit("state",{state:"transcribing"});assert.equal(h.root.querySelector(".preview").textContent,"");assert.equal(h.root.querySelector(".indicator").textContent,"•••");
  h.emit("state",{state:"done"});h.timers.tick(899);assert.equal(pill.hidden,false);h.timers.tick(1);assert.equal(pill.hidden,true);
  h.emit("error",{message:"Microphone disconnected"});h.emit("state",{state:"error"});assert.match(h.root.textContent,/Microphone disconnected/);h.timers.tick(1600);assert.equal(pill.hidden,true);
  h.emit("state",{state:"listening"});h.config.overlay.enabled=false;h.emit("config",h.config);assert.equal(pill.hidden,true);h.unload();assert.equal([...h.events.values()].reduce((n,s)=>n+s.size,0),0);
});

// ---- Enhance settings card ----
async function behaviorPage(overrides){const h=await settings(overrides);await button(h.root,"Behavior").dispatch("click");await settle();await settle();return h;}
const enhanceCard = root => root.querySelectorAll(".card").find(c=>c.querySelector(".card-title")?.textContent==="Enhance");
test("enhance card is local-only, discloses size, and never downloads without consent",async()=>{
  const h=await behaviorPage(),card=enhanceCard(h.root);assert.ok(card);
  assert.match(card.textContent,/Rewrites run locally on your CPU\. Dictation audio and text are never uploaded\./);
  assert.match(card.textContent,/1\.11 GB \(1\.03 GiB · 1,107,409,472 bytes\)/);assert.match(card.textContent,/Code and terminal dictation is not rewritten/);
  assert.match(card.textContent,/never replaces text/);assert.ok(!h.calls.some(c=>c.name==="install_enhance_model"));
  const auto=h.document.getElementById("field-rewrite-automatically");assert.equal(auto.checked,false);
  auto.checked=true;await auto.dispatch("change");await settle();
  assert.equal(auto.checked,false);assert.equal(h.config.enhance.auto_rewrite,false);assert.ok(!h.calls.some(c=>c.name==="install_enhance_model"));
  assert.match(card.textContent,/Download and enable/);assert.ok(!h.calls.some(c=>c.name==="set_config"&&c.args.patch.enhance?.auto_rewrite));
  await button(card,"Not now").dispatch("click");assert.equal(card.querySelector(".enhance-consent").hidden,true);h.unload();
});
test("download consent, progress, cancel and stale events follow one operation",async()=>{
  const h=await behaviorPage(),card=enhanceCard(h.root),auto=h.document.getElementById("field-rewrite-automatically");
  auto.checked=true;await auto.dispatch("change");await button(card,"Download and enable").dispatch("click");await settle();
  const install=h.calls.find(c=>c.name==="install_enhance_model");assert.deepEqual(plain(install.args),{modelId:"qwen3-1.7b-q4_k_m",enableAfterInstall:true});
  h.emit("enhance-download",{operation_id:"1",model_id:"qwen3-1.7b-q4_k_m",phase:"downloading",received_bytes:553704736,total_bytes:1107409472,error_code:null});
  assert.equal(enhanceCard(h.root).querySelector("progress").value,0.5);assert.match(card.textContent,/554 of 1107 MB/);
  h.emit("enhance-download",{operation_id:"0",model_id:"qwen3-1.7b-q4_k_m",phase:"complete",received_bytes:1,total_bytes:1,error_code:null});
  assert.match(card.textContent,/554 of 1107 MB/,"late event from an older operation is ignored");
  await button(card,"Cancel download").dispatch("click");assert.deepEqual(plain(h.calls.find(c=>c.name==="cancel_enhance_download").args),{operationId:"1"});
  h.emit("enhance-download",{operation_id:"1",model_id:"qwen3-1.7b-q4_k_m",phase:"cancelled",received_bytes:0,total_bytes:1107409472,error_code:null});
  assert.match(card.textContent,/Download cancelled\. Nothing was enabled\./);assert.ok(button(card,"Retry"));assert.equal(h.config.enhance.auto_rewrite,false);
  await button(card,"Retry").dispatch("click");await settle();assert.deepEqual(plain(h.calls.filter(c=>c.name==="install_enhance_model").at(-1).args),{modelId:"qwen3-1.7b-q4_k_m",enableAfterInstall:false});h.unload();
});
test("download progress rehydrates after navigating away and back",async()=>{
  const h=await behaviorPage();await button(enhanceCard(h.root),"Install").dispatch("click");await settle();
  h.emit("enhance-download",{operation_id:"1",model_id:"qwen3-1.7b-q4_k_m",phase:"verifying",received_bytes:1107409472,total_bytes:1107409472,error_code:null});
  await button(h.root,"General").dispatch("click");await settle();
  h.enhanceStatus.revision=5;h.enhanceStatus.installation_state="verifying";h.enhanceStatus.download={operation_id:"1",model_id:"qwen3-1.7b-q4_k_m",phase:"verifying",received_bytes:1107409472,total_bytes:1107409472,error_code:null};
  await button(h.root,"Behavior").dispatch("click");await settle();await settle();const card=enhanceCard(h.root);
  assert.match(card.textContent,/Verifying SHA-256/);assert.ok(button(card,"Cancel download"));
  h.enhanceModel.installed=true;h.enhanceStatus.revision=6;h.enhanceStatus.installation_state="installed";h.enhanceStatus.download=null;
  h.emit("enhance-download",{operation_id:"1",model_id:"qwen3-1.7b-q4_k_m",phase:"complete",received_bytes:1107409472,total_bytes:1107409472,error_code:null});await settle();await settle();
  assert.ok(button(enhanceCard(h.root),"Remove"));h.unload();assert.equal([...h.events.values()].reduce((n,s)=>n+s.size,0),0);
});
test("download completion while Behavior is closed clears cached controls",async()=>{
  const h=await behaviorPage();await button(enhanceCard(h.root),"Install").dispatch("click");await settle();
  await button(h.root,"General").dispatch("click");await settle();
  h.enhanceModel.installed=true;h.enhanceStatus.revision=7;h.enhanceStatus.installation_state="installed";h.enhanceStatus.download=null;
  await button(h.root,"Behavior").dispatch("click");await settle();await settle();
  assert.ok(button(enhanceCard(h.root),"Remove"));assert.equal(button(enhanceCard(h.root),"Cancel download"),undefined);
  h.unload();
});
test("installed model enables auto-rewrite, removal confirms, missing backend disables controls",async()=>{
  const h=await harness();h.enhanceModel.installed=true;h.enhanceStatus.installation_state="installed";await (await h.module("settings/app.ts")).mountSettings(h.root);await settle();
  await button(h.root,"Behavior").dispatch("click");await settle();await settle();
  const auto=h.document.getElementById("field-rewrite-automatically");auto.checked=true;await auto.dispatch("change");await settle();assert.equal(h.config.enhance.auto_rewrite,true);
  await button(enhanceCard(h.root),"Remove").dispatch("click");await settle();assert.ok(h.calls.some(c=>c.name==="remove_enhance_model"&&c.args.modelId==="qwen3-1.7b-q4_k_m"));h.unload();
  const off=await behaviorPage({get_enhance_status:async(_,{enhanceStatus})=>({...enhanceStatus,backend_available:false})});
  assert.equal(button(enhanceCard(off.root),"Install").disabled,true);
  assert.equal(button(enhanceCard(off.root),"Rewrite last dictation").disabled,true);off.unload();
});
test("overlay disabled offers the combined enable action instead of auto-enabling",async()=>{
  const h=await harness();h.config.overlay.enabled=false;h.enhanceModel.installed=true;h.enhanceStatus.installation_state="installed";
  await (await h.module("settings/app.ts")).mountSettings(h.root);await settle();await button(h.root,"Behavior").dispatch("click");await settle();await settle();
  const auto=h.document.getElementById("field-rewrite-automatically");auto.checked=true;await auto.dispatch("change");assert.equal(h.config.enhance.auto_rewrite,false);assert.match(h.root.textContent,/needs the overlay/);
  await button(enhanceCard(h.root),"Enable overlay and auto-rewrite").dispatch("click");await settle();assert.equal(h.config.overlay.enabled,true);assert.equal(h.config.enhance.auto_rewrite,true);h.unload();
});
test("enhance shortcuts default unassigned with one-click suggestions and can be cleared",async()=>{
  const h=await behaviorPage(),card=enhanceCard(h.root);
  assert.equal(h.config.enhance.rewrite_hotkey,"");assert.equal(h.config.enhance.accept_hotkey,"");assert.match(card.textContent,/Not set/);
  await button(card,"Use Ctrl+Alt+Shift+R").dispatch("click");await settle();assert.equal(h.config.enhance.rewrite_hotkey,"Ctrl+Alt+Shift+R");
  await button(enhanceCard(h.root),"Use Ctrl+Alt+Shift+A").dispatch("click");await settle();assert.equal(h.config.enhance.accept_hotkey,"Ctrl+Alt+Shift+A");
  assert.equal(button(enhanceCard(h.root),"Use Ctrl+Alt+Shift+R"),undefined);
  const capture=h.document.getElementById("field-rewrite-shortcut");await capture.dispatch("click");await capture.dispatch("keydown",{key:"Backspace",code:"Backspace"});assert.equal(h.config.enhance.rewrite_hotkey,"");
  const style=h.document.getElementById("field-style");style.value="structured";await style.dispatch("change");assert.equal(h.config.enhance.style,"structured");
  const code=h.document.getElementById("field-rewrite-code-and-terminal-dictation");assert.equal(code.checked,false);code.checked=true;await code.dispatch("change");assert.equal(h.config.enhance.rewrite_code,true);h.unload();
});
test("conflicting enhance shortcut is reported inline and not saved",async()=>{
  const h=await behaviorPage({set_config:async(args,{config})=>{if(args.patch.enhance?.rewrite_hotkey===config.general.hotkey)throw new Error("Shortcut already used for dictation");for(const [k,v] of Object.entries(args.patch))Object.assign(config[k],v);return structuredClone(config);}});
  const capture=h.document.getElementById("field-rewrite-shortcut");await capture.dispatch("click");await capture.dispatch("keydown",{key:"D",code:"KeyD",ctrlKey:true,altKey:true});
  assert.equal(h.config.enhance.rewrite_hotkey,"");assert.match(enhanceCard(h.root).textContent,/already used for dictation/);h.unload();
});
test("manual rewrite reports typed reasons; older backends without enhance still render",async()=>{
  const h=await behaviorPage();await button(enhanceCard(h.root),"Rewrite last dictation").dispatch("click");assert.match(enhanceCard(h.root).textContent,/Dictate something first\./);h.unload();
  const old=await harness();delete old.config.enhance;await (await old.module("settings/app.ts")).mountSettings(old.root);await settle();await button(old.root,"Behavior").dispatch("click");await settle();
  assert.match(enhanceCard(old.root).textContent,/unavailable in this build/);assert.ok(!old.calls.some(c=>c.name==="get_enhance_status"));old.unload();
});
test("general offers reset position and describes a clickable, draggable overlay",async()=>{
  const h=await settings();assert.doesNotMatch(h.root.textContent,/click-through/);await button(h.root,"Reset position").dispatch("click");assert.ok(h.calls.some(c=>c.name==="reset_overlay_position"));
  h.config.overlay.position="custom";h.emit("config",structuredClone(h.config));await settle();const position=h.document.getElementById("field-position");
  assert.equal(position.value,"custom");assert.match(h.root.textContent,/Where you dragged it/);h.unload();
});

// ---- Interactive overlay ----
async function overlay(overrides){const h=await harness(overrides);await (await h.module("overlay/overlay.ts")).mountOverlay(h.root);return h;}
const enh = (phase,extra={}) => ({phase,dictation_id:"7",suggestion_id:null,original_text:null,proposed_text:null,acceptance_mode:"clipboard",message:null,expires_at:null,...extra});
const snap = (revision,enhancement,extra={}) => ({revision,dictation_state:"idle",delivery_busy:false,partial_text:"",enhancement,...extra});
const READY = enh("ready",{suggestion_id:"42",original_text:"um so the the release is done",proposed_text:"The release is done."});
test("overlay shows pending then ready suggestion and ready overrides idle hiding",async()=>{
  const h=await overlay(),pill=h.root.querySelector(".pill"),panel=h.root.querySelector(".suggestion");
  h.emit("state",{state:"done"});h.emit("overlay-update",snap(1,enh("rewriting")));assert.equal(panel.hidden,false);assert.match(panel.textContent,/Rewriting on this device/);
  assert.equal(h.root.querySelector(".accept-button").hidden,true);h.emit("state",{state:"idle"});h.timers.tick(5000);assert.equal(pill.hidden,false);
  h.emit("overlay-update",snap(2,READY));assert.equal(h.root.querySelector(".suggestion-text").textContent,"The release is done.");assert.equal(h.root.dataset.enhance,"ready");
  assert.equal(h.root.querySelector(".accept-button").textContent,"Accept — copy");h.timers.tick(60000);assert.equal(pill.hidden,false);assert.equal(h.root.dataset.expanded,"true");h.unload();
});
test("generation timeout remains visible with an explanation",async()=>{
  const h=await overlay(),pill=h.root.querySelector(".pill");
  h.emit("overlay-update",snap(1,enh("notice",{message:"Enhance stopped: generation exceeded the configured time limit. Original dictation is unchanged."})));
  assert.match(h.root.textContent,/generation exceeded the configured time limit/);
  h.timers.tick(5000);assert.equal(h.root.dataset.enhance,"notice");assert.equal(pill.hidden,false);
  h.timers.tick(7000);assert.equal(h.root.dataset.enhance,"none");h.unload();
});
test("overlay ignores stale snapshot revisions and hydrates from get_overlay_snapshot",async()=>{
  const h=await overlay({get_overlay_snapshot:async()=>snap(5,READY)});await settle();assert.equal(h.root.dataset.enhance,"ready");
  h.emit("overlay-update",snap(4,enh("none")));assert.equal(h.root.dataset.enhance,"ready");h.emit("overlay-update",snap(5,enh("none")));assert.equal(h.root.dataset.enhance,"ready");
  h.emit("overlay-update",snap(6,enh("none")));assert.equal(h.root.dataset.enhance,"none");assert.equal(h.root.querySelector(".pill").hidden,true);
  assert.ok(h.calls.findIndex(c=>c.name==="get_overlay_snapshot")>=0);h.unload();
});
test("accept and discard send only suggestion ids; accept is idempotent while pending",async()=>{
  let release;const h=await overlay({accept_enhancement:async()=>new Promise(r=>{release=r;})});h.emit("overlay-update",snap(1,READY));
  const accept=h.root.querySelector(".accept-button");void accept.dispatch("click");await settle();void accept.dispatch("click");await settle();
  const accepts=h.calls.filter(c=>c.name==="accept_enhancement");assert.equal(accepts.length,1);assert.deepEqual(plain(accepts[0].args),{suggestionId:"42"});
  release({outcome:"copied"});await settle();await settle();assert.match(h.root.textContent,/Copied\. Select the original dictation and paste to replace it\./);
  assert.equal(accept.hidden,true);assert.equal(h.root.querySelector(".suggestion-text").textContent,"");h.timers.tick(4000);assert.equal(h.root.dataset.enhance,"none");h.unload();
  const d=await overlay();d.emit("overlay-update",snap(1,READY));await d.root.querySelector(".discard-button").dispatch("click");await settle();
  assert.deepEqual(plain(d.calls.find(c=>c.name==="discard_enhancement").args),{suggestionId:"42"});assert.ok(!d.calls.some(c=>c.name==="accept_enhancement"));
  assert.equal(d.root.dataset.enhance,"none");assert.equal(d.root.querySelector(".pill").hidden,true);d.unload();
});
test("pending cancel targets its request and ignores a newer rewrite",async()=>{
  let release;const h=await overlay({discard_enhancement:async()=>new Promise(r=>{release=r;})});
  h.emit("overlay-update",snap(1,enh("rewriting",{request_id:"51"})));
  const cancel=h.root.querySelector(".discard-button");assert.equal(cancel.hidden,false);
  void cancel.dispatch("click");await settle();
  assert.deepEqual(plain(h.calls.find(c=>c.name==="discard_enhancement").args),{suggestionId:"51"});
  h.emit("overlay-update",snap(2,enh("rewriting",{request_id:"52"})));
  release();await settle();assert.equal(h.root.dataset.enhance,"rewriting");h.unload();
});
test("stale or failed accept is labelled honestly and never claims replacement",async()=>{
  const h=await overlay({accept_enhancement:async()=>({outcome:"stale"})});h.emit("overlay-update",snap(1,READY));await h.root.querySelector(".accept-button").dispatch("click");await settle();
  assert.match(h.root.textContent,/no longer available/);assert.doesNotMatch(h.root.textContent,/Copied|Replaced/);h.unload();
  const f=await overlay({accept_enhancement:async()=>{throw new Error("Clipboard unavailable");}});f.emit("overlay-update",snap(1,READY));await f.root.querySelector(".accept-button").dispatch("click");await settle();
  assert.match(f.root.textContent,/Clipboard unavailable/);assert.equal(f.root.querySelector(".accept-button").disabled,false);f.unload();
});
test("hostile suggestion text renders as text; unchanged output shows no Accept",async()=>{
  const h=await overlay();h.emit("overlay-update",snap(1,enh("ready",{suggestion_id:"9",original_text:"x",proposed_text:"<img src=x onerror=alert(1)>"})));
  assert.equal(h.root.querySelector(".suggestion-text").textContent,"<img src=x onerror=alert(1)>");assert.equal(h.root.querySelector("img"),null);
  h.emit("overlay-update",snap(2,enh("ready",{suggestion_id:"10",original_text:"Same.",proposed_text:"Same."})));
  assert.match(h.root.textContent,/No changes suggested\./);assert.equal(h.root.querySelector(".accept-button").hidden,true);
  h.emit("overlay-update",snap(3,enh("ready",{original_text:"Fine.",message:"No changes suggested"})));
  assert.match(h.root.textContent,/No changes suggested/);assert.equal(h.root.querySelector(".accept-button").hidden,true);assert.equal(h.root.querySelector(".discard-button").textContent,"Dismiss");
  await h.root.querySelector(".discard-button").dispatch("click");assert.ok(!h.calls.some(c=>c.name==="discard_enhancement"));assert.equal(h.root.dataset.enhance,"none");h.unload();
});
test("new dictation replaces the suggestion; expiry clears text from the overlay",async()=>{
  const h=await overlay();h.emit("overlay-update",snap(1,READY));h.emit("state",{state:"listening"});
  assert.equal(h.root.dataset.enhance,"none");assert.equal(h.root.querySelector(".suggestion").hidden,true);assert.equal(h.root.querySelector(".suggestion-text").textContent,"");
  h.emit("overlay-update",snap(2,{...READY,suggestion_id:"43",expires_at:Date.now()+120000}));h.emit("state",{state:"idle"});assert.equal(h.root.dataset.enhance,"ready");
  h.timers.tick(119000);assert.equal(h.root.dataset.enhance,"ready");h.timers.tick(2000);assert.equal(h.root.dataset.enhance,"none");assert.equal(h.root.querySelector(".suggestion-text").textContent,"");h.unload();
});
test("drag starts only from the handle with the primary button and never while delivering",async()=>{
  const h=await overlay(),handle=h.root.querySelector(".drag-handle");await handle.dispatch("pointerdown",{button:0});assert.equal(h.drags.length,1);
  await handle.dispatch("pointerdown",{button:2});assert.equal(h.drags.length,1);
  h.emit("overlay-update",snap(1,READY));await h.root.querySelector(".suggestion-text").dispatch("pointerdown",{button:0});assert.equal(h.drags.length,1);
  h.emit("overlay-update",snap(2,READY,{delivery_busy:true}));await handle.dispatch("pointerdown",{button:0});assert.equal(h.drags.length,1);
  assert.equal(h.root.dataset.busy,"true");assert.equal(h.root.querySelector(".accept-button").disabled,true);h.unload();
});
test("denied native drag reports a visible error",async()=>{
  const h=await overlay({startOverlayDrag:async()=>{throw new Error("permission denied");}});
  await h.root.querySelector(".drag-handle").dispatch("pointerdown",{button:0});await settle();
  assert.equal(h.root.dataset.dragError,"true");assert.match(h.root.textContent,/Could not move overlay: Error: permission denied/);h.unload();
});
test("scale changes remeasure and page reload resets layout identity",async()=>{
  const h=await overlay(),pill=h.root.querySelector(".pill");h.emit("state",{state:"listening"});
  pill.rect={width:200,height:48};h.observers.trigger();
  h.config.overlay.scale=1.5;h.emit("config",structuredClone(h.config));await settle();
  pill.rect={width:320,height:72};h.observers.trigger();
  const sizes=h.calls.filter(c=>c.name==="set_overlay_size").map(c=>plain(c.args));
  assert.equal(sizes.at(-1).width,360);assert.ok(sizes.at(-1).layoutRevision>sizes[0].layoutRevision);
  h.unload();
  const reloaded=await overlay();reloaded.emit("state",{state:"listening"});reloaded.root.querySelector(".pill").rect={width:200,height:48};reloaded.observers.trigger();
  const first=reloaded.calls.find(c=>c.name==="set_overlay_size").args;
  assert.equal(first.layoutRevision,1);assert.notEqual(first.layoutInstance,sizes[0].layoutInstance);reloaded.unload();
});
test("measured overlay size is reported once per change with increasing layout revisions",async()=>{
  const h=await overlay(),pill=h.root.querySelector(".pill");h.emit("state",{state:"listening"});
  pill.rect={width:200,height:48};h.observers.trigger();pill.rect={width:199.6,height:47.5};h.observers.trigger();
  pill.rect={width:560,height:180.2};h.observers.trigger();const sizes=h.calls.filter(c=>c.name==="set_overlay_size").map(c=>plain(c.args));
  assert.deepEqual(sizes.map(({layoutInstance,...rest})=>rest),[{width:240,height:88,layoutRevision:1},{width:600,height:221,layoutRevision:2}]);
  assert.ok(sizes[0].layoutInstance);assert.equal(sizes[0].layoutInstance,sizes[1].layoutInstance);
  h.unload();assert.equal(h.observers.live.size,0);assert.equal([...h.events.values()].reduce((n,s)=>n+s.size,0),0);
});
