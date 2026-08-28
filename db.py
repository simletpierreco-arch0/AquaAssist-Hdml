"""
db.py — persistent storage for AquaAssist.

Replaces the old local-file storage (reports.csv, notifications.csv,
outages.csv, tips.csv, features.json) with a real database, because local
files on most hosts (Render free tier included) live on an EPHEMERAL disk
that gets wiped on every redeploy (and often on every restart / idle
spin-down too) — that's the actual cause of reports, subscribers, and
tracking "disappearing" or "not working".

BACKEND — controlled by the DATABASE_URL environment variable:

  - DATABASE_URL set  -> uses that Postgres database. THIS is what survives
    redeploys. Recommended: a Neon (neon.tech) free Postgres project — it
    does not expire, unlike Render's own free Postgres tier (auto-deleted
    30 days after creation). Any Postgres works here (Supabase, Railway,
    Render's own paid Postgres, etc.) — this file only needs a standard
    connection string. Requires `psycopg2-binary` in requirements.txt
    (only imported when DATABASE_URL is actually set).

  - DATABASE_URL not set -> falls back to a single SQLite file at
    DATA_DIR/aquaassist.db. No new dependencies needed. Still an upgrade
    over the old CSVs (atomic writes, no partial-write corruption, no
    cross-instance inconsistency) — but if your host's disk is ephemeral,
    this file is still wiped on redeploy, same as the CSVs were. Use this
    only for local dev/testing, or if you know your host gives you a
    persistent disk.

MIGRATION: on first startup against a fresh/empty database, this module
also checks DATA_DIR for the old reports.csv / outages.csv /
notifications.csv files and imports their rows in — see
migrate_legacy_storage() below. This only ever runs once per table (it
no-ops the moment that table has any rows), so it's safe to leave this
code in permanently and it will never duplicate or re-import data on
later restarts.

PERFORMANCE NOTE (fixed): every function used to open a brand-new database
connection and close it again on every single call. Against Postgres in
particular (SSL handshake + auth round trip every time) that adds roughly
100-400ms of pure connection overhead on top of the actual query — and a
single /api/chat turn can easily trigger 2-4 of these calls (load_features
up front, plus whichever agent tool ends up running), so this was adding
up to a very noticeable chunk of AquaAssist's total reply time. Connection
handling now goes through a small pool (Postgres) / a single shared
connection (SQLite) that lives for the life of the process — see
_cursor() below — instead of reconnecting from scratch every time.

RESILIENCE NOTE: hosts like Neon suspend their Postgres compute after a
few minutes of inactivity, and it wakes back up on the next query — but a
connection that was sitting idle in the pool when that suspend happened
can come back from pool.getconn() looking fine while actually being dead
underneath. Previously, a connection that broke mid-request (or was
already dead when handed out) got returned to the pool anyway, so every
future request would keep drawing that same broken connection and fail
forever until the process was manually restarted — a single blip could
permanently take the whole app down. _cursor() now (1) discards a
connection that's already marked closed before using it, and (2) discards
(rather than returns) any connection that raises a connection-level error
mid-request, so the pool opens a fresh one next time instead of handing
out the same corpse. The request that hit the blip still fails and
surfaces an error to its caller (app.py's /api/chat already catches this
and shows the customer a friendly retry message) — but the app recovers
on the very next request instead of staying down.

Every function here keeps the exact same name, signature, and return shape
as the old CSV-based versions that used to live in app.py, so nothing else
in the app needs to change.
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

# Grenada runs a fixed UTC-4 offset year-round (no daylight saving). Used
# below so "today" for outage matching always means Grenada's today, not
# whatever timezone the server happens to be running in (most hosts run
# UTC) — matches the same fix already applied on the frontend and to
# business-hours logic in app.py.
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
    """Yields a DB cursor from the shared pool (Postgres) or the shared
    connection (SQLite). Commits on the way out if commit=True, rolls back
    on any exception, and always returns the connection cleanly — for
    Postgres it goes back to the pool rather than being closed outright
    (unless it's actually broken — see below); for SQLite access is
    serialized with a lock, since SQLite only safely supports one writer
    at a time."""
    if USE_POSTGRES:
        pool = _init_pg_pool()
        conn = pool.getconn()

        # A connection that's already closed under us (e.g. Neon suspended
        # its compute while this connection sat idle in the pool) would
        # otherwise hand back a cursor that's guaranteed to fail. Discard
        # it and grab a fresh one instead of finding out via a failed
        # query.
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
            # The connection itself died mid-request (network blip, a
            # Neon suspend/resume caught in the middle of a query, etc).
            # Mark it broken so it gets closed instead of returned to the
            # pool — otherwise every future request would keep drawing
            # this same dead connection and fail forever until the
            # process was restarted. The request that hit this still
            # fails and raises up to the caller (app.py's /api/chat
            # already catches this and shows a friendly retry message),
            # but the app self-heals on the very next request instead of
            # staying down.
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
    every time the app starts."""
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
        return  # already has data — either migrated already, or this is a fresh non-legacy DB
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
    """One-time import of the old CSV/JSON files, if they're still sitting
    in DATA_DIR from before this database existed. No-ops completely once
    each table has at least one row, so it's safe to run on every startup."""
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
    """Returns outages currently active (start_date <= today <= end_date)
    for the given parish.

    FIX: this used to compute "today" from the server's local/UTC clock.
    Grenada runs a fixed UTC-4 offset with no daylight saving, so on a
    server running in UTC (the common case on most hosts) that clock is
    several hours ahead of Grenada for part of every day — which meant an
    outage could show as active a few hours too early, or vanish a few
    hours too early, right around the boundary of "today". "today" is now
    computed in Grenada time by default, matching business-hours logic
    elsewhere in the app and the same fix already applied on the frontend.
    An explicit `today` ("YYYY-MM-DD") can still be passed in if a caller
    ever needs to check a specific date instead of "right now".
    """
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
