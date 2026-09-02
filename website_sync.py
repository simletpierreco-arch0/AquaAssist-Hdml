"""
website_sync.py — imports content from nawasa.gd into the local database
so AquaAssist's knowledge base can include it, WITHOUT the chatbot ever
depending on nawasa.gd being reachable at the moment a customer is asking
a question. Sync runs on a timer (see app.py's _website_sync_loop) and can
also be triggered on demand by staff.

Deliberately crawls a curated, hand-picked list of pages (NAWASA_PAGES
below) rather than following every link on the site. An open-ended crawl
of an external site AquaVission doesn't control is a reliability and
content-quality risk: it could pull in navigation cruft, unrelated press
photos, or (if the site's structure changes) garbage text that pollutes
the knowledge base and degrades answers. Add a URL below when a new page
is worth including — that's a deliberate, reviewable change.

Failure handling: a single page failing to fetch (timeout, 404, site
change) is logged and recorded with status="error" in the database, but
never raises — one bad page must never break the sync for the rest, and
a sync failure must never affect an in-progress customer conversation
(this module is only ever called from a background thread or an
explicit staff-triggered admin action, never from the /api/chat path).
"""

import logging
import re

import requests

logger = logging.getLogger("aquaassist.website_sync")

REQUEST_TIMEOUT_SECONDS = 12
MAX_CONTENT_CHARS = 6000  # keep each stored page to a sane size for embedding

NAWASA_PAGES = [
    ("https://www.nawasa.gd/", "NAWASA — Home"),
    ("https://www.nawasa.gd/about-us/vision-mission", "About NAWASA — Vision & Mission"),
    ("https://www.nawasa.gd/customer-care/customer-service-charter", "Customer Service Charter"),
    ("https://www.nawasa.gd/resources", "Resources"),
    ("https://www.nawasa.gd/contact-us", "Contact Us"),
    ("https://www.nawasa.gd/career-opportunities", "Career Opportunities"),
]

_TAG_STRIP_RE = re.compile(r"<(script|style|nav|footer|header|noscript)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")


def _html_to_text(html):
    """Deliberately dependency-light (no BeautifulSoup requirement) —
    strips script/style/nav/footer/header blocks, then all remaining
    tags, then collapses whitespace. Good enough for prose-heavy content
    pages like NAWASA's; not a general-purpose HTML parser."""
    text = _TAG_STRIP_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = (text.replace("&amp;", "&").replace("&nbsp;", " ")
                .replace("&#39;", "'").replace("&quot;", '"')
                .replace("&lt;", "<").replace("&gt;", ">"))
    text = _WHITESPACE_RE.sub(" ", text)
    lines = [ln.strip() for ln in text.split("\n")]
    text = "\n".join(ln for ln in lines if ln)
    text = _BLANKLINES_RE.sub("\n\n", text)
    return text.strip()


def fetch_page(url):
    """Returns (text, error). error is None on success, or a short string
    describing what went wrong (never raises)."""
    try:
        resp = requests.get(
            url, timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": "AquaAssist-KnowledgeSync/1.0 (+https://nawasa.gd/)"},
        )
    except requests.RequestException as e:
        return None, f"request failed: {e}"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    text = _html_to_text(resp.text)
    if len(text) < 40:
        return None, "page returned little or no readable text"
    return text[:MAX_CONTENT_CHARS], None


def sync_all(db, pages=None):
    """Fetches every page in `pages` (defaults to NAWASA_PAGES) and saves
    each via db.save_website_page(). Returns a summary dict. A page that
    fails does not stop the rest — see module docstring."""
    pages = pages if pages is not None else NAWASA_PAGES
    ok_count, error_count = 0, 0
    for url, title in pages:
        text, error = fetch_page(url)
        if error:
            logger.warning("Website sync: failed to fetch %s — %s", url, error)
            db.save_website_page(url, title, "", status="error", error=error)
            error_count += 1
        else:
            db.save_website_page(url, title, text, status="ok", error="")
            ok_count += 1
    logger.info("Website sync complete: %d ok, %d failed.", ok_count, error_count)
    return {"ok": ok_count, "failed": error_count, "total": len(pages)}
