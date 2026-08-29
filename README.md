# AquaAssist — updated project

This is your existing project (agent.py / app.py / db.py / app.js / index.html /
style.css) with everything from this conversation actually merged in as real,
working code — not patch notes. Nothing that worked before was removed:
the chatbot, the customer widget, the report map, the FAQ tab, the outage
banners, TTS, all of it is intact and untouched in behavior.

## What's new

1. **Knowledge Base (FAQ) management** — the old hardcoded `FAQS` list in
   `app.py` is now a real database table. Staff can add/edit/enable-disable/
   delete FAQs from the Staff Portal, and every change re-syncs the bot's
   Pinecone RAG index automatically.

2. **"Questions AquaAssist Couldn't Answer"** — every time a customer asks
   something the knowledge base has no match for, it's logged. Staff review
   it in the portal and can answer it straight into a new FAQ in one step.

3. **Staff Portal sidebar restructure** — reorganized into a "NAWASA Digital
   Management" sidebar with three sections (🖥️ Website, 🤖 AquaAssist,
   📍 Reports & Operations) plus a Dashboard overview. Every panel that
   existed before (Service Alerts/outages, Water Tips, feature toggles,
   maintenance message, the reports table, the staff map, notify
   subscribers) is present, just regrouped — same IDs, same behavior.

4. **Conversation stats** — "Conversations today" / "Replies sent today" on
   the Dashboard, backed by a new `chat_events` table logged on every
   `/api/chat` call.

5. **Staff roles** — up to four passcodes (Administrator / Website Manager /
   AquaAssist Manager / Operations) instead of one shared passcode. Fully
   backward compatible: set only `STAFF_PASSCODE` and everyone who logs in
   is an Administrator, exactly like before.

## Setup

```bash
pip install -r requirements.txt

export GEMINI_API_KEY=your-key-here
export STAFF_PASSCODE=change-me

# Optional — role-based staff logins (leave unset to keep the single-passcode behavior)
export STAFF_PASSCODE_WEBSITE=optional-website-manager-passcode
export STAFF_PASSCODE_AQUA=optional-aquaassist-manager-passcode
export STAFF_PASSCODE_OPS=optional-operations-passcode

# Optional — Caribbean-accent read-aloud
export ELEVENLABS_API_KEY=your-elevenlabs-key
export ELEVENLABS_VOICE_ID=your-chosen-voice-id

# Optional — real RAG retrieval (recommended for the competition)
export PINECONE_API_KEY=your-pinecone-key

# Recommended for production
export DATABASE_URL=your-postgres-url
export SELF_PING_URL=https://your-app.onrender.com

python app.py
```

Then open **http://localhost:5000** for the public site + customer widget,
and **http://localhost:5000/admin** for the Staff Portal.

## First login

Use `STAFF_PASSCODE` to log in — you'll land on the Dashboard with full
access to all three sidebar sections. The database (SQLite by default, at
`data/aquaassist.db`) is created and seeded automatically on first run:
default Water Tips, default feature flags, and your original FAQ list all
get seeded into their new tables the first time `app.py` starts.

## Files

- `agent.py` — LangGraph agent + Pinecone RAG (unchanged logic, new
  `on_no_match` hook wired to unanswered-question logging)
- `db.py` — SQLite/Postgres storage layer (original tables + `faqs`,
  `unanswered_questions`, `chat_events`)
- `app.py` — Flask routes (original routes + FAQ/unanswered/chat-stats
  endpoints, role-based staff auth)
- `app.js` — frontend logic (original behavior + Staff Portal sidebar,
  FAQ admin, unanswered-questions admin, role visibility)
- `index.html` — markup (Staff Portal section restructured into the
  sidebar shell; customer widget markup unchanged)
- `style.css` — styling (original theme + `.staff-shell`/`.staff-sidebar`/
  `.staff-nav-*`/`.staff-section` rules for the new layout)

## Still outstanding (not done — flagged, not faked)

- **Public website redesign** (homepage/nav/content pages per the original
  spec) — the mock site backdrop is unchanged from your original project.
- **Individual staff accounts with audit trails** — current roles are
  passcode-based (see `staff_roles_patch.md` from earlier in this
  conversation for the reasoning); a real accounts system is a bigger,
  separate piece of work.

## Verification performed before delivery

- All three Python files (`db.py`, `app.py`, `agent.py`) compile cleanly
  (`python3 -m py_compile`).
- `app.js` passes `node --check`.
- Every DOM id `app.js` queries via `$("#...")` exists in `index.html`
  (cross-checked programmatically).
- Every `data-tab` / `data-staff-section` value has a matching panel id.
- Every API call `app.js` makes matches an actual Flask route in `app.py`.
- Every config key `app.js` reads from `/api/init` is actually returned by
  `api_init()`.

This does **not** replace running it for real — I don't have your
`GEMINI_API_KEY`/`PINECONE_API_KEY` to exercise a live chat turn end-to-end
in this sandbox — but the full request/response wiring, schema, and DOM
structure have been checked, not assumed.
