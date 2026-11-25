import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { isDeepStrictEqual as equal } from "node:util";
import { renderSeccompProfile } from "./render-seccomp-profile.mjs";

const ROOT = fileURLToPath(new URL("../", import.meta.url));
const WORKLOADS = ["uh-kernel", "uh-llama-index", "uh-langgraph", "uh-crewai"];
const PROFILE = "deploy/security/seccomp.json";
const DANGEROUS = [
  "bpf", "delete_module", "finit_module", "fsconfig", "fsmount", "fsopen", "fspick", "init_module",
  "kexec_file_load", "kexec_load", "mount", "mount_setattr", "move_mount", "open_tree", "pivot_root",
  "process_vm_readv", "process_vm_writev", "ptrace", "reboot", "setns", "umount2", "unshare", "userfaultfd",
];

export function verifySecurityHardening(yaml, root = ROOT) {
  const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
  const errors = [];
  const check = (condition, message) => { if (!condition) errors.push(message); };
  const profile = JSON.parse(read(PROFILE));
  check(profile.defaultAction === "SCMP_ACT_ERRNO" && profile.defaultErrnoRet === 1,
    "seccomp must default to EPERM");
  check(equal(profile.architectures, ["SCMP_ARCH_X86_64", "SCMP_ARCH_AARCH64"]),
    "seccomp ABIs must use clone flags argument 0");
  const rules = profile.syscalls ?? [];
  const byName = (name) => rules.filter((rule) => rule.names.includes(name));
  check(rules.every((rule) => ["SCMP_ACT_ALLOW", "SCMP_ACT_ERRNO"].includes(rule.action)),
    "seccomp rules must use only supported actions");
  for (const name of DANGEROUS) {
    const matches = byName(name);
    check(matches.length === 1 && matches[0].action === "SCMP_ACT_ERRNO"
      && matches[0].errnoRet === 13 && !matches[0].args?.length, `seccomp must deny ${name} unconditionally`);
  }
  const clone = byName("clone");
  check(clone.length === 1 && clone[0].action === "SCMP_ACT_ALLOW"
    && equal(clone[0].args, [{ index: 0, value: 0x7e020080, valueTwo: 0, op: "SCMP_CMP_MASKED_EQ" }]),
  "seccomp must mask all namespace flags on clone");
  const clone3 = byName("clone3");
  check(clone3.length === 1 && clone3[0].action === "SCMP_ACT_ERRNO" && clone3[0].errnoRet === 38
    && !clone3[0].args?.length, "seccomp must return ENOSYS for clone3");
  for (const name of ["read", "write", "openat", "execve", "prlimit64", "futex", "socket", "wait4"]) {
    check(byName(name).some((rule) => rule.action === "SCMP_ACT_ALLOW"), `seccomp must allow ${name}`);
  }
  const audit = renderSeccompProfile(profile, "audit");
  check(audit.defaultAction === "SCMP_ACT_LOG"
    && equal(audit.syscalls, rules.filter((rule) => rule.action === "SCMP_ACT_ALLOW")),
  "OCI audit must log calls outside the allowlist without denying them");
  const setup = renderSeccompProfile(profile, "bwrap");
  const setupOnly = ["mount", "pivot_root", "umount2", "unshare"];
  const ruleActions = (name, source) => source.filter((rule) => rule.names.includes(name))
    .map(({ action, errnoRet, args }) => ({ action, errnoRet, args }));
  const setupClone = setup.syscalls.filter((rule) => rule.names.includes("clone"));
  check(setup.defaultAction === profile.defaultAction
    && DANGEROUS.filter((name) => !setupOnly.includes(name))
      .every((name) => equal(ruleActions(name, setup.syscalls), ruleActions(name, rules)))
    && setupClone.length === 2 && setupClone.every((rule) => rule.action === "SCMP_ACT_ALLOW")
    && equal(setupClone[0].args, clone[0]?.args)
    && equal(setupClone[1].args, [{ index: 0, op: "SCMP_CMP_EQ", value: 0x78020011 }]),
  "OCI bubblewrap setup must keep other denials and filter clone flags");

  const compose = yaml.load(read("docker-compose.yaml"));
  const deployments = yaml.loadAll(read("deploy/k8s/deployments.yaml"));

  // 独立审查 P1-4：otel-collector 此前不在 WORKLOADS，M 项对其加固完全无感。
  // 它只在 K8s 侧交付（compose 走 observability overlay），故单独校验 K8s podSpec。
  const auxDocs = yaml.loadAll(read("deploy/k8s/observability-k8s.yaml"));
  const auxPod = auxDocs.find((d) => d?.kind === "Deployment" && d.metadata?.name === "otel-collector")
    ?.spec?.template?.spec;
  const auxCtx = auxPod?.securityContext;
  const auxSec = auxPod?.containers?.[0]?.securityContext;
  check(auxPod?.automountServiceAccountToken === false
    && auxCtx?.runAsNonRoot === true && auxCtx.runAsUser === 10001 && auxCtx.runAsGroup === 10001,
  "K8s otel-collector must be non-root and must not mount service account tokens");
  check(auxSec?.readOnlyRootFilesystem === true && auxSec.allowPrivilegeEscalation === false
    && equal(auxSec.capabilities, { drop: ["ALL"] }) && !auxSec.privileged
    && !auxPod.hostNetwork && !auxPod.containers?.[0]?.ports?.some((p) => p.hostPort !== undefined),
  "K8s otel-collector must use a read-only root, drop all capabilities and avoid host namespaces");

  for (const name of WORKLOADS) {
    const service = compose.services?.[name];
    check(service?.user === "10001:10001" && service.read_only === true
      && equal(service.cap_drop, ["ALL"]) && !service.privileged && !service.cap_add?.length,
    `Compose ${name} must be non-root with a read-only root and no capabilities`);
    check(equal(service?.security_opt, [`seccomp:./${PROFILE}`, "no-new-privileges:true"])
      && service.tmpfs?.some((mount) => /^\/tmp:rw,nosuid,nodev,size=\d+[mk],mode=1777$/.test(mount))
      && /^[1-9]\d*[mg]$/.test(service.mem_limit) && Number(service.cpus) > 0
      && Number.isInteger(service.pids_limit) && service.pids_limit > 0,
    `Compose ${name} must set seccomp, no-new-privileges, tmpfs and CPU/memory/PID limits`);

    const pod = deployments.find((doc) => doc?.kind === "Deployment" && doc.metadata?.name === name)
      ?.spec?.template?.spec;
    const context = pod?.securityContext;
    check(context?.runAsNonRoot === true && context.runAsUser === 10001 && context.runAsGroup === 10001
      && equal(context.seccompProfile, { type: "Localhost", localhostProfile: "uh/seccomp.json" }),
    `K8s ${name} must set a non-root pod identity and Localhost seccomp`);
    const container = pod?.containers?.[0];
    const security = container?.securityContext;
    check(security?.readOnlyRootFilesystem === true && security.allowPrivilegeEscalation === false
      && equal(security.capabilities, { drop: ["ALL"] }) && !security.privileged
      && !Object.hasOwn(security, "seccompProfile") && !Object.hasOwn(security, "runAsUser")
      && !Object.hasOwn(security, "runAsNonRoot"), `K8s ${name} must lock down its container`);
    check(["requests", "limits"].every((key) => container?.resources?.[key]?.cpu
      && container.resources[key].memory), `K8s ${name} must set CPU/memory requests and limits`);
    check(pod?.volumes?.some((volume) => volume.name === "tmp" && volume.emptyDir?.medium === "Memory"
      && volume.emptyDir.sizeLimit)
      && container?.volumeMounts?.some((mount) => mount.name === "tmp" && mount.mountPath === "/tmp" && !mount.readOnly),
    `K8s ${name} must mount a writable tmpfs at /tmp`);
    if (name === "uh-kernel") {
      check(service?.volumes?.includes("uh-state:/app/.uh") && context?.fsGroup === 10001
        && pod?.volumes?.some((volume) => volume.name === "state" && equal(volume.emptyDir, {}))
        && container?.volumeMounts?.some((mount) => mount.name === "state" && mount.mountPath === "/app/.uh" && !mount.readOnly),
      "kernel must have a writable state volume");
      check(service?.environment?.UH_SANDBOX_OCI_STRICT === "1" && container?.env?.some(
        (entry) => entry.name === "UH_SANDBOX_OCI_STRICT" && entry.value === "1"),
      "strict OCI deployments must fail closed for nested bubblewrap");
    }
  }

  const kubelet = yaml.load(read("deploy/security/kubelet-pids.yaml"));
  check(kubelet?.kind === "KubeletConfiguration" && kubelet.podPidsLimit === 256, "kubelet must set the pod PID limit");
  const dockerfile = read("Dockerfile");
  check(/^USER 10001:10001\s*$/m.test(dockerfile) && dockerfile.includes("libseccomp2")
    && dockerfile.includes(`COPY ${PROFILE} ./${PROFILE}`) && read(".dockerignore").includes(`!${PROFILE}`),
  "kernel image must declare its non-root user and bundle the seccomp profile and system library");
  const linux = read("kernel/linux_sandbox.py");
  const sandbox = read("kernel/sandbox.py");
  check(linux.includes('SECCOMP_MODES = ("disabled", "audit", "enforce")')
    && linux.includes('os.environ.get("UH_SANDBOX_SECCOMP", "enforce")')
    && linux.includes("return _SCMP_ACT_LOG, allowed") && sandbox.includes('"--seccomp"')
    && sandbox.includes('"pass_fds"'), "bubblewrap seccomp modes and BPF fd wiring are required");
  return errors;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const require = createRequire(import.meta.url);
    const cached = path.join(ROOT, ".work", "verify-deps", "node_modules", "js-yaml");
    const yaml = require(process.env.YAML_PATH || (fs.existsSync(cached) ? cached : "js-yaml"));
    const errors = verifySecurityHardening(yaml);
    if (errors.length) {
      for (const error of errors) console.error(`FAIL  ${error}`);
      process.exitCode = 1;
    } else {
      console.log("SECURITY_HARDENING_PASSED (seccomp, cgroups, read-only root, non-root)");
    }
  } catch (error) {
    console.error(`FAIL  ${error.message}`);
    process.exitCode = 1;
  }
}
