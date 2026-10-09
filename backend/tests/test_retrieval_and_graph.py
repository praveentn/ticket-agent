from app.config import Settings
from app.orchestration import graph as g
from app.retrieval.embeddings import HashEmbedder
from app.retrieval.ingest import chunk_markdown, parse_markdown
from app.retrieval.search import rerank


def test_front_matter_and_chunking():
    doc = parse_markdown("---\ntitle: T\nallowed_countries: [IN]\n---\n# A\npara one\n\n## B\npara two", "x", "hr")
    assert doc.title == "T" and doc.meta["allowed_countries"] == ["IN"]
    chunks = chunk_markdown(doc.body)
    assert [h for h, _ in chunks] == ["A", "B"]


def test_hash_embedder_is_normalised():
    v = HashEmbedder(64).embed_query("reset vpn password")
    assert abs(sum(x * x for x in v) - 1.0) < 1e-6


def test_rerank_prefers_term_coverage():
    hits = [
        {"rrf": 0.03, "sim": 0.5, "title": "Expense", "heading": "Meals", "content": "meal allowance per day"},
        {"rrf": 0.03, "sim": 0.5, "title": "VPN", "heading": "Troubleshooting", "content": "vpn authentication failed reset"},
    ]
    top = rerank("vpn authentication failed", hits, keep=1)
    assert top[0]["title"] == "VPN"


def test_model_chain_strips_litellm_prefixes():
    s = Settings(groq_model_primary="openai/gpt-oss-120b", groq_model_fallback="llama-3.1-8b-instant",
                 groq_model_fallbacks='groq/openai/gpt-oss-120b,groq/groq/compound,"groq/llama-3.1-8b-instant"')
    assert s.llm_model_chain == ["openai/gpt-oss-120b", "groq/compound", "llama-3.1-8b-instant"]


def test_routing_functions():
    assert g.after_route({"route": "agent", "agent": "it"}) == "agent_it"
    assert g.after_route({"route": "smalltalk"}) == "respond"
    assert g.after_plan({"plan": {"mode": "action"}}) == "action"
    assert g.after_plan({"plan": {"mode": "knowledge"}, "errors": [{"node": "plan"}]}) == "stop"
    assert g.after_authorize({"authorization": {"allowed": True, "requires_approval": True}}) == "approval"
    assert g.after_authorize({"authorization": {"allowed": False}}) == "stop"
    assert g.after_verify({"verification": {"escalate": True}}) == "escalate"


def test_graph_compiles_with_all_department_subgraphs():
    compiled = g.build_graph().compile()
    nodes = set(compiled.get_graph().nodes)
    assert {"intake", "classify", "route", "verify", "respond", "agent_it", "agent_hr", "agent_finance"} <= nodes


def test_citation_normalisation():
    from app.orchestration.nodes import normalize_citations

    assert normalize_citations("A【1†L2-L4】 B[2†source] C[1, 3]") == "A[1] B[2] C[1][3]"
