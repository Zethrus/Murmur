import test from "node:test";
import assert from "node:assert/strict";
import {harness,settle} from "./ui-harness.mjs";
const button = (root,label) => root.querySelectorAll("button").find(b=>b.textContent===label||b.dataset.page===label);
async function settings(overrides){const h=await harness(overrides);await (await h.module("settings/app.ts")).mountSettings(h.root);await settle();return h;}
test("settings mounts five pages, status/version, and releases all listeners",async()=>{
  const h=await settings();assert.equal(h.root.querySelectorAll(".nav-button").length,5);assert.match(h.root.textContent,/Version 0.1.0/);
  for(const page of ["General","Engine","Audio","Behavior","Privacy"]){await button(h.root,page).dispatch("click");await settle();assert.equal(h.root.querySelector("h1").textContent,page);assert.ok(h.root.querySelectorAll(".card").length>=2);}
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
