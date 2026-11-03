import { readdir, readFile } from "node:fs/promises";
import { join, relative } from "node:path";
import assert from "node:assert/strict";

const PRIMITIVE = /var\(--(?:grey-|blue-|amber-|green-|red-|white\b|transparent\b|sp-|fs-|r-|dur-|weight-|lh-|z-|shadow-(?:light|dark)-|overlay-(?:light|dark)\b|tracking-|ease-|border-(?:thin|focus)\b)/;
export function violations(file, source) {
  if (file.replaceAll("\\", "/").endsWith("src/styles/tokens.css")) return [];
  const errors = [];
  const lines = source.split("\n");
  lines.forEach((line, i) => {
    const fail = message => errors.push(`${file}:${i + 1}: ${message}`);
    if (/#(?:[a-f\d]{8}|[a-f\d]{6}|[a-f\d]{3})\b|\b(?:rgba?|hsla?)\s*\(/i.test(line)) fail("literal colour outside tokens.css");
    if (file.endsWith(".css")) {
      for (const match of line.matchAll(/(?:border-radius|padding(?:-\w+)?)\s*:\s*([^;}]+)/g)) {
        if (!/^var\(--[\w-]+\)(?:\s+var\(--[\w-]+\))*$/.test(match[1].trim())) fail("padding/radius must reference a token");
      }
      if (/(?:\d|\.)\s*(?:px|rem|em|vh|vw|pt)\b/.test(line)) fail("literal dimension outside tokens.css");
    }
    if (PRIMITIVE.test(line)) fail("component reads a primitive token");
    if (file.endsWith(".ts")) {
      // Dimension-bearing style strings: "12px", `${n}px`, "1.5rem", and inline style writes of numeric sizes.
      if (/(?:\d|\})(?:px|rem|em|vh|vw|pt)\b(?=["'`;\s)]|$)/.test(line) && /["'`]/.test(line)) fail("literal dimension in TS style string");
      if (/\.style\.(?:width|height|min\w*|max\w*|padding\w*|margin\w*|gap|top|left|right|bottom|borderRadius|fontSize|inset)\s*=/.test(line)) fail("inline dimension style in TS; use a token or class");
    }
  });
  return errors;
}
async function files(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  return (await Promise.all(entries.map(entry => entry.isDirectory()
    ? files(join(directory, entry.name)) : [join(directory, entry.name)]))).flat();
}
if (process.argv.includes("--self-test")) {
  const cases = [
    ["src/a.css", "a { color: #aabbcc; }", true], ["src/a.ts", "const color = 'rgb(1,2,3)';", true],
    ["src/a.css", "a { padding: 4px; }", true], ["src/a.css", "a { border-radius: 0; }", true],
    ["src/a.css", "a { margin: 1rem; }", true], ["src/a.css", "a { gap: var(--sp-1); }", true],
    ["src/a.css", "a { padding: var(--card-inset); color: var(--text); }", false],
    ["src/a.css", "a { padding: var(--card-inset) 5%; }", true],
    ["src/a.ts", "const color = '#123456';", true],
    ["src/styles/tokens.css", ":root { --accent: #aabbcc; }", false],
    ["src/a.css", "a { height: 100vh; }", true], ["src/a.css", "a { color: var(--white); }", true],
    ["src/a.css", "a { transition: opacity var(--ease-out); }", true],
    ["src/a.ts", "el.style.setProperty('--gap', '12px');", true], ["src/a.ts", "const w = `${width}px`;", true],
    ["src/a.ts", "node.style.width = String(n);", true], ["src/a.ts", "const v = 'var(--sp-2)';", true],
    ["src/a.ts", "bar.style.setProperty('--level', String(0.12));", false],
    ["src/a.ts", "const label = '12 px wide';", false], ["src/a.ts", "const mb = `${n} MB`;", false],
  ];
  for (const [file, source, bad] of cases) assert.equal(violations(file, source).length > 0, bad);
  console.log(`Token lint self-tests: ${cases.length} passed`);
} else {
  const sources = (await files("src")).filter(file => /\.(css|ts)$/.test(file));
  const failures = (await Promise.all(sources.map(async file => violations(relative(".", file), await readFile(file, "utf8"))))).flat();
  if (failures.length) { console.error(failures.join("\n")); process.exitCode = 1; }
  else console.log(`Token lint: ${sources.length} files checked, 0 violations`);
}
