// P0-6 契约校验：manifest 四类 schema + events 18 事件，正例必过、负例必拒。
// 用法：node scripts/validate-contracts.mjs（CI 先 npm install ajv@8；本地可设 AJV_PATH 指向 ajv 模块路径）
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
const require = createRequire(import.meta.url);
const Ajv = require(process.env.AJV_PATH || "ajv");
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const load = (p) => JSON.parse(fs.readFileSync(path.join(ROOT, p), "utf8"));
const ajv = new Ajv({ allErrors: true, strict: false });
let fail = 0;
const check = (ok, label, errs) => {
  console.log((ok ? "PASS" : "FAIL") + "  " + label);
  if (!ok) { fail++; if (errs) console.log(JSON.stringify(errs).slice(0, 400)); }
};
// manifest
ajv.addSchema(load("docs/contracts/manifest/common.schema.json"), "common.schema.json");
const mv = {};
for (const t of ["tool", "theme", "capability", "hook"]) mv[t] = ajv.compile(load("docs/contracts/manifest/" + t + ".schema.json"));
const entry = ajv.compile(load("docs/contracts/manifest.schema.json"));
const ex = { tool: "tool.web-search.json", theme: "theme.dark.json", capability: "capability.ocr.json", hook: "hook.audit-log.json" };
for (const t of Object.keys(ex)) {
  const d = load("docs/contracts/manifest/examples/" + ex[t]);
  check(mv[t](d) && entry(d), "manifest example " + t, mv[t].errors || entry.errors);
}
for (const n of ["tool.missing-risk-level.json", "tool.outbound-no-hosts.json"]) {
  const d = load("docs/contracts/manifest/examples-negative/" + n);
  check(!mv.tool(d), "manifest negative " + n);
}
// events
const ev = ajv.compile(load("docs/contracts/events.schema.json"));
const evDir = "docs/contracts/events/examples";
const evFiles = fs.readdirSync(path.join(ROOT, evDir)).sort();
check(evFiles.length === 18, "events example count = 18 (actual " + evFiles.length + ")");
for (const f of evFiles) check(ev(load(evDir + "/" + f)), "event " + f, ev.errors);
const negDir = "docs/contracts/events/examples-negative";
for (const f of fs.readdirSync(path.join(ROOT, negDir)).sort()) check(!ev(load(negDir + "/" + f)), "event negative " + f);
console.log(fail === 0 ? "ALL_CONTRACT_CHECKS_PASSED" : "CONTRACT_CHECKS_FAILED=" + fail);
process.exit(fail === 0 ? 0 : 1);
