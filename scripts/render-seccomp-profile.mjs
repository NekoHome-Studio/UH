// Derive an OCI audit or rootless bubblewrap setup profile from the enforced policy.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SETUP = ["capset", "mount", "pivot_root", "setgid", "setuid", "umount2"];

export function renderSeccompProfile(profile, mode) {
  if (mode === "audit") {
    return {
      defaultAction: "SCMP_ACT_LOG",
      architectures: profile.architectures,
      syscalls: profile.syscalls.filter((rule) => rule.action === "SCMP_ACT_ALLOW"),
    };
  }
  if (mode === "bwrap") {
    const syscalls = profile.syscalls.map((rule) => ({
      ...rule, names: rule.names.filter((name) => name !== "unshare" && !SETUP.includes(name)),
    })).filter((rule) => rule.names.length > 0);
    syscalls.push({ names: SETUP, action: "SCMP_ACT_ALLOW" });
    // bubblewrap uses these exact clone flags and a second user namespace for devpts.
    syscalls.push(
      { names: ["clone"], action: "SCMP_ACT_ALLOW", args: [{ index: 0, op: "SCMP_CMP_EQ", value: 0x78020011 }] },
      { names: ["unshare"], action: "SCMP_ACT_ALLOW", args: [{ index: 0, op: "SCMP_CMP_EQ", value: 0x10000000 }] },
    );
    return { ...profile, syscalls };
  }
  throw new Error("usage: node scripts/render-seccomp-profile.mjs audit|bwrap");
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const file = new URL("../deploy/security/seccomp.json", import.meta.url);
    const profile = JSON.parse(fs.readFileSync(file, "utf8"));
    console.log(JSON.stringify(renderSeccompProfile(profile, process.argv[2]), null, 2));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
