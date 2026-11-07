/**
 * Overlay buttons act on click but never take focus. Mouse down's default action is what moves
 * focus to a button; on Windows that would put keyboard focus into the overlay's WebView2 child
 * and away from the dictation target (B7, see src-tauri/src/overlay/noactivate.rs). Clicks and
 * the click handlers are unaffected.
 */
export function pressWithoutFocus(...buttons: HTMLElement[]): void {
  for (const button of buttons) button.addEventListener("mousedown",(event: Event)=>event.preventDefault());
}
