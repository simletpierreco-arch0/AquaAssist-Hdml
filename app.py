"""
AquaAssist backend — Flask API + a LangChain/LangGraph conversational agent
(see agent.py), serving a static HTML/CSS/JS frontend (see ../frontend).

Run with:
    pip install -r requirements.txt
    export GEMINI_API_KEY=your-key-here
    export STAFF_PASSCODE=change-me
    export STAFF_PASSCODE_WEBSITE=optional-website-manager-passcode
    export STAFF_PASSCODE_AQUA=optional-aquaassist-manager-passcode
    export STAFF_PASSCODE_OPS=optional-operations-passcode
    export ELEVENLABS_API_KEY=your-elevenlabs-key      # optional, enables Caribbean-accent read-aloud
    export ELEVENLABS_VOICE_ID=your-chosen-voice-id    # optional, from the ElevenLabs Voice Library
    export PINECONE_API_KEY=your-pinecone-key          # optional, enables live RAG retrieval (see agent.py)
    export DATABASE_URL=your-postgres-url              # RECOMMENDED for production — see db.py
    export SELF_PING_URL=https://your-app.onrender.com # RECOMMENDED for production — see _keep_alive_loop below
    python app.py

Then open http://localhost:5000
"""

import os
import logging
import re
import threading
import time
import uuid
import base64
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps

import requests
from flask import Flask, request, jsonify, send_from_directory
from langchain_core.tools import tool

import agent
import db
from db import (
    save_report, load_reports, update_report_status, track_report, delete_report,
    save_notification_signup, load_notifications,
    save_outage, load_outages, delete_outage, get_active_outages_for_parish,
    load_tips, save_tip, update_tip, delete_tip,
    load_features, save_features,
)

BASE_DIR = Path(__file__).parent
FRONTEND_DIR = BASE_DIR

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# ---------------------------------------------------------------------
# Staff auth — role-based passcodes.
#
# Only STAFF_PASSCODE (Administrator) is required; the other three are
# optional. Whichever passcode a staff member logs in with determines
# their role and what parts of the Staff Portal they can see/use. If you
# only set STAFF_PASSCODE, every staff login is an Administrator (full
# access) — identical behavior to before this was added.
# ---------------------------------------------------------------------
STAFF_PASSCODE = os.environ.get("STAFF_PASSCODE", "changeme123")
STAFF_PASSCODE_WEBSITE = os.environ.get("STAFF_PASSCODE_WEBSITE", "")
STAFF_PASSCODE_AQUA = os.environ.get("STAFF_PASSCODE_AQUA", "")
STAFF_PASSCODE_OPS = os.environ.get("STAFF_PASSCODE_OPS", "")

ROLE_PERMISSIONS = {
    "admin": {"website", "aquaassist", "reports"},
    "website": {"website"},
    "aquaassist": {"aquaassist"},
    "ops": {"reports"},
}


def _role_for_passcode(passcode):
    if passcode and passcode == STAFF_PASSCODE:
        return "admin"
    if passcode and STAFF_PASSCODE_WEBSITE and passcode == STAFF_PASSCODE_WEBSITE:
        return "website"
    if passcode and STAFF_PASSCODE_AQUA and passcode == STAFF_PASSCODE_AQUA:
        return "aquaassist"
    if passcode and STAFF_PASSCODE_OPS and passcode == STAFF_PASSCODE_OPS:
        return "ops"
    return None


# ElevenLabs — used for Caribbean-accented read-aloud (see /api/tts below).
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")

NAWASA_PHONE = "(473) 440-2155"
NAWASA_WEBSITE = "https://nawasa.gd/"

def _ffmpeg_available():
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, check=True, timeout=5)
        return True
    except Exception:
        return False


_HAS_FFMPEG = _ffmpeg_available()


def _normalize_media_for_gemini(raw: bytes, mime: str):
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
            pass

    if mime.startswith("video/") and _HAS_FFMPEG:
        src_suffix = ".mp4" if "mp4" in mime else ".webm"
        try:
            with tempfile.NamedTemporaryFile(suffix=src_suffix) as src, \
                 tempfile.NamedTemporaryFile(suffix=".mp4") as dst:
                src.write(raw)
                src.flush()
                subprocess.run(
                    ["ffmpeg", "-y", "-i", src.name,
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                     "-c:a", "aac", "-movflags", "+faststart", dst.name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=True, timeout=60,
                )
                converted = Path(dst.name).read_bytes()
                if converted:
                    return converted, "video/mp4"
        except Exception:
            pass

    return raw, (mime or "application/octet-stream")


app = Flask(__name__, static_folder=None)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("aquaassist")

STATUS_STAGES = ["Received", "Assigned", "Crew Dispatched", "In Progress", "Resolved"]
SEVERITY_LEVELS = ["Unknown", "Low", "Medium", "High"]
ISSUE_TYPES = ["Leak", "No water supply", "Low pressure", "Billing issue",
               "Burst main", "Damaged hydrant", "Water quality concern", "Other"]


# Common ways a customer (or the model paraphrasing them) might refer to a
# parish that don't exactly match the canonical GRENADA_PARISHES strings
# staff pick from in the Service Alerts form. Service Alerts posted by
# staff were failing to reach the bot because check_active_outages queried
# the database with whatever string the model produced — "St George's",
# "Saint George", "the capital" — none of which equal the canonical
# "St. George's (Capital area)" staff selected. This normalizes before the
# lookup so a posted alert reliably surfaces however the customer phrases it.
PARISH_ALIASES = {
    "st george": "St. George's (Capital area)", "saint george": "St. George's (Capital area)",
    "st georges": "St. George's (Capital area)", "capital": "St. George's (Capital area)",
    "st andrew": "St. Andrew's", "saint andrew": "St. Andrew's",
    "st david": "St. David's", "saint david": "St. David's",
    "st john": "St. John's", "saint john": "St. John's",
    "st mark": "St. Mark's", "saint mark": "St. Mark's",
    "st patrick": "St. Patrick's", "saint patrick": "St. Patrick's",
    "carriacou": "Carriacou and Petite Martinique",
    "petite martinique": "Carriacou and Petite Martinique",
    "petit martinique": "Carriacou and Petite Martinique",
}


def _normalize_parish(raw):
    """Best-effort match of a free-text parish name to one of
    GRENADA_PARISHES. Falls back to returning the input unchanged (the
    case-insensitive comparison in db.get_active_outages_for_parish is the
    final safety net) so this never raises on an unrecognized string."""
    if not raw:
        return raw
    key = re.sub(r"[.\-']", "", raw.strip().lower())
    for needle, canonical in PARISH_ALIASES.items():
        if re.sub(r"[.\-']", "", needle) in key:
            return canonical
    for p in GRENADA_PARISHES:
        if re.sub(r"[.\-']", "", p.lower()) == key:
            return p
    return raw


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

PARISH_REFERENCE_POINTS = {
    "St. George's (Capital area)": [
        [12.0561, -61.7488], [12.0450, -61.7350], [12.0750, -61.7300],
    ],
    "St. David's": [
        [12.0150, -61.6280], [12.0400, -61.6600],
    ],
    "St. Andrew's": [
        [12.1330, -61.6150], [12.1100, -61.6600], [12.0900, -61.6300],
    ],
    "St. John's": [
        [12.1300, -61.7250], [12.1550, -61.7100],
    ],
    "St. Mark's": [
        [12.1950, -61.6950], [12.2100, -61.6700],
    ],
    "St. Patrick's": [
        [12.2450, -61.6250], [12.2200, -61.6100],
    ],
    "Carriacou and Petite Martinique": [
        [12.4747, -61.4487], [12.3000, -61.4000],
    ],
}

# NOTE: FAQS below is now used ONLY as one-time seed data for the `faqs`
# database table (see db._seed_faqs_if_empty). After first startup, staff
# edits via the Knowledge Base admin panel (/api/faqs) are what's actually
# served to customers and fed into Pinecone — this constant is not read
# again after the seed.
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

EMPATHY & FRUSTRATION:
- If a customer sounds frustrated, upset, or describes a bad prior experience (a leak that's gone unresolved, repeated estimated bills, a missed appointment), explicitly acknowledge that frustration in your own words before moving to solutions — don't jump straight to troubleshooting as if nothing was said.
- Apologize when it's genuinely warranted (a delay, an error on NAWASA's side) — but don't apologize reflexively for things that aren't NAWASA's fault (e.g. weather-related interruptions).
- If a customer is clearly not being helped by the conversation, or asks for a person, offer to connect them with a representative rather than continuing to loop on the same answer.

ADAPTING TO DIFFERENT CUSTOMERS:
Recognize which kind of customer you're likely speaking with and adapt accordingly, while keeping the same warm, competent tone throughout:
- Residential customers: everyday language, straightforward next steps, no unnecessary detail.
- Business/commercial account holders: they may mention higher volume, larger connections, or account-management needs — be a little more precise and formal, and reference the differentiated commercial connection costs or security deposit where relevant.
- A customer reporting an active emergency (burst main, major leak, a water-quality danger): prioritize urgency over anything else — acknowledge the seriousness immediately, advise calling (473) 440-2155 right away if it sounds dangerous, and log the report without delay rather than working through a long list of questions first.

WHAT YOU DO NOT HAVE ACCESS TO:
AquaAssist is not connected to NAWASA's billing or account systems. You do not have access to any individual customer's actual account balance, consumption figures, or meter readings, and you must never state or estimate a number for these. If a customer asks for their specific balance or reading, say plainly that you can't pull up their account directly, and explain how they can check it instead (their NAWASA bill, the office, or by phone).

KNOWLEDGE BASE — use the search_knowledge_base tool, don't guess:
NAWASA's official FAQ knowledge base (new connections, billing, disconnections, water usage & leaks, general info) is NOT pre-loaded into this prompt — it lives in a searchable knowledge base. Whenever a customer asks something that sounds like a policy, cost, process, or general-information question, call the search_knowledge_base tool with their question (or a short paraphrase of it) and answer using what it returns. Treat what it returns as authoritative — prefer it over general knowledge, and never contradict it. Paraphrase naturally in your own words rather than reciting it verbatim. If it returns no close match, say so plainly rather than guessing.

LIVE STAFF HANDOFF — use the request_human_handoff tool when you can't help:
Use the request_human_handoff tool whenever a customer explicitly asks to speak with a person, representative, or agent, or whenever you genuinely cannot resolve what they need (e.g. the knowledge base has no matching entry and the customer is still stuck after you've said so, a billing dispute needs a manual account review, or the situation calls for judgment you don't have). Calling this tool alerts NAWASA staff in the Live Chat monitor and flags the conversation so a person can step in and reply directly in this same chat — you do not need to end the conversation or stop responding, staff will simply join in. After calling it, tell the customer plainly (in your own words, matching the current business-hours status) that a NAWASA representative has been notified and will follow up here, or call/WhatsApp them directly if that's more urgent. Don't call this tool for questions you can actually answer yourself — it's for genuine dead ends or explicit requests for a human, not a substitute for trying the knowledge base first.

Use the following facts to answer user questions:
- Help customers report water leaks by collecting the location and relevant details.
- When a customer asks about outages, low water pressure, "no water", or scheduled maintenance in their area, call the check_active_outages tool with their parish (from what they've told you, or shared earlier via GPS) rather than answering from general knowledge — it returns real, currently-active notices. If you don't know their parish yet, ask for it first.
- Explain the available methods for paying NAWASA bills.
- Provide NAWASA customer service contact information and transfer users to a representative when requested.
- If the issue is an emergency, advise the user to contact NAWASA immediately at (473) 440-2155.
- NAWASA's official contact details: Phone (473) 440-2155, WhatsApp via {territory_whatsapp} (this is the number for {territory}), Website https://nawasa.gd/.
- IMPORTANT — the phone line and WhatsApp are only staffed by a live representative during business hours (8:00 AM – 4:00 PM, Monday to Friday, Grenada time). Each customer message includes a "CURRENT BUSINESS HOURS STATUS" note telling you whether the office is open or closed right now — this note is for your awareness only, not something to report on unprompted. Do NOT mention business hours, office-closed status, or when the office reopens in a greeting, a general FAQ answer, or any reply where the customer hasn't raised the topic themselves. Only bring it up when the customer explicitly asks to speak with a representative, asks to be transferred, or asks about calling/WhatsApp-ing/contacting NAWASA directly. In that specific case: if the office is CLOSED, tell them plainly that no one will be able to answer the phone or reply on WhatsApp right now, let them know when the office reopens, and offer to log their issue (or take their message) so a representative can follow up as soon as the office is open again — don't just hand them the phone number or WhatsApp link as if someone will answer immediately. If the office is OPEN, you can direct them to call or WhatsApp normally. Logging a report (via log_water_report) works identically regardless of business hours, so don't bring up office hours just because you're about to log something.
- NAWASA's main office is now located on Lucas Street, St. George's (it moved from its former, over 150-year-old building on the Carenage). Sub-offices are located at Seaton James Street, Grenville; Lower Depradine Street, Gouyave; and additional sub-offices in Sauteurs, St. David's, and Grand Anse.
- When a customer describes a specific problem and gives at least a location, log it immediately using the log_water_report tool — do not tell the customer to fill out a separate form themselves. Before logging, if a key detail is genuinely ambiguous or missing (e.g. the location is too vague to act on, or the issue type isn't clear), ask one quick clarifying question rather than guessing — but don't add a confirmation round-trip for details that are already clear. Once logged, always state back the key details you captured (issue type, location, severity) alongside the reference number in the same reply, so the customer can immediately flag anything that's wrong.
- When a customer asks to check, track, or get an update on a report — or gives you a reference number (e.g. "NW-9911D93") — call the check_report_status tool with that reference number and answer using exactly what it returns. Never guess, assume, or make up a status. If the tool reports no report was found, tell the customer that plainly and ask them to double-check the reference number. Checking a report's status works identically regardless of business hours — a closed office does not mean the status can't be looked up, so don't bring up office hours just because you're checking a report.
- When a customer reports a visible physical issue (a leak, burst main, damaged hydrant, water quality concern, etc.), ask them to send a photo of it via the attachment (📎) button in the chat box. This helps our technicians assess severity and prepare before visiting. Ask for this naturally as part of your reply — don't make it a precondition for logging the report, and don't ask for a photo for issues that wouldn't have one (e.g. billing questions or no water supply with nothing to see).
- If the customer attaches a photo of the issue, look at it before calling log_water_report and set severity based on what you actually see.
- Use natural understanding, not keyword matching.
- If a customer shares their GPS location (a message like "My current location is [parish], Grenada (GPS: lat, lng)"), treat that parish as their location for the rest of the conversation — use it for outage/service questions and when logging a report, without asking them to repeat it.
- Customers may send a voice note (spoken audio) or a short video instead of typing or a photo. Always listen to / watch the attachment directly and treat what you actually hear or see as their real message — respond to its actual content (what they said, what the leak or damage looks like, etc.). Never reply with a generic "thanks for the recording" acknowledgement, and never ask them to type it out instead — if for some reason the audio or video truly can't be made out, say so plainly and ask them to also type a quick summary, rather than pretending you understood it.

If a question is unrelated to NAWASA services, politely explain that you can only assist with NAWASA-related topics and invite the user to ask another water service question.
"""


# Initialize the database (creates tables + seeds default tips/features/FAQs
# on first run — safe to call every time the app starts).
db.init_db()
db._seed_faqs_if_empty(FAQS)


def _reseed_knowledge_base():
    """Re-syncs Pinecone with whatever is currently enabled in the `faqs`
    table. Call this after any staff create/update/delete on /api/faqs so
    the bot's RAG retrieval never drifts from what the customer-facing FAQ
    tab is showing."""
    agent.seed_knowledge_base(db.load_faqs(include_disabled=False), force=True)


agent.seed_knowledge_base(db.load_faqs(include_disabled=False))

SESSIONS = {}  # session_id -> {"graph": compiled LangGraph agent, "territory": str}
LAST_REPORT = {}
CURRENT_ATTACHMENT = {}


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
        att = CURRENT_ATTACHMENT.get(session_id) or {}
        row = save_report(
            name, phone, location, issue_type, description,
            attachment_mime=att.get("mime", ""), attachment_data=att.get("data_base64", ""),
            severity=severity,
        )
        LAST_REPORT[session_id] = {
            "reference": row["reference"], "status": "Received",
            "issue_type": issue_type, "severity": severity,
            "attachment_mime": att.get("mime", ""),
        }
        return f"Report logged successfully. Reference number: {row['reference']}. A technician will follow up."
    return tool(log_water_report, parse_docstring=True)


def _make_check_status_tool(session_id):
    def check_report_status(reference: str) -> str:
        """Looks up the current status of a previously submitted water service
        report by its reference number, so the customer can be told exactly
        where things stand. Call this whenever the customer asks to check,
        track, or get an update on a report — including when they give you
        a reference number (e.g. "NW-9911D93") after you asked for one.

        Args:
            reference: The report reference number the customer gave you,
                e.g. "NW-9911D93". Matching is case-insensitive.

        Returns:
            The report's current status and details if found, or a message
            saying no report was found with that reference number.
        """
        row = track_report(reference)
        if row is None:
            return (f"No report was found with reference number {reference}. "
                     "There is no lookup by name or phone number available — ask "
                     "the customer to double check the reference number, or offer "
                     "to log a fresh report if they can't locate it.")
        return (f"Report {row['reference']}: status is '{row['status']}', "
                f"issue type '{row['issue_type']}', severity '{row['severity']}', "
                f"logged on {row['timestamp']} at location: {row['location']}. "
                f"Description: {row['description'] or 'none provided'}.")
    return tool(check_report_status, parse_docstring=True)


def _make_check_outages_tool(session_id):
    def check_active_outages(parish: str) -> str:
        """Looks up any currently active, staff-posted service notices (planned
        maintenance, outages, or interruptions) for a specific parish/territory,
        so the customer gets a real, current answer instead of a guess. Call
        this whenever a customer asks about outages, low pressure, "no water",
        or scheduled maintenance in their area — including when they've already
        shared their parish via GPS location earlier in the conversation.

        Args:
            parish: The parish or territory to check, e.g. "St. George's
                (Capital area)" or "Carriacou and Petite Martinique". Use the
                customer's stated or shared location — do not guess one.

        Returns:
            A description of any active notices for that parish, or a message
            confirming there are none currently posted.
        """
        parish = _normalize_parish(parish)
        active = get_active_outages_for_parish(parish)
        if not active:
            return (f"No active service notices are currently posted for {parish}. "
                     "Tell the customer service in their area is not currently "
                     "reported as interrupted, and that they're welcome to report "
                     "a specific issue if they're experiencing one.")
        lines = [f"- {o['message']} (in effect {o['start_date']} to {o['end_date']})" for o in active]
        return f"Active service notice(s) for {parish}:\n" + "\n".join(lines)
    return tool(check_active_outages, parse_docstring=True)


def _make_request_handoff_tool(session_id, territory):
    def request_human_handoff(reason: str) -> str:
        """Flags this conversation so a live NAWASA staff member is alerted and
        can join in and reply directly, right here in the same chat. Call this
        whenever the customer explicitly asks to speak with a person, agent, or
        representative, or whenever you genuinely can't help them yourself (the
        knowledge base has no matching answer and they're still stuck, or their
        situation needs a judgment call or account access you don't have).
        Don't call this for questions you can actually answer — try the
        knowledge base and your other tools first.

        Args:
            reason: A short internal note for staff explaining why this
                customer needs a person, e.g. "Disputes an estimated bill,
                wants a manual review" or "Asked for a representative twice."

        Returns:
            Confirmation that staff have been notified, for you to relay to
            the customer in your own words.
        """
        db.create_handoff_request(session_id, territory, reason)
        return ("Staff have been notified in the Live Chat monitor and will join this "
                "conversation as soon as possible. Tell the customer a NAWASA representative "
                "has been alerted and will follow up here — and if it sounds urgent and the "
                "office is currently open, you can also suggest they call or WhatsApp directly.")
    return tool(request_human_handoff, parse_docstring=True)


def _get_or_create_agent(session_id, territory):
    """Returns a compiled LangGraph agent for this session, rebuilding it if
    the territory changed (the system prompt is territory-specific). Chat
    history itself is NOT lost on rebuild — that lives in agent.py's shared
    checkpointer, keyed by session_id (used as the LangGraph thread_id), not
    in this per-session graph object."""
    sess = SESSIONS.get(session_id)
    if sess is None or sess["territory"] != territory:
        tools = [
            _make_log_tool(session_id),
            _make_check_status_tool(session_id),
            _make_check_outages_tool(session_id),
            _make_request_handoff_tool(session_id, territory),
            agent.make_search_knowledge_base_tool(
                on_no_match=lambda q: db.log_unanswered_question(q, session_id=session_id)
            ),
        ]
        graph = agent.build_agent(tools, build_system_instruction(territory))
        sess = {"graph": graph, "territory": territory}
        SESSIONS[session_id] = sess
    return sess["graph"]


def require_staff(fn):
    """Any valid staff passcode (any role) may reach routes using this
    decorator — same behavior as before role-based auth was added. Use
    @require_permission("website"/"aquaassist"/"reports") instead on routes
    that should be restricted to a specific role."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        supplied = request.headers.get("X-Staff-Passcode", "")
        role = _role_for_passcode(supplied)
        if role is None:
            return jsonify({"error": "Invalid or missing staff passcode."}), 401
        request.staff_role = role
        return fn(*args, **kwargs)
    return wrapper


def require_permission(area):
    """area is one of "website", "aquaassist", "reports"."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            supplied = request.headers.get("X-Staff-Passcode", "")
            role = _role_for_passcode(supplied)
            if role is None:
                return jsonify({"error": "Invalid or missing staff passcode."}), 401
            if area not in ROLE_PERMISSIONS.get(role, set()):
                return jsonify({"error": "Your role doesn't have access to this."}), 403
            request.staff_role = role
            return fn(*args, **kwargs)
        return wrapper
    return decorator


@app.route("/")
def serve_index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/admin")
def serve_admin():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:filename>")
def serve_static(filename):
    return send_from_directory(FRONTEND_DIR, filename)


@app.route("/api/init")
def api_init():
    return jsonify({
        "territories": TERRITORIES,
        "territory_whatsapp": TERRITORY_WHATSAPP,
        "parishes": GRENADA_PARISHES,
        "parish_centers": PARISH_CENTERS,
        "parish_reference_points": PARISH_REFERENCE_POINTS,
        "grenada_center": GRENADA_CENTER,
        "issue_types": ISSUE_TYPES,
        "severity_levels": SEVERITY_LEVELS,
        "status_stages": STATUS_STAGES,
        "faqs": [{"category": f["category"], "q": f["q"], "a": f["a"]} for f in db.load_faqs(include_disabled=False)],
        "business_hours": get_business_hours_status(),
        "nawasa_phone": NAWASA_PHONE,
        "nawasa_website": NAWASA_WEBSITE,
        "gemini_configured": bool(GEMINI_API_KEY),
        "tts_configured": bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID),
        "rag_configured": bool(agent.PINECONE_API_KEY),
    })


@app.route("/api/business-hours")
def api_business_hours():
    return jsonify(get_business_hours_status())


@app.route("/api/ping")
def api_ping():
    return jsonify({"ok": True, "time": datetime.now(timezone.utc).isoformat()})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.get_json(force=True)
    session_id = body.get("session_id") or str(uuid.uuid4())

    features = load_features()
    if not features.get("chatbot_available", True):
        return jsonify({
            "session_id": session_id,
            "reply": features.get("maintenance_message", db.DEFAULT_MAINTENANCE_MESSAGE),
        })

    if not GEMINI_API_KEY:
        return jsonify({"error": "Server is missing GEMINI_API_KEY."}), 503

    territory = body.get("territory", "Grenada")
    message = (body.get("message") or "").strip()
    attachments = body.get("attachments") or []

    if not message and not attachments:
        return jsonify({"error": "Empty message."}), 400

    # Log the customer's turn to the transcript immediately, so it shows up
    # in the Staff Portal's Live Chat monitor even if the agent call below
    # ends up failing.
    transcript_text = message if message else "[sent an attachment]"
    db.log_chat_message(session_id, territory, "user", transcript_text)

    graph = _get_or_create_agent(session_id, territory)
    LAST_REPORT.pop(session_id, None)

    content_blocks = []
    if message:
        content_blocks.append({"type": "text", "text": message})

    bh = get_business_hours_status()
    if bh["is_open"]:
        bh_note = "CURRENT BUSINESS HOURS STATUS: Office OPEN — phone and WhatsApp are staffed right now."
    else:
        bh_note = (f"CURRENT BUSINESS HOURS STATUS: Office CLOSED ({bh['closed_reason']}). "
                   f"Phone and WhatsApp will NOT be answered until the office reopens {bh['reopens_label']}.")
    content_blocks.append({"type": "text", "text": f"[{bh_note}]"})

    CURRENT_ATTACHMENT.pop(session_id, None)
    has_media = False
    for att in attachments:
        try:
            raw = base64.b64decode(att["data_base64"])
        except Exception:
            continue
        mime = att.get("mime", "application/octet-stream")
        CURRENT_ATTACHMENT[session_id] = {"mime": mime, "data_base64": att["data_base64"]}
        send_bytes, send_mime = _normalize_media_for_gemini(raw, mime)
        if send_mime.split("/")[0] in ("audio", "video"):
            has_media = True
        content_blocks.append({"type": "media", "mime_type": send_mime, "data": send_bytes})

    try:
        reply_text = agent.invoke_agent(graph, session_id, content_blocks)
        if not reply_text or not str(reply_text).strip():
            raise ValueError("Empty reply from agent")
        db.log_chat_event(session_id, territory, had_error=False)
    except Exception as e:
        logger.error("Agent invocation failed for session %s: %s", session_id, e)
        if has_media:
            reply_text = ("I wasn't able to process that recording — it may not have come through "
                          "correctly. Could you try sending it again, use a shorter clip, or type a "
                          "quick summary instead?")
            db.log_chat_event(session_id, territory, had_error=True)
        else:
            db.log_chat_event(session_id, territory, had_error=True)
            return jsonify({"error": "I'm having trouble connecting right now. Please try again in a "
                                      "moment, or call/WhatsApp NAWASA directly if it's urgent."}), 502

    db.log_chat_message(session_id, territory, "assistant", reply_text)

    result = {"session_id": session_id, "reply": reply_text}
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

    row = save_report(
        body.get("name"), body.get("phone"), body.get("location"),
        body.get("issue_type"), body.get("description", ""),
        attachment_mime=body.get("attachment_mime", ""),
        attachment_data=body.get("attachment_base64", ""),
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
@require_permission("reports")
def api_list_reports():
    return jsonify(load_reports())


@app.route("/api/reports/<reference>", methods=["PATCH"])
@require_permission("reports")
def api_update_report(reference):
    body = request.get_json(force=True)
    new_status = body.get("status")
    if new_status not in STATUS_STAGES:
        return jsonify({"error": "Invalid status."}), 400
    if not update_report_status(reference, new_status):
        return jsonify({"error": "Reference not found."}), 404
    return jsonify({"reference": reference, "status": new_status})


@app.route("/api/reports/<reference>", methods=["DELETE"])
@require_permission("reports")
def api_delete_report(reference):
    if not delete_report(reference):
        return jsonify({"error": "Reference not found."}), 404
    return jsonify({"deleted": reference})


@app.route("/api/outages", methods=["GET", "POST"])
def api_outages():
    if request.method == "GET":
        return jsonify(load_outages())
    role = _role_for_passcode(request.headers.get("X-Staff-Passcode", ""))
    if role is None or "website" not in ROLE_PERMISSIONS.get(role, set()):
        return jsonify({"error": "Invalid or missing staff passcode."}), 401
    body = request.get_json(force=True)
    required = ["parish", "message", "start_date", "end_date"]
    if any(not body.get(f) for f in required):
        return jsonify({"error": "parish, message, start_date, and end_date are required."}), 400
    row = save_outage(body["parish"], body["message"], body["start_date"], body["end_date"])
    return jsonify(row)


@app.route("/api/outages/<outage_id>", methods=["DELETE"])
@require_permission("website")
def api_delete_outage(outage_id):
    delete_outage(outage_id)
    return jsonify({"deleted": outage_id})


@app.route("/api/notify", methods=["GET", "POST"])
def api_notify():
    if request.method == "GET":
        role = _role_for_passcode(request.headers.get("X-Staff-Passcode", ""))
        if role is None or "reports" not in ROLE_PERMISSIONS.get(role, set()):
            return jsonify({"error": "Invalid or missing staff passcode."}), 401
        return jsonify(load_notifications())
    body = request.get_json(force=True)
    contact = body.get("contact", "").strip()
    categories = body.get("categories") or []
    if not contact or not categories:
        return jsonify({"error": "contact and at least one category are required."}), 400
    save_notification_signup(contact, categories)
    return jsonify({"ok": True})


@app.route("/api/tips", methods=["GET"])
def api_tips_list():
    tips = [t for t in load_tips() if t["enabled"]]
    return jsonify([{"id": t["id"], "text": t["text"]} for t in tips])


@app.route("/api/tips", methods=["POST"])
@require_permission("website")
def api_tips_create():
    body = request.get_json(force=True)
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Tip text is required."}), 400
    return jsonify(save_tip(text))


@app.route("/api/tips/all")
@require_permission("website")
def api_tips_all():
    return jsonify(load_tips())


@app.route("/api/tips/<tip_id>", methods=["PATCH"])
@require_permission("website")
def api_tips_update(tip_id):
    body = request.get_json(force=True) or {}
    text = body.get("text")
    enabled = body.get("enabled")
    if text is not None and not text.strip():
        return jsonify({"error": "Tip text cannot be empty."}), 400
    row = update_tip(tip_id, text=text.strip() if text is not None else None, enabled=enabled)
    if row is None:
        return jsonify({"error": "Tip not found."}), 404
    return jsonify(row)


@app.route("/api/tips/<tip_id>", methods=["DELETE"])
@require_permission("website")
def api_tips_delete(tip_id):
    if not delete_tip(tip_id):
        return jsonify({"error": "Tip not found."}), 404
    return jsonify({"deleted": tip_id})


@app.route("/api/features", methods=["GET"])
def api_features_get():
    return jsonify(load_features())


@app.route("/api/features", methods=["PATCH"])
@require_permission("aquaassist")
def api_features_update():
    body = request.get_json(force=True) or {}
    return jsonify(save_features(body))


@app.route("/api/staff/login", methods=["POST"])
def api_staff_login():
    body = request.get_json(force=True)
    role = _role_for_passcode(body.get("passcode"))
    return jsonify({"ok": role is not None, "role": role}), (200 if role else 401)


@app.route("/api/tts", methods=["POST"])
def api_tts():
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        return jsonify({"error": "Text-to-speech is not configured on this server."}), 503

    body = request.get_json(force=True)
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text provided."}), 400

    text = text[:2000]

    try:
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": ELEVENLABS_MODEL_ID,
                "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.3},
            },
            timeout=30,
        )
    except requests.RequestException as e:
        logger.error("ElevenLabs TTS request failed: %s", e)
        return jsonify({"error": "Text-to-speech request failed."}), 502

    if resp.status_code != 200:
        return jsonify({"error": f"TTS generation failed ({resp.status_code})."}), 502

    return resp.content, 200, {"Content-Type": "audio/mpeg"}


# =======================================================================
# NEW: Knowledge base (FAQ) admin endpoints
# =======================================================================
@app.route("/api/faqs", methods=["GET"])
@require_permission("aquaassist")
def api_faqs_list():
    return jsonify(db.load_faqs(include_disabled=True))


@app.route("/api/faqs", methods=["POST"])
@require_permission("aquaassist")
def api_faqs_create():
    body = request.get_json(force=True)
    category = (body.get("category") or "").strip()
    question = (body.get("q") or "").strip()
    answer = (body.get("a") or "").strip()
    if not category or not question or not answer:
        return jsonify({"error": "category, q, and a are required."}), 400
    row = db.save_faq(category, question, answer)
    _reseed_knowledge_base()
    return jsonify(row)


@app.route("/api/faqs/<faq_id>", methods=["PATCH"])
@require_permission("aquaassist")
def api_faqs_update(faq_id):
    body = request.get_json(force=True) or {}
    row = db.update_faq(faq_id, category=body.get("category"), question=body.get("q"),
                         answer=body.get("a"), enabled=body.get("enabled"))
    if row is None:
        return jsonify({"error": "FAQ not found."}), 404
    _reseed_knowledge_base()
    return jsonify(row)


@app.route("/api/faqs/<faq_id>", methods=["DELETE"])
@require_permission("aquaassist")
def api_faqs_delete(faq_id):
    if not db.delete_faq(faq_id):
        return jsonify({"error": "FAQ not found."}), 404
    _reseed_knowledge_base()
    return jsonify({"deleted": faq_id})


# =======================================================================
# NEW: Unanswered questions
# =======================================================================
@app.route("/api/unanswered", methods=["GET"])
@require_permission("aquaassist")
def api_unanswered_list():
    include_resolved = request.args.get("include_resolved") == "1"
    return jsonify(db.load_unanswered_questions(include_resolved=include_resolved))


@app.route("/api/unanswered/<q_id>", methods=["PATCH"])
@require_permission("aquaassist")
def api_unanswered_resolve(q_id):
    body = request.get_json(force=True) or {}
    staff_answer = (body.get("staff_answer") or "").strip()
    if not db.resolve_unanswered_question(q_id, staff_answer=staff_answer):
        return jsonify({"error": "Question not found."}), 404
    if body.get("add_to_faq") and staff_answer:
        db.save_faq(body.get("category") or "General", body.get("question_text") or "", staff_answer)
        _reseed_knowledge_base()
    return jsonify({"resolved": q_id})


@app.route("/api/unanswered/<q_id>", methods=["DELETE"])
@require_permission("aquaassist")
def api_unanswered_delete(q_id):
    if not db.delete_unanswered_question(q_id):
        return jsonify({"error": "Question not found."}), 404
    return jsonify({"deleted": q_id})


# =======================================================================
# NEW: Chat stats (staff Overview panel)
# =======================================================================
@app.route("/api/chat-stats")
@require_permission("aquaassist")
def api_chat_stats():
    return jsonify(db.get_chat_stats_today())


# =======================================================================
# NEW: Live Chat monitor — staff can watch conversations as they happen
# and optionally drop a message in, without turning the bot off. The bot
# keeps answering normally; a staff message is just another turn in the
# same transcript, and the model is told about it via the LangGraph
# checkpointer so its next reply is aware a human already responded.
# =======================================================================
@app.route("/api/sessions")
@require_permission("aquaassist")
def api_sessions_list():
    return jsonify(db.load_recent_sessions())


@app.route("/api/sessions/<session_id>/messages")
@require_permission("aquaassist")
def api_session_messages(session_id):
    return jsonify(db.load_session_messages(session_id))


@app.route("/api/sessions/<session_id>/staff-message", methods=["POST"])
@require_permission("aquaassist")
def api_session_staff_message(session_id):
    body = request.get_json(force=True) or {}
    text = (body.get("message") or "").strip()
    if not text:
        return jsonify({"error": "Message text is required."}), 400

    sess = SESSIONS.get(session_id)
    territory = sess["territory"] if sess else "Grenada"

    db.log_chat_message(session_id, territory, "staff", text)
    # A human has now stepped into this conversation — clear any open
    # handoff flag for it so it drops off the notification badge/list.
    db.resolve_handoff_for_session(session_id)

    # Make sure the agent's own memory of this conversation includes what
    # the staff member just said, so if the customer keeps chatting the bot
    # doesn't contradict or repeat what a human already told them. If the
    # session hasn't started an agent graph yet (never chatted with the
    # bot), there's nothing to update — the message still landed in the
    # transcript above and the widget poller will still deliver it.
    if sess is not None:
        try:
            sess["graph"].update_state(
                {"configurable": {"thread_id": session_id}},
                {"messages": [{"role": "assistant", "content": f"[NAWASA support staff]: {text}"}]},
            )
        except Exception as e:
            logger.warning("Could not sync staff message into agent memory for %s: %s", session_id, e)

    return jsonify({"ok": True})


@app.route("/api/chat/<session_id>/updates")
def api_chat_updates(session_id):
    """Public, unauthenticated polling endpoint for the customer widget —
    only ever returns 'staff' role messages (never user/assistant, which
    the widget already has locally), and only those newer than `after`.
    session_id functions as a bearer token here, the same trust model the
    existing /api/report/<reference> lookup already relies on."""
    try:
        after_id = int(request.args.get("after", 0))
    except ValueError:
        after_id = 0
    return jsonify(db.load_new_staff_messages(session_id, after_id=after_id))


# =======================================================================
# NEW: Live-agent handoff requests — powers the Live Chat monitor's
# notification badge and the Dashboard's "Needs a human" metric. Created
# by the bot's request_human_handoff tool above; cleared automatically the
# moment a staff member replies into that session (see
# api_session_staff_message).
# =======================================================================
@app.route("/api/handoffs")
@require_permission("aquaassist")
def api_handoffs_list():
    return jsonify(db.load_open_handoffs())


# ---------------------------------------------------------------------
# Keep-alive
# ---------------------------------------------------------------------
SELF_PING_URL = os.environ.get("SELF_PING_URL", "").strip()
SELF_PING_INTERVAL_SECONDS = int(os.environ.get("SELF_PING_INTERVAL_SECONDS", "600"))


def _keep_alive_loop():
    while True:
        time.sleep(SELF_PING_INTERVAL_SECONDS)
        try:
            db.load_features()
        except Exception as e:
            logger.warning("Keep-alive DB touch failed: %s", e)
        if SELF_PING_URL:
            try:
                requests.get(f"{SELF_PING_URL.rstrip('/')}/api/ping", timeout=15)
            except Exception as e:
                logger.warning("Keep-alive self-ping failed: %s", e)
        else:
            logger.info("SELF_PING_URL not set — only the database is being kept warm, "
                        "not the web service itself. Set SELF_PING_URL to your deployed "
                        "URL to keep Render's free web service from sleeping too.")


threading.Thread(target=_keep_alive_loop, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
