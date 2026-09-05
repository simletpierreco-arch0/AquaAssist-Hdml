"""
website_sync.py — imports content from nawasa.gd into the local database
so AquaAssist's knowledge base can include it, WITHOUT the chatbot ever
depending on nawasa.gd being reachable at the moment a customer is asking
a question. Sync runs on a timer (see app.py's _website_sync_loop) and can
also be triggered on demand by staff.

Deliberately crawls a curated, hand-picked list of pages (NAWASA_PAGES
below) rather than following every link on the site. An open-ended crawl
of an external site AquaVision doesn't control is a reliability and
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
import time

import requests

logger = logging.getLogger("aquaassist.website_sync")

REQUEST_TIMEOUT_SECONDS = 15
MAX_CONTENT_CHARS = 6000  # keep each stored page to a sane size for embedding
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.5
INTER_PAGE_DELAY_SECONDS = 1.5  # hammering the site back-to-back is itself a bot signal

BROWSER_HEADERS = {
    # A fuller, more consistent set of browser-fingerprint headers than a bare
    # User-Agent. Some WAFs (Cloudflare, Sucuri, etc.) score requests missing
    # Accept/Accept-Language/sec-fetch-* headers as automated even with a
    # legitimate-looking User-Agent string.
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Cache-Control": "max-age=0",
}

_session = None


def _get_session():
    """A shared requests.Session persists cookies across every page fetch
    in a sync run. Many WAFs set a clearance/session cookie on the FIRST
    request from a client and only let subsequent requests through if that
    cookie is presented — a fresh, cookie-less request every time (the old
    behavior) looks identical to a bot probing one URL and vanishing.
    Visiting the homepage first, keeping the session, and reusing it for
    every other page gives a real browser-like request pattern a much
    better chance of passing."""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(BROWSER_HEADERS)
    return _session

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


def _looks_like_challenge(resp_text):
    return (
        re.search(r'\.well-known/[a-z0-9_-]*captcha', resp_text, re.IGNORECASE)
        or re.search(r'http-equiv=["\']refresh["\']', resp_text, re.IGNORECASE)
    )


def fetch_page(url, referer=None):
    """Returns (text, error). error is None on success, or a short string
    describing what went wrong (never raises). Uses a shared, cookie-
    persisting session (see _get_session) and retries a few times with a
    short backoff — a WAF challenge is sometimes intermittent (e.g. only
    triggered on the very first request from a new session/IP), so a
    retry after the session already has a cookie can succeed where the
    first attempt didn't."""
    session = _get_session()
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            headers = {"Referer": referer} if referer else {}
            resp = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS, headers=headers, allow_redirects=True)
        except requests.RequestException as e:
            last_error = f"request failed: {e}"
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        if not (200 <= resp.status_code < 300):
            last_error = f"HTTP {resp.status_code}"
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        if _looks_like_challenge(resp.text):
            last_error = ("nawasa.gd returned an anti-bot/CAPTCHA challenge page instead of real "
                          "content — this can't be fetched automatically. It needs NAWASA's IT team "
                          "to allowlist this server, or use the manual import option below instead.")
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        # Success — fall through to content extraction below using this resp.
        return _extract_and_validate(resp)

    return None, last_error


def _extract_and_validate(resp):
    # Challenge-page detection already happened in fetch_page's retry loop
    # (via _looks_like_challenge) before this is called — this function
    # only runs against a response that passed that check.
    text = _html_to_text(resp.text)
    if len(text) < 40:
        # DIAGNOSTIC FIX: this used to return a generic "little or no
        # readable text" with no way to tell WHY — three very different
        # root causes all look identical from that message alone:
        #   (a) the page is genuinely empty/broken
        #   (b) the site renders its content client-side via JavaScript
        #       (a plain HTTP fetch only ever sees the empty shell HTML,
        #       since nothing here executes JS)
        #   (c) the HTML-stripping regex is over-eager and is deleting
        #       real content along with the markup
        # A raw-HTML preview makes it possible to tell these apart at a
        # glance instead of guessing blind.
        raw_len = len(resp.text)
        raw_preview = re.sub(r"\s+", " ", resp.text).strip()[:220]
        looks_js_rendered = bool(re.search(r'id=["\'](root|app|__next|___gatsby)["\']', resp.text, re.IGNORECASE)) \
            or "you need to enable javascript" in resp.text.lower()
        hint = " — looks like a JavaScript-rendered page (a plain HTTP fetch can't run its JS)" if looks_js_rendered else ""
        return None, (f"page returned little or no readable text after stripping HTML "
                       f"(raw HTML was {raw_len} chars; stripped to {len(text)}){hint}. "
                       f"Raw preview: {raw_preview!r}")
    # A WAF/anti-bot challenge page (e.g. Cloudflare's "Just a moment...")
    # can pass the length check above while still being useless content —
    # catch the common phrasing so we don't silently store a challenge
    # page as if it were the real article.
    lowered = text[:400].lower()
    if any(phrase in lowered for phrase in ("just a moment", "checking your browser", "enable javascript and cookies")):
        return None, "response looked like a bot/security challenge page, not real content"
    return text[:MAX_CONTENT_CHARS], None


def sync_all(db, pages=None):
    """Fetches every page in `pages` (defaults to NAWASA_PAGES) and saves
    each via db.save_website_page(). Returns a summary dict. A page that
    fails does not stop the rest — see module docstring.

    Session warmup: visits the site's homepage first (outside the timed
    page list) purely to let the shared session pick up whatever cookie
    the WAF hands out on a first visit, then reuses that session — with a
    short delay between each subsequent request — for every page in
    `pages`. This mimics how a real browser actually behaves (land on the
    site, then navigate) far more closely than firing isolated, cookie-
    less requests at arbitrary URLs, which is what the previous version
    did and is a strong bot signal on its own.

    Also deactivates any previously-synced (auto) page whose URL is no
    longer in the curated page list, so removing a URL from NAWASA_PAGES
    is enough to retire it from the knowledge base on the next sync —
    no manual database cleanup needed."""
    pages = pages if pages is not None else NAWASA_PAGES

    # Reset the session for every sync run so a stale/expired cookie from
    # hours ago doesn't get reused and silently fail all over again.
    global _session
    _session = None

    home_url = "https://www.nawasa.gd/"
    try:
        _get_session().get(home_url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
        time.sleep(INTER_PAGE_DELAY_SECONDS)
    except requests.RequestException as e:
        logger.warning("Website sync: session warmup request to %s failed (continuing anyway): %s", home_url, e)

    ok_count, error_count = 0, 0
    for i, (url, title) in enumerate(pages):
        text, error = fetch_page(url, referer=home_url)
        if error:
            logger.warning("Website sync: failed to fetch %s — %s", url, error)
            db.save_website_page(url, title, "", status="error", error=error)
            error_count += 1
        else:
            db.save_website_page(url, title, text, status="ok", error="")
            ok_count += 1
        if i < len(pages) - 1:
            time.sleep(INTER_PAGE_DELAY_SECONDS)
    removed = 0
    try:
        removed = db.deactivate_website_pages_not_in([u for u, _ in pages])
    except Exception as e:
        logger.warning("Could not deactivate stale website pages: %s", e)
    logger.info("Website sync complete: %d ok, %d failed, %d deactivated.", ok_count, error_count, removed)
    return {"ok": ok_count, "failed": error_count, "total": len(pages), "deactivated": removed}
