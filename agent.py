"""
AquaAssist agent layer — LangGraph orchestration + Pinecone-backed RAG.
Unchanged from the existing project — carried forward as-is. See the
project README for details on the LangChain agent + Pinecone RAG wiring.
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

_checkpointer = InMemorySaver()

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
