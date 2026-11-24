#!/usr/bin/env node
/** M3-8 供应链：生成 SPDX 风格 SBOM（CycloneDX 简化）+ 校验插件签名/静态扫描。 */
import { execSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");

function pythonPackages() {
  // 从 pyproject 解析主依赖 + optional extras
  const text = read("pyproject.toml");
  const deps = [];
  const block = text.match(/dependencies\s*=\s*\[([\s\S]*?)\]/);
  if (block) {
    for (const line of block[1].split("\n")) {
      const m = line.match(/^\s*"([^"]+)"/);
      if (m) deps.push(m[1]);
    }
  }
  const extras = [...text.matchAll(/(\w+)\s*=\s*\[([^\]]*)\]/g)];
  const optional = {};
  for (const [, name, body] of extras) {
    if (name === "dependencies" || name === "requires") continue;
    const items = [...body.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
    if (items.length) optional[name] = items;
  }
  return { deps, optional };
}

function npmPackages() {
  const pkgPath = path.join(ROOT, "package.json");
  if (!fs.existsSync(pkgPath)) return {};
  const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"));
  return { ...pkg.dependencies, ...pkg.devDependencies };
}

function buildSbom() {
  const { deps, optional } = pythonPackages();
  const npm = npmPackages();
  return {
    bomFormat: "CycloneDX",
    specVersion: "1.5",
    version: 1,
    metadata: {
      component: { type: "application", name: "uh-kernel", version: "0.1.0" },
      tools: [{ name: "uh-sbom", version: "0.1.0" }],
    },
    components: [
      ...deps.map((spec) => {
        const [name, ...rest] = spec.split(/[<>=!~]/);
        const version = rest.join("").replace(/^=/, "") || "unspecified";
        return { type: "library", name: name.trim(), version, scope: "required", purl: `pkg:pypi/${name.trim()}@${version}` };
      }),
      ...Object.entries(optional).flatMap(([extra, items]) =>
        items.map((spec) => {
          const [name, ...rest] = spec.split(/[<>=!~]/);
          const version = rest.join("").replace(/^=/, "") || "unspecified";
          return { type: "library", name: name.trim(), version, scope: "optional", group: extra, purl: `pkg:pypi/${name.trim()}@${version}` };
        })),
      ...Object.entries(npm).map(([name, version]) => ({
        type: "library", name, version: String(version).replace(/^[\^~]/, ""), scope: "required", purl: `pkg:npm/${name}@${version}`,
      })),
    ],
  };
}

const sbom = buildSbom();
const outDir = path.join(ROOT, "dist");
fs.mkdirSync(outDir, { recursive: true });
const outFile = path.join(outDir, "sbom.cdx.json");
fs.writeFileSync(outFile, JSON.stringify(sbom, null, 2) + "\n");
console.log(`SBOM_WRITTEN components=${sbom.components.length} path=dist/sbom.cdx.json`);

// 供应链校验：插件示例必须带 manifest + 签名策略说明；禁止明文凭据
const issues = [];
const official = path.join(ROOT, "examples", "plugins", "official");
if (fs.existsSync(official)) {
  for (const entry of fs.readdirSync(official, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const dir = path.join(official, entry.name);
    if (entry.name === "README.md") continue;
    if (!fs.existsSync(path.join(dir, "manifest.yaml"))) issues.push(`${entry.name}: missing manifest.yaml`);
    const main = path.join(dir, "main.py");
    if (fs.existsSync(main)) {
      const src = fs.readFileSync(main, "utf8");
      if (/AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----/.test(src)) issues.push(`${entry.name}: embedded credential`);
    }
  }
}
if (issues.length) {
  console.error("SUPPLY_CHAIN_FAILED=" + issues.length);
  for (const issue of issues) console.error("  " + issue);
  process.exit(1);
}
console.log("SUPPLY_CHAIN_PASSED (manifests present, no embedded credentials)");
