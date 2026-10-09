import { useEffect, useMemo, useState } from "react";
import type { RunInfo, StepEvent, WorkflowMap, WorkflowNode } from "../types";

type NodeStatus = "idle" | "active" | "done" | "waiting" | "error" | "skipped";

interface NodeState {
  status: NodeStatus;
  last?: StepEvent;
  runs: number;
}

function computeStates(map: WorkflowMap | null, events: StepEvent[], run: RunInfo): Record<string, NodeState> {
  const states: Record<string, NodeState> = {};
  for (const n of map?.nodes ?? []) states[n.id] = { status: "idle", runs: 0 };
  for (const e of events) {
    const s = (states[e.node] ??= { status: "idle", runs: 0 });
    if (e.phase === "start") {
      s.status = "active";
      s.runs += 1;
    } else {
      s.status = e.phase === "end" ? "done" : e.phase;
      s.last = e;
    }
  }
  if (run.status === "done" || run.status === "error") {
    for (const s of Object.values(states)) if (s.status === "idle") s.status = "skipped";
  }
  return states;
}

const STATUS_TEXT: Record<NodeStatus, string> = {
  idle: "not reached yet",
  active: "running",
  done: "completed",
  waiting: "waiting for a human",
  error: "failed",
  skipped: "not on this path",
};

function NodePill({
  node, state, selected, onSelect,
}: { node: WorkflowNode; state: NodeState; selected: boolean; onSelect: () => void }) {
  return (
    <button
      className={`wf-node ${state.status} ${selected ? "selected" : ""}`}
      onClick={onSelect}
      title={`${node.label}: ${STATUS_TEXT[state.status]}\n\n${node.explain}`}
      aria-label={`${node.label}, ${STATUS_TEXT[state.status]}`}
    >
      <span className="wf-dot" aria-hidden />
      <span className="wf-node-label">{node.label}</span>
      {state.last?.duration_ms !== undefined && <span className="wf-ms">{fmtMs(state.last.duration_ms)}</span>}
      {state.runs > 1 && <span className="wf-runs" title="Re-entered after resume">×{state.runs}</span>}
      {state.last?.summary && state.status !== "skipped" && <span className="wf-node-summary">{state.last.summary}</span>}
    </button>
  );
}

function fmtMs(ms: number) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export default function WorkflowPane({
  map, events, run, onClose,
}: { map: WorkflowMap | null; events: StepEvent[]; run: RunInfo; onClose?: () => void }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [pinned, setPinned] = useState(false);
  const [openSeq, setOpenSeq] = useState<number | null>(null);
  const [showMermaid, setShowMermaid] = useState(false);

  const states = useMemo(() => computeStates(map, events, run), [map, events, run]);
  const byId = useMemo(() => Object.fromEntries((map?.nodes ?? []).map((n) => [n.id, n])), [map]);

  // Follow the live step unless the user pinned a node by clicking it.
  const latest = events.length ? events[events.length - 1] : null;
  useEffect(() => {
    if (!pinned && latest) setSelected(latest.node);
  }, [latest, pinned]);
  useEffect(() => {
    if (run.status === "running" && events.length === 0) {
      setPinned(false);
      setSelected(null);
    }
  }, [run.status, events.length]);

  const agentEvent = events.find((e) => e.agent);
  const activeAgent = agentEvent?.agent ?? run.agent ?? null;
  const agentInfo = map?.agents.find((a) => a.id === activeAgent);
  const routeSummary = states.route?.last?.summary ?? "";
  const branch = states.clarify?.status !== "idle" && states.clarify?.status !== "skipped" ? "clarify"
    : activeAgent ? "agent"
    : /escalate/.test(routeSummary) ? "escalate"
    : /smalltalk|greeting/.test(routeSummary) ? "smalltalk" : null;

  const pick = (id: string) => {
    setSelected(id);
    setPinned(true);
  };
  const node = (id: string) =>
    byId[id] ? <NodePill key={id} node={byId[id]} state={states[id]} selected={selected === id} onSelect={() => pick(id)} /> : null;

  const sel = selected ? byId[selected] : null;
  const selState = selected ? states[selected] : null;
  const t0 = events.length ? Date.parse(events[0].ts) : 0;
  const finished = events.filter((e) => e.phase !== "start");
  const knowledgeUsed = ["retrieve", "rerank", "generate"].some((n) => states[n]?.status !== "idle" && states[n]?.status !== "skipped");
  const actionUsed = ["validate_action", "authorize", "approval", "execute"].some((n) => states[n]?.status !== "idle" && states[n]?.status !== "skipped");

  return (
    <aside className="wf" aria-label="Agent workflow">
      <header className="wf-head">
        <div>
          <h2>Agent workflow</h2>
          <p className="muted small">LangGraph state machine · live trace</p>
        </div>
        <span className={`run-chip ${run.status}`}>{run.replay ? "replay" : run.status}</span>
        {onClose && <button className="icon-btn only-mobile" onClick={onClose} aria-label="Close workflow">✕</button>}
      </header>

      <div className="wf-meta">
        <span title="LLM provider">
          {run.llmMode === "groq" ? `Groq · ${run.models?.[0] ?? ""}` : run.llmMode === "heuristic" ? "Offline heuristics" : "—"}
        </span>
        {run.totalMs !== undefined && <span>{fmtMs(run.totalMs)}</span>}
        {!!(run.tokensIn || run.tokensOut) && <span>{run.tokensIn} in / {run.tokensOut} out tokens</span>}
        {run.confidence != null && <span>confidence {(run.confidence * 100).toFixed(0)}%</span>}
        {run.executionId && <span className="mono" title={run.executionId}>exec {run.executionId.slice(0, 8)}</span>}
      </div>

      <div className="wf-scroll">
        {!map ? (
          <p className="muted pad">Loading workflow…</p>
        ) : (
          <div className="wf-graph">
            <div className="wf-lane">
              <div className="wf-lane-title">Main orchestrator</div>
              {map.orchestrator_pre.map(node)}
            </div>

            <div className="wf-branches" aria-label="Route decision">
              {[
                ["clarify", "Clarify"],
                ["agent", "Department agent"],
                ["escalate", "Escalate"],
                ["smalltalk", "Small talk"],
              ].map(([id, label]) => (
                <span key={id} className={`wf-branch ${branch === id ? "on" : branch ? "off" : ""}`}>{label}</span>
              ))}
            </div>

            {branch === "clarify" && <div className="wf-lane">{node("clarify")}</div>}

            <div className={`wf-agent ${activeAgent ? "on" : ""}`}>
              <div className="wf-agent-head">
                <span className="wf-lane-title">Department agent subgraph</span>
                <div className="wf-agent-tabs">
                  {map.agents.map((a) => (
                    <span key={a.id} className={`agent-tab ${a.id === activeAgent ? "on" : ""}`} title={a.description}>
                      {a.label.replace(" Agent", "").replace(" Service Desk", "")}
                    </span>
                  ))}
                </div>
              </div>
              {agentInfo && (
                <p className="small muted wf-agent-desc">
                  Tools: {agentInfo.tools.join(", ")} · Knowledge: {agentInfo.collections.join(", ")}
                </p>
              )}
              {node("plan")}
              <div className="wf-split">
                <div className={`wf-sublane ${knowledgeUsed ? "used" : ""}`}>
                  <div className="wf-sublane-title">Knowledge (RAG)</div>
                  {map.agent_knowledge.filter((n) => n !== "plan").map(node)}
                </div>
                <div className={`wf-sublane ${actionUsed ? "used" : ""}`}>
                  <div className="wf-sublane-title">Action (tools)</div>
                  {map.agent_action.map(node)}
                </div>
              </div>
            </div>

            <div className="wf-lane">
              <div className="wf-lane-title">Verify &amp; respond</div>
              {map.orchestrator_post.map(node)}
            </div>
          </div>
        )}

        {sel && selState && (
          <section className="wf-detail" aria-live="polite">
            <div className="wf-detail-head">
              <strong>{sel.label}</strong>
              <span className={`status-tag ${selState.status}`}>{STATUS_TEXT[selState.status]}</span>
              {pinned && <button className="link small" onClick={() => setPinned(false)}>follow live</button>}
            </div>
            <p className="small">{sel.explain}</p>
            {selState.last?.summary && <p className="wf-detail-summary">{selState.last.summary}</p>}
            {selState.last?.data && Object.keys(selState.last.data).length > 0 && (
              <details open>
                <summary className="small">State update written by this node</summary>
                <Json value={selState.last.data} />
              </details>
            )}
          </section>
        )}

        <section className="wf-timeline">
          <h3>Execution timeline <span className="muted small">({finished.length} steps)</span></h3>
          {events.length === 0 && <p className="muted small">Send a message to watch the agent work through each step.</p>}
          <ol>
            {events.map((e, i) =>
              e.phase === "start" && events.slice(i + 1).some((x) => x.node === e.node) ? null : (
                <li key={e.seq ?? i} className={`tl-item ${e.phase}`}>
                  <button className="tl-row" onClick={() => setOpenSeq(openSeq === (e.seq ?? i) ? null : (e.seq ?? i))}>
                    <span className="tl-time mono">+{((Date.parse(e.ts) - t0) / 1000).toFixed(2)}s</span>
                    <span className="tl-label">
                      {e.label}
                      {e.agent && <span className="tl-agent">{e.agent}</span>}
                    </span>
                    {e.duration_ms !== undefined && <span className="tl-ms">{fmtMs(e.duration_ms)}</span>}
                  </button>
                  {e.summary && <div className="tl-summary">{e.summary}</div>}
                  {openSeq === (e.seq ?? i) && e.data && <Json value={e.data} />}
                </li>
              ),
            )}
          </ol>
        </section>

        {map?.mermaid && (
          <section className="wf-mermaid">
            <button className="link small" onClick={() => setShowMermaid(!showMermaid)}>
              {showMermaid ? "Hide" : "Show"} compiled LangGraph (Mermaid source)
            </button>
            {showMermaid && <pre className="json">{map.mermaid}</pre>}
          </section>
        )}
      </div>
    </aside>
  );
}
