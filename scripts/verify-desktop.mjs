import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const parse = (file) => JSON.parse(read(file));
const config = parse("desktop/src-tauri/tauri.conf.json");
const window = config.app.windows.find((item) => item.label === "main");
assert.equal(config.build.frontendDist, "http://127.0.0.1:8080/?desktop=1");
assert.equal(window.url, config.build.frontendDist);
assert.equal(window.create, false);
assert.ok(window.width >= window.minWidth && window.height >= window.minHeight);
assert.equal(config.bundle.resources["../../ui/"], "runtime/ui/");

const kernel = read("kernel/main.py");
const page = read("ui/index.html");
assert.match(kernel, /_UI\s*=.*\/ "ui" \/ "index\.html"/);
assert.match(kernel, /FileResponse\(_UI, headers=headers\)/);
for (const asset of ["state-machine.js", "themes.js", "app.js"]) {
  assert.ok(page.includes(`<script src="/static/${asset}"></script>`));
  assert.ok(kernel.includes(`@app.get("/static/${asset}"`));
}

const themes = read("ui/themes.js");
assert.ok(themes.includes('request("/api/v1/themes", { cache: "no-store" })'));
assert.match(themes, /request\("\/api\/v1\/themes\/active",\s*\{\s*method: "PUT"/);

function checkDesktopSources(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const filename = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (!["target", "gen"].includes(entry.name)) checkDesktopSources(filename);
      continue;
    }
    assert.doesNotMatch(entry.name, /^(index\.html|state-machine\.js|themes\.js)$/i);
    if (/\.(?:[cm]?js|ts|html|rs)$/.test(entry.name)) {
      assert.doesNotMatch(fs.readFileSync(filename, "utf8"), /UHStateMachine|createThemeApplier|\/api\/v1\/themes/);
    }
  }
}
checkDesktopSources(path.join(root, "desktop"));
console.log("DESKTOP_VERIFY_PASSED (shared UI assets, state machine and theme REST)");
