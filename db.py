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

Every function here keeps the exact same name, signature, and return shape
as the old CSV-based versions that used to live in app.py, so nothing else
in the app needs to change.
"""

import csv
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3

SQLITE_PATH = DATA_DIR / "aquaassist.db"

logger = logging.getLogger("aquaassist.db")

STATUS_DEFAULT = "Received"
SEVERITY_DEFAULT = "Unknown"

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


def _connect():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL, sslmode="require",
                                 cursor_factory=psycopg2.extras.RealDictCursor)
    conn = sqlite3.connect(str(SQLITE_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ph():
    return "%s" if USE_POSTGRES else "?"


def _rows(cur_rows):
    return [dict(r) for r in cur_rows]


def init_db():
    """Creates all tables if they don't exist yet, migrates any legacy CSV
    data in, and seeds default tips/features on first run. Safe to call
    every time the app starts."""
    conn = _connect()
    cur = conn.cursor()
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
    conn.commit()
    cur.close()
    conn.close()

    migrate_legacy_storage()
    _seed_tips_if_empty()
    _seed_features_if_empty()


# ---------------------------------------------------------------------
# Legacy migration — imports old CSV/JSON data into the DB exactly once
# ---------------------------------------------------------------------
def _table_is_empty(table):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) AS c FROM {table}")
    count = cur.fetchone()["c"]
    cur.close()
    conn.close()
    return count == 0


def _insert_legacy_report(row):
    ph = _ph()
    conn = _connect()
    cur = conn.cursor()
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
    conn.commit()
    cur.close()
    conn.close()


def _insert_legacy_outage(row):
    ph = _ph()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"""
        INSERT INTO outages (id, parish, message, start_date, end_date, created_at)
        VALUES ({ph},{ph},{ph},{ph},{ph},{ph})
    """, (
        row.get("id") or uuid.uuid4().hex[:8], row.get("parish"), row.get("message"),
        row.get("start_date"), row.get("end_date"),
        row.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ))
    conn.commit()
    cur.close()
    conn.close()


def _insert_legacy_notification(row):
    ph = _ph()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO notifications (timestamp, contact, categories) VALUES ({ph},{ph},{ph})",
                (row.get("timestamp"), row.get("contact"), row.get("categories")))
    conn.commit()
    cur.close()
    conn.close()


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
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"""
        INSERT INTO reports (reference, timestamp, name, phone, location, issue_type,
                              description, attachment_mime, attachment_data, status, severity)
        VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
    """, (row["reference"], row["timestamp"], row["name"], row["phone"], row["location"],
          row["issue_type"], row["description"], row["attachment_mime"], row["attachment_data"],
          row["status"], row["severity"]))
    conn.commit()
    cur.close()
    conn.close()
    return row


def load_reports():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM reports ORDER BY timestamp ASC")
    rows = _rows(cur.fetchall())
    cur.close()
    conn.close()
    return rows


def update_report_status(reference, new_status):
    ph = _ph()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"UPDATE reports SET status = {ph} WHERE UPPER(reference) = UPPER({ph})",
                (new_status, reference))
    updated = cur.rowcount > 0
    conn.commit()
    cur.close()
    conn.close()
    return updated


def track_report(reference):
    if not reference:
        return None
    ph = _ph()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM reports WHERE UPPER(reference) = UPPER({ph})", (reference.strip(),))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None


def delete_report(reference):
    ph = _ph()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM reports WHERE UPPER(reference) = UPPER({ph})", (reference.strip(),))
    deleted = cur.rowcount > 0
    conn.commit()
    cur.close()
    conn.close()
    return deleted


# ---------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------
def save_notification_signup(contact, categories):
    ph = _ph()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO notifications (timestamp, contact, categories) VALUES ({ph},{ph},{ph})",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), contact, ", ".join(categories)))
    conn.commit()
    cur.close()
    conn.close()


def load_notifications():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT timestamp, contact, categories FROM notifications ORDER BY id ASC")
    rows = _rows(cur.fetchall())
    cur.close()
    conn.close()
    return rows


# ---------------------------------------------------------------------
# Outages
# ---------------------------------------------------------------------
def save_outage(parish, message, start_date, end_date):
    row = {"id": uuid.uuid4().hex[:8], "parish": parish, "message": message,
           "start_date": start_date, "end_date": end_date,
           "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    ph = _ph()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"""
        INSERT INTO outages (id, parish, message, start_date, end_date, created_at)
        VALUES ({ph},{ph},{ph},{ph},{ph},{ph})
    """, (row["id"], row["parish"], row["message"], row["start_date"], row["end_date"], row["created_at"]))
    conn.commit()
    cur.close()
    conn.close()
    return row


def load_outages():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM outages ORDER BY created_at ASC")
    rows = _rows(cur.fetchall())
    cur.close()
    conn.close()
    return rows


def delete_outage(outage_id):
    ph = _ph()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM outages WHERE id = {ph}", (outage_id,))
    conn.commit()
    cur.close()
    conn.close()


def get_active_outages_for_parish(parish):
    today = datetime.now().strftime("%Y-%m-%d")
    ph = _ph()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT * FROM outages WHERE parish = {ph} AND start_date <= {ph} AND end_date >= {ph}
    """, (parish, today, today))
    rows = _rows(cur.fetchall())
    cur.close()
    conn.close()
    return rows


# ---------------------------------------------------------------------
# Water Service Tips
# ---------------------------------------------------------------------
def _tip_out(row):
    return {"id": row["id"], "text": row["text"], "enabled": row.get("enabled") == "1",
            "created_at": row.get("created_at", "")}


def _seed_tips_if_empty():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM tips")
    count = cur.fetchone()["c"]
    cur.close()
    if count:
        conn.close()
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ph = _ph()
    cur = conn.cursor()
    for t in DEFAULT_TIPS:
        cur.execute(f"INSERT INTO tips (id, text, enabled, created_at) VALUES ({ph},{ph},{ph},{ph})",
                    (uuid.uuid4().hex[:8], t, "1", now))
    conn.commit()
    cur.close()
    conn.close()


def load_tips():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tips ORDER BY created_at ASC")
    rows = _rows(cur.fetchall())
    cur.close()
    conn.close()
    return [_tip_out(r) for r in rows]


def save_tip(text):
    row = {"id": uuid.uuid4().hex[:8], "text": text, "enabled": "1",
           "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    ph = _ph()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO tips (id, text, enabled, created_at) VALUES ({ph},{ph},{ph},{ph})",
                (row["id"], row["text"], row["enabled"], row["created_at"]))
    conn.commit()
    cur.close()
    conn.close()
    return _tip_out(row)


def update_tip(tip_id, text=None, enabled=None):
    ph = _ph()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM tips WHERE id = {ph}", (tip_id,))
    existing = cur.fetchone()
    if existing is None:
        cur.close()
        conn.close()
        return None
    existing = dict(existing)
    new_text = text if text is not None else existing["text"]
    new_enabled = ("1" if enabled else "0") if enabled is not None else existing["enabled"]
    cur.execute(f"UPDATE tips SET text = {ph}, enabled = {ph} WHERE id = {ph}",
                (new_text, new_enabled, tip_id))
    conn.commit()
    cur.close()
    conn.close()
    return _tip_out({"id": tip_id, "text": new_text, "enabled": new_enabled,
                      "created_at": existing["created_at"]})


def delete_tip(tip_id):
    ph = _ph()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM tips WHERE id = {ph}", (tip_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    cur.close()
    conn.close()
    return deleted


# ---------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------
def _seed_features_if_empty():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM features")
    count = cur.fetchone()["c"]
    if not count:
        ph = _ph()
        data = dict(DEFAULT_FEATURES)
        data["maintenance_message"] = DEFAULT_MAINTENANCE_MESSAGE
        cur.execute(f"INSERT INTO features (id, data) VALUES (1, {ph})", (json.dumps(data),))
        conn.commit()
    cur.close()
    conn.close()


def load_features():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT data FROM features WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
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
    conn = _connect()
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute(f"""
            INSERT INTO features (id, data) VALUES (1, {ph})
            ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
        """, (json.dumps(current),))
    else:
        cur.execute(f"INSERT OR REPLACE INTO features (id, data) VALUES (1, {ph})", (json.dumps(current),))
    conn.commit()
    cur.close()
    conn.close()
    return current
