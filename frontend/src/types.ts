export interface Employee {
  id: string;
  name: string;
  email: string;
  role: string;
  department: string;
  country: string;
  manager_id: string | null;
  title?: string;
}

export interface Citation {
  n: number;
  title: string;
  heading: string | null;
  source: string;
  version: string;
  document_id: string;
  snippet: string;
}

export interface AttachmentRef {
  id: string;
  filename: string;
  content_type: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  attachments?: AttachmentRef[];
  execution_id?: string | null;
  created_at?: string | null;
  outcome?: string | null;
  confidence?: number | null;
}

export interface Ticket {
  id: string;
  number: string;
  external_id: string | null;
  sync_status: string;
  department: string;
  category: string;
  title: string;
  priority: string;
  status: string;
  assigned_queue: string;
  resolution: string | null;
  escalation_reason: string | null;
  conversation_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ConversationSummary {
  id: string;
  title: string;
  status: string;
  updated_at: string;
}

export interface ApprovalPayload {
  kind: string;
  tool: string;
  description: string;
  args: Record<string, unknown>;
  reason: string;
  approver_role: string;
  approver_name: string | null;
  requester: string;
}

export type StepPhase = "start" | "end" | "error" | "waiting";

export interface StepEvent {
  type: "step";
  phase: StepPhase;
  node: string;
  label: string;
  group: string;
  agent: string | null;
  ts: string;
  duration_ms?: number;
  summary?: string;
  data?: Record<string, unknown>;
  seq?: number;
}

export type StreamEvent =
  | StepEvent
  | { type: "accepted"; conversation_id: string; conversation_title?: string; user_message?: ChatMessage; execution_id: string; correlation_id?: string }
  | { type: "run_start" | "run_resume"; llm_mode: string; models: string[]; execution_id: string }
  | { type: "token"; text: string }
  | { type: "interrupt"; payload: ApprovalPayload; execution_id: string }
  | {
      type: "final";
      message: ChatMessage;
      outcome: string;
      agent: string | null;
      confidence: number | null;
      ticket: Ticket | null;
      secondary_tickets: Ticket[];
      usage: { purpose: string; model: string; tokens_in: number; tokens_out: number }[];
      execution_id: string;
    }
  | { type: "error"; message: string }
  | { type: "ping" };

export interface WorkflowNode {
  id: string;
  label: string;
  group: string;
  explain: string;
}

export interface AgentInfo {
  id: string;
  label: string;
  description: string;
  tools: string[];
  collections: string[];
  queue: string;
}

export interface WorkflowMap {
  nodes: WorkflowNode[];
  orchestrator_pre: string[];
  agent_knowledge: string[];
  agent_action: string[];
  orchestrator_post: string[];
  agents: AgentInfo[];
  mermaid: string;
}

export interface RunInfo {
  executionId: string | null;
  status: "idle" | "running" | "waiting" | "done" | "error";
  llmMode?: string;
  models?: string[];
  startedAt?: number;
  totalMs?: number;
  tokensIn?: number;
  tokensOut?: number;
  outcome?: string | null;
  confidence?: number | null;
  agent?: string | null;
  replay?: boolean;
}
