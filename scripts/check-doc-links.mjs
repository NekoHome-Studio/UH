import fs from 'node:fs';
import path from 'node:path';
const ROOT = process.cwd();
const files = ['CHANGELOG.md','RELEASE.md','README.md','AGENTS.md','PROGRESS.md'];
const bad = [];
for (const f of files) {
  const p = path.join(ROOT, f);
  if (!fs.existsSync(p)) continue;
  const text = fs.readFileSync(p, 'utf8');
  const re = /\[([^\]]+)\]\(([^)]+)\)/g;
  let m;
  while ((m = re.exec(text))) {
    const target = m[2].split('#')[0];
    if (!target || /^(https?:|mailto:)/.test(target)) continue;
    const abs = path.resolve(path.dirname(p), target);
    if (!fs.existsSync(abs)) bad.push(f + ' -> ' + m[2]);
  }
}
if (bad.length) { console.log('DEAD_LINKS=' + bad.length); bad.forEach(b => console.log('  ' + b)); process.exit(1); }
console.log('LINKS_OK (0 dead links in ' + files.length + ' docs)');
