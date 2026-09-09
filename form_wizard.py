"""
form_wizard.py — the "Fill It Out With Me" guided form-filling engine.

This is additive to the existing Forms feature (db.py's `forms` table,
app.py's /api/forms* routes, the customer-facing Forms tab) — it does not
replace or restructure any of that. It adds a separate, session-scoped
question-by-question wizard that ends in a generated PDF.

Architecture:
    FORM_SCHEMAS[form_id] -> ordered list of Question objects (typed,
    with optional conditions referencing earlier answers by id)
        -> a per-chat-session WizardSession walks that list, skipping
           any question whose condition() says it doesn't apply yet
        -> on completion, _fill_pdf() downloads the real official PDF
           and produces a completed one (see the docstring on _fill_pdf
           for exactly what "completed" means and its honest limits)

Session state is in-memory only (module-level dict keyed by the chat's
session_id), same pattern as app.py's existing SESSIONS/LAST_REPORT/
CURRENT_ATTACHMENT dicts — it's lost on a server restart, and is NOT
shared across different session_ids, so one customer's in-progress
answers are never visible to another (see WizardSession / the token-
gated download in generate_pdf).

IMPORTANT, STATED PLAINLY: this module has never been run against the
real NAWASA PDFs — this sandbox has no network access to nawasa.gd to
fetch and inspect them. Two things follow from that:
  1. The question schemas below are built from the field NAMES you gave
     in your spec (Applicant Name, Alias, Billing Address, Email, Phone,
     Services Requested, etc.) for the Water Service Application form,
     and from the official form TITLES/descriptions for the other four —
     they are a strong best-effort, not a verified transcription of the
     other four forms' exact fields. Adjust FORM_SCHEMAS below once
     you've checked each PDF's actual questions.
  2. _fill_pdf tries proper AcroForm field-filling first (if the real PDF
     has fillable form fields — common for official government PDFs). If
     it doesn't, or if none of its field names can be confidently matched
     to a question, this DOES NOT guess x/y coordinates and draw text on
     top of the form (that risks producing a garbled, unprofessional
     official document). Instead it appends a clearly-labeled, cleanly
     typeset summary page of the customer's exact answers to the end of
     the real official PDF, so the output always contains the original
     form untouched plus a legible, honest record of what was collected.
     Swap in real coordinates under Question.overlay once verified
     against the live PDF to get true in-field overlay instead.
"""

import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import pdf_ingest

logger = logging.getLogger("aquaassist.form_wizard")

OUTPUT_DIR = Path("/tmp/aquaassist_generated")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_TOKEN_TTL_SECONDS = 60 * 60  # 1 hour


# ---------------------------------------------------------------------
# Question schema
# ---------------------------------------------------------------------
@dataclass
class Question:
    id: str
    prompt: str
    type: str  # "checkbox" | "radio" | "text" | "email" | "phone" | "date" | "textarea"
    options: Optional[list] = None          # for checkbox/radio
    required: bool = True
    skip_reason: str = ""                   # shown if customer tries to skip a required question
    help: str = ""                          # optional plain-language hint shown under the prompt —
                                             # for anything a customer might not immediately understand
                                             # (technical terms, form jargon, ambiguous choices)
    condition: Optional[Callable[[dict], bool]] = None   # answers -> bool; None = always shown


def _selected(answers, question_id, option):
    val = answers.get(question_id)
    return isinstance(val, list) and option in val


FORM_SCHEMAS = {
    "form-water-service-application": {
        "display_name": "Private Water Service Application",
        "filename_base": "Completed_NAWASA_Water_Connection_Application",
        "signature_note": "Remember to add your signature in Section G before submitting the form.",
        "questions": [
            Question(
                id="services", type="checkbox", required=True,
                prompt="Which service(s) do you need? You can pick more than one, then Continue.",
                options=["New Water Connection", "Additional Meter", "Temporary Water Connection", "Sewer Connection"],
            ),
            Question(id="full_name", type="text", required=True, prompt="What's your full name?"),
            Question(
                id="alias", type="text", required=False,
                prompt="Do you go by any other name (an alias)? You can skip this if not.",
            ),
            Question(
                id="is_owner", type="radio", required=True,
                prompt="Are you the property owner?", options=["Yes", "No"],
            ),
            Question(
                id="owner_id_type", type="text", required=True,
                prompt="What type of ID will you provide (e.g. Driver's Licence, Passport, National ID)?",
                condition=lambda a: a.get("is_owner") == "Yes",
            ),
            Question(
                id="permission_granted", type="radio", required=True,
                prompt="Has the property owner given you written permission to apply?", options=["Yes", "No"],
                condition=lambda a: a.get("is_owner") == "No",
            ),
            Question(
                id="deposit_ack", type="radio", required=True,
                prompt=("A security deposit applies for applicants without proof of ownership "
                        "($240 Domestic / $340 Commercial / $2,000 Projects, refundable). Do you "
                        "understand and agree to this deposit requirement?"),
                options=["Yes", "No"],
                condition=lambda a: a.get("is_owner") == "No",
            ),
            Question(id="billing_address", type="textarea", required=True, prompt="What's your billing address?"),
            Question(
                id="property_address", type="textarea", required=False,
                prompt=("What's the address where the service is needed, if different from your billing "
                        "address? You can skip this if it's the same."),
            ),
            Question(id="email", type="email", required=True, prompt="What's your email address?"),
            Question(id="phone", type="phone", required=True, prompt="What's the best phone number to reach you?"),
            Question(
                id="connection_size", type="radio", required=True,
                prompt="What size connection do you need?",
                help=("This is the diameter of the pipe connecting your property to NAWASA's main line — "
                      "not something most customers know off-hand. Most residential homes use ½\" or ¾\". "
                      "Larger sizes (1\" and up) are typically for commercial properties or high-volume use. "
                      "If you're not sure, choose ½\" — NAWASA confirms the correct size during their site "
                      "assessment, so an early guess here won't lock you into anything or affect your cost."),
                options=["½\"", "¾\"", "1\"", "1¼\"/1½\"/2\"", "4\""],
                condition=lambda a: _selected(a, "services", "New Water Connection") or _selected(a, "services", "Additional Meter"),
            ),
            Question(id="application_date", type="date", required=True, prompt="What is the application date?"),
        ],
    },
    "form-cancellation": {
        "display_name": "Private Water Service Cancellation Form",
        "filename_base": "Completed_NAWASA_Cancellation_Form",
        "signature_note": "Remember to add your signature before submitting the form.",
        "questions": [
            Question(id="reference_or_account", type="text", required=True,
                     prompt="What's your NAWASA account number or application reference number?"),
            Question(id="applicant_name", type="text", required=True, prompt="What's your full name?"),
            Question(id="cancellation_reason", type="textarea", required=True,
                     prompt="Please briefly explain why you'd like to cancel this application."),
            Question(id="phone", type="phone", required=True, prompt="What's the best phone number to reach you?"),
            Question(id="email", type="email", required=False,
                     prompt="What's your email address? You can skip this if you'd rather not provide one."),
            Question(id="cancellation_date", type="date", required=True, prompt="What is today's date?"),
        ],
    },
    "form-no-proof-of-ownership": {
        "display_name": "Customer Without Proof of Ownership Agreement",
        "filename_base": "Completed_NAWASA_No_Proof_Of_Ownership_Agreement",
        "signature_note": "Remember to add your signature before submitting the form.",
        "questions": [
            Question(id="applicant_name", type="text", required=True, prompt="What's your full name?"),
            Question(id="property_address", type="textarea", required=True,
                     prompt="What's the address of the property needing water service?"),
            Question(id="relationship_to_property", type="text", required=True,
                     prompt="What is your relationship to this property (e.g. tenant, occupant, family member)?"),
            Question(id="deposit_ack", type="radio", required=True,
                     prompt="This agreement requires a refundable security deposit. Do you understand and agree?",
                     options=["Yes", "No"]),
            Question(id="phone", type="phone", required=True, prompt="What's the best phone number to reach you?"),
            Question(id="email", type="email", required=False,
                     prompt="What's your email address? You can skip this if you'd rather not provide one."),
        ],
    },
    "form-declaration-of-ownership": {
        "display_name": "Declaration of Ownership",
        "filename_base": "Completed_NAWASA_Declaration_Of_Ownership",
        "signature_note": "Remember to add your signature before submitting the form.",
        "questions": [
            Question(id="declarant_name", type="text", required=True, prompt="What's your full name?"),
            Question(id="property_address", type="textarea", required=True, prompt="What's the address of the land?"),
            Question(id="basis_of_ownership", type="textarea", required=True,
                     prompt=("Please briefly describe how you came to own this land (e.g. inherited, "
                             "purchased, gifted) in the absence of a Deed of Conveyance.")),
            Question(id="witness_name", type="text", required=False,
                     prompt="Do you have a witness who can support this declaration? If so, what's their name? You can skip this if not."),
            Question(id="phone", type="phone", required=True, prompt="What's the best phone number to reach you?"),
            Question(id="declaration_date", type="date", required=True, prompt="What is today's date?"),
        ],
    },
    "form-permission-in-support": {
        "display_name": "Permission in Support of Application for Water Connection",
        "filename_base": "Completed_NAWASA_Permission_In_Support",
        "signature_note": "Remember that the property owner must sign this form before it's submitted.",
        "questions": [
            Question(id="owner_name", type="text", required=True,
                     prompt="What's the property owner's full name (the person granting permission)?"),
            Question(id="owner_phone", type="phone", required=True, prompt="What's the property owner's phone number?"),
            Question(id="applicant_name", type="text", required=True,
                     prompt="What's the applicant's full name (the person receiving permission)?"),
            Question(id="property_address", type="textarea", required=True, prompt="What's the property address?"),
            Question(id="permission_scope", type="textarea", required=True,
                     prompt=("Briefly describe what the owner is giving permission for (e.g. installing "
                             "a water connection at this property).")),
            Question(id="permission_date", type="date", required=True, prompt="What is today's date?"),
        ],
    },
}


def get_schema(form_id):
    return FORM_SCHEMAS.get(form_id)


def _visible_questions(schema, answers):
    return [q for q in schema["questions"] if q.condition is None or q.condition(answers)]


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def validate_answer(question, raw_value):
    """Returns (cleaned_value, error_message_or_None)."""
    if question.type == "checkbox":
        value = raw_value if isinstance(raw_value, list) else []
        value = [v for v in value if v in (question.options or [])]
        if question.required and not value:
            return None, "Please select at least one option."
        return value, None

    if question.type == "radio":
        value = raw_value if isinstance(raw_value, str) else ""
        if question.required and value not in (question.options or []):
            return None, "Please choose one of the options shown."
        return value, None

    if question.type == "date":
        value = (raw_value or "").strip()
        if question.required and not value:
            return None, "Please choose a date."
        if value and not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            return None, "That doesn't look like a valid date (expected YYYY-MM-DD)."
        return value, None

    value = (raw_value or "").strip() if isinstance(raw_value, str) else ""
    if question.required and not value:
        return None, "This field is required."
    if not value:
        return "", None

    if question.type == "email" and not _EMAIL_RE.match(value):
        return None, "That doesn't appear to be a valid email address. Please check it and try again."
    if question.type == "phone":
        digit_count = len(re.sub(r"\D", "", value))
        if digit_count < 7:
            return None, "That doesn't look like a complete phone number. Please check it and try again."
    return value, None


# ---------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------
_SESSIONS = {}          # chat session_id -> WizardSession
_DOWNLOAD_TOKENS = {}   # token -> {"path": Path, "filename": str, "created_at": float}


@dataclass
class WizardSession:
    form_id: str
    answers: dict = field(default_factory=dict)
    history: list = field(default_factory=list)   # list of question ids answered/skipped, in order
    status: str = "in_progress"                    # in_progress | review | done | cancelled


def start(session_id, form_id):
    schema = get_schema(form_id)
    if schema is None:
        return None, "That form isn't set up for guided filling yet."
    _SESSIONS[session_id] = WizardSession(form_id=form_id)
    return _current_step(session_id), None


def _current_step(session_id):
    sess = _SESSIONS.get(session_id)
    if sess is None:
        return None
    schema = get_schema(sess.form_id)
    visible = _visible_questions(schema, sess.answers)
    total = len(visible)
    if sess.status == "cancelled":
        return {"status": "cancelled"}
    answered_ids = set(sess.history)
    remaining = [q for q in visible if q.id not in answered_ids]
    if not remaining:
        sess.status = "review"
        return _review_payload(session_id)
    q = remaining[0]
    step_number = total - len(remaining) + 1
    return {
        "status": "question",
        "form_id": sess.form_id,
        "form_display_name": schema["display_name"],
        "question": {
            "id": q.id, "prompt": q.prompt, "type": q.type,
            "options": q.options, "required": q.required, "help": q.help,
        },
        "can_go_back": len(sess.history) > 0,
        "step_number": step_number,
        "step_total": total,
        "current_value": sess.answers.get(q.id),
    }


def _find_question(schema, question_id):
    for q in schema["questions"]:
        if q.id == question_id:
            return q
    return None


def get_current(session_id):
    if session_id not in _SESSIONS:
        return None, "No form is currently in progress."
    return _current_step(session_id), None


def answer(session_id, question_id, value):
    sess = _SESSIONS.get(session_id)
    if sess is None:
        return None, "No form is currently in progress."
    schema = get_schema(sess.form_id)
    q = _find_question(schema, question_id)
    if q is None:
        return None, "That question isn't part of this form."
    cleaned, error = validate_answer(q, value)
    if error:
        return {"status": "question", "error": error, **{k: v for k, v in _current_step(session_id).items() if k != "status"}}, None
    sess.answers[question_id] = cleaned
    if question_id not in sess.history:
        sess.history.append(question_id)
    return _current_step(session_id), None


def skip(session_id):
    sess = _SESSIONS.get(session_id)
    if sess is None:
        return None, "No form is currently in progress."
    step = _current_step(session_id)
    if step.get("status") != "question":
        return step, None
    q_id = step["question"]["id"]
    schema = get_schema(sess.form_id)
    q = _find_question(schema, q_id)
    if q.required:
        return None, "This information is required to complete this section of the form."
    sess.answers[q_id] = [] if q.type == "checkbox" else ""
    sess.history.append(q_id)
    return _current_step(session_id), None


def go_back(session_id):
    sess = _SESSIONS.get(session_id)
    if sess is None:
        return None, "No form is currently in progress."
    if not sess.history:
        return _current_step(session_id), None
    last_id = sess.history.pop()
    sess.status = "in_progress"
    schema = get_schema(sess.form_id)
    q = _find_question(schema, last_id)
    step = _current_step(session_id)
    if step.get("status") == "question":
        step["current_value"] = sess.answers.get(q.id)
    return step, None


def cancel(session_id):
    _SESSIONS.pop(session_id, None)
    return {"status": "cancelled"}


def _review_payload(session_id):
    sess = _SESSIONS.get(session_id)
    schema = get_schema(sess.form_id)
    items = []
    for q in schema["questions"]:
        if q.id not in sess.answers:
            continue
        val = sess.answers[q.id]
        if q.type == "checkbox":
            display = ", ".join(val) if val else "(none selected)"
        else:
            display = val if val else "(skipped)"
        items.append({"id": q.id, "label": q.prompt, "value": display})
    return {
        "status": "review",
        "form_id": sess.form_id,
        "form_display_name": schema["display_name"],
        "items": items,
    }


def get_review(session_id):
    sess = _SESSIONS.get(session_id)
    if sess is None:
        return None, "No form is currently in progress."
    return _review_payload(session_id), None


def edit_field(session_id, question_id):
    """Jumps back to a specific question from the review screen (spec
    #22 'Edit Answers') while keeping every other already-given answer."""
    sess = _SESSIONS.get(session_id)
    if sess is None:
        return None, "No form is currently in progress."
    if question_id in sess.history:
        sess.history.remove(question_id)
    sess.status = "in_progress"
    step = _current_step(session_id)
    return step, None


# ---------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------
def _safe_filename(base):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", base) + ".pdf"


def _try_acroform_fill(reader, answers, schema):
    """Best-effort: if the real PDF has fillable AcroForm fields, fuzzy-
    match this form's question ids/prompts against the PDF's actual field
    names and fill whatever confidently matches. Returns (writer, matched
    count) — writer is a PdfWriter with the fields filled (possibly zero
    of them), never raises."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.append(reader)
    matched = 0
    try:
        pdf_fields = reader.get_fields() or {}
    except Exception:
        pdf_fields = {}

    if pdf_fields:
        def norm(s):
            return re.sub(r"[^a-z0-9]", "", (s or "").lower())

        field_lookup = {norm(name): name for name in pdf_fields.keys()}
        for q in schema["questions"]:
            if q.id not in answers:
                continue
            candidates = [q.id, q.prompt]
            value = answers[q.id]
            text_value = ", ".join(value) if isinstance(value, list) else str(value or "")
            for cand in candidates:
                key = norm(cand)
                for field_key, real_name in field_lookup.items():
                    if key and (key in field_key or field_key in key):
                        try:
                            for page in writer.pages:
                                writer.update_page_form_field_values(page, {real_name: text_value})
                            matched += 1
                        except Exception as e:
                            logger.warning("Could not fill PDF field %r: %s", real_name, e)
                        break
        try:
            writer.set_need_appearances_writer(True)
        except Exception:
            pass
    return writer, matched


def _append_summary_page(writer, schema, answers, signature_note):
    """Adds a clean, clearly-labeled page listing every answer, in the
    original question order. This runs ALWAYS (regardless of whether
    AcroForm filling found matching fields) so the customer's actual
    answers are guaranteed to be visibly present in the output — see the
    module docstring for why this doesn't attempt guessed-coordinate
    overlay onto the original form pages."""
    import io
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from pypdf import PdfReader

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    margin = 54
    y = height - margin

    def draw_wrapped(text, x, y, max_width_chars=95, font="Helvetica", size=10, leading=14):
        c.setFont(font, size)
        for line in re.findall(r".{1,%d}(?:\s+|$)" % max_width_chars, text) or [""]:
            if y < margin:
                c.showPage()
                c.setFont(font, size)
                y = height - margin
            c.drawString(x, y, line.strip())
            y -= leading
        return y

    c.setFont("Helvetica-Bold", 14)
    c.drawString(margin, y, f"AquaAssist — Information Provided for: {schema['display_name']}")
    y -= 22
    c.setFont("Helvetica", 9)
    c.drawString(margin, y, f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} via NAWASA's AquaAssist chatbot.")
    y -= 24

    for q in schema["questions"]:
        if q.id not in answers:
            continue
        val = answers[q.id]
        display = ", ".join(val) if isinstance(val, list) else (val or "(skipped)")
        y = draw_wrapped(q.prompt, margin, y, font="Helvetica-Bold", size=10, leading=13)
        y = draw_wrapped(str(display), margin + 14, y, font="Helvetica", size=10, leading=13)
        y -= 6

    y -= 10
    c.setFont("Helvetica-Oblique", 9)
    y = draw_wrapped(
        "This page was generated from information the customer provided in AquaAssist and is NOT "
        "part of NAWASA's original official form (attached above/before this page). " + signature_note,
        margin, y, max_width_chars=100, font="Helvetica-Oblique", size=9, leading=12,
    )
    c.showPage()
    c.save()
    buf.seek(0)

    summary_reader = PdfReader(buf)
    for page in summary_reader.pages:
        writer.add_page(page)
    return writer


def generate_pdf(session_id):
    """Returns (download_token, filename, warnings, error). error is None
    on success. warnings is a list of human-readable notes (e.g. "the
    official PDF doesn't appear to have fillable fields — your answers
    are included as a summary page instead")."""
    sess = _SESSIONS.get(session_id)
    if sess is None:
        return None, None, [], "No form is currently in progress."
    schema = get_schema(sess.form_id)
    warnings = []

    pdf_url = sess.answers.get("__pdf_url__")
    if not pdf_url:
        return None, None, [], "This form session is missing its official PDF URL — please restart the wizard."

    try:
        pdf_bytes, error = pdf_ingest.download_pdf(pdf_url)
        if error:
            logger.error("Form wizard: could not download official PDF %s: %s", pdf_url, error)
            return None, None, [], f"Couldn't download the official form to fill it in ({error})."
    except Exception as e:
        logger.error("Form wizard: could not download official PDF %s: %s", pdf_url, e)
        return None, None, [], f"Couldn't download the official form to fill it in ({e}). Please try again shortly."

    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        return None, None, [], f"Couldn't open the official PDF ({e})."

    writer, matched = _try_acroform_fill(reader, sess.answers, schema)
    if matched == 0:
        warnings.append(
            "The official PDF doesn't have fillable fields this system could confidently match, so your "
            "answers are included as a clearly labeled summary page instead of being typed directly onto "
            "the original form fields."
        )
    writer = _append_summary_page(writer, schema, sess.answers, schema["signature_note"])

    filename = _safe_filename(schema["filename_base"])
    token = secrets.token_urlsafe(24)
    out_path = OUTPUT_DIR / f"{token}.pdf"
    try:
        with open(out_path, "wb") as f:
            writer.write(f)
    except Exception as e:
        return None, None, [], f"Couldn't generate the completed PDF ({e})."

    _DOWNLOAD_TOKENS[token] = {"path": out_path, "filename": filename, "created_at": time.time()}
    sess.status = "done"
    _cleanup_expired_tokens()
    return token, filename, warnings, None


def get_download(token):
    entry = _DOWNLOAD_TOKENS.get(token)
    if entry is None:
        return None, None
    if time.time() - entry["created_at"] > DOWNLOAD_TOKEN_TTL_SECONDS:
        _DOWNLOAD_TOKENS.pop(token, None)
        return None, None
    return entry["path"], entry["filename"]


def _cleanup_expired_tokens():
    now = time.time()
    expired = [t for t, e in _DOWNLOAD_TOKENS.items() if now - e["created_at"] > DOWNLOAD_TOKEN_TTL_SECONDS]
    for t in expired:
        entry = _DOWNLOAD_TOKENS.pop(t, None)
        if entry:
            try:
                entry["path"].unlink(missing_ok=True)
            except Exception:
                pass


def set_pdf_url(session_id, url):
    sess = _SESSIONS.get(session_id)
    if sess is not None:
        sess.answers["__pdf_url__"] = url
