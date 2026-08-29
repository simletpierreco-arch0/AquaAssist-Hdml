"""
db.py — persistent storage for AquaAssist.

Backed by SQLite by default or Postgres if DATABASE_URL is set. See the
original project docstring history for the ephemeral-disk rationale.

THIS VERSION adds (on top of the original reports/notifications/outages/
tips/features tables):
  - `faqs`               — staff-editable knowledge base entries, replacing
                            the hardcoded FAQS list in app.py.
  - `unanswered_questions`— questions AquaAssist couldn't find a knowledge-
                            base match for, logged for staff review.
  - `chat_events`         — one row per successful/failed /api/chat turn,
                            used for the staff Overview "conversations
                            today" counters.
"""

import csv
import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
else:
    import sqlite3

SQLITE_PATH = DATA_DIR / "aquaassist.db"

logger = logging.getLogger("aquaassist.db")

STATUS_DEFAULT = "Received"
SEVERITY_DEFAULT = "Unknown"

GRENADA_TZ = timezone(timedelta(hours=-4))

DEFAULT_FEATURES = {
    "faqs": True, "water_tips": True, "report_issue": True, "whatsapp": True,
    "voice_notes": True, "notify": True, "camera": True, "quick_actions": True,
    "dark_mode": True, "high_contrast": True, "large_text": True, "read_aloud": True,
    "call_us": True, "website": True, "chatbot_available": True, "settings": True,
}
DEFAULT_MAINTENANCE_MESSAGE = (
    "We're sorry — AquaAssist is temporarily unavailable. Please contact "
    "NAWASA directly at (473) 440-2155 or via WhatsApp, and we'll be back "
    "online as soon as possible."
)
DEFAULT_TIPS = [
    "Check for hidden leaks: turn off every tap in your home, then watch your meter dial. If it's still moving, water is escaping somewhere.",
    "Conserve water during dry months by fixing dripping taps promptly — a single slow drip can waste over 15 gallons a day.",
    "Protect your water storage tank by keeping the lid securely closed to prevent debris, insects, and contamination.",
    "During a scheduled interruption, store enough water for drinking, cooking, and sanitation, and avoid running your pump dry.",
    "Spot a leak on the street, a burst main, or a damaged hydrant? Report it to NAWASA right away via AquaAssist or call (473) 440-2155.",
    "Regularly check your faucets and toilets for silent leaks — a toilet that keeps running after flushing can waste hundreds of gallons a month.",
]


# ---------------------------------------------------------------------
# Connection handling (pooled)
# ---------------------------------------------------------------------
_pg_pool = None
_sqlite_conn = None
_sqlite_lock = threading.Lock()


def _init_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(
            1, 10,
            dsn=DATABASE_URL,
            sslmode="require",
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        logger.info("Initialized Postgres connection pool (1-10 connections).")
    return _pg_pool


def _init_sqlite_conn():
    global _sqlite_conn
    if _sqlite_conn is None:
        _sqlite_conn = sqlite3.connect(str(SQLITE_PATH), check_same_thread=False)
        _sqlite_conn.row_factory = sqlite3.Row
    return _sqlite_conn


@contextmanager
def _cursor(commit=False):
    if USE_POSTGRES:
        pool = _init_pg_pool()
        conn = pool.getconn()
        if conn.closed:
            pool.putconn(conn, close=True)
            conn = pool.getconn()

        cur = None
        broken = False
        try:
            cur = conn.cursor()
            yield cur
            if commit:
                conn.commit()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            broken = True
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            if cur is not None:
                cur.close()
            pool.putconn(conn, close=broken)
    else:
        with _sqlite_lock:
            conn = _init_sqlite_conn()
            cur = None
            try:
                cur = conn.cursor()
                yield cur
                if commit:
                    conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                if cur is not None:
                    cur.close()


def _ph():
    return "%s" if USE_POSTGRES else "?"


def _rows(cur_rows):
    return [dict(r) for r in cur_rows]


def init_db():
    """Creates all tables if they don't exist yet, migrates any legacy CSV
    data in, and seeds default tips/features on first run. Safe to call
    every time the app starts. FAQ seeding is a separate call
    (_seed_faqs_if_empty) made from app.py, since app.py owns the default
    FAQ list."""
    with _cursor(commit=True) as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                reference TEXT PRIMARY KEY,
                timestamp TEXT, name TEXT, phone TEXT, location TEXT,
                issue_type TEXT, description TEXT,
                attachment_mime TEXT, attachment_data TEXT,
                status TEXT, severity TEXT
            )
        """)
        if USE_POSTGRES:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id SERIAL PRIMARY KEY,
                    timestamp TEXT, contact TEXT, categories TEXT
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT, contact TEXT, categories TEXT
                )
            """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS outages (
                id TEXT PRIMARY KEY,
                parish TEXT, message TEXT, start_date TEXT, end_date TEXT, created_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tips (
                id TEXT PRIMARY KEY,
                text TEXT, enabled TEXT, created_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS features (
                id INTEGER PRIMARY KEY, data TEXT
            )
        """)
        # --- NEW TABLES ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS faqs (
                id TEXT PRIMARY KEY,
                category TEXT, question TEXT, answer TEXT,
                enabled TEXT, created_at TEXT, updated_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS unanswered_questions (
                id TEXT PRIMARY KEY,
                question TEXT, timestamp TEXT, category TEXT,
                session_id TEXT, resolved TEXT, staff_answer TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_events (
                id TEXT PRIMARY KEY,
                session_id TEXT, territory TEXT, timestamp TEXT,
                had_error TEXT
            )
        """)

    migrate_legacy_storage()
    _seed_tips_if_empty()
    _seed_features_if_empty()


# ---------------------------------------------------------------------
# Legacy migration — imports old CSV/JSON data into the DB exactly once
# ---------------------------------------------------------------------
def _table_is_empty(table):
    with _cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM {table}")
        count = cur.fetchone()["c"]
    return count == 0


def _insert_legacy_report(row):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"""
            INSERT INTO reports (reference, timestamp, name, phone, location, issue_type,
                                  description, attachment_mime, attachment_data, status, severity)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
        """, (
            row.get("reference"), row.get("timestamp"), row.get("name"), row.get("phone"),
            row.get("location"), row.get("issue_type"), row.get("description", ""),
            row.get("attachment_mime", ""), row.get("attachment_data", ""),
            row.get("status") or STATUS_DEFAULT, row.get("severity") or SEVERITY_DEFAULT,
        ))


def _insert_legacy_outage(row):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"""
            INSERT INTO outages (id, parish, message, start_date, end_date, created_at)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph})
        """, (
            row.get("id") or uuid.uuid4().hex[:8], row.get("parish"), row.get("message"),
            row.get("start_date"), row.get("end_date"),
            row.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ))


def _insert_legacy_notification(row):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"INSERT INTO notifications (timestamp, contact, categories) VALUES ({ph},{ph},{ph})",
                    (row.get("timestamp"), row.get("contact"), row.get("categories")))


def _migrate_legacy_csv(table, csv_name, insert_fn):
    if not _table_is_empty(table):
        return
    path = DATA_DIR / csv_name
    if not path.exists():
        return
    migrated = 0
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                insert_fn(row)
                migrated += 1
            except Exception as e:
                logger.warning("Skipped malformed legacy row in %s: %s", csv_name, e)
    if migrated:
        logger.info("Migrated %d legacy rows from %s into %s.", migrated, csv_name, table)


def _migrate_legacy_features_json():
    if not _table_is_empty("features"):
        return
    path = DATA_DIR / "features.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Could not read legacy features.json: %s", e)
        return
    save_features(data)
    logger.info("Migrated legacy features.json into the features table.")


def migrate_legacy_storage():
    _migrate_legacy_csv("reports", "reports.csv", _insert_legacy_report)
    _migrate_legacy_csv("outages", "outages.csv", _insert_legacy_outage)
    _migrate_legacy_csv("notifications", "notifications.csv", _insert_legacy_notification)
    _migrate_legacy_features_json()


# ---------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------
def new_reference():
    return "NW-" + uuid.uuid4().hex[:7].upper()


def save_report(name, phone, location, issue_type, description,
                 attachment_mime="", attachment_data="", severity=SEVERITY_DEFAULT):
    row = {
        "reference": new_reference(),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "name": name or "Not provided", "phone": phone or "Not provided", "location": location,
        "issue_type": issue_type, "description": description,
        "attachment_mime": attachment_mime or "", "attachment_data": attachment_data or "",
        "status": STATUS_DEFAULT, "severity": severity or SEVERITY_DEFAULT,
    }
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"""
            INSERT INTO reports (reference, timestamp, name, phone, location, issue_type,
                                  description, attachment_mime, attachment_data, status, severity)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
        """, (row["reference"], row["timestamp"], row["name"], row["phone"], row["location"],
              row["issue_type"], row["description"], row["attachment_mime"], row["attachment_data"],
              row["status"], row["severity"]))
    return row


def load_reports():
    with _cursor() as cur:
        cur.execute("SELECT * FROM reports ORDER BY timestamp ASC")
        rows = _rows(cur.fetchall())
    return rows


def update_report_status(reference, new_status):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"UPDATE reports SET status = {ph} WHERE UPPER(reference) = UPPER({ph})",
                    (new_status, reference))
        updated = cur.rowcount > 0
    return updated


def track_report(reference):
    if not reference:
        return None
    ph = _ph()
    with _cursor() as cur:
        cur.execute(f"SELECT * FROM reports WHERE UPPER(reference) = UPPER({ph})", (reference.strip(),))
        row = cur.fetchone()
    return dict(row) if row else None


def delete_report(reference):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"DELETE FROM reports WHERE UPPER(reference) = UPPER({ph})", (reference.strip(),))
        deleted = cur.rowcount > 0
    return deleted


# ---------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------
def save_notification_signup(contact, categories):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"INSERT INTO notifications (timestamp, contact, categories) VALUES ({ph},{ph},{ph})",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), contact, ", ".join(categories)))


def load_notifications():
    with _cursor() as cur:
        cur.execute("SELECT timestamp, contact, categories FROM notifications ORDER BY id ASC")
        rows = _rows(cur.fetchall())
    return rows


# ---------------------------------------------------------------------
# Outages
# ---------------------------------------------------------------------
def save_outage(parish, message, start_date, end_date):
    row = {"id": uuid.uuid4().hex[:8], "parish": parish, "message": message,
           "start_date": start_date, "end_date": end_date,
           "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"""
            INSERT INTO outages (id, parish, message, start_date, end_date, created_at)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph})
        """, (row["id"], row["parish"], row["message"], row["start_date"], row["end_date"], row["created_at"]))
    return row


def load_outages():
    with _cursor() as cur:
        cur.execute("SELECT * FROM outages ORDER BY created_at ASC")
        rows = _rows(cur.fetchall())
    return rows


def delete_outage(outage_id):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"DELETE FROM outages WHERE id = {ph}", (outage_id,))


def get_active_outages_for_parish(parish, today=None):
    if today is None:
        today = datetime.now(GRENADA_TZ).strftime("%Y-%m-%d")
    ph = _ph()
    with _cursor() as cur:
        cur.execute(f"""
            SELECT * FROM outages WHERE parish = {ph} AND start_date <= {ph} AND end_date >= {ph}
        """, (parish, today, today))
        rows = _rows(cur.fetchall())
    return rows


# ---------------------------------------------------------------------
# Water Service Tips
# ---------------------------------------------------------------------
def _tip_out(row):
    return {"id": row["id"], "text": row["text"], "enabled": row.get("enabled") == "1",
            "created_at": row.get("created_at", "")}


def _seed_tips_if_empty():
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM tips")
        count = cur.fetchone()["c"]
    if count:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ph = _ph()
    with _cursor(commit=True) as cur:
        for t in DEFAULT_TIPS:
            cur.execute(f"INSERT INTO tips (id, text, enabled, created_at) VALUES ({ph},{ph},{ph},{ph})",
                        (uuid.uuid4().hex[:8], t, "1", now))


def load_tips():
    with _cursor() as cur:
        cur.execute("SELECT * FROM tips ORDER BY created_at ASC")
        rows = _rows(cur.fetchall())
    return [_tip_out(r) for r in rows]


def save_tip(text):
    row = {"id": uuid.uuid4().hex[:8], "text": text, "enabled": "1",
           "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"INSERT INTO tips (id, text, enabled, created_at) VALUES ({ph},{ph},{ph},{ph})",
                    (row["id"], row["text"], row["enabled"], row["created_at"]))
    return _tip_out(row)


def update_tip(tip_id, text=None, enabled=None):
    ph = _ph()
    with _cursor() as cur:
        cur.execute(f"SELECT * FROM tips WHERE id = {ph}", (tip_id,))
        existing = cur.fetchone()
    if existing is None:
        return None
    existing = dict(existing)
    new_text = text if text is not None else existing["text"]
    new_enabled = ("1" if enabled else "0") if enabled is not None else existing["enabled"]
    with _cursor(commit=True) as cur:
        cur.execute(f"UPDATE tips SET text = {ph}, enabled = {ph} WHERE id = {ph}",
                    (new_text, new_enabled, tip_id))
    return _tip_out({"id": tip_id, "text": new_text, "enabled": new_enabled,
                      "created_at": existing["created_at"]})


def delete_tip(tip_id):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"DELETE FROM tips WHERE id = {ph}", (tip_id,))
        deleted = cur.rowcount > 0
    return deleted


# ---------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------
def _seed_features_if_empty():
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM features")
        count = cur.fetchone()["c"]
    if not count:
        ph = _ph()
        data = dict(DEFAULT_FEATURES)
        data["maintenance_message"] = DEFAULT_MAINTENANCE_MESSAGE
        with _cursor(commit=True) as cur:
            cur.execute(f"INSERT INTO features (id, data) VALUES (1, {ph})", (json.dumps(data),))


def load_features():
    with _cursor() as cur:
        cur.execute("SELECT data FROM features WHERE id = 1")
        row = cur.fetchone()
    saved = {}
    if row:
        try:
            saved = json.loads(row["data"])
        except Exception:
            saved = {}
    merged = dict(DEFAULT_FEATURES)
    merged.update({k: bool(v) for k, v in saved.items() if k in DEFAULT_FEATURES})
    merged["maintenance_message"] = saved.get("maintenance_message") or DEFAULT_MAINTENANCE_MESSAGE
    return merged


def save_features(updates):
    current = load_features()
    for k, v in (updates or {}).items():
        if k in DEFAULT_FEATURES:
            current[k] = bool(v)
        elif k == "maintenance_message":
            text = (v or "").strip()
            current["maintenance_message"] = text or DEFAULT_MAINTENANCE_MESSAGE
    ph = _ph()
    with _cursor(commit=True) as cur:
        if USE_POSTGRES:
            cur.execute(f"""
                INSERT INTO features (id, data) VALUES (1, {ph})
                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
            """, (json.dumps(current),))
        else:
            cur.execute(f"INSERT OR REPLACE INTO features (id, data) VALUES (1, {ph})", (json.dumps(current),))
    return current


# =======================================================================
# NEW: Knowledge base (FAQs)
# =======================================================================
def _faq_out(row):
    return {
        "id": row["id"], "category": row["category"], "q": row["question"],
        "a": row["answer"], "enabled": row.get("enabled", "1") == "1",
        "created_at": row.get("created_at", ""), "updated_at": row.get("updated_at", ""),
    }


def _seed_faqs_if_empty(default_faqs):
    """default_faqs: list of {"category","q","a"} dicts — app.py passes its
    existing FAQS constant in here once, at startup. No-ops after the first
    successful run (staff edits from then on live in the table)."""
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM faqs")
        count = cur.fetchone()["c"]
    if count:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ph = _ph()
    with _cursor(commit=True) as cur:
        for f in default_faqs:
            cur.execute(
                f"INSERT INTO faqs (id, category, question, answer, enabled, created_at, updated_at) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (uuid.uuid4().hex[:8], f["category"], f["q"], f["a"], "1", now, now),
            )


def load_faqs(include_disabled=True):
    with _cursor() as cur:
        if include_disabled:
            cur.execute("SELECT * FROM faqs ORDER BY category ASC, created_at ASC")
        else:
            ph = _ph()
            cur.execute(f"SELECT * FROM faqs WHERE enabled = {ph} ORDER BY category ASC, created_at ASC", ("1",))
        rows = _rows(cur.fetchall())
    return [_faq_out(r) for r in rows]


def save_faq(category, question, answer):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = {"id": uuid.uuid4().hex[:8], "category": category, "question": question,
           "answer": answer, "enabled": "1", "created_at": now, "updated_at": now}
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(
            f"INSERT INTO faqs (id, category, question, answer, enabled, created_at, updated_at) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            (row["id"], row["category"], row["question"], row["answer"], row["enabled"],
             row["created_at"], row["updated_at"]),
        )
    return _faq_out(row)


def update_faq(faq_id, category=None, question=None, answer=None, enabled=None):
    ph = _ph()
    with _cursor() as cur:
        cur.execute(f"SELECT * FROM faqs WHERE id = {ph}", (faq_id,))
        existing = cur.fetchone()
    if existing is None:
        return None
    existing = dict(existing)
    new_vals = {
        "category": category if category is not None else existing["category"],
        "question": question if question is not None else existing["question"],
        "answer": answer if answer is not None else existing["answer"],
        "enabled": ("1" if enabled else "0") if enabled is not None else existing["enabled"],
    }
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE faqs SET category={ph}, question={ph}, answer={ph}, enabled={ph}, updated_at={ph} WHERE id={ph}",
            (new_vals["category"], new_vals["question"], new_vals["answer"], new_vals["enabled"], now, faq_id),
        )
    return _faq_out({**existing, **new_vals, "id": faq_id, "updated_at": now})


def delete_faq(faq_id):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"DELETE FROM faqs WHERE id = {ph}", (faq_id,))
        return cur.rowcount > 0


# =======================================================================
# NEW: Unanswered questions
# =======================================================================
def _unanswered_out(row):
    return {
        "id": row["id"], "question": row["question"], "timestamp": row["timestamp"],
        "category": row.get("category") or "", "session_id": row.get("session_id") or "",
        "resolved": row.get("resolved") == "1", "staff_answer": row.get("staff_answer") or "",
    }


def log_unanswered_question(question, session_id=""):
    ph = _ph()
    row = {
        "id": uuid.uuid4().hex[:8], "question": question,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "category": "", "session_id": session_id, "resolved": "0", "staff_answer": "",
    }
    with _cursor(commit=True) as cur:
        cur.execute(
            f"INSERT INTO unanswered_questions (id, question, timestamp, category, session_id, resolved, staff_answer) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            (row["id"], row["question"], row["timestamp"], row["category"],
             row["session_id"], row["resolved"], row["staff_answer"]),
        )
    return row


def load_unanswered_questions(include_resolved=False):
    ph = _ph()
    with _cursor() as cur:
        if include_resolved:
            cur.execute("SELECT * FROM unanswered_questions ORDER BY timestamp DESC")
        else:
            cur.execute(f"SELECT * FROM unanswered_questions WHERE resolved = {ph} ORDER BY timestamp DESC", ("0",))
        rows = _rows(cur.fetchall())
    return [_unanswered_out(r) for r in rows]


def resolve_unanswered_question(q_id, staff_answer=""):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE unanswered_questions SET resolved={ph}, staff_answer={ph} WHERE id={ph}",
            ("1", staff_answer, q_id),
        )
        return cur.rowcount > 0


def delete_unanswered_question(q_id):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"DELETE FROM unanswered_questions WHERE id = {ph}", (q_id,))
        return cur.rowcount > 0


# =======================================================================
# NEW: Chat events (conversation stats for the staff Overview panel)
# =======================================================================
def log_chat_event(session_id, territory, had_error=False):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(
            f"INSERT INTO chat_events (id, session_id, territory, timestamp, had_error) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph})",
            (uuid.uuid4().hex[:8], session_id, territory,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "1" if had_error else "0"),
        )


def get_chat_stats_today():
    today = datetime.now(GRENADA_TZ).strftime("%Y-%m-%d")
    ph = _ph()
    with _cursor() as cur:
        cur.execute(f"SELECT * FROM chat_events WHERE timestamp LIKE {ph}", (f"{today}%",))
        rows = _rows(cur.fetchall())
    total = len(rows)
    errors = sum(1 for r in rows if r.get("had_error") == "1")
    distinct_sessions = len(set(r["session_id"] for r in rows))
    return {
        "messages_today": total,
        "conversations_today": distinct_sessions,
        "errors_today": errors,
        "questions_answered_today": total - errors,
    }
