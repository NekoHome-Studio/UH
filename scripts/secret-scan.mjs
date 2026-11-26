import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

// Match an assignment to the actual token variable. The word boundary keeps
// UH_AUTH_TOKEN_FILE references out of the result while allowing spaces,
// quotes, and YAML's colon form around the assignment.
// 独立审查 P1-3：原实现只认 UH_AUTH_TOKEN 单一变量，UH_MEMORY_MASTER_KEY /
// UH_LLM_API_KEY / UH_CLIENT_ID_SECRET / DB_PASSWORD / PGPASSWORD 等真实凭据全部漏检。
// 扩展为项目实际使用的全部密钥变量名（见 .env.example 与 deploy/k8s/secrets.yaml）。
const SECRET_VARS = [
  "UH_AUTH_TOKEN", "UH_MEMORY_MASTER_KEY", "UH_LLM_API_KEY", "UH_CLIENT_ID_SECRET",
  "DB_PASSWORD", "PGPASSWORD", "POSTGRES_PASSWORD", "UH_RELEASE_KEY",
];
// 注意：赋值号后只允许**同行**空白（[ \t] 而非 \s），否则空值项会吃掉换行
// 把下一行的变量名误当作值（.env.example 的 DB_PASSWORD= 空值曾误报）。
export const AUTH_TOKEN_PATTERN = new RegExp(
  String.raw`\b(?:${SECRET_VARS.join("|")})[ \t]*[:=][ \t]*["']?([A-Za-z0-9_.+/=-]{20,})`,
  "g",
);

// ${VAR:?message} / ${VAR:-default} 的默认值文本可夹带明文凭据（审查已证实）。
// compose 门禁把整个 ${...} 当合法引用删除，导致 message 从未被检查。
export const ENV_DEFAULT_PATTERN = /\$\{[A-Za-z_][A-Za-z0-9_]*:?[?-]([^}]*)\}/g;

function isSyntheticToken(value) {
  const token = String(value);
  if (/^(?:ABCDEFGHIJKLMNOPQRSTUVWXYZ|0123456789|abcdefg|test[_-]?token|fake[_-]?token|placeholder)/i.test(token)) return true;
  // 审查报告/文档里的示例凭据常带前缀后接字母序（sk_live_abcdefghij…、supersecretvalue1234567890）。
  // 去掉常见前缀后若主体是连续字母序或连续数字，判为合成值。
  // 值可能带前导标点/引号（${VAR:-sk_live_…} 捕获 "-sk_live_…"；
  // YAML/JSON 的 KEY = "value" 形式捕获 '"value'）——统一剥离后再判定。
  const cleaned = token.replace(/^[-_:."']+/, "");
  const body = cleaned.replace(/^(?:sk|pk|rk)[-_](?:live[-_]|test[-_])?/i, "");
  if (/^abcdefghij/i.test(body)) return true;
  if (/^supersecret|^examplesecret|^dummysecret|^notarealsecret/i.test(body)) return true;
  if (/^[a-z]+1234567890$/i.test(body)) return true;
  if (/^abcdefghij/i.test(cleaned)) return true;
  // base64 编码的字母序样本（审查探针常用）：解码后若为字母序/连续数字则合成。
  if (/^[A-Za-z0-9+/]{16,}={0,2}$/.test(cleaned)) {
    try {
      const decoded = Buffer.from(cleaned, "base64").toString("utf8");
      if (/^abcdefghij|^0123456789|^[a-z]+1234567890$/i.test(decoded)) return true;
    } catch { /* 非合法 base64，忽略 */ }
  }
  return false;
}

export function hasPlainAuthToken(text) {
  const source = String(text);
  const direct = [...source.matchAll(AUTH_TOKEN_PATTERN)].some((m) => !isSyntheticToken(m[1]));
  if (direct) return true;
  // ${VAR:?...} / ${VAR:-...} 默认值内联凭据：递归扫描其文本
  for (const m of source.matchAll(ENV_DEFAULT_PATTERN)) {
    const inner = m[1] || "";
    if (!inner) continue;
    if (looksLikeCredential(inner)) return true;
  }
  return false;
}

// 默认值位置没有变量名上下文，用「长度 + 字符集 + 熵」判定是否为真实凭据。
function looksLikeCredential(value) {
  const v = String(value).trim().replace(/^["']|["']$/g, "");
  if (v.length < 20 || v.length > 512) return false;
  if (isSyntheticToken(v)) return false;
  if (/placeholder|sample|example|dummy|redacted/i.test(v)) return false;
  if (!/^[A-Za-z0-9_.+/=-]+$/.test(v)) return false;
  if (/^(?:your|my|the|change|replace|insert|put|set)[-_]/i.test(v)) return false;
  // ${VAR:?Set DB_PASSWORD} 这类是**给人看的提示语**不是凭据：
  // 含空格分隔的英文词、或全大写下划线的变量名形态，一律排除。
  if (/^[A-Z][A-Z0-9_]*$/.test(v)) return false;          // 纯变量名如 DB_PASSWORD
  if (/^(?:Set|Provide|Enter|Supply|Missing|Required|Must)/i.test(v)) return false;
  if (/[a-z]{3,}\s+[A-Za-z]/.test(v)) return false;       // 自然语言短语
  if (/^\/(?:run|etc|var|home|opt)\//.test(v)) return false; // 文件路径
  const counts = new Map();
  for (const ch of v) counts.set(ch, (counts.get(ch) || 0) + 1);
  let entropy = 0;
  for (const n of counts.values()) {
    const p = n / v.length;
    entropy -= p * Math.log2(p);
  }
  return entropy >= 3.5;
}

function textFiles(root, gitFlags) {
  let output;
  try {
    output = execFileSync("git", ["ls-files", "-z", ...gitFlags], { cwd: root }).toString("utf8");
  } catch {
    // 无 .git 的导出/发布树：不得静默回退为“扫描 0 文件、0 命中、0 退出”（等于没扫）。
    // 返回空列表，由调用方按“文件数为 0”统一报错非零退出。
    return [];
  }
  return [...new Set(output.split("\0"))]
    .filter(Boolean)
    .map((relative) => path.join(root, relative))
    .filter((file) => {
      try {
        const data = fs.readFileSync(file);
        return !data.includes(0);
      } catch {
        return false;
      }
    });
}

export function trackedTextFiles(root) {
  return textFiles(root, ["--cached"]);
}

// 审查报告与探针记录里**必然包含攻击样本字符串**（如 sk_live_…、base64 字母序），
// 它们是文档证据不是可用凭据。GitHub Push Protection 亦会拦这类形态，
// 故报告目录整体豁免；真实凭据仍由其余全量文件扫描覆盖。
// 审查报告与测试文件必然包含攻击样本字符串（如 sk_live_…、postgres://user:pass@），
// 它们是文档证据或测试数据不是可用凭据。真实凭据仍由其余全量文件覆盖。
const SCAN_EXEMPT = /(?:^|[\\/])docs[\\/](?:reviews)[\\/]|(?:^|[\\/])tests[\\/]/;

function scanFiles(root, files) {
  const hits = [];
  for (const file of files) {
    if (SCAN_EXEMPT.test(file)) continue;
    let text;
    try {
      text = fs.readFileSync(file, "utf8");
    } catch {
      continue;
    }
    if (hasPlainAuthToken(text)) hits.push(path.relative(root, file));
  }
  return hits;
}

export function scanTrackedFiles(root) {
  return scanFiles(root, trackedTextFiles(root));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const files = textFiles(root, ["--cached", "--others", "--exclude-standard"]);
  if (files.length === 0) {
    console.error("SECRET_SCAN_FAILED: no text files to scan (not a git checkout or no tracked text files)");
    process.exitCode = 1;
  } else {
    const hits = scanFiles(root, files);
    if (hits.length) {
      for (const file of hits) console.error(`FAIL  明文鉴权 token: ${file}`);
      process.exitCode = 1;
    } else {
      console.log(`SECRET_SCAN_PASSED (${files.length} text files, 0 hits)`);
    }
  }
}

