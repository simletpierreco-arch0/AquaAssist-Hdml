"""
AquaAssist agent layer — LangGraph orchestration + Pinecone-backed RAG.
"""

import json
import logging
import os
import re

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
# BUG FIX: Pinecone's query() always returns its top_k nearest vectors,
# even when every one of them is a poor/irrelevant match for the query —
# there's no built-in relevance cutoff. That meant the "no matches found"
# branch below (which is what triggers on_no_match / unanswered-question
# logging) almost never fired once the index had any content in it at
# all: a customer could ask something completely absent from the
# knowledge base and still get back three low-relevance FAQ entries,
# which the model would then read, correctly judge as not answering the
# question, and improvise a "check nawasa.gd" reply — all without ever
# triggering the logging path, since from the tool's perspective matches
# WERE found. Filtering out matches below this cosine-similarity floor
# makes "no matches" actually reflect "nothing relevant was found".
# Tunable via env var since the right cutoff depends on the embedding
# model and the mix of content indexed.
KB_MIN_RELEVANCE_SCORE = float(os.environ.get("KB_MIN_RELEVANCE_SCORE", "0.55"))

GRENADA_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


def _build_checkpointer():
    """LangGraph's checkpointer is what actually gives the chatbot memory
    of a conversation — separate from db.py's `chat_messages` transcript
    table (which is just what staff see in the Live Chat monitor). If
    DATABASE_URL isn't set, this falls back to plain in-memory storage,
    which is fine for local dev but means the bot forgets every
    in-progress conversation on every restart/redeploy in production.
    When DATABASE_URL IS set (e.g. Neon), checkpoints are persisted to the
    same Postgres database everything else already uses, so conversation
    memory survives restarts too. Never raises — a checkpointer setup
    failure falls back to in-memory with a logged error rather than
    crashing the whole app."""
    if not GRENADA_DATABASE_URL:
        logger.warning(
            "DATABASE_URL is not set — LangGraph conversation memory will live only "
            "in this process's RAM and is lost on every restart/redeploy. Reports, "
            "FAQs, staff accounts, etc. in db.py are unaffected by this; this is "
            "specifically the chatbot's own turn-by-turn memory. Set DATABASE_URL "
            "to persist it too."
        )
        return InMemorySaver()
    try:
        from psycopg_pool import ConnectionPool
        from langgraph.checkpoint.postgres import PostgresSaver

        pool_kwargs = {"autocommit": True, "prepare_threshold": 0}
        if "sslmode=" not in GRENADA_DATABASE_URL:
            pool_kwargs["sslmode"] = "require"
        pool = ConnectionPool(conninfo=GRENADA_DATABASE_URL, max_size=10, kwargs=pool_kwargs)
        checkpointer = PostgresSaver(pool)
        checkpointer.setup()  # idempotent — creates the checkpoint tables on first run only
        logger.info("LangGraph checkpoints are persisted to Postgres — conversation memory now survives restarts.")
        return checkpointer
    except Exception as e:
        logger.error(
            "Failed to set up a Postgres-backed LangGraph checkpointer (%s) — "
            "falling back to in-memory (conversation memory will be lost on "
            "restart). Check DATABASE_URL and that psycopg/"
            "langgraph-checkpoint-postgres are installed.", e,
        )
        return InMemorySaver()


_checkpointer = _build_checkpointer()

_embeddings = None
_pinecone_index = None
_static_faq_fallback_text = ""


def _get_embeddings():
    global _embeddings
    if _embeddings is None:
        _embeddings = GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL, google_api_key=GEMINI_API_KEY)
    return _embeddings


def _get_pinecone_index():
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
    global _static_faq_fallback_text
    _static_faq_fallback_text = _format_faqs_as_text(faqs)

    try:
        index = _get_pinecone_index()
    except Exception as e:
        logger.error("Could not reach Pinecone to seed the knowledge base (%s) — "
                     "falling back to the static FAQ/website text dump for now.", e)
        return
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
        else:
            try:
                index.delete(delete_all=True)
            except Exception as e:
                logger.info("Pinecone delete-all before reseed: %s (continuing)", e)
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
        logger.info("Seeded %d FAQ/website entries into Pinecone index %r.", len(vectors), PINECONE_INDEX_NAME)
    except Exception as e:
        logger.error("Failed to seed Pinecone knowledge base: %s", e)


def make_search_knowledge_base_tool(on_no_match=None):
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
        try:
            index = _get_pinecone_index()
        except Exception as e:
            logger.error("Could not reach Pinecone for a knowledge-base search (%s) — using static fallback.", e)
            return _static_faq_fallback_text
        if index is None:
            return _static_faq_fallback_text
        try:
            query_vector = _get_embeddings().embed_query(query)
            results = index.query(vector=query_vector, top_k=3, include_metadata=True)
        except Exception as e:
            logger.error("Pinecone query failed, falling back to static FAQ text: %s", e)
            return _static_faq_fallback_text

        matches = results.get("matches") if isinstance(results, dict) else getattr(results, "matches", [])
        # Apply the relevance floor — see KB_MIN_RELEVANCE_SCORE above.
        relevant_matches = []
        for m in matches or []:
            score = m.get("score") if isinstance(m, dict) else getattr(m, "score", None)
            if score is None or score >= KB_MIN_RELEVANCE_SCORE:
                relevant_matches.append(m)
        matches = relevant_matches
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
    model = ChatGoogleGenerativeAI(model=MODEL_NAME, temperature=0.7, google_api_key=GEMINI_API_KEY)
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        checkpointer=_checkpointer,
    )


def _extract_reply_text(content):
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
    result = graph.invoke(
        {"messages": [{"role": "user", "content": content_blocks}]},
        {"configurable": {"thread_id": thread_id}},
    )
    final_message = result["messages"][-1]
    return _extract_reply_text(final_message.content)


def suggest_staff_replies(transcript_messages, max_suggestions=3):
    """transcript_messages: list of {"role","content",...} dicts from
    db.load_session_messages() (role is "user"/"assistant"/"staff").
    Returns up to max_suggestions short reply drafts, or [] — never
    raises — if GEMINI_API_KEY isn't configured or the call fails for any
    reason. Suggestions are a convenience; the Live Chat monitor must
    keep working perfectly well without them."""
    if not GEMINI_API_KEY:
        return []
    if not transcript_messages:
        return []
    try:
        model = ChatGoogleGenerativeAI(model=MODEL_NAME, temperature=0.4, google_api_key=GEMINI_API_KEY)
        role_labels = {"user": "Customer", "assistant": "AquaAssist (bot)", "staff": "Staff"}
        convo_lines = [
            f"{role_labels.get(m.get('role'), m.get('role'))}: {m.get('content', '')}"
            for m in transcript_messages[-12:]
        ]
        convo_text = "\n".join(convo_lines)

        prompt = (
            "You are helping a NAWASA (National Water and Sewerage Authority, Grenada) customer "
            "service representative reply to a customer in a live chat. Here is the conversation "
            "so far, oldest first:\n\n"
            f"{convo_text}\n\n"
            f"Suggest {max_suggestions} short, genuinely different reply drafts the staff member "
            "could send next, in a warm, professional customer-service tone. Each must be a complete, "
            "ready-to-send reply on its own (not a fragment), under 40 words, and take a distinct "
            "angle where the situation allows it (e.g. one apologetic/reassuring, one action-oriented "
            "with concrete next steps, one asking a clarifying question) — never near-duplicates of "
            "each other. Reply with ONLY a JSON array of strings — no markdown fences, no numbering, "
            "no explanation before or after it."
        )
        result = model.invoke(prompt)
        text = result.content if isinstance(result.content, str) else _extract_reply_text(result.content)
        text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", text.strip())
        suggestions = json.loads(text)
        if not isinstance(suggestions, list):
            return []
        return [str(s).strip() for s in suggestions if str(s).strip()][:max_suggestions]
    except Exception as e:
        logger.warning("suggest_staff_replies failed (non-fatal — Live Chat still works without it): %s", e)
        return []
