# AquaAssist

AquaAssist is a customer-service chatbot and staff-management portal built
for NAWASA (the National Water & Sewerage Authority of Grenada, Carriacou,
and Petit Martinique). It handles leak/outage reporting, FAQs, billing
questions, service-notice lookups, and a staff dashboard for managing
reports and outages — all wrapped in NAWASA's branding.

## Architecture

Three pieces, one deploy:

- **`app.py`** — Flask backend. Serves the static frontend, exposes a small
  JSON API, holds the Gemini chat sessions, and talks to Gemini + (optionally)
  ElevenLabs.
- **`index.html` / `app.js` / `style.css`** — a single-page frontend with no
  build step (plain `fetch` + DOM, Leaflet from a CDN for maps). One page
  serves two audiences:
  - the **customer-facing widget** — a floating chat launcher that expands
    into the full AquaAssist app (chat, report & track, FAQ, notify, settings)
  - a **demo marketing site** wrapping it, used to preview how the widget
    would sit on nawasa.gd, plus the **Staff Portal** (see below)
- **`data/*.csv`** — the "database." Plain CSV files via Python's `csv`
  module (no ORM, no external DB) for reports, outages, notification
  signups, and water-service tips. `data/features.json` holds the staff
  feature-flag toggles. Created automatically on first run.

### Why CSV instead of a real database?

Deliberate, for a small deployment: zero setup, human-readable, easy to
inspect/export for a demo. It does **not** survive a redeploy on a host
without a persistent disk (e.g. Render's free tier) — every CSV resets to
empty (tips reseed to the defaults; reports/outages/subscribers are lost).
If this needs to survive production redeploys, attach a persistent disk at
the `data/` path, or move to Postgres + object storage (S3/R2) for
attachments.

### Why is chat state in memory (`SESSIONS = {}`)?

Same reasoning — fine for a single-process demo, but two real
consequences in production: a server restart wipes every in-progress
conversation, and if the host ever runs more than one instance, a
customer's next message could land on a different instance with no memory
of the conversation so far. Swap for a shared store (e.g. Redis) if this
needs to scale past a single dyno/instance.

## Running locally

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=your-key-here
export STAFF_PASSCODE=change-me
export ELEVENLABS_API_KEY=your-elevenlabs-key      # optional — enables Caribbean-accent read-aloud
export ELEVENLABS_VOICE_ID=your-chosen-voice-id    # optional — from the ElevenLabs Voice Library
python app.py
```

Then open <http://localhost:5000>. Without `GEMINI_API_KEY` set, the app
still runs — every other feature works, but `/api/chat` returns a clean
503 instead of crashing. Without the ElevenLabs vars set, read-aloud
silently falls back to the browser's built-in voice.

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | for chat | Powers the AquaAssist conversation |
| `STAFF_PASSCODE` | recommended | Gates the Staff Portal and all write APIs |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | optional | Caribbean-accented read-aloud |
| `ELEVENLABS_MODEL_ID` | optional | Defaults to `eleven_multilingual_v2` |
| `PORT` | optional | Defaults to `5000` |

## Customer-facing features

- **Chat** — Gemini-powered assistant, grounded in NAWASA's real FAQ
  content (see "How the assistant is grounded" below), with 3 tools:
  logging a report, checking a report's status, and checking real,
  currently-active outage notices for a parish.
- **Attachments** — photo/video/document upload, live camera capture,
  voice notes (recorded audio, not just transcribed), and GPS location
  sharing — all sent to Gemini natively (see "Media handling" below).
- **Report & Track** — a standalone form (with a pin-on-map location
  picker) for reporting an issue without going through chat, plus a
  reference-number tracker with a status progress bar.
- **FAQ** — searchable, categorized, with a read-aloud button per answer.
- **Water Service Tips** — a small rotating tip card (25s interval),
  content fully managed from the Staff Portal.
- **Notify** — customers can subscribe to be notified about outage/
  maintenance categories. *(Currently just collects signups into
  `notifications.csv` for staff to see — there's no automatic email/SMS
  dispatch when an outage is posted. That's the next logical step if this
  needs to actually notify people, not just list who asked to be.)*
- **Accessibility** — dark mode, high-contrast mode, larger text, and
  read-aloud (ElevenLabs with a browser-voice fallback), each individually
  toggleable by staff.

## Staff Portal

Reached at **`/admin`** — a real, bookmarkable URL (not buried inside the
customer chat widget). Same single-page app; the JS detects the path on
load and jumps straight to the Staff Portal login instead of the marketing
homepage. Gated by `STAFF_PASSCODE`, sent as an `X-Staff-Passcode` header
on every write request (`require_staff` decorator in `app.py`).

From the Staff Portal, staff can:
- View/filter all reports on a map + table, update report status
- Post/remove outage announcements (by parish, with a date range)
- View notification subscribers
- Add/edit/enable-disable/delete Water Service Tips
- Toggle 12 customer-facing features on/off in real time (FAQs, Water
  Tips, Report an Issue, WhatsApp, Voice Notes, Get Notified, Camera,
  Quick Actions, and the 4 accessibility toggles) — the customer widget
  re-reads `/api/features` on load and reflects changes immediately

## How the assistant is grounded

The Gemini system prompt (built fresh per session in
`build_system_instruction()`) includes the **exact same FAQ content**
shown on the customer FAQ tab (`FAQS` in `app.py`), so the chat and the
FAQ tab can't drift out of sync with each other. It's explicitly told to
prefer this content over general knowledge and never contradict it, and
to say plainly when it doesn't have access to something (e.g. a
customer's actual account balance — AquaAssist isn't connected to
NAWASA's billing system, so it's instructed to never invent a number).

## Media handling for Gemini

Browser-recorded audio/video doesn't always arrive in a format Gemini's
media understanding accepts reliably:

- **Audio** — `MediaRecorder` produces `audio/webm` (Opus) in most
  browsers; Gemini doesn't reliably parse that container. When `ffmpeg`
  is available, the backend remuxes it to Ogg (same audio, different
  container — instant, lossless).
- **Video** — even when a browser reports `video/mp4`, the actual bytes
  are often a fragmented/streamed MP4 with the `moov` atom at the end
  rather than the front ("faststart"). Many parsers, Gemini's ingestion
  included, expect faststart and silently fail to read the file
  otherwise. The backend re-encodes to a proper faststart MP4 when
  `ffmpeg` is available.
- What gets **stored** for staff to view later is always the customer's
  original, unmodified bytes — normalization only affects the copy sent
  to Gemini, so staff never depend on `ffmpeg` being installed just to
  play back a clip.

If `ffmpeg` isn't installed, the app still works — it just sends the raw
browser output to Gemini as-is, which may occasionally fail to parse on
some browser/format combinations.

## Known limitations / next steps

- CSV storage and in-memory chat sessions don't survive a redeploy /
  multi-instance host without extra infrastructure (see above).
- Notify subscribers are collected but never actually notified — no
  email/SMS dispatch exists yet.
- The chatbot replies in Standard English only, even when it understands
  a customer writing in Creole/patois — a deliberate client decision, not
  a bug, but worth knowing if that requirement changes.
- No automated test suite — testing so far has been manual + live
  end-to-end checks against a running server (booting the app, hitting
  every endpoint with `curl`, cross-checking every HTML `id` referenced
  by the JS actually exists in the markup).
