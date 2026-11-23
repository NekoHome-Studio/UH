import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// Use Python's AST so multiline declarations, aliases and nonliteral label
// lists are checked without adding a JS parser dependency.
const scan = String.raw`
import ast
from pathlib import Path

allowed = {"method", "route", "status_code", "task_type", "module", "model", "plugin_name", "outcome"}
client_types = {"Counter", "Gauge", "Histogram", "Summary", "Info", "Enum", "StateSet"}
http_labels = {
    "REQUESTS": ("method", "route", "status_code", "task_type"),
    "REQUEST_ERRORS": ("method", "route", "status_code"),
    "REQUEST_DURATION": ("method", "route"),
}
errors = []
route_sites = {}
registrations = 0

def argument(call, keyword, position, default):
    return next((kw.value for kw in call.keywords if kw.arg == keyword),
                call.args[position] if len(call.args) > position else default)

for file in sorted(Path("kernel").rglob("*.py")):
    tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    local = {"Metric"}
    client = set()
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.asname or alias.name.split(".")[0] for alias in node.names
                           if alias.name.startswith("prometheus_client"))
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "metrics" or module == "kernel.metrics":
                local.update(alias.asname or alias.name for alias in node.names if alias.name == "Metric")
            if module.startswith("prometheus_client"):
                client.update(alias.asname or alias.name for alias in node.names if alias.name in client_types)
                if any(alias.name == "*" for alias in node.names):
                    errors.append(f"{file}:{node.lineno}: use explicit Prometheus imports")

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "# TYPE uh_" in node.value:
            errors.append(f"{file}:{node.lineno}: declare metric families through Metric")
        if not isinstance(node, ast.Call):
            continue
        callee = ast.unparse(node.func)
        custom = callee in local or callee.endswith(".Metric")
        prometheus = callee in client or (
            callee.split(".")[0] in modules and callee.rsplit(".", 1)[-1] in client_types
        )
        if not (custom or prometheus):
            continue
        registrations += 1
        location = f"{file}:{node.lineno}"
        try:
            if any(kw.arg is None for kw in node.keywords) or any(isinstance(arg, ast.Starred) for arg in node.args):
                raise ValueError
            name = ast.literal_eval(argument(node, "name", 0, ast.Constant(None)))
            labels = ast.literal_eval(argument(node, "labels" if custom else "labelnames", 2, ast.Tuple(elts=[])))
            if not isinstance(name, str) or not isinstance(labels, (tuple, list)) or not all(isinstance(x, str) for x in labels):
                raise ValueError
        except (ValueError, TypeError):
            errors.append(f"{location}: metric name and label names must be literal")
            continue
        forbidden = sorted(set(labels) - allowed)
        if forbidden:
            errors.append(f"{location}: {name} has forbidden labels {', '.join(forbidden)}")
        if len(set(labels)) != len(labels):
            errors.append(f"{location}: {name} has duplicate label names")
        if "route" in labels:
            parent = parents[node]
            target = ast.unparse(parent.targets[0]) if isinstance(parent, ast.Assign) else ""
            if file != Path("kernel/metrics.py") or tuple(labels) != http_labels.get(target):
                errors.append(f"{location}: route labels must use the HTTP template instrumentation")
            else:
                route_sites[target] = name

tree = ast.parse(Path("kernel/metrics.py").read_text(encoding="utf-8"))
functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
resolver = functions.get("route_template")
observer = functions.get("observe_http")
if route_sites.keys() != http_labels.keys() or resolver is None or observer is None:
    errors.append("kernel/metrics.py: missing HTTP route metric or template resolver")
else:
    template_returns = {"matched.path_format", "route.path_format", "partial.path_format", "'unmatched'"}
    actual_returns = {ast.unparse(node.value) for node in ast.walk(resolver) if isinstance(node, ast.Return)}
    resolver_calls = {ast.unparse(node) for node in ast.walk(resolver) if isinstance(node, ast.Call)}
    if actual_returns != template_returns or not {"scope.get('route')", "route.matches(scope)"} <= resolver_calls:
        errors.append("kernel/metrics.py: route values must come from declared route path_format or the unmatched sentinel")

    route_bindings = [node for node in ast.walk(observer)
                      if isinstance(node, ast.Name) and node.id == "route" and isinstance(node.ctx, ast.Store)]
    assignments = {ast.unparse(node) for node in ast.walk(observer) if isinstance(node, ast.Assign)}
    increments = {ast.unparse(node.target) for node in ast.walk(observer) if isinstance(node, ast.AugAssign)}
    if (len(route_bindings) != 1
            or not {"route = route_template(scope, routes)", "key = (method, route)"} <= assignments
            or not {"self._counters[REQUESTS][method, route, status_code, task_type]",
                    "self._counters[REQUEST_ERRORS][method, route, status_code]"} <= increments):
        errors.append("kernel/metrics.py: HTTP counters and histogram must use the resolved route template")

if errors:
    raise SystemExit("\n".join(errors))
print(f"{registrations} registrations, {len(route_sites)} template-based HTTP families")
`;

export function verifyMetricsCardinality(root = ROOT) {
  return execFileSync("python", ["-c", scan], { cwd: root, encoding: "utf8", stdio: "pipe" }).trim();
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    console.log(`METRICS_CARDINALITY_PASSED (${verifyMetricsCardinality()})`);
  } catch (error) {
    console.error(String(error.stderr || error.message).trim());
    process.exitCode = 1;
  }
}
