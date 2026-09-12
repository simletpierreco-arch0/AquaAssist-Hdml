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
import db

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
    # =====================================================================
    # Water Service Application Form — rebuilt directly from the uploaded
    # PDF's actual text (Sections A/B/C/D/E; Section F is informational
    # terms, Section G is the signature/declaration, and "FOR OFFICE USE
    # ONLY" is staff-only — none of those three have Question entries).
    # Corrections vs. the earlier guess-based schema: "Service Required"
    # is actually 3 checkboxes (Water / Additional Meter / Sewer) — there
    # is NO separate "Temporary" checkbox despite it being in the form's
    # title; "Nature of Building" is a 3-way radio (Domestic / Non-
    # Domestic / Industrial), not free text; ID type is a defined 4-option
    # radio with a conditional DL number; and Section B/C contains a full
    # 5-option (A–E) sub-declaration for applicants with no proof of
    # ownership, previously missing entirely from this schema.
    # =====================================================================
    "form-water-service-application": {
        "display_name": "Private Water Service Application",
        "filename_base": "Completed_NAWASA_Water_Connection_Application",
        "signature_note": "Remember to add your signature in Section G before submitting the form.",
        "questions": [
            # --- Section A: Applicant's Information ---
            Question(id="full_name", type="text", required=True, prompt="What's your full name (Applicant's Name)?"),
            Question(id="alias", type="text", required=False,
                     prompt="Do you go by any other known name/alias? You can skip this if not."),
            Question(id="billing_address", type="textarea", required=True, prompt="What's your billing address?"),
            Question(id="email", type="email", required=True, prompt="What's your email address?"),
            Question(id="phone", type="phone", required=True,
                     prompt="What telephone number(s) can we reach you at (work, home, and/or cell)?"),
            Question(
                id="id_type", type="radio", required=True,
                prompt="What type of ID will you provide sight of?",
                options=["National ID", "Passport", "NIS", "Driver's Licence"],
            ),
            Question(
                id="dl_number", type="text", required=True, prompt="What's your Driver's Licence number?",
                condition=lambda a: a.get("id_type") == "Driver's Licence",
            ),
            Question(id="application_date", type="date", required=True, prompt="What is the application date?"),
            Question(
                id="new_or_transfer", type="radio", required=True,
                prompt="Is this a New Service or a Transfer of Service?",
                options=["New Service", "Transfer of Service"],
            ),
            Question(
                id="existing_account_number", type="text", required=True,
                prompt="What's the existing account number being transferred?",
                condition=lambda a: a.get("new_or_transfer") == "Transfer of Service",
            ),
            Question(
                id="services", type="checkbox", required=True,
                prompt="What service is required? You can pick more than one, then Continue.",
                options=["Water", "Additional Meter", "Sewer"],
            ),
            Question(id="service_location", type="textarea", required=True, prompt="What's the service location?"),
            Question(id="service_directions", type="textarea", required=False,
                     prompt="Directions to get to the intended service point? You can skip this if the address is enough on its own."),
            Question(
                id="nature_of_building", type="radio", required=True,
                prompt="What is the nature of the building?",
                options=["Domestic", "Non-Domestic", "Industrial"],
            ),
            Question(
                id="trading_name", type="text", required=True, prompt="What's the trading/registered name of the business?",
                condition=lambda a: a.get("nature_of_building") in ("Non-Domestic", "Industrial"),
            ),
            Question(
                id="applicant_company_position", type="text", required=True, prompt="What is your position in the company?",
                condition=lambda a: a.get("nature_of_building") in ("Non-Domestic", "Industrial"),
            ),

            # --- Section B: Ownership ---
            Question(id="is_owner", type="radio", required=True,
                     prompt="Do you own the property where service is required?", options=["Yes", "No"]),
            Question(
                id="has_title_documents", type="radio", required=True,
                prompt="Do you have title documents proving that you are the owner of the property?",
                options=["Yes", "No"],
                condition=lambda a: a.get("is_owner") == "Yes",
            ),
            Question(
                id="supporting_documents", type="checkbox", required=True,
                prompt="Which of these will you attach in support of this application? You can pick more than one, then Continue.",
                options=["Conveyance", "Will/Administration/Probate", "Statutory Declaration"],
                condition=lambda a: a.get("has_title_documents") == "Yes",
            ),

            # --- No proof of ownership: the form's own 5-option (A-E) sub-declaration ---
            Question(
                id="no_proof_statement", type="radio", required=True,
                prompt="Since you don't have title documents, which of these statements applies to you?",
                options=[
                    "A - Continuous undisturbed possession for a period of years",
                    "B - Sole beneficiary of a deceased owner's estate",
                    "C - Joint beneficial interest with other person(s), beneficiaries of a deceased owner's estate",
                    "D - Purchased from the previous owners, but not yet formally conveyed",
                    "E - Owners agreed to convey by Deed of Gift, not yet complete",
                ],
                help=("If you're not sure which of these applies to your situation, let me know - I'll flag this "
                      "for NAWASA's Customer Service team to help you determine the right option rather than "
                      "guessing, since this is a legal determination."),
                condition=lambda a: a.get("has_title_documents") == "No",
            ),
            Question(
                id="possession_years", type="text", required=True,
                prompt="For how many years have you been in continuous undisturbed possession of the property?",
                condition=lambda a: (a.get("no_proof_statement") or "").startswith("A"),
            ),
            Question(
                id="possession_start_date", type="text", required=True,
                prompt="Around what month and year did that possession begin?",
                condition=lambda a: (a.get("no_proof_statement") or "").startswith("A"),
            ),
            Question(
                id="deceased_owner_name", type="text", required=True,
                prompt="What's the name of the deceased person whose estate you're the beneficiary of?",
                condition=lambda a: (a.get("no_proof_statement") or "")[:1] in ("B", "C"),
            ),
            Question(
                id="deceased_owner_death_date", type="date", required=True,
                prompt="What was their date of death?",
                condition=lambda a: (a.get("no_proof_statement") or "")[:1] in ("B", "C"),
            ),
            Question(
                id="co_owners", type="textarea", required=True,
                prompt="Please list the name(s) of the other person(s) who jointly share this beneficial interest with you.",
                condition=lambda a: (a.get("no_proof_statement") or "").startswith("C"),
            ),
            Question(
                id="purchase_date", type="date", required=True,
                prompt="What date did you purchase the property from the previous owners?",
                condition=lambda a: (a.get("no_proof_statement") or "").startswith("D"),
            ),

            # --- Section D: applicant is NOT the owner ---
            Question(id="owner_name", type="text", required=True, prompt="What's the property owner's full name?",
                     condition=lambda a: a.get("is_owner") == "No"),
            Question(id="owner_address", type="textarea", required=True, prompt="What's the property owner's address?",
                     condition=lambda a: a.get("is_owner") == "No"),
            Question(id="owner_phone", type="phone", required=True,
                     prompt="What telephone number(s) can the owner be reached at?",
                     condition=lambda a: a.get("is_owner") == "No"),

            # --- Section E: User of the property ---
            Question(id="is_main_user", type="radio", required=True,
                     prompt="Will you be the main user of the service(s) applied for?", options=["Yes", "No"]),
            Question(id="main_user_name", type="text", required=True, prompt="What's the main user's full name?",
                     condition=lambda a: a.get("is_main_user") == "No"),
            Question(id="had_previous_service", type="radio", required=True,
                     prompt="Have you (or any intended user) ever been provided with service by NAWASA before?",
                     options=["Yes", "No"]),
            Question(id="had_previous_line", type="radio", required=True,
                     prompt="Was there a water line on the property before?", options=["Yes", "No"]),
            Question(
                id="previous_account_details", type="text", required=True,
                prompt="What's the previous account number, meter number, or owner's name, if known?",
                condition=lambda a: a.get("had_previous_line") == "Yes",
            ),
            Question(id="nearest_customer_name", type="text", required=False,
                     prompt="Name of the nearest NAWASA customer to the service location, if known. You can skip this if not."),
            Question(
                id="nearest_customer_phone", type="phone", required=False,
                prompt="Their telephone number, if known. You can skip this if not.",
                condition=lambda a: bool((a.get("nearest_customer_name") or "").strip()),
            ),
        ],
    },

    # =====================================================================
    # Private Water Service Cancellation Form - rebuilt from the actual
    # PDF. "Service Location" (a text line) and "Location of Property"
    # (a large empty box below it) are two DIFFERENT real fields - the
    # box is for a hand-drawn location sketch, which isn't something a
    # customer can sensibly complete via a chat question, so it's flagged
    # in the signature note instead of asked here. No date field exists
    # on this form, so none is asked. User/Owner/NAWASA Officer
    # signatures are never asked.
    # =====================================================================
    "form-cancellation": {
        "display_name": "Private Water Service Cancellation Form",
        "filename_base": "Completed_NAWASA_Cancellation_Form",
        "signature_note": ("Remember that both the User and the Owner (if different) need to sign before "
                            "submitting the form. The form also has a \"Location of Property\" box for a hand-"
                            "drawn location sketch - please complete that yourself if NAWASA needs it."),
        "questions": [
            Question(id="property_owner_name", type="text", required=True, prompt="What's the name of the property owner?"),
            Question(id="phone", type="phone", required=True, prompt="What's the owner's telephone number?"),
            Question(id="user_name_if_not_owner", type="text", required=False,
                     prompt="If the user is not the owner, what's the user's name? You can skip this if the user and owner are the same person."),
            Question(id="service_location", type="textarea", required=True, prompt="What's the service location?"),
            Question(id="meter_id", type="text", required=False,
                     prompt="What's the Meter ID, if one has been assigned? You can skip this if not."),
            Question(id="cancellation_reason", type="textarea", required=True,
                     prompt="What's the reason(s) for cancellation of service?"),
        ],
    },

    # =====================================================================
    # Customer Without Proof of Legal Ownership - Agreement Document.
    # Rebuilt from the actual PDF. The security deposit amount is set by
    # NAWASA ("as assessed"), not chosen by the customer, so it's handled
    # as an acknowledgment rather than a number the customer supplies.
    # There's no separately-labeled "witness name" field on this
    # document - the witnessing lines are for the witness's own
    # signature - so it isn't asked here.
    # =====================================================================
    "form-no-proof-of-ownership": {
        "display_name": "Customer Without Proof of Ownership Agreement",
        "filename_base": "Completed_NAWASA_No_Proof_Of_Ownership_Agreement",
        "signature_note": "Remember to add your signature before submitting the form.",
        "questions": [
            Question(id="agreement_date", type="date", required=True, prompt="What is the date of this agreement?"),
            Question(id="applicant_name", type="text", required=True, prompt="What's your full name?"),
            Question(id="applicant_address", type="textarea", required=True, prompt="What's your address?"),
            Question(id="parish", type="text", required=True, prompt="Which parish is this in?"),
            Question(
                id="deposit_ack", type="radio", required=True,
                prompt=("This agreement requires a security deposit, as assessed by NAWASA, before installation. "
                        "Do you understand and agree to this?"),
                options=["Yes", "No"],
            ),
        ],
    },

    # =====================================================================
    # Declaration of Ownership - rebuilt from the actual PDF. The form's
    # two ownership-basis clauses (item 1's i/ii/iii and item 2's i/ii)
    # overlap conceptually; this schema implements the union as three
    # clear options tied to the concrete details the form actually asks
    # for. The $EC Water Connection Fee amount is NAWASA-set, not
    # customer-provided, so it's not asked. Only ONE declarant is
    # supported per wizard session - the real form has two signature
    # slots for joint declarants, so a second declarant's own details
    # would need to be added by hand if applicable.
    # =====================================================================
    "form-declaration-of-ownership": {
        "display_name": "Declaration of Ownership",
        "filename_base": "Completed_NAWASA_Declaration_Of_Ownership",
        "signature_note": "Remember to add your signature before submitting the form. If there's a second joint declarant, their name/signature/date need to be added by hand.",
        "questions": [
            Question(id="declarant_name", type="text", required=True, prompt="What's your full name?"),
            Question(id="declarant_address", type="textarea", required=True, prompt="What's your own address?"),
            Question(id="parish", type="text", required=True, prompt="Which parish is the property in?"),
            Question(id="property_address", type="textarea", required=True, prompt="What's the address of the property?"),
            Question(
                id="ownership_basis", type="radio", required=True,
                prompt="Which best describes the basis of your ownership, in the absence of a Deed of Conveyance?",
                options=[
                    "Continuous undisturbed possession (12+ years)",
                    "Sole beneficiary of a deceased owner's estate",
                    "Joint beneficial interest with other person(s)",
                ],
                help=("If you're not sure which of these applies to your situation, that's alright - say so and "
                      "AquaAssist will flag this as needing guidance from NAWASA or a qualified professional "
                      "rather than guessing, since this is a legal determination."),
            ),
            Question(
                id="possession_years", type="text", required=True,
                prompt="For how many years have you been in continuous undisturbed possession?",
                condition=lambda a: a.get("ownership_basis") == "Continuous undisturbed possession (12+ years)",
            ),
            Question(
                id="possession_start_period", type="text", required=True,
                prompt="Around what month and year did that possession begin?",
                condition=lambda a: a.get("ownership_basis") == "Continuous undisturbed possession (12+ years)",
            ),
            Question(
                id="deceased_owner_name", type="text", required=True,
                prompt="What's the name of the deceased person whose estate you're the beneficiary of?",
                condition=lambda a: a.get("ownership_basis") in (
                    "Sole beneficiary of a deceased owner's estate", "Joint beneficial interest with other person(s)"),
            ),
            Question(
                id="deceased_owner_death_date", type="date", required=True,
                prompt="What was their date of death?",
                condition=lambda a: a.get("ownership_basis") in (
                    "Sole beneficiary of a deceased owner's estate", "Joint beneficial interest with other person(s)"),
            ),
            Question(
                id="co_owners", type="textarea", required=True,
                prompt="Please list the name(s) of the other person(s) who jointly share this beneficial interest with you.",
                condition=lambda a: a.get("ownership_basis") == "Joint beneficial interest with other person(s)",
            ),
            Question(id="declaration_date", type="date", required=True, prompt="What is today's date?"),
        ],
    },

    # =====================================================================
    # Permission in Support of Application for Water Connection - rebuilt
    # from the actual PDF. The deed citation is ONE date (day/month/year
    # together in the original text) plus Liber and Page - there's no
    # separate "Year" field independent of that date. The Notarial
    # Certificate section is completed by the notary, never the customer.
    # =====================================================================
    "form-permission-in-support": {
        "display_name": "Permission in Support of Application for Water Connection",
        "filename_base": "Completed_NAWASA_Permission_In_Support",
        "signature_note": "Remember that the property owner needs to sign this in front of a Notary Public - the Notarial Certificate section is completed by the notary, not by you.",
        "questions": [
            Question(id="owner_names", type="text", required=True,
                     prompt="Full name(s) of the property owner(s) granting permission?"),
            Question(id="property_location", type="textarea", required=True, prompt="Where is the property situated?"),
            Question(id="parish", type="text", required=True, prompt="Which parish is this in?"),
            Question(id="deed_date", type="date", required=True, prompt="What's the date of the Deed of Conveyance?"),
            Question(id="deed_liber", type="text", required=True, prompt="What's the Liber (volume) number?"),
            Question(id="deed_page", type="text", required=True, prompt="What's the Page number?"),
            Question(id="applicant_names", type="text", required=True,
                     prompt="Full name(s) of the applicant(s) receiving permission?"),
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
    # Pre-check per spec: detect a missing master template BEFORE the
    # customer answers a single question, not after they've filled out
    # the whole form. See generate_pdf for why a template must already
    # be stored (never fetched live from nawasa.gd during a session).
    template = db.get_form_template(form_id, include_data=False)
    if template is None:
        return None, ("Sorry, I'm unable to prepare this form for guided filling right now — "
                       "the official template hasn't been loaded yet. Please open the official "
                       "form directly, or check back shortly.")
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
    are included as a summary page instead").

    ARCHITECTURE NOTE: this NEVER contacts nawasa.gd. It reads the
    persistent master template already stored in Neon (see
    db.get_form_template / save_form_template) — acquired ahead of time
    via a staff upload or an admin-triggered sync attempt, both outside
    any customer's session. If no template is stored, start() already
    refused to begin the wizard at all (see the pre-check there), so
    reaching this function with no template means the template was
    deleted mid-session — handled below as a clear, non-crashing error.
    """
    sess = _SESSIONS.get(session_id)
    if sess is None:
        return None, None, [], "No form is currently in progress."
    schema = get_schema(sess.form_id)
    warnings = []

    template = db.get_form_template(sess.form_id, include_data=True)
    if template is None or not template.get("pdf_base64"):
        return None, None, [], ("The official template for this form is no longer available. "
                                 "Please contact NAWASA directly, or try again once it's been reloaded.")

    try:
        import base64
        pdf_bytes = base64.b64decode(template["pdf_base64"])
    except Exception as e:
        logger.error("Form wizard: stored template for %s is corrupted: %s", sess.form_id, e)
        return None, None, [], "The stored official template appears to be corrupted. Please contact NAWASA directly."

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
