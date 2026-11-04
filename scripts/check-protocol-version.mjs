#!/usr/bin/env node
/**
 * The companion protocol version lives in three files. They must agree, or a Flipper and
 * a host will each think the other is broken. Runs as part of `npm run build`.
 */
import {readFileSync} from "node:fs";

const sources = [
  ["companion/protocol.md", /PROTOCOL_VERSION\s*=\s*(\d+)/],
  ["companion/flipper/src/protocol.h", /#define\s+MURMUR_PROTOCOL_VERSION\s+(\d+)/],
  ["src-tauri/src/companion/protocol.rs", /pub const PROTOCOL_VERSION:\s*u8\s*=\s*(\d+)\s*;/],
];

const found = [];
for (const [file, pattern] of sources) {
  const text = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
  const match = text.match(pattern);
  if (!match) {
    console.error(`protocol version check: ${file} has no version declaration`);
    process.exit(1);
  }
  found.push([file, Number(match[1])]);
}

const expected = found[0][1];
const mismatched = found.filter(([, version]) => version !== expected);
if (mismatched.length) {
  console.error(`protocol version mismatch: expected ${expected}`);
  for (const [file, version] of found) console.error(`  ${version}  ${file}`);
  process.exit(1);
}
console.log(`Protocol version check: ${expected} in ${found.length} files`);
