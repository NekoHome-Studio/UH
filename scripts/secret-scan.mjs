import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";

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

export function trackedTextFiles(root) {
  const output = execFileSync("git", ["ls-files", "-z"], { cwd: root }).toString("utf8");
  return output
    .split("\0")
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

export function scanTrackedFiles(root) {
  const hits = [];
  for (const file of trackedTextFiles(root)) {
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

