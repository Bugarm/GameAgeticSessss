import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.dirname(scriptDirectory);
const mermaidBundle = path.join(
  projectRoot,
  "gamelearn",
  "static",
  "vendor",
  "mermaid-11.12.1.min.js",
);

function decodeHtmlEntities(value) {
  return value
    .replace(/&#x([0-9a-f]+);/gi, (_, digits) => String.fromCodePoint(Number.parseInt(digits, 16)))
    .replace(/&#([0-9]+);/g, (_, digits) => String.fromCodePoint(Number.parseInt(digits, 10)))
    .replace(/&quot;/gi, '"')
    .replace(/&apos;|&#39;/gi, "'")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&amp;/gi, "&");
}

function mermaidDefinitions(documentText) {
  const definitions = [];
  const containers = /<(div|pre)\b([^>]*)>([\s\S]*?)<\/\1\s*>/gi;
  for (const match of documentText.matchAll(containers)) {
    const attributes = match[2];
    const classMatch = attributes.match(
      /\bclass\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/i,
    );
    const classes = (classMatch?.[1] ?? classMatch?.[2] ?? classMatch?.[3] ?? "")
      .split(/\s+/)
      .filter(Boolean);
    if (!classes.includes("mermaid")) continue;
    definitions.push(decodeHtmlEntities(match[3]).trim());
  }
  return definitions;
}

function conciseError(error) {
  const message = String(error?.message || error || "Unknown Mermaid parse error");
  return message.replace(/\x1b\[[0-9;]*m/g, "").split(/\r?\n/).slice(0, 8).join("\n");
}

async function main() {
  const documentPath = process.argv[2];
  if (!documentPath) {
    console.error("Usage: node tools/validate_mermaid.mjs <learning-page.html>");
    process.exitCode = 2;
    return;
  }
  if (!fs.existsSync(documentPath) || !fs.statSync(documentPath).isFile()) {
    console.error(`Learning-page file not found: ${documentPath}`);
    process.exitCode = 2;
    return;
  }

  const definitions = mermaidDefinitions(fs.readFileSync(documentPath, "utf8"));
  if (definitions.length === 0) {
    console.log("No Mermaid diagrams found.");
    return;
  }

  const browserBundle = fs.readFileSync(mermaidBundle, "utf8");
  const parserBundle = browserBundle.replace(
    "yh=pV()",
    "yh={sanitize:value=>value,addHook:()=>{},removeAllHooks:()=>{}}",
  );
  if (parserBundle === browserBundle) {
    throw new Error("The bundled Mermaid sanitizer hook could not be prepared for local parsing.");
  }
  vm.runInThisContext(parserBundle, {
    filename: mermaidBundle,
  });
  globalThis.mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    flowchart: { htmlLabels: false, useMaxWidth: true },
    sequence: { useMaxWidth: true, wrap: true },
  });

  let failures = 0;
  for (const [index, definition] of definitions.entries()) {
    try {
      await globalThis.mermaid.parse(definition, { suppressErrors: false });
      console.log(`Diagram ${index + 1}: valid`);
    } catch (error) {
      failures += 1;
      console.error(`Diagram ${index + 1}: invalid\n${conciseError(error)}`);
    }
  }
  if (failures > 0) process.exitCode = 1;
}

await main();
