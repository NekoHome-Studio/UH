/* UH! M1-9 UI 状态机：纯函数，可由 Web/Desktop 共用。 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.UHStateMachine = api;
})(typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : this), function () {
  const STATES = Object.freeze(["idle", "routing", "thinking", "checking", "acting", "streaming", "awaiting_user", "yielded", "error", "cancelled"]);
  const TERMINAL = new Set(["error", "cancelled"]);
  const initialState = () => ({ state: "idle", degraded: false, returnState: null, terminal: false, lastEvent: null });
  function eventParts(event) {
    if (typeof event === "string") return { name: event, payload: {} };
    if (!event || typeof event !== "object") return { name: "", payload: {} };
    return { name: event.event || "", payload: event.payload || {} };
  }
  function transition(current, event) {
    const input = typeof current === "string" ? { state: current, degraded: false, returnState: null, terminal: TERMINAL.has(current), lastEvent: null } : current || initialState();
    const next = { ...input };
    const { name, payload } = eventParts(event);
    next.lastEvent = name || next.lastEvent;
    if (!STATES.includes(next.state)) next.state = "idle";
    if (next.terminal && name !== "task.created") return next;
    if (name === "task.created" && (next.state === "idle" || TERMINAL.has(next.state))) return { state: "routing", degraded: false, returnState: null, terminal: false, lastEvent: name };
    if (name === "task.degraded") { next.degraded = true; return next; }
    if (name === "task.finished") return { ...next, state: "idle", degraded: false, returnState: null, terminal: true };
    if (name === "task.failed") return { ...next, state: "error", returnState: null, terminal: true };
    if (name === "task.cancelled") return { ...next, state: "cancelled", returnState: null, terminal: true };
    if (name === "task.yielded") { next.state = "yielded"; next.terminal = false; return next; }
    if (next.state === "yielded" && name === "task.resumed") { next.state = "thinking"; return next; }
    if (next.state === "routing") {
      if (name === "step.finished" && payload.module === "router") next.state = "thinking";
      else if (name === "step.started" && payload.module !== "router") next.state = "thinking";
      return next;
    }
    if ((next.state === "thinking" || next.state === "acting") && name === "safety.check_started") { next.returnState = next.state; next.state = "checking"; return next; }
    if (next.state === "checking" && name === "safety.check_finished") { next.state = next.returnState === "acting" ? "acting" : "thinking"; next.returnState = null; return next; }
    if (next.state === "checking" && name === "safety.denied" && payload.action_taken !== "fail_task") { next.state = next.returnState === "acting" ? "acting" : "thinking"; next.returnState = null; return next; }
    if (next.state === "thinking" && name === "tool.call") { next.state = "acting"; return next; }
    if (next.state === "acting" && name === "tool.result") { next.state = "thinking"; return next; }
    if (next.state === "acting" && name === "step.failed" && payload.module === "mcp_tool") { next.state = "thinking"; return next; }
    if (next.state === "acting" && name === "task.confirmation_required") { next.state = "awaiting_user"; return next; }
    if (next.state === "awaiting_user" && name === "task.confirmation_resolved" && payload.decision === "approved") { next.state = "acting"; return next; }
    if (next.state === "thinking" && name === "delta") { next.state = "streaming"; return next; }
    if (next.state === "streaming" && name === "delta") return next;
    if (next.state === "streaming" && name === "step.finished" && payload.module === "llm") { next.state = "thinking"; return next; }
    return next;
  }
  function reduce(state, event) { return transition(state, event); }
  return Object.freeze({ STATES, initialState, transition, reduce });
});
