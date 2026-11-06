import {call, on, appVersion, type Config, type ConfigPatch, type Events, type Status} from "../bridge";
import type {PageContext} from "./context";
import {el, button} from "./widgets/row";
import {general} from "./pages/general";
import {engine} from "./pages/engine";
import {audio} from "./pages/audio";
import {behavior} from "./pages/behavior";
import {privacy} from "./pages/privacy";
import {companion} from "./pages/companion";
const pages = {General:general, Engine:engine, Audio:audio, Behavior:behavior, Privacy:privacy, Companion:companion};
type Page = keyof typeof pages;
const descriptions: Record<Page,string> = {
  General:"A quieter way to get your thoughts down.", Engine:"A capable voice, running entirely on your device.",
  Audio:"Help Murmur hear you clearly.", Behavior:"Your words, delivered just the way you work.", Privacy:"Private by default. Always under your control.",
  Companion:"Connect your Flipper Zero or pair a phone as a remote for Murmur."
};
/** The Murmur mark: microphone capsule with a wave slit, the same glyph as the tray icon. */
function brandMark(){
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns,"svg");
  svg.setAttribute("viewBox","204 202 616 616"); svg.setAttribute("class","brand-mark");
  svg.setAttribute("role","img"); svg.setAttribute("aria-label","Murmur");
  svg.innerHTML = [
    '<path fill="currentColor" d="M372 436V344A140 140 0 0 1 652 344V436C636 444 612 445 582 445C558 445 534 426 512 419C490 412 466 393 442 393C412 393 388 410 372 436Z"/>',
    '<path fill="currentColor" d="M372 436C396 431 418 427 442 427C466 427 490 446 512 453C534 460 558 479 582 479C612 479 636 462 652 436V528A140 140 0 0 1 372 528Z"/>',
    '<path fill="none" stroke="currentColor" stroke-width="56" stroke-linecap="round" d="M304 420V528A208 208 0 0 0 720 528V420"/>',
    '<rect x="484" y="740" width="56" height="50" fill="currentColor"/>',
    '<rect x="392" y="778" width="240" height="40" rx="20" fill="currentColor"/>'
  ].join("");
  return svg;
}

/** The app logo: the packaged icon tile, so the header carries the same mark as the launcher. */
function appLogo(){
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns,"svg");
  svg.setAttribute("viewBox","0 0 1024 1024"); svg.setAttribute("class","page-logo");
  svg.setAttribute("role","img"); svg.setAttribute("aria-label","Murmur");
  svg.innerHTML = [
    '<rect class="logo-tile" width="1024" height="1024" rx="180"/>',
    '<path class="logo-capsule" d="M372 436V344A140 140 0 0 1 652 344V436C636 444 612 445 582 445C558 445 534 426 512 419C490 412 466 393 442 393C412 393 388 410 372 436Z"/>',
    '<path class="logo-capsule" d="M372 436C396 431 418 427 442 427C466 427 490 446 512 453C534 460 558 479 582 479C612 479 636 462 652 436V528A140 140 0 0 1 372 528Z"/>',
    '<path class="logo-cradle" fill="none" stroke-width="56" stroke-linecap="round" d="M304 420V528A208 208 0 0 0 720 528V420"/>',
    '<rect class="logo-cradle" x="484" y="740" width="56" height="50"/>',
    '<rect class="logo-cradle" x="392" y="778" width="240" height="40" rx="20"/>'
  ].join("");
  return svg;
}

export async function mountSettings(root: HTMLElement): Promise<void> {
  root.replaceChildren(el("p","boot-message","Opening your workspace…"));
  let config: Config, status: Status, version = "", page: Page = "General", disposed = false, saving = 0, lastTranscript = "", generation = 0;
  const disposers: (()=>void)[] = [], pageDisposers: (()=>void)[] = [];
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
  window.addEventListener("beforeunload",()=>{disposed = true; disposers.forEach(f=>f()); pageDisposers.forEach(f=>f()); clearTimeout(refreshTimer);},{once:true});
  [config,status,version] = await Promise.all([call("get_config"),call("get_status"),appVersion()]);
  if (disposed) return;
  const shell = el("div","settings-shell"), sidebar = el("aside","sidebar"), brand = el("div","brand"), nav = el("nav","navigation"), footer = el("footer","sidebar-footer"), main = el("main","settings-main");
  brand.append(brandMark(),el("span","brand-name","murmur")); nav.setAttribute("aria-label","Settings pages");
  const dot = el("i","status-dot"), engineStatus = el("span","status-label"), footerLine = el("div","footer-line"); dot.setAttribute("aria-hidden","true"); footerLine.append(dot,engineStatus);
  footer.append(footerLine,el("span","version",`Version ${version} · On-device dictation`));
  const header = el("header","page-header"), eyebrow = el("p","eyebrow","YOUR WORKSPACE"), title = el("h1"), description = el("p","page-description"), content = el("div","page-content"), notice = el("p","app-notice"), banner = el("div","model-banner");
  notice.setAttribute("role","status"); notice.setAttribute("aria-live","polite");
  banner.append(el("span","","Choose a speech model to start dictating"),button("Choose model",()=>navigate("Engine"),"primary"));
  const heading = el("div","page-heading"), headingText = el("div","page-heading-text");
  headingText.append(eyebrow,title,description); heading.append(appLogo(),headingText); header.append(heading);
  main.append(header,banner,notice,content); sidebar.append(brand,nav,footer); shell.append(sidebar,main); root.replaceChildren(shell);
  const report = (e: unknown): void => { notice.textContent = String(e); notice.dataset.tone = "error"; };
  const theme = (): void => { document.documentElement.dataset.theme = config.general.theme; };
  const chrome = (): void => { dot.dataset.online = String(status.sidecar_alive); engineStatus.textContent = !status.sidecar_alive ? "Engine offline" : !status.model_available ? "No model selected" : status.state === "idle" ? "Ready when you are" : status.state;
    banner.hidden = status.model_available; if (status.error) report(status.error); };
  let writes: Promise<void> = Promise.resolve();
  const save = (patch: ConfigPatch): Promise<void> => {
    const job = writes.then(() => persist(patch));
    writes = job.catch(() => {});
    return job;
  };
  const persist = async (patch: ConfigPatch): Promise<void> => {
    saving++; notice.textContent = "Saving…"; notice.dataset.tone = "";
    try { config = await call("set_config",{patch}); theme(); notice.textContent = "All changes saved"; }
    catch(e) { report(e); throw e; }
    finally { saving--; }
    renderPage();
  };
  function renderPage(): void {
    generation++; const current = generation; pageDisposers.splice(0).forEach(f=>f());
    const focused = document.activeElement instanceof HTMLElement ? document.activeElement.id : "";
    const scroll = main.scrollTop;
    const ctx: PageContext = {config,status,lastTranscript,save,report,cleanup:fn=>pageDisposers.push(fn),subscribe(event,handler) {
      void on(event,data=>{if (generation === current && !disposed) handler(data);}).then(off=>{
        if (generation !== current || disposed) off(); else pageDisposers.push(off);
      }).catch(report);
    }};
    content.replaceChildren(pages[page](ctx)); title.textContent = page; description.textContent = descriptions[page];
    nav.querySelectorAll("button").forEach(b=>{b.setAttribute("aria-current",b.dataset.page === page ? "page" : "false");});
    if (focused) document.getElementById(focused)?.focus({preventScroll:true}); main.scrollTop = scroll;
  }
  function navigate(next: Page): void { page = next; renderPage(); main.scrollTop = 0; }
  (Object.keys(pages) as Page[]).forEach((name,index)=>{const b = button(name,()=>navigate(name),"nav-button"); b.dataset.page = name;
    b.prepend(el("span","nav-number",String(index+1).padStart(2,"0"))); nav.append(b);});
  const subscribe = async <K extends keyof Events>(event: K, handler: (data: Events[K])=>void): Promise<void> => {
    const off = await on(event,handler); if (disposed) off(); else disposers.push(off);
  };
  await subscribe("config",next=>{config = next; theme(); if (!saving) renderPage();});
  await subscribe("latency",event=>{if (event.text !== undefined) lastTranscript = event.text;});
  const refreshStatus = (): void => { if (refreshTimer !== undefined) return;
    refreshTimer = setTimeout(()=>{refreshTimer = undefined; void call("get_status").then(next=>{
      if (disposed) return; const changed = next.sidecar_alive !== status.sidecar_alive || next.model_available !== status.model_available || next.device !== status.device || next.compute !== status.compute || next.fallback_reason !== status.fallback_reason;
      status = next; chrome(); if (changed && page === "Engine") renderPage();
    }).catch(report);},250);
  };
  await subscribe("error",event=>{report(event.message); refreshStatus();});
  await subscribe("state",refreshStatus);
  theme(); chrome(); renderPage();
}
