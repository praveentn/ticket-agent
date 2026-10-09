"""Prometheus metrics for the NFRs in spec section 6 (latency, tokens, routing, tools, resolution, escalation)."""

from prometheus_client import Counter, Histogram

HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "API latency", ["method", "route", "status"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 5, 10, 30),
)
NODE_LATENCY = Histogram(
    "agent_node_duration_seconds", "Workflow node latency", ["node"],
    buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
LLM_CALLS = Counter("llm_calls_total", "LLM calls", ["purpose", "model", "outcome"])
LLM_TOKENS = Counter("llm_tokens_total", "LLM token consumption", ["direction", "model"])
ROUTING = Counter("agent_routing_total", "Requests routed per department and route", ["department", "route"])
TOOL_CALLS = Counter("agent_tool_calls_total", "Tool executions", ["tool", "outcome"])
OUTCOMES = Counter("agent_outcomes_total", "Workflow outcomes", ["outcome"])
ESCALATIONS = Counter("agent_escalations_total", "Escalations by reason", ["reason"])
RETRIEVAL_TOP_SCORE = Histogram(
    "retrieval_top_score", "Top rerank score per query", buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
)
