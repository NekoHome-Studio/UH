// MS-0 评审预检：契约交叉一致性扫描（P0-1~6 交付物互核）
// 用法：node scripts/review-precheck.mjs（依赖 js-yaml；本地可设 YAML_PATH 指向模块）
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
const require = createRequire(import.meta.url);
const yaml = require(process.env.YAML_PATH || "js-yaml");
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");
const walk = (dir, exts) => {
  const out = [];
  for (const e of fs.readdirSync(path.join(ROOT, dir), { withFileTypes: true })) {
    const rel = dir + "/" + e.name;
    if (e.isDirectory()) out.push(...walk(rel, exts));
    else if (exts.some(x => e.name.endsWith(x))) out.push(rel);
  }
  return out;
};
const results = [];
const check = (name, ok, detail) => { results.push({ name, ok, detail }); console.log((ok ? "PASS" : "FAIL") + "  " + name + (detail ? "  | " + detail : "")); };

// 基准集
const errorsMd = read("docs/contracts/errors.md");
const activeCodes = [...errorsMd.split("## 2.")[1].split("## 3.")[0].matchAll(/^\| (UH-[A-Z]+-\d{4}) \|/gm)].map(m => m[1]);
const deprecatedCodes = [...errorsMd.split("## 5.")[1].split("## 6.")[0].matchAll(/^\| (UH-[A-Z]+-\d{4}) \|/gm)].map(m => m[1]);
const evSchema = JSON.parse(read("docs/contracts/events.schema.json"));
const extEvents = evSchema.properties.event.enum;
const internalEvents = ["kernel.request.received","kernel.route.decided","capability.invoked","capability.finished","capability.failed","memory.write","llm.delta","mcp.tool.call","mcp.tool.result","tool.confirm","tool.confirm.resolved","kernel.task.cancelled","kernel.task.resumed","kernel.task.degraded","kernel.task.finished","kernel.task.failed","cua.yield","route.decided"];
const eventAllow = new Set([...extEvents, ...internalEvents]);

// 1) 错误码：未知码硬失败；deprecated 仅限历史/修订语境
const codeFiles = [...walk("docs/contracts", [".md", ".yaml", ".json"]), "README.md", "PROGRESS.md", ...walk("uh-issues", [".md", ".csv"])];
const deprecatedAllowed = (f) => f.startsWith("docs/contracts/revisions/") || f.startsWith("uh-issues/") || f === "PROGRESS.md" || f === "docs/contracts/errors.md";
const unknown = [], depMisuse = [];
for (const f of codeFiles) {
  const text = read(f);
  for (const m of text.matchAll(/UH-[A-Z]+-\d{4}/g)) {
    const c = m[0];
    if (!activeCodes.includes(c) && !deprecatedCodes.includes(c)) unknown.push(f + ": " + c);
    else if (deprecatedCodes.includes(c) && !deprecatedAllowed(f)) depMisuse.push(f + ": " + c);
  }
}
check("1 错误码注册一致（13 有效 + 4 deprecated）", unknown.length === 0 && depMisuse.length === 0, "未知=" + unknown.length + " 误用=" + depMisuse.length + (unknown.length ? " " + unknown.slice(0,5).join("; ") : "") + (depMisuse.length ? " " + depMisuse.slice(0,5).join("; ") : ""));

// 2) 事件名：外部 18 + 内部登记，三源一致
const asyncapi = yaml.load(read("docs/contracts/asyncapi.yaml"));
const aaEvents = asyncapi.channels["/api/v1/stream"].subscribe.message.oneOf.map(m => m.name).filter(n => extEvents.includes(n));
const evMdTable = [...errorsMd.matchAll(/x/g)]; // placeholder no-op
const eventsMd = read("docs/contracts/events.md");
const mdEvents = [...eventsMd.split("## 3.")[1].split("## 4.")[0].matchAll(/\| ((?:(?:task|step|safety|tool)\.[a-z_]+)|delta) \|/g)].map(m => m[1]);
const threeSource = JSON.stringify([...extEvents].sort()) === JSON.stringify([...aaEvents].sort()) && JSON.stringify([...extEvents].sort()) === JSON.stringify([...mdEvents].sort());
const badEventRefs = [];
const nonEventTokens = new Set(["memory.short_term.read", "memory.short_term.write", "fs.sandbox.read", "fs.sandbox.write", "fs.host.read", "memory.max", "cpu.max", "pids.max", "tool.network_allow_hosts", "kernel.task", "mcp.tool"]);
const isNonEvent = (t) => {
  if (nonEventTokens.has(t)) return true;
  if (/\.(json|md|yaml|yml|csv|mjs|cjs|ps1|txt|docx|png|sh)$/.test(t)) return true;
  const seg = t.split(".");
  if (seg.length === 3 && eventAllow.has(seg[0] + "." + seg[1])) return true; // 已知事件上的字段路径
  return false;
};
for (const f of [...walk("docs/contracts", [".md", ".yaml"]), ...walk("uh-issues/issues", [".md"]), "README.md", "PROGRESS.md"]) {
  for (const m of read(f).matchAll(/\b(?:task|step|safety|tool|kernel|capability|memory|mcp|llm|cua|route)(?:\.[a-z_]+){1,2}\b/g)) {
    if (!eventAllow.has(m[0]) && !isNonEvent(m[0])) badEventRefs.push(f + ": " + m[0]);
  }
}
check("2 事件名三源一致 + 引用受控（18 外部）", threeSource && badEventRefs.length === 0, "三源=" + threeSource + " 越界引用=" + badEventRefs.length + (badEventRefs.length ? " " + [...new Set(badEventRefs)].slice(0,6).join("; ") : ""));

// 3) 枚举一致：module / risk_level / mode
const openapi = yaml.load(read("docs/contracts/openapi.yaml"));
const modSchema = evSchema.$defs.p_step_started.properties.module.enum;
const modOapi = openapi.components.schemas.Step.properties.module.enum;
const riskManifest = JSON.parse(read("docs/contracts/manifest/tool.schema.json")).properties.risk_level.enum;
const riskEvents = evSchema.$defs.p_tool_call.properties.risk_level.enum;
const riskOapi = openapi.components.schemas.PluginInfo.properties.risk_level.enum;
const modeEvents = evSchema.$defs.p_task_created.properties.mode.enum;
const modeOapi = openapi.components.schemas.TaskStatus.properties.mode.enum;
const eq = (a, b) => JSON.stringify([...a].sort()) === JSON.stringify([...b].sort());
check("3 枚举一致（module/risk_level/mode）", eq(modSchema, modOapi) && eq(riskManifest, riskEvents) && eq(riskManifest, riskOapi) && eq(modeEvents, modeOapi), "module=" + eq(modSchema, modOapi) + " risk=" + (eq(riskManifest, riskEvents) && eq(riskManifest, riskOapi)) + " mode=" + eq(modeEvents, modeOapi));

// 4) 文档链接可达
const badLinks = [];
for (const f of [...walk("docs/contracts", [".md"]), "README.md", "PROGRESS.md", ...walk("uh-issues", [".md"])]) {
  const dir = path.dirname(f);
  for (const m of read(f).matchAll(/\]\(([^)\s]+)\)/g)) {
    const t = m[1];
    if (/^(https?:|mailto:|#)/.test(t)) continue;
    const clean = t.split("#")[0];
    if (!clean) continue;
    if (!fs.existsSync(path.join(ROOT, dir, clean))) badLinks.push(f + " -> " + t);
  }
}
check("4 文档内链可达", badLinks.length === 0, "死链=" + badLinks.length + (badLinks.length ? " " + badLinks.slice(0,6).join("; ") : ""));

// 5) 数量基准
const nIssues = fs.readdirSync(path.join(ROOT, "uh-issues/issues")).filter(f => f.endsWith(".md")).length;
const nCsv = read("uh-issues/issues.csv").trim().split("\n").length - 1;
const nMap = read("uh-issues/issue-map.csv").trim().split("\n").length - 1;
const nEvEx = fs.readdirSync(path.join(ROOT, "docs/contracts/events/examples")).length;
const nEvNeg = fs.readdirSync(path.join(ROOT, "docs/contracts/events/examples-negative")).length;
check("5 数量基准（39/39/39/18/7）", nIssues === 39 && nCsv === 39 && nMap === 39 && nEvEx === 18 && nEvNeg === 7, [nIssues, nCsv, nMap, nEvEx, nEvNeg].join("/"));

// 6) 信封字段一致
const envReq = evSchema.required;
const aaReq = asyncapi.channels["/api/v1/stream"].subscribe.message.oneOf[0].payload.required;
check("6 信封必填字段一致", eq(envReq, aaReq), envReq.join(","));

// 7) 契约区无残留占位符
const ph = [];
for (const f of walk("docs/contracts", [".md", ".yaml", ".json"])) if (/\{\{[A-Za-z0-9-]+\}\}/.test(read(f))) ph.push(f);
check("7 契约区无 {{ID}} 残留", ph.length === 0, ph.join("; "));

// 8) SBX 引用受控
const sbxDefined = [...read("docs/contracts/sandbox.md").matchAll(/SBX-0\d/g)].map(m => m[0]);
const sbxRefs = [];
for (const f of walk("uh-issues/issues", [".md"])) for (const m of read(f).matchAll(/SBX-0\d/g)) if (!sbxDefined.includes(m[0])) sbxRefs.push(f + ": " + m[0]);
check("8 SBX 引用受控（01~09 已定义）", sbxRefs.length === 0, "定义=" + [...new Set(sbxDefined)].sort().join(",") + " 越界=" + sbxRefs.length);

const failed = results.filter(r => !r.ok);
console.log(failed.length === 0 ? "PRECHECK_ALL_PASSED (" + results.length + " checks)" : "PRECHECK_FAILED=" + failed.length);
fs.writeFileSync(path.join(ROOT, ".work/precheck-results.json"), JSON.stringify(results, null, 2));
process.exit(failed.length === 0 ? 0 : 1);
