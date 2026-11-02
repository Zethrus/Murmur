import {readFile} from "node:fs/promises";
import {resolve,dirname} from "node:path";
import vm from "node:vm";
import ts from "typescript";
import {Element,makeDocument,clock} from "./dom.mjs";
export const settle = () => new Promise(resolve=>setImmediate(resolve));
export function fixture() {
  return {
    general:{hotkey:"Ctrl+Alt+D",hotkey_code:"",hotkey_mode:"hold",language:"en",translate:false,autostart:false,theme:"auto"},
    overlay:{enabled:true,position:"bottom-center",scale:1,show_preview:true,idle_hide:true}, sounds:{enabled:true,volume:0.6,muted:false},
    engine:{model:"distil-small.en",device:"auto",compute_type:"auto",beam_size:1,vad_filter:true,dictionary:["Tauri","TypeScript"]},
    audio:{device:"default",gain:1,silence_threshold:0.012,silence_timeout_ms:1200,max_utterance_s:90,min_utterance_ms:250},
    injection:{method:"auto",clipboard_only:false,trailing_space:true,capitalize:true,remove_fillers:false,exclude_apps:[],prefer_paste:true},
    postproc:{style:"auto",code_apps:["code","kitty"],spoken_punctuation:true,allow_newlines:false,replacements:{"mur mur":"Murmur"},fillers:["um","uh"]},
    privacy:{history_enabled:false,history_retention_days:7}
  };
}
export async function harness(overrides={}) {
  const {document,root}=makeDocument(),timers=clock(),events=new Map(),calls=[],unloads=[];
  let config=fixture(); const status={state:"idle",model:"distil-small.en",device:"cpu",compute:"int8",sidecar_alive:true,model_available:true,error:null};
  const models=[{name:"tiny.en",repo:"test/fixture",size_mb:75,accuracy:"fastest",installed:false},{name:"distil-small.en",repo:"test/fixture",size_mb:335,accuracy:"balanced",installed:true}];
  const emit=(name,data)=>{for(const fn of events.get(name)??[])fn(data);};
  const call=async(name,args)=>{calls.push({name,args});if(overrides[name])return overrides[name](args,{config,status,models,emit});
    switch(name){case "get_config":return structuredClone(config);case "get_status":return structuredClone(status);
      case "set_config":for(const [key,values] of Object.entries(args.patch))Object.assign(config[key],values);emit("config",structuredClone(config));return structuredClone(config);
      case "list_models":return structuredClone(models);case "list_devices":return [{id:"mic",name:"Test microphone",is_default:true}];
      case "test_engine":return {text:"",language:"en",duration_ms:3000,infer_ms:12};
      case "install_model":models.find(m=>m.name===args.name).installed=true;emit("download",{name:args.name,phase:"complete",progress:1});return {name:args.name,installed:true};
      case "remove_model":models.find(m=>m.name===args.name).installed=false;return {name:args.name,installed:false};
      case "history_search":return [];case "pick_exclude_app":return "TestEditor";default:return undefined;}
  };
  const on=async(name,handler)=>{const listeners=events.get(name)??new Set();listeners.add(handler);events.set(name,listeners);return ()=>listeners.delete(handler);};
  const context=vm.createContext({console,document,window:{addEventListener:(name,fn)=>{if(name==="beforeunload")unloads.push(fn);},confirm:()=>true},HTMLElement:Element,HTMLInputElement:Element,HTMLButtonElement:Element,performance,URLSearchParams,...timers});
  const bridge=new vm.SyntheticModule(["call","on","appVersion"],function(){this.setExport("call",call);this.setExport("on",on);this.setExport("appVersion",async()=>"0.1.0");},{context});
  const cache=new Map();
  async function load(file){file=resolve(file);if(file.endsWith("src/bridge.ts"))return bridge;if(cache.has(file))return cache.get(file);
    const source=await readFile(file,"utf8"),code=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText;
    const mod=new vm.SourceTextModule(code,{context,identifier:file});cache.set(file,mod);await mod.link((specifier)=>load(resolve(dirname(file),specifier+".ts")));return mod;}
  return {root,document,calls,emit,timers,events,config,status,async module(file){const mod=await load(resolve("src",file));await mod.evaluate();return mod.namespace;},unload(){unloads.forEach(fn=>fn());}};
}
