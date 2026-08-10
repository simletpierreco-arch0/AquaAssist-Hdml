# AquaAssist — HTML/JS frontend + Flask backend

This replaces the Streamlit app with:
- `backend/app.py` — a small Flask API that holds the Gemini key, CSV "database"
  (reports, outages, notification signups), business-hours logic, and staff auth.
- `frontend/` — plain HTML/CSS/JS (no build step) that calls that API. Open it
  by visiting the Flask server's URL — Flask serves the frontend files itself.

## Run it

```bash
cd backend
pip install -r requirements.txt
export GEMINI_API_KEY="your-real-key"      # get one at https://aistudio.google.com/
export STAFF_PASSCODE="something-not-changeme123"
python app.py
```

Then open **http://localhost:5000** in a browser. That's the whole app —
customer chat, report & track, FAQ, notify signups, and (via the "🔐 Staff
Portal" switch at the top) the staff dashboard.

## What moved where

| Streamlit concept | Here |
|---|---|
| `st.session_state` | `localStorage` / `sessionStorage` in `app.js`, plus an in-memory `SESSIONS` dict on the server keyed by a `session_id` the browser generates |
| `client.chats.create(...)` per browser session | Same call, now made once per `session_id` on the server and reused across `/api/chat` calls |
| `log_water_report` tool | Identical function, now defined per-session on the server so the report card it produces can be attached to that request's JSON response |
| `data/*.csv` | Same CSV files, same folder (`backend/data/`), read/written with the plain `csv` module instead of pandas |
| `folium` / `streamlit-folium` maps | [Leaflet.js](https://leafletjs.com/) loaded from a CDN, same OpenTopoMap tiles |
| `streamlit-geolocation` | The browser's native `navigator.geolocation` API |
| Staff passcode gate | `POST /api/staff/login` checks the passcode; the browser then sends it back on every staff request via an `X-Staff-Passcode` header (see `require_staff` in `app.py`) |

## Deliberately left out of this pass

These existed in the Streamlit version but add real complexity for a browser
port, so they're not here yet — flagging where to hook them back in if you
want them:

- **Live mic recording / transcription** — the chat still accepts typed
  text and file attachments (including audio files, if you want to route
  those through Gemini transcription like the original did). Adding live
  recording means the browser's `MediaRecorder` API on the frontend, plus
  a transcription call in `/api/chat` on the backend.
- **Text-to-speech replies** — would need a TTS call (`gTTS` or similar)
  in `api_chat()`, returned as base64 audio the frontend plays.
- **In-browser camera capture** — file *upload* of a photo works today;
  live capture needs the browser's `getUserMedia`/`<video>` + canvas snapshot.
- **Pinecone knowledge-base retrieval** — the original's `retrieve_nawasa_knowledge()`
  can be ported into `api_chat()` almost as-is if you want it back.
- **Auto-send subscriber emails from the staff portal** — the original's
  SMTP test flow isn't included; the notify table is read-only here.

## Notes

- CSV storage is still local-file based, same as the original — fine for a
  single server, not for multiple server instances or ephemeral hosting
  without a persistent disk.
- The frontend keeps a design close to the original: blue/white "wave" hero,
  glass cards, the AI orbit glow around the assistant avatar, dark mode,
  high-contrast mode, and larger-text mode — all in `frontend/style.css`
  using CSS custom properties, toggled from the Settings tab.
- CORS isn't configured because the frontend is served by the same Flask
  app (same-origin). If you later split them onto different hosts, add
  `flask-cors`.
