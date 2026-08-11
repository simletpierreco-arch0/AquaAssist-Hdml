"""
AquaAssist backend — Flask API + Gemini chat, serving a static HTML/CSS/JS
frontend (see ../frontend). This replaces the Streamlit UI: the browser is
now a plain client that talks to this server over JSON; the server holds
the Gemini API key, the CSV "database", and the staff passcode.

Run with:
    pip install -r requirements.txt
    export GEMINI_API_KEY=your-key-here
    export STAFF_PASSCODE=change-me
    python app.py

Then open http://localhost:5000

NOTE ON SCOPE (see chat with the user for full rationale): this port keeps
chat, tool-triggered report logging, manual report submission + tracking,
FAQ search, outage announcements, notification signups, and a staff portal.
It intentionally drops: live mic transcription, text-to-speech replies,
in-browser camera capture (photo *upload* still works), and Pinecone
retrieval — each is a self-contained addition if you want it back later.
"""

import os
import csv
import io
import re
import uuid
import base64
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory, g
from google import genai
from google.genai import types

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
ATTACH_DIR = BASE_DIR / "attachments"
FRONTEND_DIR = BASE_DIR

DATA_DIR.mkdir(exist_ok=True)
ATTACH_DIR.mkdir(exist_ok=True)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
STAFF_PASSCODE = os.environ.get("STAFF_PASSCODE", "changeme123")
MODEL_NAME = "gemini-3.1-flash-lite"

NAWASA_PHONE = "(473) 440-2155"
NAWASA_WEBSITE = "https://nawasa.gd/"

# ---------------------------------------------------------------------------
# Media normalization for Gemini — the model's audio understanding only
# reliably accepts a specific set of container/codec combos (wav, mp3, aiff,
# aac, ogg, flac). Browsers record voice notes with MediaRecorder, and on
# most browsers/OSes the only thing actually available is audio/webm
# (Opus) — a container Gemini does not recognize for audio understanding.
# That meant voice notes were uploading fine but being "heard" as nothing:
# the model got bytes it couldn't decode as audio and just gave a generic
# reply instead of responding to what the customer said.
#
# When ffmpeg is available on the host we remux (NOT re-encode — same Opus
# audio, different container, so it's instant and lossless) the WebM file
# into an Ogg container, which Gemini does accept. The frontend also now
# prefers a Gemini-friendly recording format when the browser supports one,
# so this server-side step is a safety net, not the only fix.
#
# Video is left alone — video/webm is already an accepted video type.
# ---------------------------------------------------------------------------
def _ffmpeg_available():
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, check=True, timeout=5)
        return True
    except Exception:
        return False


_HAS_FFMPEG = _ffmpeg_available()


def _normalize_media_for_gemini(raw: bytes, mime: str):
    """Returns (bytes_to_send, mime_type_to_send) for a chat attachment.
    Only touches audio/webm (remux to audio/ogg); everything else passes
    through unchanged."""
    mime = (mime or "").split(";")[0].strip().lower()
    if mime in ("audio/webm", "audio/x-webm") and _HAS_FFMPEG:
        try:
            with tempfile.NamedTemporaryFile(suffix=".webm") as src, \
                 tempfile.NamedTemporaryFile(suffix=".ogg") as dst:
                src.write(raw)
                src.flush()
                subprocess.run(
                    ["ffmpeg", "-y", "-i", src.name, "-c:a", "libopus", dst.name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=True, timeout=25,
                )
                converted = Path(dst.name).read_bytes()
                if converted:
                    return converted, "audio/ogg"
        except Exception:
            pass  # fall through and send the original bytes/mime below
    return raw, (mime or "application/octet-stream")


app = Flask(__name__, static_folder=None)

# ---------------------------------------------------------------------------
# CSV storage — same shape as the Streamlit app's data/*.csv files, plain
# csv module instead of pandas (one less heavy dependency for a small API).
# ---------------------------------------------------------------------------
REPORTS_PATH = DATA_DIR / "reports.csv"
NOTIFY_PATH = DATA_DIR / "notifications.csv"
OUTAGES_PATH = DATA_DIR / "outages.csv"

REPORTS_FIELDS = ["reference", "timestamp", "name", "phone", "location", "issue_type",
                   "description", "attachment", "status", "severity"]
NOTIFY_FIELDS = ["timestamp", "contact", "categories"]
OUTAGE_FIELDS = ["id", "parish", "message", "start_date", "end_date", "created_at"]
STATUS_STAGES = ["Received", "Assigned", "Crew Dispatched", "In Progress", "Resolved"]
SEVERITY_LEVELS = ["Unknown", "Low", "Medium", "High"]
ISSUE_TYPES = ["Leak", "No water supply", "Low pressure", "Billing issue",
               "Burst main", "Damaged hydrant", "Water quality concern", "Other"]


def _ensure_csv(path, fields):
    if not path.exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()


def _read_csv(path, fields):
    _ensure_csv(path, fields)
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def new_reference():
    return "NW-" + uuid.uuid4().hex[:7].upper()


def save_report(name, phone, location, issue_type, description, attachment_name="", severity="Unknown"):
    _ensure_csv(REPORTS_PATH, REPORTS_FIELDS)
    reference = new_reference()
    row = {
        "reference": reference,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "name": name or "Not provided", "phone": phone or "Not provided", "location": location,
        "issue_type": issue_type, "description": description,
        "attachment": attachment_name, "status": "Received", "severity": severity or "Unknown",
    }
    with open(REPORTS_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=REPORTS_FIELDS).writerow(row)
    return row


def load_reports():
    return _read_csv(REPORTS_PATH, REPORTS_FIELDS)


def update_report_status(reference, new_status):
    rows = load_reports()
    found = False
    for r in rows:
        if r["reference"] == reference:
            r["status"] = new_status
            found = True
    if found:
        _write_csv(REPORTS_PATH, REPORTS_FIELDS, rows)
    return found


def track_report(reference):
    for r in load_reports():
        if r["reference"].upper() == reference.strip().upper():
            return r
    return None


def save_notification_signup(contact, categories):
    _ensure_csv(NOTIFY_PATH, NOTIFY_FIELDS)
    with open(NOTIFY_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=NOTIFY_FIELDS).writerow({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "contact": contact, "categories": ", ".join(categories),
        })


def load_notifications():
    return _read_csv(NOTIFY_PATH, NOTIFY_FIELDS)


def save_outage(parish, message, start_date, end_date):
    _ensure_csv(OUTAGES_PATH, OUTAGE_FIELDS)
    outage_id = uuid.uuid4().hex[:8]
    row = {"id": outage_id, "parish": parish, "message": message,
           "start_date": start_date, "end_date": end_date,
           "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    with open(OUTAGES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=OUTAGE_FIELDS).writerow(row)
    return row


def load_outages():
    return _read_csv(OUTAGES_PATH, OUTAGE_FIELDS)


def delete_outage(outage_id):
    rows = [r for r in load_outages() if r["id"] != outage_id]
    _write_csv(OUTAGES_PATH, OUTAGE_FIELDS, rows)


def get_active_outages_for_parish(parish):
    today = datetime.now().strftime("%Y-%m-%d")
    return [r for r in load_outages()
            if r["parish"] == parish and r["start_date"] <= today <= r["end_date"]]


def parse_report_coords(location_text):
    if not isinstance(location_text, str):
        return None
    match = re.search(r"GPS:\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)", location_text)
    if not match:
        return None
    try:
        return float(match.group(1)), float(match.group(2))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Business hours — identical logic to the Streamlit version.
# ---------------------------------------------------------------------------
BUSINESS_HOURS_START = 8
BUSINESS_HOURS_END = 16
CLOSING_SOON_WINDOW_MINUTES = 60
NAWASA_HOLIDAYS = []
GRENADA_TZ = timezone(timedelta(hours=-4))
_WEEKDAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def get_business_hours_status():
    now = datetime.now(GRENADA_TZ)
    today_str = now.strftime("%Y-%m-%d")
    weekday_idx = now.weekday()
    is_weekend = weekday_idx >= 5
    is_holiday = today_str in NAWASA_HOLIDAYS
    is_open_hour = BUSINESS_HOURS_START <= now.hour < BUSINESS_HOURS_END
    is_open = (not is_weekend) and (not is_holiday) and is_open_hour

    next_day = now
    if is_weekend or is_holiday or now.hour >= BUSINESS_HOURS_END:
        next_day = next_day + timedelta(days=1)
    while next_day.weekday() >= 5 or next_day.strftime("%Y-%m-%d") in NAWASA_HOLIDAYS:
        next_day = next_day + timedelta(days=1)

    if is_weekend:
        closed_reason = "It's the weekend"
    elif is_holiday:
        closed_reason = "Today is a NAWASA holiday"
    elif now.hour < BUSINESS_HOURS_START:
        closed_reason = "We open later this morning"
    else:
        closed_reason = "We've closed for the day"

    same_day = next_day.strftime("%Y-%m-%d") == today_str
    reopens_label = ("today" if same_day else _WEEKDAY_LABELS[next_day.weekday()]) + f" at {BUSINESS_HOURS_START}:00 AM"

    minutes_until_close, closing_soon = None, False
    if is_open:
        close_time = now.replace(hour=BUSINESS_HOURS_END, minute=0, second=0, microsecond=0)
        minutes_until_close = max(0, int((close_time - now).total_seconds() // 60))
        closing_soon = minutes_until_close <= CLOSING_SOON_WINDOW_MINUTES

    return {"is_open": is_open, "closed_reason": closed_reason, "reopens_label": reopens_label,
            "closing_soon": closing_soon, "minutes_until_close": minutes_until_close}


# ---------------------------------------------------------------------------
# NAWASA reference data
# ---------------------------------------------------------------------------
TERRITORIES = ["Grenada", "Carriacou", "Petit Martinique"]
TERRITORY_WHATSAPP = {
    "Grenada": "https://wa.link/rt9dj1",
    "Carriacou": "https://wa.link/wp6vfj",
    "Petit Martinique": "https://wa.link/3dpbnj",
}
GRENADA_PARISHES = [
    "St. George's (Capital area)", "St. Andrew's", "St. David's",
    "St. John's", "St. Mark's", "St. Patrick's", "Carriacou and Petite Martinique",
]
PARISH_CENTERS = {
    "St. George's (Capital area)": [12.0561, -61.7488],
    "St. Andrew's": [12.1500, -61.6500],
    "St. David's": [12.0333, -61.6500],
    "St. John's": [12.1667, -61.7167],
    "St. Mark's": [12.2167, -61.6833],
    "St. Patrick's": [12.2333, -61.6167],
    "Carriacou and Petite Martinique": [12.4747, -61.4487],
}
GRENADA_CENTER = [12.1165, -61.6790]

FAQS = [
    {"category": "New Connections", "q": "How do I apply for a new connection?",
     "a": "Fill out the application for a new service connection. Review the Requirements for Private Water Service and the Terms and Conditions for Water Service on nawasa.gd."},
    {"category": "New Connections", "q": "What is the cost of a new connection?",
     "a": "Connection to ½\" main: $75. ¾\" main: $125. 1\" main: $175. 1¼\"/1½\"/2\" main: $420. 4\" main: $1000. Plus variable costs (transportation, pipes & fittings, VAT) — an estimate is prepared to determine the total."},
    {"category": "New Connections", "q": "How long does it take NAWASA to install a new service?",
     "a": "Per the customer service charter, a new service should be installed within 10 working days after payment of the connection fee."},
    {"category": "New Connections", "q": "I don't own the property — can I still get a connection in my name?",
     "a": "Yes, with written permission from the property owner plus the owner's ID. A security deposit is also required: $240 (Domestic), $340 (Commercial), or $2,000 (Projects) — refundable if you later become the owner or the service is permanently terminated."},
    {"category": "Billing", "q": "How may I change my account name or billing/mailing address?",
     "a": "To change the account name, fill out the application for change of name and provide one of: Title Deed/Conveyance, Death Certificate, Letter from Lawyer, Will, or Court Judgement. To change the mailing address, fill out the Change of Mailing Address Form. A valid picture ID is required for all account changes."},
    {"category": "Billing", "q": "I've been paying my bills, why does my bill show arrears?",
     "a": "Your current bill may have already been issued prior to processing your previous payment."},
    {"category": "Billing", "q": "How are estimated bills calculated?",
     "a": "Estimated bills use an average of your last three months' consumption."},
    {"category": "Water Usage & Leaks", "q": "My water consumption is unusually high — what could be the problem?",
     "a": "High consumption can come from estimated bills, leaks, unsecured taps, or a faulty meter. To check for a leak: turn off all taps and watch the meter dial — if it's still turning, there's a leak. If not, contact Customer Services."},
    {"category": "Disconnection", "q": "Under what circumstances does NAWASA disconnect service?",
     "a": "At the customer's request, for non-payment of arrears, for wastage/abuse, or for illegal tampering of meters and fittings."},
    {"category": "Disconnection", "q": "How do I request a disconnection?",
     "a": "Request in writing or in person using a 'Request for Disconnection' form. Only the account owner or an authorized person (with documentation) can request this, and valid ID is required."},
    {"category": "Disconnection", "q": "What is the minimum balance for disconnection?",
     "a": "A customer can be disconnected once arrears reach at least $50.00 and are at least 30 days overdue."},
    {"category": "Disconnection", "q": "After paying the reconnection fee, how long until reconnection?",
     "a": "Reconnection is not guaranteed within 48 hours after payment of the reconnection fee."},
    {"category": "General", "q": "What does NAWASA mean?",
     "a": "National Water & Sewerage Authority."},
    {"category": "General", "q": "Where is NAWASA's main office?",
     "a": "NAWASA's main office is now located on Lucas Street, St. George's (previously on the Carenage). Sub-offices are located at Seaton James Street, Grenville; Lower Depradine Street, Gouyave; and additional sub-offices in Sauteurs, St. David's, and Grand Anse."},
]


def build_system_instruction(territory):
    territory_whatsapp = TERRITORY_WHATSAPP.get(territory, TERRITORY_WHATSAPP["Grenada"])
    return f"""
You are AquaAssist, a friendly virtual customer assistant for the National Water and Sewerage Authority (NAWASA) of Grenada, serving the {territory} territory.

LANGUAGE RULE:
Always reply in clear, professional Standard English, regardless of what language or dialect the customer writes in. You must still fully UNDERSTAND Grenadian Creole (patois) if a customer writes in it — correctly interpret their meaning and intent — but your reply itself must always be in Standard English. Never reply in Creole, patois, or any other language, even if asked to.

CONVERSATION STYLE:
Sound like an experienced, caring NAWASA customer service representative — not a generic AI chatbot. Be warm, natural, and conversational, never robotic or overly formal.
- Prefer natural phrasing over stiff, templated wording.
- Vary your wording across a conversation; avoid repeating the same stock phrases turn after turn.
- Greet customers naturally and maintain a friendly, professional tone throughout.
- Keep track of what's already been said in the conversation and don't ask the customer to repeat information they've already given you.
- When a customer reports a problem, show empathy first, then guide them calmly through the next steps.
- Keep responses concise, clear, and easy to understand.

Use the following facts to answer user questions:
- Help customers report water leaks by collecting the location and relevant details.
- Provide information about water supply issues and service interruptions.
- Help customers check for planned maintenance and scheduled outages.
- Explain the available methods for paying NAWASA bills.
- Provide NAWASA customer service contact information and transfer users to a representative when requested.
- If the issue is an emergency, advise the user to contact NAWASA immediately at (473) 440-2155.
- NAWASA's official contact details: Phone (473) 440-2155, WhatsApp via {territory_whatsapp} (this is the number for {territory}), Website https://nawasa.gd/.
- NAWASA's main office is now located on Lucas Street, St. George's (it moved from its former, over 150-year-old building on the Carenage). Sub-offices are located at Seaton James Street, Grenville; Lower Depradine Street, Gouyave; and additional sub-offices in Sauteurs, St. David's, and Grand Anse.
- When a customer describes a specific problem and gives at least a location, log it immediately using the log_water_report tool — do not tell the customer to fill out a separate form themselves.
- When a customer reports a visible physical issue (a leak, burst main, damaged hydrant, water quality concern, etc.), ask them to send a photo of it via the attachment (📎) button in the chat box. This helps our technicians assess severity and prepare before visiting. Ask for this naturally as part of your reply — don't make it a precondition for logging the report, and don't ask for a photo for issues that wouldn't have one (e.g. billing questions or no water supply with nothing to see).
- If the customer attaches a photo of the issue, look at it before calling log_water_report and set severity based on what you actually see.
- Use natural understanding, not keyword matching.
- If a customer shares their GPS location (a message like "My current location is [parish], Grenada (GPS: lat, lng)"), treat that parish as their location for the rest of the conversation — use it for outage/service questions and when logging a report, without asking them to repeat it.
- Customers may send a voice note (spoken audio) or a short video instead of typing or a photo. Always listen to / watch the attachment directly and treat what you actually hear or see as their real message — respond to its actual content (what they said, what the leak or damage looks like, etc.). Never reply with a generic "thanks for the recording" acknowledgement, and never ask them to type it out instead — if for some reason the audio or video truly can't be made out, say so plainly and ask them to also type a quick summary, rather than pretending you understood it.

If a question is unrelated to NAWASA services, politely explain that you can only assist with NAWASA-related topics and invite the user to ask another water service question.
"""


# ---------------------------------------------------------------------------
# Gemini chat sessions — kept in memory, one per browser session id. Good
# enough for a single-process dev/small-deployment server; for production
# scale-out, swap this dict for a shared store (e.g. Redis) keyed the same
# way.
# ---------------------------------------------------------------------------
_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
SESSIONS = {}       # session_id -> {"chat": chat_obj, "territory": str}
LAST_REPORT = {}    # session_id -> report dict, set by the tool call for that turn


def _make_log_tool(session_id):
    def log_water_report(location: str, issue_type: str, description: str,
                          name: str = "Not provided", phone: str = "Not provided",
                          severity: str = "Unknown") -> str:
        """Logs a customer's water service issue into the NAWASA staff system so a
        technician can follow up on it. Call this as soon as the customer has
        described their problem and given at least a location — even in normal
        conversation, without requiring them to fill out a separate form.

        Args:
            location: The location or address where the issue is happening.
            issue_type: One of "Leak", "No water supply", "Low pressure", "Billing issue", "Other".
            description: A short description of the issue in the customer's own words.
            name: The customer's name, if given.
            phone: The customer's phone number, if given.
            severity: One of "Unknown", "Low", "Medium", "High". If the customer
                attached a photo of the issue, assess how serious it looks; otherwise
                leave it "Unknown" rather than guessing from text alone.

        Returns:
            A confirmation message including the reference number for tracking.
        """
        row = save_report(name, phone, location, issue_type, description, severity=severity)
        LAST_REPORT[session_id] = {
            "reference": row["reference"], "status": "Received",
            "issue_type": issue_type, "severity": severity,
        }
        return f"Report logged successfully. Reference number: {row['reference']}. A technician will follow up."
    return log_water_report


def _get_or_create_chat(session_id, territory):
    sess = SESSIONS.get(session_id)
    if sess is None or sess["territory"] != territory:
        chat = _client.chats.create(
            model=MODEL_NAME,
            config=types.GenerateContentConfig(
                system_instruction=build_system_instruction(territory),
                temperature=0.7,
                tools=[_make_log_tool(session_id)],
            ),
        )
        sess = {"chat": chat, "territory": territory}
        SESSIONS[session_id] = sess
    return sess["chat"]


# ---------------------------------------------------------------------------
# Staff auth — a simple passcode check. The frontend sends the passcode back
# on every staff request via the X-Staff-Passcode header rather than a
# session cookie, which keeps the server stateless for staff auth.
# ---------------------------------------------------------------------------
def require_staff(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        supplied = request.headers.get("X-Staff-Passcode", "")
        if supplied != STAFF_PASSCODE:
            return jsonify({"error": "Invalid or missing staff passcode."}), 401
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Routes — static frontend
# ---------------------------------------------------------------------------
@app.route("/")
def serve_index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:filename>")
def serve_static(filename):
    return send_from_directory(FRONTEND_DIR, filename)


@app.route("/attachments/<path:filename>")
def serve_attachment(filename):
    return send_from_directory(ATTACH_DIR, filename)


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------
@app.route("/api/init")
def api_init():
    return jsonify({
        "territories": TERRITORIES,
        "territory_whatsapp": TERRITORY_WHATSAPP,
        "parishes": GRENADA_PARISHES,
        "parish_centers": PARISH_CENTERS,
        "grenada_center": GRENADA_CENTER,
        "issue_types": ISSUE_TYPES,
        "severity_levels": SEVERITY_LEVELS,
        "status_stages": STATUS_STAGES,
        "faqs": FAQS,
        "business_hours": get_business_hours_status(),
        "nawasa_phone": NAWASA_PHONE,
        "nawasa_website": NAWASA_WEBSITE,
        "gemini_configured": _client is not None,
    })


@app.route("/api/business-hours")
def api_business_hours():
    return jsonify(get_business_hours_status())


@app.route("/api/chat", methods=["POST"])
def api_chat():
    if _client is None:
        return jsonify({"error": "Server is missing GEMINI_API_KEY."}), 503

    body = request.get_json(force=True)
    session_id = body.get("session_id") or str(uuid.uuid4())
    territory = body.get("territory", "Grenada")
    message = (body.get("message") or "").strip()
    attachments = body.get("attachments") or []  # [{name, mime, data_base64}]

    if not message and not attachments:
        return jsonify({"error": "Empty message."}), 400

    chat = _get_or_create_chat(session_id, territory)
    LAST_REPORT.pop(session_id, None)

    parts = []
    if message:
        parts.append(message)
    saved_attachment_name = ""
    for att in attachments:
        try:
            raw = base64.b64decode(att["data_base64"])
        except Exception:
            continue
        saved_attachment_name = f"{uuid.uuid4().hex[:8]}_{att.get('name', 'upload')}"
        with open(ATTACH_DIR / saved_attachment_name, "wb") as f:
            f.write(raw)  # keep the original file on disk for staff review
        send_bytes, send_mime = _normalize_media_for_gemini(raw, att.get("mime", "application/octet-stream"))
        parts.append(types.Part.from_bytes(data=send_bytes, mime_type=send_mime))

    try:
        response = chat.send_message(parts if len(parts) > 1 else parts[0])
        reply_text = response.text
    except Exception as e:
        return jsonify({"error": f"Gemini error: {e}"}), 502

    result = {"session_id": session_id, "reply": reply_text, "attachment": saved_attachment_name or None}
    report_card = LAST_REPORT.pop(session_id, None)
    if report_card:
        result["report_card"] = report_card
    return jsonify(result)


@app.route("/api/report", methods=["POST"])
def api_create_report():
    body = request.get_json(force=True)
    required = ["name", "phone", "location", "issue_type"]
    if any(not body.get(f) for f in required):
        return jsonify({"error": "name, phone, location, and issue_type are required."}), 400

    saved_attachment_name = body.get("attachment", "")
    b64 = body.get("attachment_base64")
    if b64:
        try:
            raw = base64.b64decode(b64)
            saved_attachment_name = f"{uuid.uuid4().hex[:8]}_{body.get('attachment_name', 'upload')}"
            with open(ATTACH_DIR / saved_attachment_name, "wb") as f:
                f.write(raw)
        except Exception:
            saved_attachment_name = ""

    row = save_report(
        body.get("name"), body.get("phone"), body.get("location"),
        body.get("issue_type"), body.get("description", ""),
        attachment_name=saved_attachment_name,
        severity=body.get("severity", "Unknown"),
    )
    return jsonify(row)


@app.route("/api/report/<reference>")
def api_track_report(reference):
    row = track_report(reference)
    if row is None:
        return jsonify({"error": "No report found with that reference number."}), 404
    return jsonify(row)


@app.route("/api/reports")
@require_staff
def api_list_reports():
    return jsonify(load_reports())


@app.route("/api/reports/<reference>", methods=["PATCH"])
@require_staff
def api_update_report(reference):
    body = request.get_json(force=True)
    new_status = body.get("status")
    if new_status not in STATUS_STAGES:
        return jsonify({"error": "Invalid status."}), 400
    if not update_report_status(reference, new_status):
        return jsonify({"error": "Reference not found."}), 404
    return jsonify({"reference": reference, "status": new_status})


@app.route("/api/outages", methods=["GET", "POST"])
def api_outages():
    if request.method == "GET":
        return jsonify(load_outages())
    # POST — staff only
    if request.headers.get("X-Staff-Passcode", "") != STAFF_PASSCODE:
        return jsonify({"error": "Invalid or missing staff passcode."}), 401
    body = request.get_json(force=True)
    required = ["parish", "message", "start_date", "end_date"]
    if any(not body.get(f) for f in required):
        return jsonify({"error": "parish, message, start_date, and end_date are required."}), 400
    row = save_outage(body["parish"], body["message"], body["start_date"], body["end_date"])
    return jsonify(row)


@app.route("/api/outages/<outage_id>", methods=["DELETE"])
@require_staff
def api_delete_outage(outage_id):
    delete_outage(outage_id)
    return jsonify({"deleted": outage_id})


@app.route("/api/notify", methods=["GET", "POST"])
def api_notify():
    if request.method == "GET":
        if request.headers.get("X-Staff-Passcode", "") != STAFF_PASSCODE:
            return jsonify({"error": "Invalid or missing staff passcode."}), 401
        return jsonify(load_notifications())
    body = request.get_json(force=True)
    contact = body.get("contact", "").strip()
    categories = body.get("categories") or []
    if not contact or not categories:
        return jsonify({"error": "contact and at least one category are required."}), 400
    save_notification_signup(contact, categories)
    return jsonify({"ok": True})


@app.route("/api/staff/login", methods=["POST"])
def api_staff_login():
    body = request.get_json(force=True)
    ok = body.get("passcode") == STAFF_PASSCODE
    return jsonify({"ok": ok}), (200 if ok else 401)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
