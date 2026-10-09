import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, hasToken, setToken, streamSSE } from "./api";
import ChatPane from "./components/ChatPane";
import Login from "./components/Login";
import Sidebar from "./components/Sidebar";
import WorkflowPane from "./components/WorkflowPane";
import type {
  ApprovalPayload, ChatMessage, ConversationSummary, Employee, RunInfo, StepEvent, StreamEvent, Ticket, WorkflowMap,
} from "./types";

interface ConversationDetail {
  id: string;
  title: string;
  messages: ChatMessage[];
  tickets: Ticket[];
  pending_approval: { execution_id: string; payload: ApprovalPayload } | null;
  last_execution_id: string | null;
}

interface ExecutionDetail {
  id: string;
  status: string;
  agent: string | null;
  outcome: string | null;
  confidence: number | null;
  llm_mode: string;
  steps: StepEvent[];
  tokens_in: number;
  tokens_out: number;
  latency_ms: number | null;
}

const IDLE: RunInfo = { executionId: null, status: "idle" };

export default function App() {
  const [employee, setEmployee] = useState<Employee | null>(null);
  const [map, setMap] = useState<WorkflowMap | null>(null);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [stats, setStats] = useState<Record<string, unknown> | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [events, setEvents] = useState<StepEvent[]>([]);
  const [run, setRun] = useState<RunInfo>(IDLE);
  const [streamingText, setStreamingText] = useState("");
  const [busy, setBusy] = useState(false);
  const [approval, setApproval] = useState<{ executionId: string; payload: ApprovalPayload } | null>(null);
  const [lastTicket, setLastTicket] = useState<Ticket | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showFlow, setShowFlow] = useState(false);
  const models = useRef<{ llm: string; models: string[] }>({ llm: "heuristic", models: [] });

  const refreshLists = useCallback(async () => {
    const [c, t, s] = await Promise.all([
      api.get<{ items: ConversationSummary[] }>("/conversations?page_size=50"),
      api.get<{ items: Ticket[] }>("/tickets?page_size=50"),
      api.get<Record<string, unknown>>("/stats"),
    ]);
    setConversations(c.items);
    setTickets(t.items);
    setStats(s);
  }, []);

  const logout = useCallback(() => {
    setToken(null);
    setEmployee(null);
    setConversationId(null);
    setMessages([]);
    setEvents([]);
    setRun(IDLE);
    setApproval(null);
    setLastTicket(null);
  }, []);

  useEffect(() => {
    api.get<{ llm: string; models: string[] }>("/auth/config").then((c) => (models.current = c)).catch(() => {});
    if (hasToken()) api.get<Employee>("/auth/me").then(setEmployee).catch(() => logout());
  }, [logout]);

  useEffect(() => {
    if (!employee) return;
    api.get<WorkflowMap>("/workflow/graph").then(setMap).catch(() => {});
    refreshLists().catch(() => {});
  }, [employee, refreshLists]);

  const handleEvent = useCallback((e: StreamEvent) => {
    switch (e.type) {
      case "accepted":
        if (e.conversation_id) setConversationId(e.conversation_id);
        if (e.user_message) {
          const um = e.user_message;
          setMessages((p) => p.map((m) => (m.id === "pending-user" ? { ...m, id: um.id } : m)));
        }
        setRun((r) => ({ ...r, executionId: e.execution_id }));
        break;
      case "run_start":
      case "run_resume":
        setRun((r) => ({ ...r, status: "running", llmMode: e.llm_mode, models: e.models, executionId: e.execution_id }));
        break;
      case "step":
        setEvents((p) => [...p, e]);
        if (e.agent) setRun((r) => ({ ...r, agent: e.agent }));
        break;
      case "token":
        setStreamingText((t) => t + e.text);
        break;
      case "interrupt":
        setStreamingText("");
        setApproval({ executionId: e.execution_id, payload: e.payload });
        setRun((r) => ({ ...r, status: "waiting" }));
        break;
      case "final": {
        setStreamingText("");
        setApproval(null);
        setMessages((p) => [...p, { ...e.message, outcome: e.outcome, confidence: e.confidence }]);
        if (e.ticket) setLastTicket(e.ticket);
        setRun((r) => ({
          ...r, status: "done", outcome: e.outcome, confidence: e.confidence, agent: e.agent,
          totalMs: r.startedAt ? Date.now() - r.startedAt : undefined,
          tokensIn: e.usage.reduce((a, u) => a + u.tokens_in, 0),
          tokensOut: e.usage.reduce((a, u) => a + u.tokens_out, 0),
        }));
        break;
      }
      case "error":
        setError(e.message);
        setRun((r) => ({ ...r, status: "error" }));
        break;
    }
  }, []);

  const runStream = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return logout();
      setError((err as Error).message);
      setRun((r) => ({ ...r, status: "error" }));
    } finally {
      setBusy(false);
      refreshLists().catch(() => {});
    }
  };

  const send = (text: string, attachmentIds: string[]) => {
    setMessages((p) => [...p, { id: "pending-user", role: "user", content: text }]);
    setEvents([]);
    setStreamingText("");
    setRun({ executionId: null, status: "running", startedAt: Date.now(), llmMode: models.current.llm, models: models.current.models });
    runStream(() => streamSSE("/chat/stream", { conversation_id: conversationId, message: text, attachment_ids: attachmentIds }, handleEvent));
  };

  const decide = (approved: boolean, comment: string) => {
    if (!approval) return;
    const id = approval.executionId;
    setApproval(null);
    setRun((r) => ({ ...r, status: "running" }));
    runStream(() => streamSSE(`/executions/${id}/approval`, { approved, comment }, handleEvent));
  };

  const showTrace = async (executionId: string) => {
    const ex = await api.get<ExecutionDetail>(`/executions/${executionId}`);
    setEvents(ex.steps);
    setRun({
      executionId: ex.id, status: ex.status === "interrupted" ? "waiting" : ex.status === "failed" ? "error" : "done",
      replay: ex.status !== "interrupted", llmMode: ex.llm_mode, totalMs: ex.latency_ms ?? undefined,
      tokensIn: ex.tokens_in, tokensOut: ex.tokens_out, outcome: ex.outcome, confidence: ex.confidence, agent: ex.agent,
    });
    setShowFlow(true);
  };

  const openConversation = async (id: string) => {
    const c = await api.get<ConversationDetail>(`/conversations/${id}`);
    setConversationId(c.id);
    setMessages(c.messages);
    setApproval(c.pending_approval ? { executionId: c.pending_approval.execution_id, payload: c.pending_approval.payload } : null);
    setLastTicket(c.tickets.length ? c.tickets[c.tickets.length - 1] : null);
    setStreamingText("");
    setError(null);
    if (c.last_execution_id) await showTrace(c.last_execution_id);
    else {
      setEvents([]);
      setRun(IDLE);
    }
  };

  const newConversation = () => {
    setConversationId(null);
    setMessages([]);
    setEvents([]);
    setRun(IDLE);
    setApproval(null);
    setLastTicket(null);
    setError(null);
  };

  const feedback = async (m: ChatMessage, f: { helpful?: boolean; resolved?: boolean }) => {
    const r = await api.post<{ ticket: Ticket | null }>("/feedback", { message_id: m.id, ...f });
    if (r.ticket) setLastTicket(r.ticket);
    refreshLists().catch(() => {});
  };

  if (!employee) {
    return <Login onLogin={(t, e) => { setToken(t); setEmployee(e); }} />;
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand"><span className="logo" aria-hidden />Agentic Support Desk</div>
        <button className="btn small only-mobile" onClick={() => setShowFlow(!showFlow)}>
          {showFlow ? "Chat" : "Workflow"}
        </button>
        <span className="spacer" />
        <span className="badge" title={models.current.models.join(" → ")}>
          {models.current.llm === "groq" ? `Groq · ${models.current.models[0] ?? ""}` : "Offline heuristics"}
        </span>
        <span className="who">
          <span className="avatar small" aria-hidden>{employee.name.split(" ").map((p) => p[0]).join("")}</span>
          <span className="who-text">
            <strong>{employee.name}</strong>
            <span className="small muted">{employee.role} · {employee.department} · {employee.country}</span>
          </span>
        </span>
        <button className="btn small" onClick={logout}>Switch user</button>
      </header>
      <div className={`layout ${showFlow ? "flow-open" : ""}`}>
        <WorkflowPane map={map} events={events} run={run} onClose={() => setShowFlow(false)} />
        <ChatPane
          messages={messages}
          streamingText={streamingText}
          busy={busy}
          approval={approval}
          activeExecution={run.executionId}
          lastTicket={lastTicket}
          error={error}
          onSend={send}
          onDecide={decide}
          onShowTrace={showTrace}
          onFeedback={feedback}
        />
        <Sidebar
          conversations={conversations}
          tickets={tickets}
          currentId={conversationId}
          onSelect={openConversation}
          onNew={newConversation}
          stats={stats}
        />
      </div>
    </div>
  );
}
