import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

// Match an assignment to the actual token variable. The word boundary keeps
// UH_AUTH_TOKEN_FILE references out of the result while allowing spaces,
// quotes, and YAML's colon form around the assignment.
export const AUTH_TOKEN_PATTERN = /\bUH_AUTH_TOKEN\s*[:=]\s*["']?([A-Za-z0-9_-]{20,})/g;

function isSyntheticToken(value) {
  const token = String(value);
  return /^(?:ABCDEFGHIJKLMNOPQRSTUVWXYZ|0123456789|abcdefg|test[_-]?token|fake[_-]?token|placeholder)/i.test(token);
}

export function hasPlainAuthToken(text) {
  return [...String(text).matchAll(AUTH_TOKEN_PATTERN)].some((match) => !isSyntheticToken(match[1]));
}

function textFiles(root, gitFlags) {
  const output = execFileSync("git", ["ls-files", "-z", ...gitFlags], { cwd: root }).toString("utf8");
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

function scanFiles(root, files) {
  const hits = [];
  for (const file of files) {
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
  const hits = scanFiles(root, files);
  if (hits.length) {
    for (const file of hits) console.error(`FAIL  明文鉴权 token: ${file}`);
    process.exitCode = 1;
  } else {
    console.log(`SECRET_SCAN_PASSED (${files.length} text files, 0 hits)`);
  }
}

