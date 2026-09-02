"""
db.py — persistent storage for AquaAssist.

Backed by SQLite by default or Postgres if DATABASE_URL is set.

THIS VERSION adds (on top of the previous reports/notifications/outages/
tips/features/faqs/unanswered_questions/chat_events/chat_messages/
handoff_requests/paused_sessions tables):

  - `staff_accounts`   — individual named staff accounts with a hashed
                          password, a free-text role label, a JSON list of
                          granular permission keys, an avatar, and a status
                          (Active/Disabled). Replaces the old shared
                          STAFF_PASSCODE model entirely.
  - `staff_sessions`   — bearer tokens issued on login (X-Staff-Token
                          header), so "passcode in every request" becomes
                          "log in once, use a token".
  - `audit_log`        — one row per administrative action (account
                          created/disabled/deleted, permission changed,
                          password reset, content published, report status
                          changed, etc.) for accountability.
  - `report_notes`     — internal, staff-only notes attached to a report
                          (separate from the customer-facing description),
                          backing the "Add Internal Notes" permission.

See app.py for the permission-checking decorators and route wiring.
"""

import csv
import json
import logging
import os
import secrets
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import generate_password_hash, check_password_hash

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
DEFAULT_CHATBOT_NAME = "AquaAssist"
DEFAULT_TIPS = [
    "Check for hidden leaks: turn off every tap in your home, then watch your meter dial. If it's still moving, water is escaping somewhere.",
    "Conserve water during dry months by fixing dripping taps promptly — a single slow drip can waste over 15 gallons a day.",
    "Protect your water storage tank by keeping the lid securely closed to prevent debris, insects, and contamination.",
    "During a scheduled interruption, store enough water for drinking, cooking, and sanitation, and avoid running your pump dry.",
    "Spot a leak on the street, a burst main, or a damaged hydrant? Report it to NAWASA right away via AquaAssist or call (473) 440-2155.",
    "Regularly check your faucets and toilets for silent leaks — a toilet that keeps running after flushing can waste hundreds of gallons a month.",
]

# =======================================================================
# NEW: Granular staff permissions — the single source of truth for what
# permission keys exist, their display labels, and which category they
# render under in the Staff Accounts permissions panel. app.py imports
# PERMISSION_DEFS / ALL_PERMISSION_KEYS from here so the backend decorator
# checks and the /api/staff/permission-defs response (used by the frontend
# to render checkboxes) never drift apart.
# =======================================================================
PERMISSION_DEFS = [
    # key, category, label
    ("view_website_management", "Website", "View Website Management"),
    ("edit_website_content", "Website", "Edit Website Content"),
    ("create_edit_news", "Website", "Create/Edit News"),
    ("manage_service_alerts", "Website", "Manage Service Alerts"),
    ("manage_water_tips", "Website", "Manage Water Service Tips"),
    ("manage_events", "Website", "Manage Events"),
    ("publish_content", "Website", "Publish Content"),
    ("view_website_analytics", "Website", "View Website Analytics"),

    ("view_aquaassist_dashboard", "AquaAssist", "View AquaAssist Dashboard"),
    ("access_live_chat", "AquaAssist", "Access Live Chat (view & reply)"),
    ("manage_faqs", "AquaAssist", "Manage FAQs"),
    ("manage_knowledge_base", "AquaAssist", "Manage Knowledge Base"),
    ("review_unanswered_questions", "AquaAssist", "Review Unanswered Questions"),
    ("manage_aquaassist_announcements", "AquaAssist", "Manage AquaAssist Announcements"),
    ("manage_quick_actions", "AquaAssist", "Manage Quick Actions"),
    ("manage_chatbot_settings", "AquaAssist", "Manage Chatbot Settings"),
    ("view_chat_analytics", "AquaAssist", "View Chat Analytics"),
    ("manage_voice_settings", "AquaAssist", "Manage Voice Settings"),
    ("sync_website_content", "AquaAssist", "Sync Website Content (nawasa.gd)"),

    ("view_reports", "Reports & Operations", "View Reports"),
    ("view_reporting_map", "Reports & Operations", "View Reporting Map"),
    ("create_reports", "Reports & Operations", "Create Reports"),
    ("edit_reports", "Reports & Operations", "Edit Reports"),
    ("assign_reports", "Reports & Operations", "Assign Reports"),
    ("change_report_status", "Reports & Operations", "Change Report Status"),
    ("add_internal_notes", "Reports & Operations", "Add Internal Notes"),
    ("view_report_photos", "Reports & Operations", "View Report Photos"),
    ("view_report_statistics", "Reports & Operations", "View Report Statistics"),
    ("manage_subscribers", "Reports & Operations", "Manage Subscribers"),

    ("manage_staff_accounts", "Administration", "Manage Staff Accounts"),
    ("create_accounts", "Administration", "Create Accounts"),
    ("edit_accounts", "Administration", "Edit Accounts"),
    ("disable_accounts", "Administration", "Disable Accounts"),
    ("delete_accounts", "Administration", "Delete Accounts"),
    ("manage_permissions", "Administration", "Manage Permissions"),
    ("system_settings", "Administration", "System Settings"),
    ("api_integration_settings", "Administration", "API/Integration Settings"),
]
ALL_PERMISSION_KEYS = [p[0] for p in PERMISSION_DEFS]
_VALID_PERMISSION_SET = set(ALL_PERMISSION_KEYS)

SUPER_ADMIN_USERNAME = "AquaVission"
DEFAULT_SUPER_ADMIN_PASSWORD = os.environ.get("AQUAVISSION_PASSWORD", "Admin123")


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
    every time the app starts. FAQ seeding and the Super Administrator
    seed are separate calls made from app.py."""
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
        if USE_POSTGRES:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id SERIAL PRIMARY KEY,
                    session_id TEXT, territory TEXT, role TEXT,
                    content TEXT, timestamp TEXT
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT, territory TEXT, role TEXT,
                    content TEXT, timestamp TEXT
                )
            """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS handoff_requests (
                id TEXT PRIMARY KEY,
                session_id TEXT, territory TEXT, reason TEXT,
                timestamp TEXT, resolved TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS paused_sessions (
                session_id TEXT PRIMARY KEY,
                paused_at TEXT
            )
        """)

        # --- NEW TABLES: accounts, sessions, audit log, report notes ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS staff_accounts (
                id TEXT PRIMARY KEY,
                full_name TEXT, username TEXT, username_lower TEXT,
                password_hash TEXT, role TEXT, permissions TEXT,
                avatar TEXT, status TEXT, is_super_admin TEXT,
                created_at TEXT, updated_at TEXT, created_by TEXT
            )
        """)
        if USE_POSTGRES:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id SERIAL PRIMARY KEY,
                    timestamp TEXT, staff_user TEXT, action TEXT,
                    item TEXT, details TEXT
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT, staff_user TEXT, action TEXT,
                    item TEXT, details TEXT
                )
            """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS staff_sessions (
                token TEXT PRIMARY KEY,
                account_id TEXT, created_at TEXT, last_seen_at TEXT
            )
        """)
        if USE_POSTGRES:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS report_notes (
                    id SERIAL PRIMARY KEY,
                    reference TEXT, author TEXT, note TEXT, timestamp TEXT
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS report_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT, author TEXT, note TEXT, timestamp TEXT
                )
            """)
        # `website_pages` holds imported content from nawasa.gd — one row
        # per source page, refreshed by a periodic/on-demand sync (see
        # website_sync.py) rather than fetched live during a customer
        # conversation. This gets merged into the same knowledge base the
        # chatbot already searches (see app.py's _build_kb_entries()), so
        # the bot never depends on nawasa.gd being reachable at chat time.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS website_pages (
                url TEXT PRIMARY KEY,
                title TEXT, content TEXT, fetched_at TEXT, status TEXT, error TEXT
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


# =======================================================================
# NEW: Report notes (internal, staff-only — backs "Add Internal Notes")
# =======================================================================
def add_report_note(reference, author, note):
    ph = _ph()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _cursor(commit=True) as cur:
        cur.execute(
            f"INSERT INTO report_notes (reference, author, note, timestamp) VALUES ({ph},{ph},{ph},{ph})",
            (reference, author, note, now),
        )
    return {"reference": reference, "author": author, "note": note, "timestamp": now}


def load_report_notes(reference):
    ph = _ph()
    with _cursor() as cur:
        cur.execute(f"SELECT * FROM report_notes WHERE reference = {ph} ORDER BY id ASC", (reference,))
        rows = _rows(cur.fetchall())
    return rows


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
        cur.execute("SELECT id, timestamp, contact, categories FROM notifications ORDER BY id ASC")
        rows = _rows(cur.fetchall())
    return rows


def delete_notification(notification_id):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"DELETE FROM notifications WHERE id = {ph}", (notification_id,))
        return cur.rowcount > 0


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
            SELECT * FROM outages
            WHERE LOWER(TRIM(parish)) = LOWER(TRIM({ph})) AND start_date <= {ph} AND end_date >= {ph}
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
    merged["chatbot_name"] = (saved.get("chatbot_name") or "").strip() or DEFAULT_CHATBOT_NAME
    return merged


def save_features(updates):
    current = load_features()
    for k, v in (updates or {}).items():
        if k in DEFAULT_FEATURES:
            current[k] = bool(v)
        elif k == "maintenance_message":
            text = (v or "").strip()
            current["maintenance_message"] = text or DEFAULT_MAINTENANCE_MESSAGE
        # NOTE: chatbot_name is intentionally NOT settable through this
        # generic function — it's changed only via set_chatbot_name()
        # below, which app.py gates to the AquaVission Super Administrator
        # account specifically, per the "AquaVission only" requirement.
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


def set_chatbot_name(new_name):
    """Dedicated setter so the AquaVission-only restriction lives entirely
    in app.py's route (which checks is_super_admin before ever calling
    this), rather than being bypassable through the general-purpose
    save_features()."""
    current = load_features()
    current["chatbot_name"] = (new_name or "").strip() or DEFAULT_CHATBOT_NAME
    ph = _ph()
    with _cursor(commit=True) as cur:
        if USE_POSTGRES:
            cur.execute(f"""
                INSERT INTO features (id, data) VALUES (1, {ph})
                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
            """, (json.dumps(current),))
        else:
            cur.execute(f"INSERT OR REPLACE INTO features (id, data) VALUES (1, {ph})", (json.dumps(current),))
    return current["chatbot_name"]


# =======================================================================
# Knowledge base (FAQs)
# =======================================================================
def _faq_out(row):
    return {
        "id": row["id"], "category": row["category"], "q": row["question"],
        "a": row["answer"], "enabled": row.get("enabled", "1") == "1",
        "created_at": row.get("created_at", ""), "updated_at": row.get("updated_at", ""),
    }


def _seed_faqs_if_empty(default_faqs):
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
# NEW: Imported nawasa.gd website content
#
# One row per source URL. `status` is "ok" or "error" (a page that failed
# to fetch keeps its last-known-good `content` rather than being wiped —
# a transient site outage during a sync shouldn't erase what the bot
# already knew). See website_sync.py for the fetch/parse logic.
# =======================================================================
def save_website_page(url, title, content, status="ok", error=""):
    ph = _ph()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _cursor() as cur:
        cur.execute(f"SELECT url FROM website_pages WHERE url = {ph}", (url,))
        existing = cur.fetchone()
    with _cursor(commit=True) as cur:
        if existing:
            if status == "ok":
                cur.execute(
                    f"UPDATE website_pages SET title={ph}, content={ph}, fetched_at={ph}, status={ph}, error={ph} WHERE url={ph}",
                    (title, content, now, status, error, url),
                )
            else:
                # Fetch failed — keep the last-good title/content, just
                # record that this attempt failed and when.
                cur.execute(
                    f"UPDATE website_pages SET fetched_at={ph}, status={ph}, error={ph} WHERE url={ph}",
                    (now, status, error, url),
                )
        else:
            cur.execute(
                f"INSERT INTO website_pages (url, title, content, fetched_at, status, error) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
                (url, title, content, now, status, error),
            )


def load_website_pages():
    with _cursor() as cur:
        cur.execute("SELECT * FROM website_pages ORDER BY url ASC")
        rows = _rows(cur.fetchall())
    return rows


# =======================================================================
# Unanswered questions
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
# Chat events (conversation stats for the staff Overview panel)
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


# =======================================================================
# Chat transcripts (Live Chat monitor in the Staff Portal)
# =======================================================================
def log_chat_message(session_id, territory, role, content):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(
            f"INSERT INTO chat_messages (session_id, territory, role, content, timestamp) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph})",
            (session_id, territory, role, content, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def load_recent_sessions(limit=50):
    with _cursor() as cur:
        cur.execute("SELECT * FROM chat_messages ORDER BY id DESC LIMIT 2000")
        rows = _rows(cur.fetchall())
    paused_ids = load_paused_session_ids()
    seen = {}
    for r in rows:
        sid = r["session_id"]
        if sid in seen:
            continue
        seen[sid] = {
            "session_id": sid,
            "territory": r["territory"],
            "last_message": r["content"][:120],
            "last_role": r["role"],
            "last_timestamp": r["timestamp"],
            "paused": sid in paused_ids,
        }
        if len(seen) >= limit:
            break
    return list(seen.values())


def load_session_messages(session_id):
    ph = _ph()
    with _cursor() as cur:
        cur.execute(f"SELECT * FROM chat_messages WHERE session_id = {ph} ORDER BY id ASC", (session_id,))
        rows = _rows(cur.fetchall())
    return rows


def load_new_staff_messages(session_id, after_id=0):
    ph = _ph()
    with _cursor() as cur:
        cur.execute(
            f"SELECT * FROM chat_messages WHERE session_id = {ph} AND role = {ph} AND id > {ph} ORDER BY id ASC",
            (session_id, "staff", after_id),
        )
        rows = _rows(cur.fetchall())
    return rows


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


# =======================================================================
# Live-agent handoff requests
# =======================================================================
def _handoff_out(row):
    return {
        "id": row["id"], "session_id": row["session_id"], "territory": row.get("territory") or "",
        "reason": row.get("reason") or "", "timestamp": row["timestamp"],
        "resolved": row.get("resolved") == "1",
    }


def create_handoff_request(session_id, territory, reason):
    ph = _ph()
    with _cursor() as cur:
        cur.execute(f"SELECT id FROM handoff_requests WHERE session_id={ph} AND resolved={ph}",
                    (session_id, "0"))
        existing = cur.fetchone()
    if existing:
        return None
    row = {
        "id": uuid.uuid4().hex[:8], "session_id": session_id, "territory": territory or "",
        "reason": (reason or "").strip() or "Customer needs a live representative.",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "resolved": "0",
    }
    with _cursor(commit=True) as cur:
        cur.execute(
            f"INSERT INTO handoff_requests (id, session_id, territory, reason, timestamp, resolved) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
            (row["id"], row["session_id"], row["territory"], row["reason"], row["timestamp"], row["resolved"]),
        )
    return _handoff_out(row)


def load_open_handoffs():
    ph = _ph()
    with _cursor() as cur:
        cur.execute(f"SELECT * FROM handoff_requests WHERE resolved={ph} ORDER BY timestamp DESC", ("0",))
        rows = _rows(cur.fetchall())
    return [_handoff_out(r) for r in rows]


def resolve_handoff_for_session(session_id):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE handoff_requests SET resolved={ph} WHERE session_id={ph} AND resolved={ph}",
            ("1", session_id, "0"),
        )
        return cur.rowcount > 0


# =======================================================================
# Pause/resume
# =======================================================================
def pause_session(session_id):
    ph = _ph()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _cursor(commit=True) as cur:
        if USE_POSTGRES:
            cur.execute(f"""
                INSERT INTO paused_sessions (session_id, paused_at) VALUES ({ph}, {ph})
                ON CONFLICT (session_id) DO UPDATE SET paused_at = EXCLUDED.paused_at
            """, (session_id, now))
        else:
            cur.execute(
                f"INSERT OR REPLACE INTO paused_sessions (session_id, paused_at) VALUES ({ph}, {ph})",
                (session_id, now),
            )


def resume_session(session_id):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"DELETE FROM paused_sessions WHERE session_id = {ph}", (session_id,))


def is_session_paused(session_id):
    ph = _ph()
    with _cursor() as cur:
        cur.execute(f"SELECT session_id FROM paused_sessions WHERE session_id = {ph}", (session_id,))
        return cur.fetchone() is not None


def load_paused_session_ids():
    with _cursor() as cur:
        cur.execute("SELECT session_id FROM paused_sessions")
        rows = _rows(cur.fetchall())
    return {r["session_id"] for r in rows}


# =======================================================================
# Deleting conversations
# =======================================================================
def delete_session_messages(session_id):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"DELETE FROM chat_messages WHERE session_id = {ph}", (session_id,))
        deleted = cur.rowcount > 0
        cur.execute(f"DELETE FROM handoff_requests WHERE session_id = {ph}", (session_id,))
        cur.execute(f"DELETE FROM paused_sessions WHERE session_id = {ph}", (session_id,))
        cur.execute(f"DELETE FROM chat_events WHERE session_id = {ph}", (session_id,))
    return deleted


def delete_all_sessions():
    with _cursor(commit=True) as cur:
        cur.execute("DELETE FROM chat_messages")
        cur.execute("DELETE FROM handoff_requests")
        cur.execute("DELETE FROM paused_sessions")
        cur.execute("DELETE FROM chat_events")


# =======================================================================
# NEW: Staff accounts, sessions (tokens), permissions, audit log
# =======================================================================
def _clean_permissions(permissions):
    """Keeps only known permission keys, de-duplicated, in canonical order."""
    if not permissions:
        return []
    given = set(permissions) & _VALID_PERMISSION_SET
    return [k for k in ALL_PERMISSION_KEYS if k in given]


def _account_out(row, include_hash=False):
    try:
        perms = json.loads(row.get("permissions") or "[]")
    except Exception:
        perms = []
    out = {
        "id": row["id"], "full_name": row["full_name"], "username": row["username"],
        "role": row.get("role") or "", "permissions": perms,
        "avatar": row.get("avatar") or "", "status": row.get("status") or "Active",
        "is_super_admin": row.get("is_super_admin") == "1",
        "created_at": row.get("created_at", ""), "updated_at": row.get("updated_at", ""),
        "created_by": row.get("created_by") or "",
    }
    if include_hash:
        out["password_hash"] = row.get("password_hash")
    return out


def account_has_permission(account, key):
    """account is the dict shape returned by _account_out(). Super Admins
    always have every permission, regardless of what's stored — this is
    what makes "Super Administrator must always have full access" true
    even if a permission is added to PERMISSION_DEFS later."""
    if account.get("is_super_admin"):
        return True
    return key in (account.get("permissions") or [])


def _seed_super_admin_if_missing():
    """Creates the AquaVission Super Administrator account on first run,
    with full access to every current and future permission key. No-op if
    an account with this username already exists (so this is safe to call
    on every startup)."""
    ph = _ph()
    with _cursor() as cur:
        cur.execute(f"SELECT id FROM staff_accounts WHERE username_lower = {ph}",
                    (SUPER_ADMIN_USERNAME.lower(),))
        existing = cur.fetchone()
    if existing:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "id": uuid.uuid4().hex[:10],
        "full_name": "AquaVission", "username": SUPER_ADMIN_USERNAME,
        "username_lower": SUPER_ADMIN_USERNAME.lower(),
        "password_hash": generate_password_hash(DEFAULT_SUPER_ADMIN_PASSWORD),
        "role": "Super Administrator",
        "permissions": json.dumps(ALL_PERMISSION_KEYS),
        "avatar": "👑", "status": "Active", "is_super_admin": "1",
        "created_at": now, "updated_at": now, "created_by": "system",
    }
    with _cursor(commit=True) as cur:
        cur.execute(
            f"INSERT INTO staff_accounts (id, full_name, username, username_lower, password_hash, role, "
            f"permissions, avatar, status, is_super_admin, created_at, updated_at, created_by) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            (row["id"], row["full_name"], row["username"], row["username_lower"], row["password_hash"],
             row["role"], row["permissions"], row["avatar"], row["status"], row["is_super_admin"],
             row["created_at"], row["updated_at"], row["created_by"]),
        )
    logger.warning(
        "Seeded the AquaVission Super Administrator account with the configured default "
        "password. Log in and change it immediately via Staff Accounts \u2192 Change Password "
        "(or set AQUAVISSION_PASSWORD before first startup)."
    )


def get_account_by_username(username):
    ph = _ph()
    with _cursor() as cur:
        cur.execute(f"SELECT * FROM staff_accounts WHERE username_lower = {ph}", ((username or "").lower(),))
        row = cur.fetchone()
    return dict(row) if row else None


def get_account_by_id(account_id):
    ph = _ph()
    with _cursor() as cur:
        cur.execute(f"SELECT * FROM staff_accounts WHERE id = {ph}", (account_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def load_staff_accounts():
    with _cursor() as cur:
        cur.execute("SELECT * FROM staff_accounts ORDER BY (is_super_admin = '1') DESC, created_at ASC")
        rows = _rows(cur.fetchall())
    return [_account_out(r) for r in rows]


def create_staff_account(full_name, username, password, role, permissions, avatar="", created_by=""):
    if get_account_by_username(username) is not None:
        return None, "That username is already taken."
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "id": uuid.uuid4().hex[:10], "full_name": full_name, "username": username,
        "username_lower": username.lower(), "password_hash": generate_password_hash(password),
        "role": role or "Staff", "permissions": json.dumps(_clean_permissions(permissions)),
        "avatar": avatar or "💧", "status": "Active", "is_super_admin": "0",
        "created_at": now, "updated_at": now, "created_by": created_by,
    }
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(
            f"INSERT INTO staff_accounts (id, full_name, username, username_lower, password_hash, role, "
            f"permissions, avatar, status, is_super_admin, created_at, updated_at, created_by) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            (row["id"], row["full_name"], row["username"], row["username_lower"], row["password_hash"],
             row["role"], row["permissions"], row["avatar"], row["status"], row["is_super_admin"],
             row["created_at"], row["updated_at"], row["created_by"]),
        )
    return _account_out(row), None


def update_staff_account(account_id, full_name=None, username=None, role=None, avatar=None):
    """Edits identity fields only — not permissions or status, which have
    their own dedicated (and separately permissioned) update functions
    below so each can be audit-logged with its own action label."""
    existing = get_account_by_id(account_id)
    if existing is None:
        return None, "Account not found."
    if username and username.lower() != existing["username_lower"]:
        clash = get_account_by_username(username)
        if clash is not None and clash["id"] != account_id:
            return None, "That username is already taken."
    new_vals = {
        "full_name": full_name if full_name is not None else existing["full_name"],
        "username": username if username is not None else existing["username"],
        "role": role if role is not None else existing["role"],
        "avatar": avatar if avatar is not None else existing["avatar"],
    }
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE staff_accounts SET full_name={ph}, username={ph}, username_lower={ph}, role={ph}, "
            f"avatar={ph}, updated_at={ph} WHERE id={ph}",
            (new_vals["full_name"], new_vals["username"], new_vals["username"].lower(),
             new_vals["role"], new_vals["avatar"], now, account_id),
        )
    return get_account_by_id(account_id) and _account_out(get_account_by_id(account_id)), None


def update_staff_permissions(account_id, permissions):
    ph = _ph()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE staff_accounts SET permissions={ph}, updated_at={ph} WHERE id={ph}",
            (json.dumps(_clean_permissions(permissions)), now, account_id),
        )
        return cur.rowcount > 0


def set_staff_status(account_id, status):
    """status is 'Active' or 'Disabled'. A disabled account's historical
    actions/reports are untouched — only future login/session validity is
    affected (see get_account_for_token, which rejects disabled accounts)."""
    ph = _ph()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE staff_accounts SET status={ph}, updated_at={ph} WHERE id={ph}",
            (status, now, account_id),
        )
        return cur.rowcount > 0


def set_staff_password(account_id, new_password):
    ph = _ph()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE staff_accounts SET password_hash={ph}, updated_at={ph} WHERE id={ph}",
            (generate_password_hash(new_password), now, account_id),
        )
        return cur.rowcount > 0


def delete_staff_account(account_id):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"DELETE FROM staff_accounts WHERE id = {ph}", (account_id,))
        deleted = cur.rowcount > 0
        cur.execute(f"DELETE FROM staff_sessions WHERE account_id = {ph}", (account_id,))
    return deleted


def verify_login(username, password):
    """Returns the account dict (with password_hash) on success, or None
    on a bad username/password/disabled account. Callers must check
    status themselves if they need a specific error message."""
    row = get_account_by_username(username)
    if row is None:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    return row


# ---------------------------------------------------------------------
# Sessions (bearer tokens issued on login)
# ---------------------------------------------------------------------
def create_session(account_id):
    token = secrets.token_urlsafe(32)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(
            f"INSERT INTO staff_sessions (token, account_id, created_at, last_seen_at) VALUES ({ph},{ph},{ph},{ph})",
            (token, account_id, now, now),
        )
    return token


def get_account_for_token(token):
    """Returns the account dict (public shape, no password_hash) for a
    valid session token belonging to an Active account, or None. Also
    touches last_seen_at, best-effort."""
    if not token:
        return None
    ph = _ph()
    with _cursor() as cur:
        cur.execute(f"SELECT account_id FROM staff_sessions WHERE token = {ph}", (token,))
        srow = cur.fetchone()
    if srow is None:
        return None
    account = get_account_by_id(srow["account_id"])
    if account is None or account.get("status") != "Active":
        return None
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _cursor(commit=True) as cur:
        cur.execute(f"UPDATE staff_sessions SET last_seen_at = {ph} WHERE token = {ph}", (now, token))
    return _account_out(account)


def delete_session(token):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"DELETE FROM staff_sessions WHERE token = {ph}", (token,))


def delete_sessions_for_account(account_id):
    """Called when an account is disabled or deleted, so any tokens it
    already issued stop working immediately rather than staying valid
    until they happen to be checked against a disabled account."""
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(f"DELETE FROM staff_sessions WHERE account_id = {ph}", (account_id,))


# ---------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------
def log_audit(staff_user, action, item="", details=""):
    ph = _ph()
    with _cursor(commit=True) as cur:
        cur.execute(
            f"INSERT INTO audit_log (timestamp, staff_user, action, item, details) VALUES ({ph},{ph},{ph},{ph},{ph})",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), staff_user, action, item, details),
        )


def load_audit_log(limit=300):
    with _cursor() as cur:
        cur.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT %d" % int(limit))
        rows = _rows(cur.fetchall())
    return rows
