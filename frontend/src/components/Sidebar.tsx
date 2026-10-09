import { useState } from "react";
import type { ConversationSummary, Ticket } from "../types";

export default function Sidebar({
  conversations, tickets, currentId, onSelect, onNew, stats,
}: {
  conversations: ConversationSummary[];
  tickets: Ticket[];
  currentId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  stats: Record<string, unknown> | null;
}) {
  const [tab, setTab] = useState<"history" | "tickets" | "stats">("history");
  const [openTicket, setOpenTicket] = useState<string | null>(null);
  return (
    <aside className="side" aria-label="History and tickets">
      <button className="btn primary block" onClick={onNew}>+ New conversation</button>
      <div className="tabs" role="tablist">
        {(["history", "tickets", "stats"] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={`tab ${tab === t ? "on" : ""}`} onClick={() => setTab(t)}>
            {t === "history" ? "History" : t === "tickets" ? `Tickets (${tickets.length})` : "Ops"}
          </button>
        ))}
      </div>
      <div className="side-scroll">
        {tab === "history" && (
          <ul className="list">
            {conversations.length === 0 && <li className="muted small pad">No conversations yet.</li>}
            {conversations.map((c) => (
              <li key={c.id}>
                <button className={`list-item ${c.id === currentId ? "on" : ""}`} onClick={() => onSelect(c.id)}>
                  <span className="ellipsis">{c.title}</span>
                  <span className="small muted">{new Date(c.updated_at).toLocaleString()} · {c.status}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {tab === "tickets" && (
          <ul className="list">
            {tickets.length === 0 && <li className="muted small pad">No tickets yet.</li>}
            {tickets.map((t) => (
              <li key={t.id}>
                <button className="list-item" onClick={() => setOpenTicket(openTicket === t.id ? null : t.id)}>
                  <span className="row gap-s">
                    <span className={`status-dot ${t.status}`} />
                    <strong>{t.number}</strong>
                    <span className="badge">{t.priority}</span>
                    <span className="small muted">{t.status.replace("_", " ")}</span>
                  </span>
                  <span className="ellipsis small">{t.title}</span>
                </button>
                {openTicket === t.id && (
                  <dl className="kv small ticket-detail">
                    <dt>Queue</dt><dd>{t.assigned_queue}</dd>
                    <dt>Department</dt><dd>{t.department} · {t.category}</dd>
                    <dt>Legacy system</dt><dd>{t.external_id ?? "—"} ({t.sync_status})</dd>
                    {t.escalation_reason && (<><dt>Escalation</dt><dd>{t.escalation_reason}</dd></>)}
                    {t.resolution && (<><dt>Resolution</dt><dd>{t.resolution}</dd></>)}
                    {t.conversation_id && (
                      <><dt /><dd><button className="link small" onClick={() => onSelect(t.conversation_id!)}>Open conversation</button></dd></>
                    )}
                  </dl>
                )}
              </li>
            ))}
          </ul>
        )}
        {tab === "stats" && (
          <div className="pad small">
            {!stats ? <p className="muted">Loading…</p> : (
              <dl className="kv">
                <dt>Executions</dt><dd>{String(stats.executions)}</dd>
                <dt>Resolution rate</dt><dd>{(Number(stats.resolution_rate) * 100).toFixed(0)}%</dd>
                <dt>Escalation rate</dt><dd>{(Number(stats.escalation_rate) * 100).toFixed(0)}%</dd>
                <dt>Avg latency</dt><dd>{String(stats.avg_latency_ms)} ms</dd>
                <dt>p95 latency</dt><dd>{String(stats.p95_latency_ms)} ms</dd>
                <dt>Tokens</dt><dd>{String(stats.tokens_in)} in / {String(stats.tokens_out)} out</dd>
                <dt>By agent</dt><dd>{Object.entries((stats.by_agent as Record<string, number>) ?? {}).map(([k, v]) => `${k}: ${v}`).join(", ") || "—"}</dd>
                <dt>Outcomes</dt><dd>{Object.entries((stats.outcomes as Record<string, number>) ?? {}).map(([k, v]) => `${k}: ${v}`).join(", ") || "—"}</dd>
              </dl>
            )}
            <p className="muted">Prometheus metrics: <a href="/metrics" target="_blank" rel="noreferrer">/metrics</a> · API docs: <a href="/api/docs" target="_blank" rel="noreferrer">/api/docs</a></p>
          </div>
        )}
      </div>
    </aside>
  );
}
