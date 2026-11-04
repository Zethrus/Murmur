import {call, type CompanionButtons, type CompanionConfig, type CompanionStatus, type CompanionVerb} from "../../bridge";
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
  ["ok", "OK", "The obvious one: one press to talk, one to stop."],
  ["up", "Up", "Press-only. Does nothing while a dictation is running."],
  ["down", "Down", "Copies the suggestion the overlay is showing."],
  ["back", "Back", "Dismisses the suggestion without copying it. Holding Back exits the app on the device."],
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
  enabled: false, transport: "auto", usb_port: "", ble_address: "",
  buttons: {
    ok: "dictate_toggle", up: "enhance", down: "accept", back: "dismiss",
    left: "none", right: "none", ok_long: "cancel", up_long: "none", down_long: "none",
  },
};

export function companion(ctx: PageContext): HTMLElement {
  const incoming = ctx.config.companion;
  const c: CompanionConfig = {...defaults, ...(incoming ?? {}), buttons: {...defaults.buttons, ...(incoming?.buttons ?? {})}};
  const save = (patch: Partial<CompanionConfig>) => ctx.save({companion: patch});

  const dot = el("i", "status-dot"), headline = el("span", "status-label", "Companion disabled");
  const detail = el("p", "card-note", "Enable the companion, then connect the Flipper with a data cable.");
  dot.setAttribute("aria-hidden", "true");

  const render = (status: CompanionStatus) => {
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

  return stack(
    card("Flipper Zero",
      stack(dot, headline),
      detail,
      row("Enable companion", "Listens for the Flipper over the selected link. Off by default, and it never touches dictation when disabled.",
        toggle(c.enabled, (on) => save({enabled: on}))),
      row("Transport", "USB is the wired path. Bluetooth arrives in the next phase and needs pairing on both ends.",
        select(c.transport, [["auto", "Automatic (USB first)"], ["usb", "USB cable"], ["ble", "Bluetooth (next phase)"]],
          (value) => save({transport: value as CompanionConfig["transport"]}))),
      row("Serial device", "Leave blank to discover the Flipper automatically. Only needed when several ACM devices are attached.",
        stack(port, detect)),
      row("Reconnect", "Re-pushes the button map and retries the link without restarting Murmur.",
        button("Reconnect now", async () => { await call("companion_resync"); }))),
    card("Buttons",
      el("p", "card-note", "One press per action. The map is pushed to the device the moment you change it, and cached there so it survives a reconnect."),
      ...buttons.map(([key, label, hint]) =>
        row(label, hint, select(c.buttons[key], verbs, (verb) => saveVerb(key, verb))))),
    card("Installing the app",
      el("p", "card-note", "The companion app is built from companion/flipper in this repository. Copy murmur_companion.fap to the Flipper's apps folder, or run ufbt launch with the device connected."),
      el("p", "card-note", "While the app holds the USB serial port, qFlipper and the Flipper mobile app cannot use it. Exiting the app or unplugging restores normal behaviour.")),
  );
}
