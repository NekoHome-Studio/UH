import fs from "node:fs";
import { createRequire } from "node:module";
import { isIP } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { isDeepStrictEqual as equal } from "node:util";

const MANIFESTS = fileURLToPath(new URL("../deploy/k8s/", import.meta.url));
const NAMESPACE = "uh-prod";
const KERNEL = "uh-kernel";
const CREWAI = "uh-crewai";
const LLAMA = "uh-llama-index";
const LANGGRAPH = "uh-langgraph";
const SCALER = "uh-scaler";
const ROLE = "uh-crewai-scaler";
const RBAC = "rbac.authorization.k8s.io";
const capabilities = [LLAMA, LANGGRAPH, CREWAI];
const stores = { redis: 6379, chroma: 8000, postgres: 5432 };
const workloads = [KERNEL, ...capabilities];

const resources = {
  Namespace: ["v1", [NAMESPACE]],
  Secret: ["v1", ["uh-runtime"]],
  ConfigMap: ["v1", ["otel-collector-config"]],
  ServiceAccount: ["v1", [SCALER]],
  Role: [RBAC + "/v1", [ROLE]],
  RoleBinding: [RBAC + "/v1", [ROLE]],
  Deployment: ["apps/v1", [...workloads, "otel-collector"]],
  HorizontalPodAutoscaler: ["autoscaling/v2", capabilities],
  StatefulSet: ["apps/v1", Object.keys(stores)],
  Service: ["v1", [...workloads, ...Object.keys(stores), "otel-collector", "uh-kernel-metrics"]],
  NetworkPolicy: ["networking.k8s.io/v1", [
    "default-deny", "allow-dns", "kernel-ingress", "capability-ingress-from-kernel",
    "kernel-egress-capabilities", "kernel-egress-datastores", "capability-egress-datastores",
    "capability-egress-datastores-llama-index", "redis-ingress", "chroma-ingress", "postgres-ingress",
    "llm-egress", "kernel-egress-apiserver", "kernel-egress-otel", "otel-collector-ingress",
  ]],
};

const selector = (matchLabels) => ({ matchLabels });
const pod = (matchLabels) => ({ podSelector: selector(matchLabels) });
const app = (name) => pod({ app: name });
const capability = pod({ tier: "capability" });
const namespace = (name) => ({ namespaceSelector: selector({ "kubernetes.io/metadata.name": name }) });
const rule = (peerKey, port, ...peers) => ({ [peerKey]: peers, ports: [{ port }] });
const from = (port, ...peers) => rule("from", port, ...peers);
const to = (port, ...peers) => rule("to", port, ...peers);
const ingress = (target, ...rules) => ({ ...target, policyTypes: ["Ingress"], ingress: rules });
const egress = (source, ...rules) => ({ ...source, policyTypes: ["Egress"], egress: rules });
const storeTo = (name) => to(stores[name], app(`uh-${name}`));
const storeFrom = (name, client) => ingress(app(`uh-${name}`), from(stores[name], app(KERNEL), app(client)));
const cpu70 = [{
  type: "Resource",
  resource: { name: "cpu", target: { type: "Utilization", averageUtilization: 70 } },
}];

function externalHost(peer) {
  const cidr = peer?.ipBlock?.cidr;
  if (typeof cidr !== "string" || !equal(peer, { ipBlock: { cidr } })) return false;
  const [address, prefix, extra] = cidr.split("/");
  if (extra !== undefined) return false;
  if (isIP(address) === 6) return prefix === "128" && /^[23]/.test(address);
  if (isIP(address) !== 4 || prefix !== "32") return false;
  const [a, b] = address.split(".").map(Number);
  return a > 0 && a < 224 && ![10, 127].includes(a)
    && !(a === 100 && b >= 64 && b <= 127) && !(a === 169 && b === 254)
    && !(a === 172 && b >= 16 && b <= 31) && !(a === 192 && b === 168)
    && !(a === 198 && [18, 19].includes(b));
}

function checkNetwork(get, check) {
  const llmPeers = get("llm-egress")?.egress?.[0]?.to;
  check(Array.isArray(llmPeers) && llmPeers.length > 0 && llmPeers.every(externalHost),
    "llm-egress must select explicit external IPv4 /32 or IPv6 /128 addresses");
  const expected = {
    "default-deny": { podSelector: {}, policyTypes: ["Ingress", "Egress"] },
    "allow-dns": egress({ podSelector: {} }, {
      to: [{ ...namespace("kube-system"), ...pod({ "k8s-app": "kube-dns" }) }],
      ports: [{ port: 53, protocol: "UDP" }, { port: 53, protocol: "TCP" }],
    }),
    "kernel-ingress": ingress(app(KERNEL), from(8080, namespace("ingress-controller"))),
    "capability-ingress-from-kernel": ingress(capability, from(8080, app(KERNEL))),
    "kernel-egress-capabilities": egress(app(KERNEL), to(8080, capability)),
    "kernel-egress-datastores": egress(app(KERNEL), ...Object.keys(stores).map(storeTo)),
    "capability-egress-datastores": egress(app(LANGGRAPH), storeTo("redis"), storeTo("postgres")),
    "capability-egress-datastores-llama-index": egress(app(LLAMA), storeTo("chroma")),
    "redis-ingress": storeFrom("redis", LANGGRAPH),
    "chroma-ingress": storeFrom("chroma", LLAMA),
    "postgres-ingress": storeFrom("postgres", LANGGRAPH),
    "llm-egress": egress({
      podSelector: { matchExpressions: [{ key: "app", operator: "In", values: [KERNEL, CREWAI] }] },
    }, { to: llmPeers, ports: [{ port: 443 }] }),
    // 二轮复检阻断①②：apiserver 控制面 + OTel collector
    "kernel-egress-apiserver": egress(app(KERNEL),
      { to: [{ ipBlock: { cidr: "192.0.2.10/32" } }], ports: [{ port: 443 }] }),
    "kernel-egress-otel": egress(app(KERNEL),
      { to: [app("otel-collector")], ports: [{ port: 4317 }, { port: 4318 }] }),
    "otel-collector-ingress": ingress(app("otel-collector"),
      { from: [app(KERNEL)], ports: [{ port: 4317 }, { port: 4318 }] }),
  };
  for (const [name, spec] of Object.entries(expected)) {
    check(equal(get(name), spec), `NetworkPolicy ${name} differs from the required traffic rules`);
  }
}

const secretName = /(?:^|[_-])(?:password|passwd|token|key|secret)s?(?:$|[_-])|^pgpassword$/i;
const secretMetadata = /(?:[_-](?:file|source|path|required|ttl|ttl_s|id|name))$/i;
const placeholder = /^\$\{[A-Z_][A-Z0-9_]*\}$/;
const embeddedSecret = /(?:\b[a-z][\w+.-]*:\/\/[^\s/@:]*:[^\s/@]+@|(?:^|[?&\s])(?:password|passwd|token|key|api_key|secret)=\S+|(?:^|\s)--(?:password|passwd|token|key|api[-_]key|secret)\b)/i;
const secretKeys = ["DB_PASSWORD", "UH_CLIENT_ID_SECRET", "UH_LLM_API_KEY", "UH_MEMORY_MASTER_KEY",
  "uh_auth_token", "uh_plugin_trusted_keys"];
const sensitive = (name) => {
  const normalized = String(name).replace(/([a-z0-9])([A-Z])/g, "$1_$2");
  return secretName.test(normalized) && !secretMetadata.test(normalized);
};

function hasLiteralSecret(value, context = "") {
  if (typeof value === "string") return embeddedSecret.test(value);
  if (Array.isArray(value)) return value.some((item) => hasLiteralSecret(item, context));
  if (!value || typeof value !== "object") return false;
  if (context === "env" && sensitive(value.name ?? "") && Object.hasOwn(value, "value")) {
    return true;
  }
  return Object.entries(value).some(([key, child]) => {
    const referenceKey = key === "key" && ["secretKeyRef", "configMapKeyRef", "items", "matchExpressions"].includes(context);
    if (key === "automountServiceAccountToken" && typeof child === "boolean") return false;
    return (!referenceKey && sensitive(key) && (child === null || typeof child !== "object"))
      || hasLiteralSecret(child, key);
  });
}

export function verifyK8sManifests(documents) {
  const errors = [];
  const check = (ok, message) => { if (!ok) errors.push(message); };
  const byName = new Map();
  for (const doc of documents) {
    const kind = doc?.kind;
    const name = doc?.metadata?.name;
    const expected = Object.hasOwn(resources, kind) && resources[kind];
    if (!expected || doc.apiVersion !== expected[0] || !expected[1].includes(name)
        || doc.metadata.namespace !== (kind === "Namespace" ? undefined : NAMESPACE)) {
      check(false, "unexpected Kubernetes resource, version or namespace");
      continue;
    }
    const id = kind + "/" + name;
    check(!byName.has(id), `duplicate ${id}`);
    byName.set(id, doc);
    if (kind === "Secret") {
      const entries = doc.stringData;
      check(doc.type === "Opaque" && !Object.hasOwn(doc, "data")
        && equal(Object.keys(entries ?? {}).sort(), secretKeys)
        && Object.values(entries).every((value) => typeof value === "string" && placeholder.test(value)),
      "Secret must use stringData with external environment placeholders only");
      const { stringData, ...rest } = doc;
      check(!hasLiteralSecret(rest), "literal credential in Secret metadata");
    } else {
      check(!hasLiteralSecret(doc), `literal credential in ${id}`);
    }
  }
  for (const [kind, [, names]] of Object.entries(resources)) {
    for (const name of names) check(byName.has(kind + "/" + name), `missing ${kind}/${name}`);
  }
  const get = (kind, name) => byName.get(kind + "/" + name);
  const serviceAccount = get("ServiceAccount", SCALER);
  check(serviceAccount && serviceAccount.automountServiceAccountToken !== false && !serviceAccount.secrets?.length,
    "scaler ServiceAccount must use the Pod's projected credential");
  const rules = get("Role", ROLE)?.rules;
  const expectedRules = [
    { apiGroups: ["apps"], resources: ["deployments/scale"], resourceNames: [CREWAI], verbs: ["get", "patch", "update"] },
    { apiGroups: ["apps"], resources: ["deployments"], resourceNames: [CREWAI], verbs: ["get"] },
  ];
  check(Array.isArray(rules) && rules.length === 2 && expectedRules.every((expected) =>
    rules.some((rule) => Array.isArray(rule?.verbs) && equal({ ...rule, verbs: [...rule.verbs].sort() }, expected))),
  "Role grants must exactly match the two scoped uh-crewai rules");
  const binding = get("RoleBinding", ROLE);
  check(equal(binding?.subjects, [{ kind: "ServiceAccount", name: SCALER, namespace: NAMESPACE }])
    && equal(binding?.roleRef, { kind: "Role", name: ROLE, apiGroup: RBAC }),
  "RoleBinding must bind the in-namespace scaler ServiceAccount to its Role only");

  for (const name of [...workloads, ...Object.keys(stores)]) {
    const datastore = Object.hasOwn(stores, name);
    const tier = datastore ? "datastore" : name === KERNEL ? "kernel" : "capability";
    const labels = { app: datastore ? `uh-${name}` : name, tier };
    const workload = get(datastore ? "StatefulSet" : "Deployment", name);
    const spec = workload?.spec;
    const podSpec = spec?.template?.spec;
    const container = podSpec?.containers?.[0];
    const service = get("Service", name)?.spec;
    check(equal(workload?.metadata?.labels, labels) && equal(spec?.selector, selector(labels))
      && equal(spec?.template?.metadata?.labels, labels) && equal(service?.selector, labels),
    `${name} workload and Service labels/selectors must agree`);
    const port = container?.ports?.find((item) => item.containerPort === (datastore ? stores[name] : 8080));
    check(port?.name && equal(service?.ports, [{ name: port.name, port: port.containerPort, targetPort: port.name }])
      && (service?.type ?? "ClusterIP") === "ClusterIP" && !service?.externalIPs?.length
      && (service?.clusterIP === "None") === datastore, `${name} must have the contract's internal Service and port`);
    check(podSpec && !podSpec.hostNetwork && podSpec.containers?.length === 1
      && ![...podSpec.containers, ...(podSpec.initContainers ?? [])]
        .some((item) => item.ports?.some((p) => p.hostPort !== undefined)),
    `${name} must run in an isolated Pod with no host port`);
    if (name === KERNEL) {
      check(Number.isInteger(spec?.replicas) && spec.replicas >= 2
        && podSpec?.serviceAccountName === SCALER && podSpec?.automountServiceAccountToken !== false,
      "kernel requires at least two replicas and the uh-scaler ServiceAccount");
    } else {
      check(podSpec?.automountServiceAccountToken === false
        && (podSpec?.serviceAccountName ?? "default") === "default", `${name} must not use scaler credentials`);
    }
    if (datastore) {
      const claims = spec?.volumeClaimTemplates;
      check(spec?.serviceName === name && Array.isArray(claims) && claims.length > 0
        && claims.every((claim) => claim.spec?.resources?.requests?.storage
          && container?.volumeMounts?.some((mount) => mount.name === claim.metadata?.name)),
      `${name} requires a PVC template mounted by its StatefulSet`);
    } else {
      const readiness = container?.readinessProbe;
      const command = readiness?.exec?.command;
      const checksReady = name === KERNEL
        ? command?.[0] === "python" && command[1] === "-c" && command[2]?.includes("127.0.0.1:8080/ready")
        : readiness?.httpGet?.path === "/ready" && readiness.httpGet.port === port?.name;
      check(checksReady
        && container?.livenessProbe?.httpGet?.path === "/health" && container.livenessProbe.httpGet.port === port?.name
        && ["requests", "limits"].every((key) => container?.resources?.[key]?.cpu && container.resources[key].memory),
      `${name} requires HTTP probes and CPU/memory requests and limits`);
    }
  }
  check(get("Deployment", CREWAI)?.spec?.replicas === 0, "uh-crewai must start at zero replicas");
  for (const name of capabilities) {
    const spec = get("HorizontalPodAutoscaler", name)?.spec;
    check(equal(spec?.scaleTargetRef, { apiVersion: "apps/v1", kind: "Deployment", name })
      && equal(spec?.metrics, cpu70) && Number.isInteger(spec.minReplicas) && Number.isInteger(spec.maxReplicas)
      && (name === CREWAI ? spec.minReplicas === 1 && spec.maxReplicas === 1
        : spec.minReplicas >= 2 && spec.maxReplicas > spec.minReplicas
          && get("Deployment", name)?.spec?.replicas === undefined),
    `${name} requires CPU utilization at 70% and bounded replicas`);
  }
  checkNetwork((name) => get("NetworkPolicy", name)?.spec, check);
  return errors;
}

export function readK8sManifests(yaml, directory = MANIFESTS) {
  return fs.readdirSync(directory).filter((name) => name.endsWith(".yaml")).sort()
    .flatMap((name) => yaml.loadAll(fs.readFileSync(path.join(directory, name), "utf8")))
    .filter((doc) => doc !== null && doc !== undefined);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const require = createRequire(import.meta.url);
    const cached = fileURLToPath(new URL("../.work/verify-deps/node_modules/js-yaml", import.meta.url));
    const yaml = require(process.env.YAML_PATH || (fs.existsSync(cached) ? cached : "js-yaml"));
    const errors = verifyK8sManifests(readK8sManifests(yaml, process.argv[2]));
    if (errors.length) {
      for (const error of errors) console.error(`FAIL  ${error}`);
      process.exitCode = 1;
    } else {
      console.log("K8S_MANIFESTS_PASSED (7 traffic allowances, scoped scaler RBAC)");
    }
  } catch {
    console.error("FAIL  Could not parse Kubernetes YAML or load js-yaml (run verify-all first)");
    process.exitCode = 1;
  }
}
