import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DEFAULT_COMPOSE = fileURLToPath(new URL("../docker-compose.yaml", import.meta.url));
const KERNEL = "uh-kernel";
const CAPABILITIES = ["uh-llama-index", "uh-langgraph", "uh-crewai"];
const SERVICES = [KERNEL, ...CAPABILITIES, "redis", "chroma", "postgres"];
const SECRET_NAME = /(?:^|[_-])(?:password|passwd|token|key|secret)(?:s)?(?:$|[_-])|^pgpassword$/i;
const SECRET_METADATA = /(?:[_-](?:file|source|path|required|ttl|ttl_s|id|name))$/i;
const ENV_REF = /^(?:\$\{[A-Z_][A-Z0-9_]*(?::?\?[^}]*|:-)?\})+$/;
// R2 审查 P1-1：${VAR:?message} / ${VAR:-default} 的默认值可夹带明文凭据。
// 原实现把整个 ${...} 判为合法引用放行，且查内联凭据前先把它整段删除，
// message 从未被检查（实测 ${VAR:?Set YOUR_VALUE} 门禁 0 errors）。
const ENV_DEFAULT = /\$\{[A-Z_][A-Z0-9_]*:?[?-]([^}]*)\}/g;

// 默认值位置没有变量名上下文，用「长度 + 字符集 + 熵」判定真实凭据；
// 排除提示语（Set DB_PASSWORD）、文件路径、纯变量名与合成样本。
function defaultLooksLikeCredential(value) {
  const v = String(value).trim().replace(/^["']|["']$/g, "");
  if (v.length < 8 || v.length > 512) return false;
  if (/^[A-Z][A-Z0-9_]*$/.test(v)) return false;
  if (/^(?:Set|Provide|Enter|Supply|Missing|Required|Must|your|change|replace)\b/i.test(v)) return false;
  if (/[a-z]{3,}\s+[A-Za-z]/.test(v)) return false;
  if (/^\/(?:run|etc|var|home|opt)\//.test(v)) return false;
  if (/placeholder|sample|example|dummy|redacted|^abcdefg/i.test(v)) return false;
  // 无凭据的 URL / 主机端口 / 模型名是正常默认值，不是密钥：
  //   https://api.openai.com/v1 · redis://redis:6379 · chroma:8000 · gpt-4o-mini
  // 只有 URL 里带 user:pass@ 才算凭据（由 INLINE_CREDENTIAL 单独覆盖）。
  if (/^[a-z][\w+.-]*:\/\//i.test(v) && !/:\/\/[^/@\s]*:[^/@\s]*@/.test(v)) return false;
  if (/^[\w.-]+:\d+(?:\/|$)/.test(v)) return false;
  if (/^(?:gpt|claude|gemini|qwen|llama|mistral|deepseek|glm|ernie)[\w.-]*$/i.test(v)) return false;
  if (!/^[A-Za-z0-9_.+/=:@-]+$/.test(v)) return false;
  const counts = new Map();
  for (const ch of v) counts.set(ch, (counts.get(ch) || 0) + 1);
  let entropy = 0;
  for (const n of counts.values()) {
    const p = n / v.length;
    entropy -= p * Math.log2(p);
  }
  return entropy >= 3.0;
}
const INLINE_CREDENTIAL = /(?:\b[a-z][\w+.-]*:\/\/[^\s/@:]*:([^\s/@]+)@|[?&](?:password|passwd|token|key|api_key|secret)=([^\s&#]+))/gi;
const assignments = /^([a-z_][\w-]*)=(.*)$/i;
const isMapping = (value) => Boolean(value && typeof value === "object" && !Array.isArray(value));
const only = (value, item) => Array.isArray(value) && value.length === 1 && value[0] === item;

function checkSecrets(document, errors) {
  function scan(value, location, name = "") {
    if (Array.isArray(value)) {
      value.forEach((item, index) => scan(item, `${location}[${index}]`));
      return;
    }
    if (isMapping(value)) {
      for (const [key, item] of Object.entries(value)) scan(item, `${location}.${key}`, key);
      return;
    }
    const raw = String(value);
    const assignment = name ? null : raw.match(assignments);
    const key = assignment?.[1] ?? name;
    const literal = assignment?.[2] ?? raw;
    if (SECRET_NAME.test(key) && !SECRET_METADATA.test(key) && !ENV_REF.test(literal)) {
      errors.push(`${location}（${key}）必须引用环境变量`);
    }
    // 先检查 ${VAR:?...} / ${VAR:-...} 的默认值本体，再剥离引用查其余内联凭据。
    for (const m of literal.matchAll(ENV_DEFAULT)) {
      const inner = m[1] || "";
      if (!inner) continue;
      if ([...inner.matchAll(INLINE_CREDENTIAL)].length || defaultLooksLikeCredential(inner)) {
        errors.push(`${location} 环境变量默认值含明文凭据`);
      }
    }
    const inline = literal.replace(/\$\{[A-Z_][A-Z0-9_]*(?::?\?[^}]*|:-)?\}/g, "");
    if ([...inline.matchAll(INLINE_CREDENTIAL)].length) {
      errors.push(`${location} 含明文连接凭据`);
    }
  }
  scan(document, "compose");
}

export function verifyComposeExposure(document) {
  if (!isMapping(document) || !isMapping(document.services)) {
    return ["Compose 必须定义 services 映射"];
  }
  const errors = [];
  if (Object.hasOwn(document, "version")) errors.push("移除已废弃的 version");
  if (Object.hasOwn(document, "include")) errors.push("暴露面校验要求所有服务在此文件定义");
  const { services } = document;
  if (Object.keys(services).length !== SERVICES.length || SERVICES.some((name) => !isMapping(services[name]))) {
    errors.push("Compose 必须定义契约中的七个服务");
  }

  for (const [name, service] of Object.entries(services)) {
    if (!isMapping(service)) continue;
    if (name === KERNEL ? !only(service.ports, "8080:8080") : Object.hasOwn(service, "ports")) {
      errors.push(`${name} 的 ports 不符合契约，仅 uh-kernel 可发布 8080:8080`);
    }
    if (CAPABILITIES.includes(name) && !only(service.expose, "8080")) {
      errors.push(`${name} 必须仅 expose 容器端口 8080`);
    }
    if (name === "uh-crewai" ? !only(service.profiles, "collaborate") : Object.hasOwn(service, "profiles")) {
      errors.push(`${name} 的 profiles 不符合默认启动契约`);
    }
    if (Object.hasOwn(service, "extends") || service.network_mode === "host") {
      errors.push(`${name} 禁止 extends 或 host 网络模式`);
    }
  }
  checkSecrets(document, errors);
  return errors;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const require = createRequire(import.meta.url);
    const cached = fileURLToPath(new URL("../.work/verify-deps/node_modules/js-yaml", import.meta.url));
    const yaml = require(process.env.YAML_PATH || (fs.existsSync(cached) ? cached : "js-yaml"));
    const file = process.argv[2] || DEFAULT_COMPOSE;
    const errors = verifyComposeExposure(yaml.load(fs.readFileSync(file, "utf8")));
    if (errors.length) {
      for (const error of errors) console.error(`FAIL  ${error}`);
      process.exitCode = 1;
    } else {
      console.log("COMPOSE_EXPOSURE_PASSED (7 services, 1 published port)");
    }
  } catch {
    console.error("FAIL  无法读取 Compose YAML 或加载 js-yaml（可先运行 verify-all）");
    process.exitCode = 1;
  }
}
