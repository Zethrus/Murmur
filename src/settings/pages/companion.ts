import {call, type CompanionButtons, type CompanionConfig, type CompanionStatus, type CompanionNetworkStatus, type CompanionVerb} from "../../bridge";
import type {PageContext} from "../context";
import {card, row, stack, el, button, textField} from "../widgets/row";
import {select} from "../widgets/select";
import {toggle} from "../widgets/toggle";

/** Verbs the Flipper can send, in the order an admin is most likely to want them. */
const verbs: [CompanionVerb, string][] = [
  ["dictate_toggle", "Start / stop dictation"],
  ["enhance", "Enhance last dictation"],
  ["accept", "Accept suggestion"],
  ["dismiss", "Dismiss suggestion"],
  ["cancel", "Cancel dictation"],
  ["none", "Nothing"],
];

/** Physical buttons in reading order: config key, label, hint. */
const buttons: [keyof CompanionButtons, string, string][] = [
  ["ok", "OK", "In toggle mode, one press starts dictation and the next stops it."],
  ["up", "Up", "Press-only. Does nothing while a dictation is running."],
  ["down", "Down", "Copies the suggestion the overlay is showing."],
  ["back", "Back", "Dismisses the suggestion. Holding Back while listening asks to discard; otherwise it exits the app."],
  ["left", "Left", "Unused by default."],
  ["right", "Right", "Unused by default."],
  ["ok_long", "OK (long press)", "Hold for 500 ms or more."],
  ["up_long", "Up (long press)", "Hold for 500 ms or more."],
  ["down_long", "Down (long press)", "Hold for 500 ms or more."],
];

/**
 * Mirrors `Companion` in config.rs. The defaults matter here too: a backend that predates
 * this section, or a truncated config, must still render a usable page rather than throwing.
 */
const defaults: CompanionConfig = {
  enabled: false, ptt: false, transport: "auto", usb_port: "", ble_address: "",
  source: "auto", network: {enabled: false, bind: "127.0.0.1", port: 8787, mdns: false},
  buttons: {
    ok: "dictate_toggle", up: "enhance", down: "accept", back: "dismiss",
    left: "none", right: "none", ok_long: "cancel", up_long: "none", down_long: "none",
  },
};

export function companion(ctx: PageContext): HTMLElement {
  const incoming = ctx.config.companion;
  const c: CompanionConfig = {...defaults, ...(incoming ?? {}), buttons: {...defaults.buttons, ...(incoming?.buttons ?? {})}, network: {...defaults.network, ...(incoming?.network ?? {})}};
  const save = (patch: Partial<CompanionConfig>) => ctx.save({companion: patch});

  const dot = el("i", "status-dot"), headline = el("span", "status-label", "Companion disabled");
  const detail = el("p", "card-note", "Enable the companion, then connect the Flipper with a data cable.");
  const quality = el("p", "card-note", "Link quality: RSSI — · battery — · latency —");
  dot.setAttribute("aria-hidden", "true");

  const render = (status: CompanionStatus) => {
    quality.textContent = `Link quality: RSSI ${status.rssi == null ? "—" : `${status.rssi} dBm`} · battery ${status.battery == null ? "—" : `${status.battery}%`} · latency ${status.connected ? `${status.latency_ms} ms` : "—"}`;
    dot.classList.toggle("ok", status.connected);
    headline.textContent = status.connected
      ? `Connected${status.device ? ` · ${status.device}` : ""}`
      : status.enabled
        ? "Waiting for the Flipper"
        : "Companion disabled";
    if (status.error) detail.textContent = status.error;
    else if (!status.enabled) detail.textContent = "Nothing is listening. Dictation and shortcuts are unaffected.";
    else if (!status.connected) detail.textContent = `${status.transport} · ${status.port || "no device found"}. Open the Murmur app on the Flipper.`;
    else detail.textContent = `${status.port} · protocol ${status.protocol}${status.last_button ? ` · last press ${status.last_button}` : ""}`;
  };

  call("companion_status").then(render).catch(ctx.report);
  ctx.subscribe("companion", render);

  const saveVerb = (key: keyof CompanionButtons, verb: string) =>
    save({buttons: {...c.buttons, [key]: verb as CompanionVerb}});

  const port = textField(c.usb_port, (value) => save({usb_port: value}), "auto-discover");
  const detect = button("Detect devices", async () => {
    try {
      const ports = await call("companion_ports");
      detail.textContent = ports.length
        ? `Found: ${ports.join(", ")}`
        : "No Flipper serial device found. Use a data cable, not a charge-only one.";
    } catch (e) { ctx.report(e); }
  });

  const fingerprint = el("p", "card-note", "Fingerprint: —");
  const pairing = el("p", "card-note", "Pairing code: hidden");
  const devices = el("div", "stack");
  const networkNote = el("p", "card-note", "Phone listener off");
  const liveSource = el("p", "card-note", `Live dictation source: ${ctx.status.source ?? "idle"}`);
  ctx.subscribe("state", ({source}) => {
    if (source !== undefined) liveSource.textContent = `Live dictation source: ${source ?? "idle"}`;
  });
  const renderNetwork = (status: CompanionNetworkStatus) => {
    fingerprint.textContent = `TLS fingerprint: ${status.fingerprint ?? "—"}`;
    networkNote.textContent = status.error ?? (status.enabled ? `Listening on ${c.network.bind}:${c.network.port} · refused peers ${status.rejected_peers} · dropped audio frames ${status.dropped_audio_frames}` : "Phone listener off");
    devices.replaceChildren(...(status.devices.length ? status.devices.map(device =>
      row(device.name || "Phone", `Paired ${new Date(device.created_at * 1000).toLocaleString()} · last seen ${device.last_seen ? new Date(device.last_seen * 1000).toLocaleString() : "never"}`,
        button("Revoke", async () => { try { await call("companion_revoke_device", {id: device.id}); await refreshNetwork(); } catch (e) { ctx.report(e); } }))) : [el("p", "card-note", "No paired phones.")]));
  };
  const refreshNetwork = async () => { renderNetwork(await call("companion_network_status")); };
  let polling = true, refresh: ReturnType<typeof setTimeout> | undefined;
  const poll = async () => {
    try { await refreshNetwork(); } catch (e) { ctx.report(e); }
    if (polling) refresh = setTimeout(() => { void poll(); }, 2000);
  };
  void poll();
  ctx.cleanup(() => { polling = false; clearTimeout(refresh); });
  const bind = textField(c.network.bind, (value) => save({network: {...c.network, bind: value}}), "127.0.0.1");
  const networkPort = textField(String(c.network.port), async (value) => {
    const port = Number(value);
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Port must be 1–65535");
    await save({network: {...c.network, port}});
  }, "8787");

  return stack(
    card("Flipper Zero",
      stack(dot, headline),
      detail,
      quality,
      row("Enable companion", "Listens for the Flipper over the selected link. Off by default, and it never touches dictation when disabled.",
        toggle(c.enabled, (on) => save({enabled: on}))),
      row("Transport", "Choose a cable, Bluetooth, or automatic discovery.",
        select(c.transport, [["auto", "Automatic"], ["usb", "USB cable"], ["ble", "Bluetooth"]],
          (value) => save({transport: value as CompanionConfig["transport"]}))),
      row("Serial device", "Leave blank to discover the Flipper automatically. Only needed when several ACM devices are attached.",
        stack(port, detect)),
      row("BLE device", "Leave blank to discover the Flipper automatically.",
        textField(c.ble_address, (value) => save({ble_address: value}), "auto-discover")),
      row("Push-to-talk", "Hold OK to dictate and release it to stop.",
        toggle(c.ptt, (on) => save({ptt: on}))),
      row("Reconnect", "Re-pushes the button map and retries the link without restarting Murmur.",
        button("Reconnect now", async () => { await call("companion_resync"); }))),
    card("Buttons",
      el("p", "card-note", "One press per action. The map is pushed to the device the moment you change it, and cached there so it survives a reconnect."),
      ...buttons.map(([key, label, hint]) =>
        row(label, hint, select(c.buttons[key], verbs, (verb) => saveVerb(key, verb))))),
    card("Phone over WiFi",
      networkNote,
      liveSource,
      row("Audio source", "Choose the microphone for each dictation turn. Automatic uses a connected phone.",
        select(c.source, [["auto", "Automatic"], ["local", "Built-in microphone"], ["phone", "Phone microphone"]],
          (value) => save({source: value as CompanionConfig["source"]}))),
      row("Enable phone listener", "TLS only. Connections are accepted from local addresses and require pairing.",
        toggle(c.network.enabled, (enabled) => save({network: {...c.network, enabled}}))),
      row("Bind address", "Use your LAN address to connect from a phone. Loopback allows local testing.", bind),
      row("Port", "Port for the phone connection.", networkPort),
      fingerprint,
      row("Pairing", "A new six-digit code replaces the old one and expires after five minutes.",
        button("Show pairing code", async () => {
          try { const result = await call("companion_pairing_code"); pairing.textContent = `Pairing code: ${result.code} · expires ${new Date(result.expires_at * 1000).toLocaleTimeString()}`; }
          catch (e) { ctx.report(e); }
        })),
      pairing,
      el("h3", "card-title", "Paired phones"), devices),
    card("Installing the app",
      el("p", "card-note", "The companion app is built from companion/flipper in this repository. Copy murmur_companion.fap to the Flipper's apps folder, or run ufbt launch with the device connected."),
      el("p", "card-note", "While the app holds the USB serial port, qFlipper and the Flipper mobile app cannot use it. Exiting the app or unplugging restores normal behaviour.")),
  );
}
