"""
pdf_ingest.py — downloads NAWASA's official PDF forms and extracts their
ACTUAL text content, so AquaAssist can answer questions about what a form
requires instead of only knowing its name/description/URL.

Pipeline implemented here:

    PDF url -> download -> per-page text extraction (pypdf)
            -> cleaning -> page-aware chunking
            -> [{"content", "page_number", "chunk_index"}, ...]

The caller (app.py) hands the returned chunk list to
agent.sync_documents_to_pinecone(), which computes content hashes, embeds
only new/changed chunks, and upserts them into Pinecone with metadata
(source_type="pdf", form_id, page_number, etc.) — see agent.py.

Deliberately dependency-light: uses pypdf (pure Python, no system
dependency like poppler/tesseract) rather than OCR. NAWASA's forms are
expected to be text-based PDFs. If a given PDF turns out to be a scanned
image with no text layer, fetch_and_chunk_pdf returns an empty chunk list
and a DESCRIPTIVE error explaining exactly that, rather than silently
producing nothing that looks like a successful no-op — that distinction
matters for the staff-facing sync summary and for debugging "why doesn't
AquaAssist know this form's contents".

Never raises: every public function returns (result, error) and problems
are reported as strings, not exceptions, so one bad PDF can never break a
sync run for the other four forms (same failure-isolation philosophy as
website_sync.py).
"""

import io
import logging
import re

import requests

logger = logging.getLogger("aquaassist.pdf_ingest")

REQUEST_TIMEOUT_SECONDS = 30
MAX_CHUNK_CHARS = 1400
MIN_CHUNK_CHARS = 200

_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")

_DOWNLOAD_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": "application/pdf,*/*;q=0.8",
}


def _clean_pdf_text(text):
    """PDF text extraction commonly produces ragged line breaks (one per
    visual line on the page, not per paragraph) and repeated whitespace.
    This does NOT try to reconstruct paragraphs across a hard page-width
    wrap — it normalizes whitespace and drops empty lines, and leaves
    paragraph-boundary detection to _chunk_page_text's blank-line/length
    based splitting, which is good enough for form-style PDFs (short
    fields, numbered clauses, tables) rather than long prose."""
    text = text or ""
    text = text.replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    text = "\n".join(ln for ln in lines if ln != "" or True)  # keep blank lines as paragraph breaks
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANKLINES_RE.sub("\n\n", text)
    return text.strip()


def _chunk_page_text(text, max_chars=MAX_CHUNK_CHARS, min_chars=MIN_CHUNK_CHARS):
    """Splits one page's cleaned text into paragraph-bounded chunks sized
    for semantic retrieval — not so small they lose context (a lone form
    field), not so large they dilute a single embedding across unrelated
    clauses. Deliberately duplicated (not imported) from agent.chunk_text's
    logic, so this module never needs to import the LangChain-heavy agent
    module just to chunk text — pdf_ingest can be tested/used standalone."""
    text = (text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks, current, current_len = [], [], 0
    for para in paragraphs:
        if current_len + len(para) + 1 > max_chars and current:
            chunks.append("\n\n".join(current))
            current, current_len = [], 0
        if len(para) > max_chars:
            start = 0
            while start < len(para):
                chunks.append(para[start:start + max_chars].strip())
                start += max_chars
            continue
        current.append(para)
        current_len += len(para) + 1
    if current:
        chunks.append("\n\n".join(current))

    merged = []
    for c in chunks:
        if merged and len(c) < min_chars:
            merged[-1] = merged[-1] + "\n\n" + c
        else:
            merged.append(c)
    return merged


def download_pdf(url):
    """Returns (bytes, error). error is None on success."""
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, headers=_DOWNLOAD_HEADERS)
    except requests.RequestException as e:
        return None, f"request failed: {e}"
    if not (200 <= resp.status_code < 300):
        return None, f"HTTP {resp.status_code}"
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if resp.content[:4] != b"%PDF" and "pdf" not in content_type:
        preview = resp.content[:120]
        return None, (f"response doesn't look like a PDF (Content-Type: "
                       f"{content_type or 'unknown'}, first bytes: {preview!r}) — "
                       f"the URL may be returning an HTML error/redirect page instead")
    return resp.content, None


def extract_pages(pdf_bytes):
    """Returns (list_of_cleaned_page_texts, error). Never raises."""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        return None, f"pypdf is not installed ({e}) — add 'pypdf' to requirements.txt"

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        return None, f"could not open PDF: {e}"

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            return None, "PDF is password-protected and could not be opened with an empty password"

    pages = []
    for i, page in enumerate(reader.pages):
        try:
            raw = page.extract_text() or ""
        except Exception as e:
            logger.warning("Failed to extract text from page %d: %s", i + 1, e)
            raw = ""
        pages.append(_clean_pdf_text(raw))
    return pages, None


def fetch_and_chunk_pdf(url, title, form_id):
    """Downloads `url`, extracts per-page text, and returns (chunks, error).

    On success: chunks is a non-empty list of
        {"content": str, "page_number": int, "chunk_index": int}
    ready to hand to agent.sync_documents_to_pinecone as a document's
    "prechunked" list.

    On failure: chunks is [] and error is a human-readable string
    explaining what went wrong (download failure, extraction failure, or
    "this looks like a scanned image with no text layer"). Never raises.
    """
    pdf_bytes, error = download_pdf(url)
    if error:
        return [], f"download failed: {error}"

    pages, error = extract_pages(pdf_bytes)
    if error:
        return [], f"text extraction failed: {error}"

    total_chars = sum(len(p) for p in pages)
    if total_chars < 30:
        return [], (f"extracted almost no text ({total_chars} chars across {len(pages)} page(s)) — "
                     f"this PDF may be a scanned image with no text layer, which this pipeline can't "
                     f"OCR. It would need manual transcription (e.g. via the website content 'Add "
                     f"manually' option) to be searchable.")

    chunks = []
    chunk_index = 0
    for page_num, page_text in enumerate(pages, start=1):
        if not page_text.strip():
            continue
        for piece in _chunk_page_text(page_text):
            if not piece.strip():
                continue
            chunks.append({"content": piece, "page_number": page_num, "chunk_index": chunk_index})
            chunk_index += 1

    if not chunks:
        return [], "no usable text chunks were produced after cleaning"

    logger.info("PDF ingest: %s -> %d chunk(s) across %d page(s) (%s)",
                title, len(chunks), len(pages), url)
    return chunks, None
