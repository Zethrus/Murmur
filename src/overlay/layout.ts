/** Pure overlay layout helpers; no DOM globals so they stay unit-testable. */
export interface Size { width: number; height: number }

/** Parse a computed CSS length (always resolved to px by the engine) into a number. */
export function cssLength(value: string | null | undefined): number {
  const n = Number.parseFloat(value ?? "");
  return Number.isFinite(n) ? n : 0;
}

/** Window size needed for a rendered surface plus the canvas padding around it, rounded up to whole logical pixels. */
export function windowSize(surface: Size, padding: { top: number; right: number; bottom: number; left: number }): Size {
  return { width: Math.ceil(surface.width + padding.left + padding.right), height: Math.ceil(surface.height + padding.top + padding.bottom) };
}

/** Returns true when a measured size differs from the last reported one by at least one logical pixel. */
export function sizeChanged(previous: Size | undefined, next: Size): boolean {
  if (!(next.width > 0 && next.height > 0) || !Number.isFinite(next.width) || !Number.isFinite(next.height)) return false;
  return !previous || Math.abs(previous.width - next.width) >= 1 || Math.abs(previous.height - next.height) >= 1;
}

/** Snapshot revisions are monotonically increasing; equal or older revisions are stale. */
export function isNewer(current: number, incoming: number): boolean {
  return Number.isFinite(incoming) && incoming > current;
}
