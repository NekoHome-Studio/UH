// UH! 契约一键验证
// 用法：node scripts/verify-all.mjs          （fast：离线，秒级）
//       node scripts/verify-all.mjs --full   （追加 OpenAPI/AsyncAPI 官方校验，需网络）
//       node scripts/verify-all.mjs --remote （追加 git 远端同步核对）
// 依赖自动安装到 .work/verify-deps（gitignored，不污染仓库）
import fs from "node:fs";
import path from "node:path";
import { execSync } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { verifyComposeExposure } from "./verify-compose-exposure.mjs";
import { readK8sManifests, verifyK8sManifests } from "./verify-k8s-manifests.mjs";
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DEPS = path.join(ROOT, ".work", "verify-deps");
const FULL = process.argv.includes("--full");
const REMOTE = process.argv.includes("--remote");
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");
const results = [];
const check = (name, ok, detail, info = false) => {
  results.push({ name, ok, info });
  console.log((info ? "INFO" : ok ? "PASS" : "FAIL") + "  " + name + (detail ? "  | " + detail : ""));
};
const childError = (error) => String(error?.stderr || error?.stdout || error?.message || "").trim().slice(0, 400);
const ensure = (pkgs) => {
  fs.mkdirSync(DEPS, { recursive: true });
  const missing = pkgs.filter(p => !fs.existsSync(path.join(DEPS, "node_modules", p)));
  if (missing.length) {
    console.log("安装依赖: " + missing.join(" ") + " → .work/verify-deps");
    execSync("npm install --prefix " + JSON.stringify(DEPS) + " --no-audit --no-fund --loglevel=error " + missing.join(" "), { stdio: "inherit", cwd: ROOT });
  }
  return createRequire(path.join(DEPS, "noop.js"));
};
const req = ensure(["ajv", "js-yaml"]);
const Ajv = req("ajv");
const yaml = req("js-yaml");
const ajv = new Ajv({ allErrors: true, strict: false });

// A. manifest 四类 schema + 入口
ajv.addSchema(JSON.parse(read("docs/contracts/manifest/common.schema.json")), "common.schema.json");
const mv = {};
for (const t of ["tool", "theme", "capability", "hook"]) mv[t] = ajv.compile(JSON.parse(read("docs/contracts/manifest/" + t + ".schema.json")));
const entry = ajv.compile(JSON.parse(read("docs/contracts/manifest.schema.json")));
const ex = { tool: "tool.web-search.json", theme: "theme.dark.json", capability: "capability.ocr.json", hook: "hook.audit-log.json" };
let aOk = true, aDetail = [];
for (const t of Object.keys(ex)) {
  const d = JSON.parse(read("docs/contracts/manifest/examples/" + ex[t]));
  const ok = mv[t](d) && entry(d);
  aOk = aOk && ok; if (!ok) aDetail.push(t);
}
for (const n of ["tool.missing-risk-level.json", "tool.outbound-no-hosts.json"]) {
  const ok = !mv.tool(JSON.parse(read("docs/contracts/manifest/examples-negative/" + n)));
  aOk = aOk && ok; if (!ok) aDetail.push("neg:" + n);
}
check("A manifest schema（4 正例 + 2 负例 + 入口判别）", aOk, aDetail.join("; ") || "ajv " + req("ajv/package.json").version);

// B. events schema
const ev = ajv.compile(JSON.parse(read("docs/contracts/events.schema.json")));
const evFiles = fs.readdirSync(path.join(ROOT, "docs/contracts/events/examples")).sort();
const negFiles = fs.readdirSync(path.join(ROOT, "docs/contracts/events/examples-negative")).sort();
let bOk = evFiles.length === 18 && negFiles.length === 7;
for (const f of evFiles) bOk = bOk && ev(JSON.parse(read("docs/contracts/events/examples/" + f)));
for (const f of negFiles) bOk = bOk && !ev(JSON.parse(read("docs/contracts/events/examples-negative/" + f)));
check("B events schema（18 正例 + 7 负例）", bOk, evFiles.length + "+" + negFiles.length);

// B2. 运行时双向门禁：Python 映射器必须拒绝未知/非法外部事件，且内部名遵守 ADR-002。
try {
  const validProbe = "from kernel.event_mapping import validate_external_event; from kernel.protocol import validate_event_name; import json; validate_external_event(json.loads(open('docs/contracts/events/examples/09-delta.json',encoding='utf-8').read())); validate_event_name('capability.module.invoked')";
  execSync("python -c " + JSON.stringify(validProbe), { cwd: ROOT, stdio: "pipe" });
  const rejected = (probe) => { try { execSync("python -c " + JSON.stringify(probe), { cwd: ROOT, stdio: "pipe" }); return false; } catch { return true; } };
  const unknownExternal = "from kernel.event_mapping import validate_external_event; import json; e=json.loads(open('docs/contracts/events/examples/09-delta.json',encoding='utf-8').read()); e['event']='future.event'; validate_external_event(e)";
  const badInternal = "from kernel.protocol import validate_event_name; validate_event_name('capability.invoked')";
  if (!rejected(unknownExternal) || !rejected(badInternal)) throw new Error("negative probe unexpectedly accepted");
  check("B2 运行时映射双向校验 + ADR-002 三段命名", true, "push/subscribe negative probe");
} catch (e) {
  check("B2 运行时映射双向校验 + ADR-002 三段命名", false, childError(e));
}

// C. 交叉一致性预检（8 类）
try {
  execSync("node " + JSON.stringify(path.join(ROOT, "scripts/review-precheck.mjs")), { cwd: ROOT, env: { ...process.env, YAML_PATH: path.join(DEPS, "node_modules", "js-yaml") }, stdio: "pipe" });
  check("C 交叉一致性预检（8 类）", true, "详见 scripts/review-precheck.mjs 输出");
} catch (e) {
  check("C 交叉一致性预检（8 类）", false, childError(e));
}

// D. OpenAPI 端点覆盖（19 操作）
const oapi = yaml.load(read("docs/contracts/openapi.yaml"));
const expected = [["post","/sessions"],["get","/sessions/{id}"],["get","/sessions/{id}/messages"],["post","/sessions/{id}/messages"],["delete","/sessions/{id}"],["get","/tasks/{id}"],["get","/tasks/{id}/steps"],["post","/tasks/{id}/cancel"],["post","/tasks/{id}/resume"],["post","/tasks/{id}/confirm"],["get","/plugins"],["post","/plugins"],["delete","/plugins/{name}"],["patch","/plugins/{name}/config"],["get","/memory/short-term"],["delete","/memory/long-term"],["get","/health"],["get","/ready"],["get","/metrics"]];
const missing = expected.filter(([m, p]) => !(oapi.paths[p] && oapi.paths[p][m]));
check("D OpenAPI 端点覆盖（19/19）", missing.length === 0, missing.length ? JSON.stringify(missing) : Object.keys(oapi.paths).length + " 路径");

// E. 基线待修订项（信息项：docx 冲突应在评审后按修订对照消除）
const v3p = path.join(ROOT, ".work/docx-v3-text.txt");
if (fs.existsSync(v3p)) {
  const t = fs.readFileSync(v3p, "utf8");
  const markers = { "UH-CAP-50210（五位数笔误）": (t.match(/UH-CAP-50210/g) || []).length, "UH-LLM-*（域冲突）": (t.match(/UH-LLM-\d{4}/g) || []).length, "UH-KRN-1001（二义）": (t.match(/UH-KRN-1001/g) || []).length };
  check("E v3 基线待修订项（评审后应用修订对照消除）", true, Object.entries(markers).map(([k, v]) => k + "=" + v).join(", "), true);
} else {
  check("E v3 基线待修订项", true, "提取文本不存在，跳过（pwsh .work/extract-docx-param.ps1 可再生成）", true);
}

if (FULL) {
  // F. OpenAPI 官方校验
  try {
    const out = execSync("npx --yes @apidevtools/swagger-cli validate docs/contracts/openapi.yaml", { cwd: ROOT, env: { ...process.env, CI: "true" }, stdio: "pipe" }).toString();
    check("F swagger-cli validate（OpenAPI 3.1）", /valid/.test(out), out.trim().split("\n").pop());
  } catch (e) { check("F swagger-cli validate", false, childError(e)); }
  // G. AsyncAPI 官方 parser
  try {
    const req2 = ensure(["@asyncapi/parser"]);
    const { Parser } = req2("@asyncapi/parser");
    const { document, diagnostics } = await new Parser().parse(read("docs/contracts/asyncapi.yaml"));
    const errs = (diagnostics || []).filter(d => d.severity === 0 || d.severity === "error");
    check("G @asyncapi/parser（AsyncAPI 2.6）", !!document && errs.length === 0, "errors=" + errs.length + " version=" + (document && document.version()));
  } catch (e) { check("G @asyncapi/parser", false, childError(e)); }
}
if (REMOTE) {
  try {
    const local = execSync("git rev-parse HEAD", { cwd: ROOT }).toString().trim();
    const remote = execSync("git ls-remote origin refs/heads/main", { cwd: ROOT, env: { ...process.env, GIT_SSH_COMMAND: "ssh -o BatchMode=yes -o ConnectTimeout=15" } }).toString().trim().split(/\s/)[0];
    check("H git 远端同步", local === remote, local.slice(0, 7) + " vs " + remote.slice(0, 7));
  } catch (e) { check("H git 远端同步", false, String(e.message).slice(0, 150)); }
}
try {
  const errors = verifyComposeExposure(yaml.load(read("docker-compose.yaml")));
  check("I Compose 暴露面与密钥引用", errors.length === 0, errors.join("; ") || "7 services, 1 published port");
} catch {
  check("I Compose 暴露面与密钥引用", false, "无法读取 Compose YAML");
}
try {
  const errors = verifyK8sManifests(readK8sManifests(yaml));
  check("J K8s 工作负载、RBAC 与 NetworkPolicy", errors.length === 0, errors.join("; ") || "7 traffic allowances, scoped scaler RBAC");
} catch {
  check("J K8s 工作负载、RBAC 与 NetworkPolicy", false, "无法读取或校验 K8s YAML");
}
const failed = results.filter(r => !r.ok && !r.info);
console.log(failed.length === 0 ? "VERIFY_ALL_PASSED (" + results.length + " checks" + (FULL ? ", full" : ", fast") + ")" : "VERIFY_FAILED=" + failed.length);
process.exit(failed.length === 0 ? 0 : 1);
