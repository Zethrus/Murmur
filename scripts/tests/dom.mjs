// Minimal DOM contract harness, not a browser or a visual-layout emulator.
export class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null; this.attributes = new Map(); this.listeners = new Map();
    this.className = ""; this._text = ""; this.value = ""; this.disabled = false; this.checked = false; this.hidden = false; this.scrollTop = 0;
    this.dataset = {}; this.style = {values:new Map(),setProperty(k,v){this.values.set(k,v);},removeProperty(k){this.values.delete(k);}};
    this.classList = {add:(...names)=>{this.className = [...new Set([...this.className.split(" ").filter(Boolean),...names])].join(" ");},
      remove:name=>{this.className = this.className.split(" ").filter(x=>x!==name).join(" ");},
      contains:name=>this.className.split(" ").includes(name), toggle:(name,on)=>{on ??= !this.classList.contains(name); if(on)this.classList.add(name);else this.classList.remove(name);return on;}};
  }
  set textContent(value) { this.children.forEach(c=>c.parentNode=null); this.children=[];this._text=String(value); }
  get textContent() { return this._text+this.children.map(c=>c.textContent).join(""); }
  get firstElementChild() { return this.children[0] ?? null; }
  get rows() { return this.children.filter(c=>c.tagName === "TR"); }
  append(...nodes) { for (let node of nodes) { if(typeof node === "string") {const s=node;node=new Element("#text");node.textContent=s;} node.parentNode=this;this.children.push(node); } }
  prepend(node) { node.parentNode=this; this.children.unshift(node); }
  replaceChildren(...nodes) { this.textContent="";this.append(...nodes); }
  remove() { if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(x=>x!==this);this.parentNode=null; }
  setAttribute(key,value) { this.attributes.set(key,String(value));if(key === "id")this.id=String(value);if(key === "class")this.className=String(value); }
  getAttribute(key) { if(key === "id")return this.id??null;if(key === "class")return this.className;return this.attributes.get(key)??null; }
  removeAttribute(key) { this.attributes.delete(key); }
  addEventListener(type,handler) { const list=this.listeners.get(type)??[];list.push(handler);this.listeners.set(type,list); }
  async dispatch(type,props={}) { if(type === "click" && this.disabled)return;const event={type,target:this,preventDefault(){},stopPropagation(){},...props};for(const fn of this.listeners.get(type)??[])await fn(event); }
  focus() { this.ownerDocument.activeElement=this; }
  /** Layout is not emulated; tests assign `rect` to model a rendered size. */
  getBoundingClientRect() { const r=this.rect??{width:0,height:0};return {x:0,y:0,top:0,left:0,right:r.width,bottom:r.height,...r}; }
  matches(selector) {
    return selector.split(",").some(part=>{
      const chain=part.trim().split(/\s+/);const simple=chain.pop();if(!this.simple(simple))return false;
      let ancestor=this.parentNode;while(chain.length){const expected=chain.pop();while(ancestor&&!ancestor.simple(expected))ancestor=ancestor.parentNode;if(!ancestor)return false;ancestor=ancestor.parentNode;}return true;
    });
  }
  simple(selector) {
    const tag=selector.match(/^[a-z][a-z0-9-]*/i)?.[0];if(tag&&this.tagName!==tag.toUpperCase())return false;
    for(const [,name] of selector.matchAll(/\.([\w-]+)/g))if(!this.classList.contains(name))return false;
    const id=selector.match(/#([\w-]+)/)?.[1];if(id&&this.id!==id)return false;
    for(const [,key,value] of selector.matchAll(/\[([\w-]+)(?:=["']?([^"'\]]+)["']?)?\]/g)){
      const actual=key.startsWith("data-")?this.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]:this.getAttribute(key);
      if(actual==null||(value!==undefined&&String(actual)!==value))return false;
    }return true;
  }
  querySelectorAll(selector) { return this.children.flatMap(c=>[...(c.matches(selector)?[c]:[]),...c.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0]??null; }
  set innerHTML(source) {
    this.replaceChildren();const stack=[this];for(const token of source.match(/<[^>]+>|[^<]+/g)??[]){
      if(token.startsWith("</")){stack.pop();continue;}if(token.startsWith("<")){const tag=token.match(/^<(\w+)/)[1],node=this.ownerDocument.createElement(tag);
        for(const [,key,value] of token.matchAll(/([\w-]+)="([^"]*)"/g))node.setAttribute(key,value);
        stack.at(-1).append(node);stack.push(node);
      }else stack.at(-1)._text+=token;
    }
  }
}
export function makeDocument() {
  const document={activeElement:null,createElement(tag){const node=new Element(tag);node.ownerDocument=document;return node;},
    createElementNS(_namespace,tag){const node=new Element(tag);node.ownerDocument=document;return node;}};
  document.documentElement=document.createElement("html");document.body=document.createElement("body");document.documentElement.append(document.body);
  document.querySelector=s=>document.documentElement.querySelector(s);document.getElementById=id=>document.querySelector(`#${id}`);
  const root=document.createElement("div");root.id="app";document.body.append(root);return {document,root};
}
/** ResizeObserver stand-in: tests call trigger() after changing an element's rect. */
export function resizeObservers() {
  const live=new Set();
  class ResizeObserver { constructor(fn){this.fn=fn;this.targets=new Set();live.add(this);} observe(t){this.targets.add(t);} unobserve(t){this.targets.delete(t);} disconnect(){this.targets.clear();live.delete(this);} }
  return {ResizeObserver,live,trigger(){for(const o of live)o.fn([...o.targets].map(target=>({target})));}};
}
export function clock() {
  let now=0,id=0;const tasks=new Map();return {setTimeout(fn,delay){tasks.set(++id,{fn,at:now+delay});return id;},clearTimeout(id){tasks.delete(id);},
    tick(ms){const end=now+ms;for(;;){const next=[...tasks].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!next)break;now=next[1].at;tasks.delete(next[0]);next[1].fn();}now=end;}};
}
