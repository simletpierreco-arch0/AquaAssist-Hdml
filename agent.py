"""
AquaAssist agent layer — LangGraph orchestration + Pinecone-backed RAG.

This module replaces the previous direct-`google-genai` chat orchestration
(a single `genai.Client` plus a hand-rolled `SESSIONS` dict in app.py) with:

- LANGCHAIN / LANGGRAPH: the agent loop is built with
  `langchain.agents.create_agent`, LangChain's current (non-deprecated)
  entry point for a standard ReAct-style tool-calling agent — the model
  decides whether to call a tool, the tool runs, its result goes back to
  the model, and this repeats until the model produces a final answer.
  (Older examples online use `langgraph.prebuilt.create_react_agent` —
  that's deprecated as of LangGraph v1.0 in favor of this.) Conversation
  memory persists via a LangGraph checkpointer (`InMemorySaver`), keyed
  by a `thread_id` — see `get_or_create_agent()` below.

- PINECONE / RAG: NAWASA's FAQ knowledge base is embedded once at startup
  (`seed_knowledge_base()`) using Gemini's `gemini-embedding-001` model
  via `langchain_google_genai.GoogleGenerativeAIEmbeddings`, and upserted
  into a Pinecone serverless index. At query time, the agent has a
  `search_knowledge_base` tool (`make_search_knowledge_base_tool()`) that
  embeds the customer's actual question and retrieves the top-K most
  relevant FAQ entries from Pinecone. This is real retrieval-at-query-time
  RAG — a deliberate change from the previous approach (the entire FAQ
  list concatenated into every system prompt regardless of what was
  asked), which doesn't scale and isn't what "RAG" means.

REQUIRED environment variables for this to be fully active:
    GEMINI_API_KEY      (also used elsewhere in app.py)
    PINECONE_API_KEY
Optional (all have defaults):
    PINECONE_INDEX_NAME  (default: "aquaassist-knowledge-base")
    PINECONE_CLOUD       (default: "aws")
    PINECONE_REGION      (default: "us-east-1")

If PINECONE_API_KEY isn't set, `search_knowledge_base` falls back to
returning the full static FAQ text instead of a live retrieval, so local
development is still possible without a Pinecone account — but this is a
fallback, not the real feature. Real RAG retrieval requires Pinecone to
actually be configured.
"""

import logging
import os

from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langgraph.checkpoint.memory import InMemorySaver

logger = logging.getLogger("aquaassist.agent")

MODEL_NAME = "gemini-3.1-flash-lite"
EMBEDDING_MODEL = "gemini-embedding-001"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY", "")
PINECONE_INDEX_NAME = os.environ.get("PINECONE_INDEX_NAME", "aquaassist-knowledge-base")
PINECONE_CLOUD = os.environ.get("PINECONE_CLOUD", "aws")
PINECONE_REGION = os.environ.get("PINECONE_REGION", "us-east-1")

# One shared checkpointer for every agent graph — it's just a storage
# backend keyed by thread_id, so multiple compiled graphs (one gets
# (re)built per session, since tools below are session-scoped closures)
# can safely share a single instance.
_checkpointer = InMemorySaver()

_embeddings = None
_pinecone_index = None
_static_faq_fallback_text = ""  # set by seed_knowledge_base()


def _get_embeddings():
    global _embeddings
    if _embeddings is None:
        _embeddings = GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL, google_api_key=GEMINI_API_KEY)
    return _embeddings


def _get_pinecone_index():
    """Lazily connects to (and creates, if missing) the Pinecone index.
    Returns None if PINECONE_API_KEY isn't configured — callers must
    handle that by falling back to the static FAQ text."""
    global _pinecone_index
    if not PINECONE_API_KEY:
        return None
    if _pinecone_index is not None:
        return _pinecone_index
    from pinecone import Pinecone, ServerlessSpec
    pc = Pinecone(api_key=PINECONE_API_KEY)
    existing = [idx.name for idx in pc.list_indexes()]
    if PINECONE_INDEX_NAME not in existing:
        probe_vector = _get_embeddings().embed_query("dimension probe")
        pc.create_index(
            name=PINECONE_INDEX_NAME,
            dimension=len(probe_vector),
            metric="cosine",
            spec=ServerlessSpec(cloud=PINECONE_CLOUD, region=PINECONE_REGION),
        )
        logger.info("Created Pinecone index %r (dimension=%d)", PINECONE_INDEX_NAME, len(probe_vector))
    _pinecone_index = pc.Index(PINECONE_INDEX_NAME)
    return _pinecone_index


def _format_faqs_as_text(faqs):
    lines = []
    current_cat = None
    for f in faqs:
        if f["category"] != current_cat:
            current_cat = f["category"]
            lines.append(f"\n[{current_cat}]")
        lines.append(f"Q: {f['q']}\nA: {f['a']}")
    return "\n".join(lines)


def seed_knowledge_base(faqs, force=False):
    """Embeds and upserts the FAQ list into Pinecone. Call once at startup,
    and again (with force=True) whenever staff add/edit/disable an FAQ via
    the Knowledge Base admin panel, so Pinecone stays in sync with what's
    actually being served to customers.

    Idempotent by default — if the index already has at least as many
    vectors as there are FAQ entries, this is a no-op, so a server restart
    doesn't re-embed (and re-bill) the same content every time. Pass
    force=True to always re-upsert regardless of count (safe: Pinecone
    upserts are by-id, so this just overwrites existing vectors rather than
    duplicating them).
    """
    global _static_faq_fallback_text
    _static_faq_fallback_text = _format_faqs_as_text(faqs)

    index = _get_pinecone_index()
    if index is None:
        logger.warning(
            "PINECONE_API_KEY is not set — RAG knowledge base is INACTIVE. "
            "search_knowledge_base will fall back to a static FAQ dump instead "
            "of live Pinecone retrieval."
        )
        return
    try:
        if not force:
            stats = index.describe_index_stats()
            existing_count = stats.get("total_vector_count", 0) if isinstance(stats, dict) else getattr(stats, "total_vector_count", 0)
            if existing_count >= len(faqs):
                logger.info("Pinecone index %r already seeded (%d vectors) — skipping.", PINECONE_INDEX_NAME, existing_count)
                return
        embeddings = _get_embeddings()
        texts = [f"{f['q']} {f['a']}" for f in faqs]
        vectors_list = embeddings.embed_documents(texts)
        vectors = [
            {
                "id": f"faq-{i}",
                "values": vec,
                "metadata": {"question": f["q"], "answer": f["a"], "category": f["category"]},
            }
            for i, (f, vec) in enumerate(zip(faqs, vectors_list))
        ]
        index.upsert(vectors=vectors)
        logger.info("Seeded %d FAQ entries into Pinecone index %r.", len(vectors), PINECONE_INDEX_NAME)
    except Exception as e:
        logger.error("Failed to seed Pinecone knowledge base: %s", e)


def make_search_knowledge_base_tool(on_no_match=None):
    """on_no_match, if given, is called as on_no_match(query) whenever
    neither Pinecone nor the static fallback finds a close match. This is
    how "Questions AquaAssist Couldn't Answer" gets populated for staff
    review, without changing search_knowledge_base's own retrieval logic."""
    @tool
    def search_knowledge_base(query: str) -> str:
        """Searches NAWASA's official knowledge base — FAQs on billing, new
        connections, disconnections, leaks, and general service policy —
        for content relevant to the customer's question. Call this whenever
        a customer asks something that sounds like a policy, cost, process,
        or general-information question, instead of answering from memory.

        Args:
            query: The customer's question, in their own words or a short
                paraphrase of what they're actually asking about.

        Returns:
            The most relevant knowledge base entries for this query, or a
            note that no close match was found.
        """
        index = _get_pinecone_index()
        if index is None:
            return _static_faq_fallback_text
        try:
            query_vector = _get_embeddings().embed_query(query)
            results = index.query(vector=query_vector, top_k=3, include_metadata=True)
        except Exception as e:
            logger.error("Pinecone query failed, falling back to static FAQ text: %s", e)
            return _static_faq_fallback_text

        matches = results.get("matches") if isinstance(results, dict) else getattr(results, "matches", [])
        if not matches:
            if on_no_match:
                try:
                    on_no_match(query)
                except Exception as e:
                    logger.warning("on_no_match callback failed: %s", e)
            return "No closely matching knowledge base entry was found for this question."

        lines = []
        for m in matches:
            md = m.get("metadata") if isinstance(m, dict) else getattr(m, "metadata", {})
            md = md or {}
            lines.append(f"Q: {md.get('question', '')}\nA: {md.get('answer', '')}")
        return "\n\n".join(lines)
    return search_knowledge_base


def build_agent(tools, system_prompt):
    """Compiles a fresh LangGraph agent graph bound to the given session-
    scoped tools and system prompt. The caller (app.py) is responsible for
    caching this per session/territory, same as the old chat-session dict."""
    model = ChatGoogleGenerativeAI(model=MODEL_NAME, temperature=0.7, google_api_key=GEMINI_API_KEY)
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        checkpointer=_checkpointer,
    )


def _extract_reply_text(content):
    """LangChain's AIMessage.content is typed as str | list[str | dict], and
    for Gemini 3+ models specifically it's the list form. Every caller of
    invoke_agent() expects a plain string, so this normalizes either shape.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "".join(parts)
    return str(content) if content is not None else ""


def invoke_agent(graph, thread_id, content_blocks):
    """Runs one turn of the agent. content_blocks is a list of LangChain
    multimodal content blocks (text / image_url / file), already built by
    the caller. Returns the final reply text."""
    result = graph.invoke(
        {"messages": [{"role": "user", "content": content_blocks}]},
        {"configurable": {"thread_id": thread_id}},
    )
    final_message = result["messages"][-1]
    return _extract_reply_text(final_message.content)
