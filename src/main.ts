import {mountSettings} from "./settings/app";
import {mountOverlay} from "./overlay/overlay";
import "./styles/tokens.css";
import "./styles/settings.css";
import "./styles/overlay.css";
const root = document.querySelector<HTMLElement>("#app");
if (!root) throw new Error("Missing app root");
const overlay = new URLSearchParams(location.search).get("window") === "overlay";
document.body.dataset.window = overlay ? "overlay" : "settings";
void (overlay ? mountOverlay(root) : mountSettings(root)).catch(error=>{
  root.classList.add("boot-error"); root.textContent = `Murmur could not connect to its backend: ${String(error)}. Start with npm run tauri dev, not Vite alone.`;
});
