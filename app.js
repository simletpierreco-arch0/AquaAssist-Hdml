// AquaAssist frontend — talks to the Flask backend over /api/*.
// No build step: plain fetch + DOM. Leaflet (via CDN) handles the maps.

const API = ""; // same-origin

const state = {
  config: null,
  sessionId: localStorage.getItem("aqua_session_id") || null,
  territory: localStorage.getItem("aqua_territory") || "Grenada",
  messages: JSON.parse(localStorage.getItem("aqua_messages") || "[]"),
  staffPasscode: sessionStorage.getItem("aqua_staff_passcode") || "",
  reportPin: null,
  reportMap: null,
  reportMarker: null,
  staffMap: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function saveMessages() {
  localStorage.setItem("aqua_messages", JSON.stringify(state.messages));
}
function saveSession(id) {
  state.sessionId = id;
  localStorage.setItem("aqua_session_id", id);
}

// ---------------------------------------------------------------------
// Widget shell — collapsed floating button <-> expanded chat panel.
// This wraps the app; nothing below this function had to change to work
// inside the widget, since the panel is just a fixed-position, scrollable
// box and the app content inside it is untouched.
// ---------------------------------------------------------------------
function setupWidgetToggle() {
  const widget = $("#aquaWidget");
  const panel = $("#widgetPanel");
  const fab = $("#widgetToggleBtn");
  const closeBtn = $("#widgetCloseBtn");
  const minimizeBtn = $("#widgetMinimizeBtn");

  function openWidget() {
    widget.classList.remove("collapsed");
    widget.classList.add("expanded");
    panel.style.display = "flex";
    fab.setAttribute("aria-expanded", "true");
    document.body.classList.add("aqua-widget-open");
    // Maps render at 0 size while their container is display:none — force
    // Leaflet to recalculate now that the panel is actually visible.
    setTimeout(() => {
      if (state.reportMap) state.reportMap.invalidateSize();
      if (state.staffMap) state.staffMap.invalidateSize();
    }, 260);
    // Move focus into the panel for keyboard/screen-reader users.
    setTimeout(() => closeBtn && closeBtn.focus(), 300);
  }

  function closeWidget() {
    widget.classList.remove("expanded");
    widget.classList.add("collapsed");
    panel.style.display = "none";
    fab.setAttribute("aria-expanded", "false");
    document.body.classList.remove("aqua-widget-open");
    fab.focus();
  }

  fab.addEventListener("click", () => {
    if (widget.classList.contains("expanded")) closeWidget();
    else openWidget();
  });
  closeBtn.addEventListener("click", closeWidget);
  minimizeBtn.addEventListener("click", closeWidget);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && widget.classList.contains("expanded")) closeWidget();
  });
}

// ---------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------
async function init() {
  setupWidgetToggle();
  applyPrefsFromStorage();
  const res = await fetch(`${API}/api/init`);
  state.config = await res.json();

  populateSelect($("#territorySelect"), state.config.territories);
  $("#territorySelect").value = state.territory;

  if (localStorage.getItem("aqua_auth_done") === "1") {
    startApp();
  }

  $("#loginDarkToggle").addEventListener("change", (e) => {
    document.body.classList.toggle("dark", e.target.checked);
  });

  $("#startChatBtn").addEventListener("click", () => {
    const territory = $("#territorySelect").value;
    if (!territory) {
      $("#loginError").style.display = "block";
      return;
    }
    state.territory = territory;
    localStorage.setItem("aqua_territory", territory);
    localStorage.setItem("aqua_auth_done", "1");
    startApp();
  });
}

function applyPrefsFromStorage() {
  const dark = localStorage.getItem("aqua_dark") === "1";
  const hc = localStorage.getItem("aqua_hc") === "1";
  const large = localStorage.getItem("aqua_large") === "1";
  document.body.classList.toggle("dark", dark);
  document.body.classList.toggle("high-contrast", hc);
  document.body.classList.toggle("large-text", large);
}

function populateSelect(el, values, formatFn) {
  el.innerHTML = "";
  values.forEach((v) => {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = formatFn ? formatFn(v) : v;
    el.appendChild(opt);
  });
}

function startApp() {
  $("#loginGate").style.display = "none";
  $("#app").style.display = "block";

  renderHero();
  renderContactRow();
  renderQuickActions();
  renderFAQ();
  populateSelect($("#reportParish"), state.config.parishes);
  populateSelect($("#reportIssueType"), state.config.issue_types);
  populateSelect($("#reportSeverity"), state.config.severity_levels);
  populateSelect($("#customerParishSelect"), ["", ...state.config.parishes], (v) => v || "— none selected —");
  renderNotifyCategories();
  renderChat();
  renderOutageBanners();
  setupReportMap();

  setupTabs();
  setupPortalSwitch();
  setupChatForm();
  setupReportForm();
  setupTrackForm();
  setupNotifyForm();
  setupSettings();
  setupStaffPortal();
  setupCamera();
  setupMic();
  setupLocationShare();

  setInterval(async () => {
    const res = await fetch(`${API}/api/business-hours`);
    state.config.business_hours = await res.json();
    renderHero();
  }, 60000);
}

// ---------------------------------------------------------------------
// Hero / business hours
// ---------------------------------------------------------------------
function renderHero() {
  const bh = state.config.business_hours;
  const pill = $("#hoursPill");
  if (bh.is_open && bh.closing_soon) {
    pill.className = "hours-pill soon";
    pill.textContent = `Office Open — closing in ${bh.minutes_until_close} min`;
  } else if (bh.is_open) {
    pill.className = "hours-pill open";
    pill.textContent = "Office Open";
  } else {
    pill.className = "hours-pill closed";
    pill.textContent = `Offices Closed — reopens ${bh.reopens_label}`;
  }

  const banner = $("#hoursBanner");
  banner.innerHTML = "";
  if (!bh.is_open) {
    banner.innerHTML = `<div class="card" style="font-size:.85rem;">🕒 Our Customer Service team has closed for the day and will reopen ${bh.reopens_label}. AquaAssist remains available 24/7 — you're welcome to leave a message here, call, or WhatsApp us any time.</div>`;
  } else if (bh.closing_soon) {
    banner.innerHTML = `<div class="card" style="font-size:.85rem;">⏳ Heads up — our Customer Service office closes in ${bh.minutes_until_close} minute(s). AquaAssist stays available 24/7, but if you'd like a live representative today, call ${state.config.nawasa_phone} or WhatsApp us now.</div>`;
  }
}

function renderContactRow() {
  const phoneDigits = state.config.nawasa_phone.replace(/\D/g, "");
  $("#contactCallCard").href = `tel:${phoneDigits}`;
  const wa = state.config.territory_whatsapp[state.territory];
  $("#contactWhatsappCard").href = wa;
  $("#contactWebsiteCard").href = state.config.nawasa_website;
  $("#whatsappFloat").href = wa;
}

// ---------------------------------------------------------------------
// Tabs / portal switch
// ---------------------------------------------------------------------
function setupTabs() {
  $$(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab-btn").forEach((b) => b.classList.remove("active"));
      $$(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $(`#tab-${btn.dataset.tab}`).classList.add("active");
      if (btn.dataset.tab === "report" && state.reportMap) {
        setTimeout(() => state.reportMap.invalidateSize(), 50);
      }
    });
  });
}

function setupPortalSwitch() {
  $("#btnCustomerPortal").addEventListener("click", () => {
    $("#btnCustomerPortal").classList.add("active");
    $("#btnStaffPortal").classList.remove("active");
    $("#customerHero").style.display = "block";
    $("#staffHero").style.display = "none";
    $("#customerPortal").style.display = "block";
    $("#staffPortal").style.display = "none";
  });
  $("#btnStaffPortal").addEventListener("click", () => {
    $("#btnStaffPortal").classList.add("active");
    $("#btnCustomerPortal").classList.remove("active");
    $("#customerHero").style.display = "none";
    $("#staffHero").style.display = "block";
    $("#customerPortal").style.display = "none";
    $("#staffPortal").style.display = "block";
    if (state.staffPasscode) {
      staffLoginSuccess();
    }
  });
}

// ---------------------------------------------------------------------
// Quick actions
// ---------------------------------------------------------------------
const QUICK_ACTIONS = [
  { label: "👷 Report a Leak", prompt: "I'd like to report a water leak." },
  { label: "🚰 Water Supply & Outages", prompt: "Are there any scheduled outages or planned maintenance in my area?" },
  { label: "💳 Pay My Bill", prompt: "What are my options for paying my NAWASA bill?" },
  { label: "📄 Check My Bill", prompt: "How can I check my current NAWASA bill balance and consumption?" },
  { label: "📍 Office Locations", prompt: "Where are NAWASA's office locations?" },
  { label: "👤 Speak to an Agent", prompt: "I'd like to speak with a customer service representative." },
];
function renderQuickActions() {
  const grid = $("#quickActions");
  grid.innerHTML = "";
  QUICK_ACTIONS.forEach((qa) => {
    const btn = document.createElement("button");
    btn.className = "quick-action-btn";
    btn.textContent = qa.label;
    btn.addEventListener("click", () => sendMessage(qa.prompt));
    grid.appendChild(btn);
  });
}

function suggestFollowupChips() {
  const recent = state.messages.slice(-3).map((m) => m.content).join(" ").toLowerCase();
  const topics = [
    { keys: ["leak", "burst", "hydrant", "drip"], chips: [
      ["📷 Send a photo", "I'd like to send a photo of the issue."],
      ["📍 Update location", "I need to update the location of my report."],
      ["👤 Talk to an agent", "I'd like to speak with a customer service representative."],
    ]},
    { keys: ["bill", "payment", "arrears", "invoice"], chips: [
      ["📄 Check my balance", "How can I check my current NAWASA bill balance and consumption?"],
      ["💳 Payment options", "What are my options for paying my NAWASA bill?"],
      ["👤 Talk to an agent", "I'd like to speak with a customer service representative."],
    ]},
    { keys: ["outage", "no water", "supply", "maintenance"], chips: [
      ["🚰 Any updates?", "Are there any updates on the outage in my area?"],
      ["📍 Office locations", "Where are NAWASA's office locations?"],
      ["👤 Talk to an agent", "I'd like to speak with a customer service representative."],
    ]},
  ];
  for (const t of topics) {
    if (t.keys.some((k) => recent.includes(k))) return t.chips;
  }
  return [
    ["👷 Report a leak", "I'd like to report a water leak."],
    ["💳 Billing help", "What are my options for paying my NAWASA bill?"],
    ["👤 Talk to an agent", "I'd like to speak with a customer service representative."],
  ];
}

// ---------------------------------------------------------------------
// FAQ
// ---------------------------------------------------------------------
function renderFAQ(query) {
  const q = (query || "").toLowerCase();
  const results = state.config.faqs.filter(
    (f) => !q || f.q.toLowerCase().includes(q) || f.a.toLowerCase().includes(q) || f.category.toLowerCase().includes(q)
  );
  const list = $("#faqList");
  list.innerHTML = "";
  if (!results.length) {
    list.innerHTML = `<p class="hint-text">No matching FAQ found. Try the Chat tab to ask directly.</p>`;
    return;
  }
  const cats = [...new Set(results.map((f) => f.category))];
  cats.forEach((cat) => {
    const h = document.createElement("div");
    h.className = "faq-cat-heading";
    h.textContent = cat;
    list.appendChild(h);
    results.filter((f) => f.category === cat).forEach((f) => {
      const item = document.createElement("div");
      item.className = "faq-item";
      item.innerHTML = `<div class="faq-cat">${f.category}</div><b>${escapeHtml(f.q)}</b><br>${escapeHtml(f.a)}`;
      // Read-aloud button — reuses the same speakText() helper (and voice
      // preference) already used for chat replies.
      const speakBtn = document.createElement("button");
      speakBtn.type = "button";
      speakBtn.className = "speak-btn";
      speakBtn.title = "Read this answer aloud";
      speakBtn.textContent = "🔊";
      speakBtn.addEventListener("click", () => speakText(`${f.q}. ${f.a}`));
      item.appendChild(speakBtn);
      list.appendChild(item);
    });
  });
  $("#faqSearch").oninput = (e) => renderFAQ(e.target.value);
}

// ---------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------
let pendingAttachment = null; // {name, mime, data_base64}
let pendingReportPhoto = null; // {name, mime, data_base64} — from report form's file input or camera

function renderChat() {
  const box = $("#chatMessages");
  box.innerHTML = "";
  if (!state.messages.length) {
    appendBubble("assistant", welcomeText());
  } else {
    state.messages.forEach((m) => appendBubble(m.role, m.content, m.attachmentName, m.reportCard, m.attachmentMime, m.locationCard));
  }
  $("#messageCount").textContent = `${state.messages.length} messages in this session.`;
  renderFollowupChips();
}

function welcomeText() {
  return "👋 **Welcome to AquaAssist**\n\nI'm NAWASA's official virtual assistant, available 24/7 to help with water outages, billing, new connections, reporting leaks, office locations, FAQs, and general support.\n\nHow may I assist you today?";
}

function appendBubble(role, content, attachmentName, reportCard, attachmentMime, locationCard) {
  const row = document.createElement("div");
  row.className = `msg-row ${role}`;
  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  const avatarSrc = role === "assistant" ? "aquaassist_avatar.png.png" : "user_avatar.png.jpg";
  const avatarFallback = role === "assistant" ? "💧" : "🙂";
  avatar.innerHTML = `<img src="${avatarSrc}" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='inline';" /><span style="display:none;">${avatarFallback}</span>`;
  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  const p = document.createElement("p");
  p.innerHTML = mdLite(content);
  bubble.appendChild(p);
  if (attachmentName) {
    const att = document.createElement("div");
    att.className = "msg-attachment";
    const icon = attachmentMime && attachmentMime.startsWith("audio") ? "🎤" : attachmentMime && attachmentMime.startsWith("video") ? "🎥" : "📎";
    att.textContent = `${icon} ${attachmentName}`;
    bubble.appendChild(att);
  }
  if (reportCard) {
    bubble.appendChild(buildReportCardEl(reportCard));
  }
  if (locationCard) {
    bubble.appendChild(buildLocationCardEl(locationCard));
  }
  if (role === "assistant") {
    const speakBtn = document.createElement("button");
    speakBtn.type = "button";
    speakBtn.className = "speak-btn";
    speakBtn.title = "Read this reply aloud";
    speakBtn.textContent = "🔊";
    speakBtn.addEventListener("click", () => speakText(content));
    bubble.appendChild(speakBtn);
  }
  row.appendChild(avatar);
  row.appendChild(bubble);
  $("#chatMessages").appendChild(row);
  $("#chatMessages").scrollTop = $("#chatMessages").scrollHeight;
  if (role === "assistant" && localStorage.getItem("aqua_read_aloud") === "1") {
    speakText(content);
  }
}

// ---------------------------------------------------------------------
// Text-to-speech
// ---------------------------------------------------------------------
// Primary path: the backend's /api/tts endpoint, which proxies to
// ElevenLabs so AquaAssist can speak with an actual Caribbean-accented
// voice (browsers essentially never ship a real Caribbean voice for the
// native Web Speech API, no matter which one you pick from the list).
//
// Fallback path: if /api/tts isn't configured on the server (no
// ELEVENLABS_API_KEY set) or the request fails for any reason, we fall
// back to the browser's built-in speechSynthesis so read-aloud still
// works — just without the Caribbean accent.
let availableVoices = [];
let currentAudio = null;

// Caribbean English locale codes — used only by the browser-voice fallback
// path below. Some platforms (Edge/Windows in particular) ship neural
// voices for these locales even though they're easy to miss in a long,
// mostly US/UK/AU list.
const CARIBBEAN_LOCALES = [
  "en-gd", // Grenada
  "en-jm", "en-tt", "en-bb", "en-lc", "en-vc", "en-ag", "en-kn", "en-dm", "en-bs", "en-bz", "en-gy",
];

function loadVoices() {
  if (!("speechSynthesis" in window)) return;
  availableVoices = window.speechSynthesis.getVoices();
  populateVoiceSelect();
}

function populateVoiceSelect() {
  const sel = $("#voiceSelect");
  if (!sel || !availableVoices.length) return;
  const englishVoices = availableVoices.filter((v) => v.lang && v.lang.toLowerCase().startsWith("en"));
  const voices = englishVoices.length ? englishVoices : availableVoices;

  const isCaribbean = (v) => CARIBBEAN_LOCALES.includes((v.lang || "").toLowerCase());
  const sortedVoices = [...voices].sort((a, b) => (isCaribbean(b) ? 1 : 0) - (isCaribbean(a) ? 1 : 0));

  const currentValue = sel.value;
  sel.innerHTML = "";
  sortedVoices.forEach((v) => {
    const opt = document.createElement("option");
    opt.value = v.name;
    opt.textContent = isCaribbean(v) ? `🌴 ${v.name} (${v.lang})` : `${v.name} (${v.lang})`;
    sel.appendChild(opt);
  });

  const grenadaVoice = sortedVoices.find((v) => (v.lang || "").toLowerCase() === "en-gd");
  const anyCaribbeanVoice = sortedVoices.find(isCaribbean);
  const saved = localStorage.getItem("aqua_tts_voice");

  if (saved && sortedVoices.some((v) => v.name === saved)) {
    sel.value = saved;
  } else if (currentValue && sortedVoices.some((v) => v.name === currentValue)) {
    sel.value = currentValue;
  } else if (grenadaVoice) {
    sel.value = grenadaVoice.name;
    localStorage.setItem("aqua_tts_voice", grenadaVoice.name);
  } else if (anyCaribbeanVoice) {
    sel.value = anyCaribbeanVoice.name;
    localStorage.setItem("aqua_tts_voice", anyCaribbeanVoice.name);
  } else if (sortedVoices.length) {
    const defaultIdx = sortedVoices.length > 1 ? 1 : 0;
    sel.value = sortedVoices[defaultIdx].name;
    localStorage.setItem("aqua_tts_voice", sortedVoices[defaultIdx].name);
  }
}

function speakWithBrowserVoice(plainText) {
  if (!("speechSynthesis" in window)) return;
  window.speechSynthesis.cancel();
  const utter = new SpeechSynthesisUtterance(plainText);
  utter.rate = 1;
  const savedVoiceName = localStorage.getItem("aqua_tts_voice");
  const voice = availableVoices.find((v) => v.name === savedVoiceName);
  if (voice) utter.voice = voice;
  window.speechSynthesis.speak(utter);
}

async function speakText(text) {
  const plain = text.replace(/\*\*(.+?)\*\*/g, "$1").replace(/[#_*`]/g, "");

  // Stop anything currently playing/queued before starting the next clip.
  if (currentAudio) {
    currentAudio.pause();
    currentAudio = null;
  }
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();

  try {
    const res = await fetch(`${API}/api/tts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: plain }),
    });
    if (!res.ok) throw new Error("tts unavailable");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    currentAudio = new Audio(url);
    currentAudio.addEventListener("ended", () => URL.revokeObjectURL(url));
    await currentAudio.play();
  } catch (err) {
    // Server-side TTS isn't configured or the request failed — fall back
    // to the browser's built-in voice so read-aloud still works.
    speakWithBrowserVoice(plain);
  }
}

function mdLite(text) {
  // Minimal, safe markdown: escape HTML first, then bold + line breaks,
  // then turn bare URLs into clickable links.
  let html = escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\n/g, "<br>");
  html = html.replace(
    /(https?:\/\/[^\s<]+)/g,
    (url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`
  );
  return html;
}
function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

const SEVERITY_COLORS = {
  Unknown: "#005A9C", Low: "#2E9E5B", Medium: "#C98A11", High: "#D64545",
};
function buildReportCardEl(card) {
  const wrap = document.createElement("div");
  wrap.className = "report-card";
  const color = SEVERITY_COLORS[card.severity] || SEVERITY_COLORS.Unknown;
  wrap.innerHTML = `
    <div class="report-card-head">
      <span style="font-size:1.3rem;">✅</span>
      <div><div class="report-card-title">Report logged</div><div class="report-card-ref">${card.reference}</div></div>
    </div>
    <div class="report-card-row"><span>Status</span><b>${card.status}</b></div>
    <div class="report-card-row"><span>Issue</span><b>${card.issue_type}</b></div>
    <div class="report-card-row"><span>Severity</span><b class="severity-badge" style="color:${color};background:${color}22;">${card.severity}</b></div>
  `;
  return wrap;
}

// Nicer bubble for a shared GPS location — shows the resolved parish,
// the accuracy radius reported by the device, and a link to view the pin
// on a real map, instead of just a plain "My current location is..." line.
function buildLocationCardEl(loc) {
  const wrap = document.createElement("div");
  wrap.className = "location-card";
  const mapsUrl = `https://www.openstreetmap.org/?mlat=${loc.lat}&mlon=${loc.lng}#map=16/${loc.lat}/${loc.lng}`;
  wrap.innerHTML = `
    <div class="location-card-head">
      <span style="font-size:1.2rem;">📍</span>
      <div><div class="location-card-title">Shared location</div><div class="location-card-parish">${escapeHtml(loc.parish)}</div></div>
    </div>
    <div class="location-card-row"><span>Accuracy</span><b>${loc.accuracy ? `± ${loc.accuracy} m` : "—"}</b></div>
    <a class="location-card-link" href="${mapsUrl}" target="_blank" rel="noopener">View on map ↗</a>
  `;
  return wrap;
}

function renderFollowupChips() {
  const wrap = $("#followupChips");
  wrap.innerHTML = "";
  if (!state.messages.length || state.messages[state.messages.length - 1].role !== "assistant") return;
  suggestFollowupChips().forEach(([label, prompt]) => {
    const btn = document.createElement("button");
    btn.className = "chip-btn";
    btn.textContent = label;
    btn.addEventListener("click", () => sendMessage(prompt));
    wrap.appendChild(btn);
  });
}

function setupChatForm() {
  $("#chatForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("#chatText").value.trim();
    if (!text && !pendingAttachment) return;
    sendMessage(text);
  });

  $("#chatAttachment").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const b64 = await fileToBase64(file);
    pendingAttachment = { name: file.name, mime: file.type || "application/octet-stream", data_base64: b64 };
    $("#attachmentPreview").textContent = `📎 ${file.name} attached — will be sent with your next message.`;
  });
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",")[1]);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function sendMessage(text, directAttachment, locationCard) {
  const attachment = directAttachment || pendingAttachment;
  const displayText = text || (attachment ? (attachment.mime && attachment.mime.startsWith("audio") ? `🎤 Voice note${attachment.durationLabel ? ` (${attachment.durationLabel})` : ""}` : "📎 Sent an attachment") : "");
  if (!displayText) return;

  state.messages.push({ role: "user", content: displayText, attachmentName: attachment ? attachment.name : null, attachmentMime: attachment ? attachment.mime : null, locationCard: locationCard || null });
  saveMessages();
  appendBubble("user", displayText, attachment ? attachment.name : null, null, attachment ? attachment.mime : null, locationCard);
  $("#chatText").value = "";

  const typingRow = document.createElement("div");
  typingRow.className = "msg-row assistant";
  typingRow.innerHTML = `<div class="msg-avatar">💧</div><div class="msg-bubble"><div class="typing-bubble"><span></span><span></span><span></span></div></div>`;
  $("#chatMessages").appendChild(typingRow);
  $("#chatMessages").scrollTop = $("#chatMessages").scrollHeight;

  const attachments = attachment ? [attachment] : [];
  pendingAttachment = null;
  $("#attachmentPreview").textContent = "";
  $("#chatAttachment").value = "";

  try {
    const res = await fetch(`${API}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, territory: state.territory, message: text || "", attachments }),
    });
    const data = await res.json();
    typingRow.remove();
    if (data.session_id) saveSession(data.session_id);

    if (data.error) {
      appendBubble("assistant", `⚠️ ${data.error}`);
      return;
    }
    state.messages.push({ role: "assistant", content: data.reply, reportCard: data.report_card || null });
    saveMessages();
    appendBubble("assistant", data.reply, null, data.report_card);
    renderFollowupChips();
  } catch (err) {
    typingRow.remove();
    appendBubble("assistant", "⚠️ Something went wrong reaching AquaAssist. Please try again, or call/WhatsApp us directly.");
  }
}

// ---------------------------------------------------------------------
// Outage banners (customer)
// ---------------------------------------------------------------------
async function renderOutageBanners() {
  const parish = localStorage.getItem("aqua_customer_parish");
  const wrap = $("#outageBanners");
  wrap.innerHTML = "";
  if (!parish) return;
  const res = await fetch(`${API}/api/outages`);
  const outages = await res.json();
  const today = new Date().toISOString().slice(0, 10);
  outages.filter((o) => o.parish === parish && o.start_date <= today && o.end_date >= today).forEach((o) => {
    const div = document.createElement("div");
    div.className = "card";
    div.style.borderLeft = "4px solid #F5A623";
    div.innerHTML = `⚠️ <b>Service notice for ${o.parish}:</b> ${escapeHtml(o.message)} (${o.start_date} – ${o.end_date})`;
    wrap.appendChild(div);
  });
}

// ---------------------------------------------------------------------
// Report & Track
// ---------------------------------------------------------------------
function nearestParish(lat, lng) {
  let best = null, bestDist = Infinity;
  for (const [parish, [plat, plng]] of Object.entries(state.config.parish_centers)) {
    const d = (lat - plat) ** 2 + (lng - plng) ** 2;
    if (d < bestDist) { bestDist = d; best = parish; }
  }
  return best;
}

// Straight-line "nearest parish center" is only a rough fallback — parish
// boundaries don't actually line up with distance to one arbitrary point
// per parish, so a house right on a border can easily snap to the wrong
// parish. We try a real reverse-geocode against OpenStreetMap first (which
// knows actual parish/county boundaries) and only fall back to the center
// trick if that lookup fails or the network is unavailable.
const NOMINATIM_PARISH_MAP = {
  "saint george": "St. George's (Capital area)",
  "st george": "St. George's (Capital area)",
  "saint andrew": "St. Andrew's",
  "st andrew": "St. Andrew's",
  "saint david": "St. David's",
  "st david": "St. David's",
  "saint john": "St. John's",
  "st john": "St. John's",
  "saint mark": "St. Mark's",
  "st mark": "St. Mark's",
  "saint patrick": "St. Patrick's",
  "st patrick": "St. Patrick's",
  "carriacou": "Carriacou and Petite Martinique",
  "petite martinique": "Carriacou and Petite Martinique",
  "petit martinique": "Carriacou and Petite Martinique",
};

async function reverseGeocodeParish(lat, lng) {
  try {
    const res = await fetch(
      `https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${lat}&lon=${lng}&zoom=12&addressdetails=1`,
      { headers: { "Accept-Language": "en" } }
    );
    if (!res.ok) return null;
    const data = await res.json();
    const addr = data.address || {};
    const candidates = [addr.county, addr.state_district, addr.state, addr.city_district, addr.suburb, addr.municipality].filter(Boolean);
    for (const raw of candidates) {
      const key = raw.toLowerCase();
      for (const [needle, parish] of Object.entries(NOMINATIM_PARISH_MAP)) {
        if (key.includes(needle)) return parish;
      }
    }
    return null;
  } catch (err) {
    return null;
  }
}

function composedLocation() {
  const landmark = $("#reportLandmark").value.trim();
  const parish = $("#reportParish").value;
  const pin = state.reportPin || { lat: state.config.grenada_center[0], lng: state.config.grenada_center[1] };
  const parts = [landmark, parish].filter(Boolean);
  return `${parts.join(", ")} (GPS: ${pin.lat.toFixed(5)}, ${pin.lng.toFixed(5)})`;
}

function setupReportMap() {
  const center = state.config.grenada_center;
  state.reportPin = { lat: center[0], lng: center[1] };
  state.reportMap = L.map("reportMap").setView(center, 11);
  L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap contributors, SRTM | © OpenTopoMap (CC-BY-SA)",
  }).addTo(state.reportMap);
  state.reportMarker = L.marker(center).addTo(state.reportMap);

  state.reportMap.on("click", (e) => {
    setReportPin(e.latlng.lat, e.latlng.lng);
  });

  $("#gpsBtn").addEventListener("click", () => {
    if (!navigator.geolocation) {
      alert("Geolocation isn't available in this browser.");
      return;
    }
    const btn = $("#gpsBtn");
    const originalLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = "📡 Finding you...";
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        await setReportPin(pos.coords.latitude, pos.coords.longitude);
        btn.disabled = false;
        btn.textContent = originalLabel;
      },
      (err) => {
        btn.disabled = false;
        btn.textContent = originalLabel;
        alert(`Location unavailable: ${err.message}`);
      },
      { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 }
    );
  });

  $("#reportLocation").value = composedLocation();
  $("#reportParish").addEventListener("change", () => { $("#reportLocation").value = composedLocation(); });
  $("#reportLandmark").addEventListener("input", () => { $("#reportLocation").value = composedLocation(); });
}

async function setReportPin(lat, lng) {
  state.reportPin = { lat, lng };
  state.reportMarker.setLatLng([lat, lng]);
  state.reportMap.panTo([lat, lng]);
  const parish = (await reverseGeocodeParish(lat, lng)) || nearestParish(lat, lng);
  $("#reportParish").value = parish;
  $("#reportLocation").value = composedLocation();
}

function setupReportForm() {
  $("#reportFile").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const b64 = await fileToBase64(file);
    pendingReportPhoto = { name: file.name, mime: file.type || "application/octet-stream", data_base64: b64 };
    $("#reportFilePreview").textContent = `📎 ${file.name} attached.`;
  });

  $("#reportForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      name: $("#reportName").value.trim(),
      phone: $("#reportPhone").value.trim(),
      location: $("#reportLocation").value.trim(),
      issue_type: $("#reportIssueType").value,
      description: $("#reportDescription").value.trim(),
      severity: $("#reportSeverity").value,
    };
    if (pendingReportPhoto) {
      body.attachment_mime = pendingReportPhoto.mime;
      body.attachment_base64 = pendingReportPhoto.data_base64;
    }
    const res = await fetch(`${API}/api/report`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await res.json();
    const resultEl = $("#reportResult");
    if (data.error) {
      resultEl.innerHTML = `<p class="error-text">${data.error}</p>`;
      return;
    }
    resultEl.innerHTML = "";
    resultEl.appendChild(buildReportCardEl(data));
    const note = document.createElement("p");
    note.className = "hint-text";
    note.textContent = "Save this reference number to track your report below.";
    resultEl.appendChild(note);
    $("#reportForm").reset();
    $("#reportFilePreview").textContent = "";
    pendingReportPhoto = null;
  });
}

// ---------------------------------------------------------------------
// Camera capture (live photo or short video) — shared modal used by chat and the report form
// ---------------------------------------------------------------------
let cameraStream = null;
let cameraFacingMode = "environment";
let cameraTarget = null; // "chat" | "report"
let cameraMode = "photo"; // "photo" | "video"
let cameraRecorder = null;
let cameraChunks = [];
let capturedPhotoB64 = null;
let capturedVideoBlob = null;

// Pick the best MediaRecorder mimeType a browser actually supports, out of
// a preference-ordered candidate list. Used for both the mic (voice notes)
// and camera (video) recorders so we record in a format Gemini can read
// natively wherever possible, instead of always falling back to whatever
// the browser's default happens to be.
function pickSupportedMimeType(candidates) {
  if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
  return candidates.find((t) => MediaRecorder.isTypeSupported(t)) || "";
}

const PREFERRED_VIDEO_MIME_TYPES = [
  "video/mp4",
  "video/webm;codecs=vp9,opus",
  "video/webm;codecs=vp8,opus",
  "video/webm",
];

function setupCamera() {
  const hasCamera = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  if (!hasCamera) {
    $("#chatCameraBtn").style.display = "none";
    $("#reportCameraBtn").style.display = "none";
    return;
  }

  $("#chatCameraBtn").addEventListener("click", () => openCamera("chat"));
  $("#reportCameraBtn").addEventListener("click", () => openCamera("report"));
  $("#cameraCloseBtn").addEventListener("click", closeCamera);
  $("#cameraSwitchBtn").addEventListener("click", () => {
    cameraFacingMode = cameraFacingMode === "environment" ? "user" : "environment";
    startCameraStream();
  });

  $("#cameraModePhotoBtn").addEventListener("click", () => setCameraMode("photo"));
  $("#cameraModeVideoBtn").addEventListener("click", () => setCameraMode("video"));

  $("#cameraShotBtn").addEventListener("click", () => {
    if (cameraMode === "photo") capturePhoto();
    else toggleVideoRecording();
  });

  $("#cameraRetakeBtn").addEventListener("click", resetCameraCaptureUI);

  $("#cameraUseBtn").addEventListener("click", () => {
    if (cameraMode === "photo" && capturedPhotoB64) {
      attachCapturedMedia(`photo_${Date.now()}.jpg`, "image/jpeg", capturedPhotoB64);
    } else if (cameraMode === "video" && capturedVideoBlob) {
      const type = capturedVideoBlob.type || "video/webm";
      const ext = type.includes("mp4") ? "mp4" : "webm";
      const reader = new FileReader();
      reader.onload = () => attachCapturedMedia(`video_${Date.now()}.${ext}`, type, reader.result.split(",")[1]);
      reader.readAsDataURL(capturedVideoBlob);
      return;
    }
    closeCamera();
  });
}

function attachCapturedMedia(name, mime, b64) {
  const icon = mime.startsWith("video") ? "🎥" : "📷";
  if (cameraTarget === "chat") {
    pendingAttachment = { name, mime, data_base64: b64 };
    $("#attachmentPreview").textContent = `${icon} ${name} attached — will be sent with your next message.`;
  } else if (cameraTarget === "report") {
    pendingReportPhoto = { name, mime, data_base64: b64 };
    $("#reportFilePreview").textContent = `${icon} ${name} captured — will be attached to your report.`;
  }
  closeCamera();
}

function setCameraMode(mode) {
  const changed = cameraMode !== mode;
  cameraMode = mode;
  $("#cameraModePhotoBtn").classList.toggle("active", mode === "photo");
  $("#cameraModeVideoBtn").classList.toggle("active", mode === "video");
  $("#cameraModalTitle").textContent = mode === "photo" ? "📷 Take a photo" : "🎥 Record a video";
  $("#cameraShotBtn").textContent = mode === "photo" ? "Capture" : "● Record";
  resetCameraCaptureUI();
  // The initial stream (opened in photo mode) is requested without a mic
  // track, since photo capture doesn't need audio. Switching into video
  // mode needs its own getUserMedia call with audio:true, or the recorded
  // clip plays back silent — restart the stream whenever the mode actually
  // changes and the modal is already open.
  if (changed && cameraStream) {
    startCameraStream();
  }
}

function resetCameraCaptureUI() {
  capturedPhotoB64 = null;
  capturedVideoBlob = null;
  $("#cameraPreviewImg").style.display = "none";
  $("#cameraPreviewVideo").style.display = "none";
  $("#cameraRecordingStatus").style.display = "none";
  $("#cameraVideo").style.display = "block";
  $("#cameraShotBtn").style.display = "inline-block";
  $("#cameraShotBtn").textContent = cameraMode === "photo" ? "Capture" : "● Record";
  $("#cameraSwitchBtn").style.display = "inline-block";
  $("#cameraRetakeBtn").style.display = "none";
  $("#cameraUseBtn").style.display = "none";
}

function capturePhoto() {
  const video = $("#cameraVideo");
  const canvas = $("#cameraCanvas");
  canvas.width = video.videoWidth || 640;
  canvas.height = video.videoHeight || 480;
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  const dataUrl = canvas.toDataURL("image/jpeg", 0.9);
  capturedPhotoB64 = dataUrl.split(",")[1];
  $("#cameraPreviewImg").src = dataUrl;
  $("#cameraPreviewImg").style.display = "block";
  video.style.display = "none";
  $("#cameraShotBtn").style.display = "none";
  $("#cameraSwitchBtn").style.display = "none";
  $("#cameraRetakeBtn").style.display = "inline-block";
  $("#cameraUseBtn").style.display = "inline-block";
}

function toggleVideoRecording() {
  if (cameraRecorder && cameraRecorder.state === "recording") {
    cameraRecorder.stop();
    return;
  }
  if (!cameraStream) return;
  if (cameraStream.getAudioTracks().length === 0) {
    // Safety net: if we somehow still don't have a mic track (e.g. the
    // browser silently dropped it), grab one now rather than recording
    // a silent clip.
    $("#cameraError").textContent = "Recording without audio — microphone wasn't available.";
    $("#cameraError").style.display = "block";
  } else {
    $("#cameraError").style.display = "none";
  }
  cameraChunks = [];
  const videoMimeType = pickSupportedMimeType(PREFERRED_VIDEO_MIME_TYPES);
  try {
    cameraRecorder = videoMimeType ? new MediaRecorder(cameraStream, { mimeType: videoMimeType }) : new MediaRecorder(cameraStream);
  } catch (err) {
    $("#cameraError").textContent = "Video recording isn't supported in this browser.";
    $("#cameraError").style.display = "block";
    return;
  }
  cameraRecorder.ondataavailable = (e) => { if (e.data.size > 0) cameraChunks.push(e.data); };
  cameraRecorder.onstop = () => {
    const actualType = cameraRecorder.mimeType || videoMimeType || "video/webm";
    capturedVideoBlob = new Blob(cameraChunks, { type: actualType });
    const url = URL.createObjectURL(capturedVideoBlob);
    $("#cameraPreviewVideo").src = url;
    $("#cameraPreviewVideo").style.display = "block";
    $("#cameraVideo").style.display = "none";
    $("#cameraRecordingStatus").style.display = "none";
    $("#cameraShotBtn").style.display = "none";
    $("#cameraSwitchBtn").style.display = "none";
    $("#cameraRetakeBtn").style.display = "inline-block";
    $("#cameraUseBtn").style.display = "inline-block";
  };
  cameraRecorder.start();
  $("#cameraRecordingStatus").style.display = "block";
  $("#cameraShotBtn").textContent = "■ Stop";
}

function openCamera(target) {
  cameraTarget = target;
  $("#cameraModal").style.display = "flex";
  $("#cameraError").style.display = "none";
  setCameraMode("photo");
  startCameraStream();
}

async function startCameraStream() {
  stopCameraStream();
  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: cameraFacingMode }, audio: cameraMode === "video" });
    $("#cameraVideo").srcObject = cameraStream;
  } catch (err) {
    $("#cameraError").textContent = `Camera unavailable: ${err.message}. You can still use the attach button to upload a file.`;
    $("#cameraError").style.display = "block";
  }
}

function stopCameraStream() {
  if (cameraRecorder && cameraRecorder.state === "recording") cameraRecorder.stop();
  if (cameraStream) {
    cameraStream.getTracks().forEach((t) => t.stop());
    cameraStream = null;
  }
}

function closeCamera() {
  stopCameraStream();
  $("#cameraModal").style.display = "none";
}

// ---------------------------------------------------------------------
// Voice notes — records actual audio (like a WhatsApp voice note) and
// sends it as a chat attachment, rather than just transcribing to text.
// ---------------------------------------------------------------------
let voiceRecorder = null;
let voiceChunks = [];
let voiceStartTime = null;

// Gemini's audio understanding reliably reads wav/mp3/aiff/aac/ogg/flac.
// Most browsers only ever offer webm/opus to MediaRecorder for audio, which
// the model can't parse as audio — so voice notes were uploading fine but
// being "heard" as nothing. We prefer any better-supported type the browser
// actually has, and the backend also remuxes webm -> ogg as a second safety
// net for browsers (mainly Chrome/Firefox) that only offer webm.
const PREFERRED_VOICE_MIME_TYPES = [
  "audio/mp4",
  "audio/aac",
  "audio/ogg;codecs=opus",
  "audio/webm;codecs=opus",
  "audio/webm",
];
function extForVoiceMime(mime) {
  if (mime.includes("mp4") || mime.includes("aac")) return "m4a";
  if (mime.includes("ogg")) return "ogg";
  return "webm";
}

function setupMic() {
  const hasMic = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  if (!hasMic) {
    $("#chatMicBtn").style.display = "none";
    return;
  }

  $("#chatMicBtn").addEventListener("click", async () => {
    if (voiceRecorder && voiceRecorder.state === "recording") {
      voiceRecorder.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      voiceChunks = [];
      const mimeType = pickSupportedMimeType(PREFERRED_VOICE_MIME_TYPES);
      voiceRecorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      voiceRecorder.ondataavailable = (e) => { if (e.data.size > 0) voiceChunks.push(e.data); };
      voiceRecorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        $("#chatMicBtn").classList.remove("mic-active");
        $("#micStatus").style.display = "none";
        const actualType = voiceRecorder.mimeType || mimeType || "audio/webm";
        const blob = new Blob(voiceChunks, { type: actualType });
        const seconds = Math.round((Date.now() - voiceStartTime) / 1000);
        if (seconds < 1) return; // accidental tap, discard
        const reader = new FileReader();
        reader.onload = () => {
          const b64 = reader.result.split(",")[1];
          sendMessage("", { name: `voice_note_${Date.now()}.${extForVoiceMime(actualType)}`, mime: actualType, data_base64: b64, durationLabel: `${seconds}s` });
        };
        reader.readAsDataURL(blob);
      };
      voiceRecorder.start();
      voiceStartTime = Date.now();
      $("#chatMicBtn").classList.add("mic-active");
      $("#micStatus").textContent = "🔴 Recording voice note... tap the mic again to send.";
      $("#micStatus").style.display = "block";
    } catch (err) {
      $("#micStatus").textContent = `Mic unavailable: ${err.message}`;
      $("#micStatus").style.display = "block";
    }
  });
}

// ---------------------------------------------------------------------
// Location sharing — lets the assistant see where the customer actually is
// (NAWASA only serves Grenada, Carriacou & Petit Martinique, so we resolve
// straight to the nearest parish rather than asking for a country).
// ---------------------------------------------------------------------
function setupLocationShare() {
  if (!navigator.geolocation) {
    $("#chatLocationBtn").style.display = "none";
    return;
  }
  $("#chatLocationBtn").addEventListener("click", () => {
    const btn = $("#chatLocationBtn");
    btn.disabled = true;
    btn.classList.add("locating");
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        const { latitude, longitude, accuracy } = pos.coords;
        const parish = (await reverseGeocodeParish(latitude, longitude)) || nearestParish(latitude, longitude) || "Grenada";
        btn.disabled = false;
        btn.classList.remove("locating");
        sendLocationMessage(latitude, longitude, accuracy, parish);
      },
      (err) => {
        btn.disabled = false;
        btn.classList.remove("locating");
        alert(`Location unavailable: ${err.message}`);
      },
      { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 }
    );
  });
}

function sendLocationMessage(lat, lng, accuracy, parish) {
  const gpsText = `📍 My current location is ${parish}, Grenada (GPS: ${lat.toFixed(5)}, ${lng.toFixed(5)}).`;
  sendMessage(gpsText, null, { parish, lat, lng, accuracy: accuracy ? Math.round(accuracy) : null });
}

function setupTrackForm() {
  let timer = null;
  $("#trackRef").addEventListener("input", (e) => {
    clearTimeout(timer);
    const ref = e.target.value.trim();
    if (!ref) { $("#trackResult").innerHTML = ""; return; }
    timer = setTimeout(async () => {
      const res = await fetch(`${API}/api/report/${encodeURIComponent(ref)}`);
      const data = await res.json();
      const el = $("#trackResult");
      if (data.error) {
        el.innerHTML = `<p class="error-text">${data.error}</p>`;
        return;
      }
      const stages = state.config.status_stages;
      const idx = stages.indexOf(data.status);
      const pct = Math.round(((idx + 1) / stages.length) * 100);
      el.innerHTML = `
        <p><b>Status:</b> ${data.status}</p>
        <div style="background:var(--bg-soft);border-radius:8px;height:8px;overflow:hidden;margin-bottom:.4rem;">
          <div style="background:var(--primary);height:100%;width:${pct}%;"></div>
        </div>
        <p class="hint-text">${stages.join(" → ")}</p>
        <p><b>Issue:</b> ${escapeHtml(data.issue_type)} — ${escapeHtml(data.description || "")}</p>
        <p><b>Location:</b> ${escapeHtml(data.location)}</p>
        <p><b>Submitted:</b> ${escapeHtml(data.timestamp)}</p>
      `;
    }, 400);
  });
}

// ---------------------------------------------------------------------
// Notify
// ---------------------------------------------------------------------
const NOTIFY_CATEGORIES = ["Planned maintenance", "Water outages", "Emergency repairs", "Service updates"];
function renderNotifyCategories() {
  const wrap = $("#notifyCategories");
  wrap.innerHTML = "";
  NOTIFY_CATEGORIES.forEach((cat) => {
    const label = document.createElement("label");
    label.className = "toggle-row";
    label.innerHTML = `<input type="checkbox" value="${cat}" /> ${cat}`;
    wrap.appendChild(label);
  });
}
function setupNotifyForm() {
  $("#notifyForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const contact = $("#notifyContact").value.trim();
    const categories = $$('#notifyCategories input:checked').map((c) => c.value);
    const el = $("#notifyResult");
    if (!contact || !categories.length) {
      el.innerHTML = `<p class="error-text">Please enter a contact and select at least one category.</p>`;
      return;
    }
    const res = await fetch(`${API}/api/notify`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ contact, categories }),
    });
    const data = await res.json();
    el.innerHTML = data.ok ? `<p style="color:#2E9E5B;">You're subscribed to notifications.</p>` : `<p class="error-text">${data.error}</p>`;
    if (data.ok) $("#notifyForm").reset();
  });
}

// ---------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------
function setupSettings() {
  const dark = $("#darkModeToggle"), hc = $("#highContrastToggle"), large = $("#largeTextToggle"), readAloud = $("#readAloudToggle");
  dark.checked = document.body.classList.contains("dark");
  hc.checked = document.body.classList.contains("high-contrast");
  large.checked = document.body.classList.contains("large-text");
  readAloud.checked = localStorage.getItem("aqua_read_aloud") === "1";

  // The voice picker below only affects the browser-voice fallback path
  // (used when the server's Caribbean-accent TTS isn't reachable), so it
  // stays available even on browsers without speechSynthesis support —
  // it just won't do anything in that case.
  if ("speechSynthesis" in window) {
    loadVoices();
    // Chrome/Edge populate the voice list asynchronously.
    window.speechSynthesis.onvoiceschanged = loadVoices;
  }

  dark.addEventListener("change", () => { document.body.classList.toggle("dark", dark.checked); localStorage.setItem("aqua_dark", dark.checked ? "1" : "0"); });
  hc.addEventListener("change", () => { document.body.classList.toggle("high-contrast", hc.checked); localStorage.setItem("aqua_hc", hc.checked ? "1" : "0"); });
  large.addEventListener("change", () => { document.body.classList.toggle("large-text", large.checked); localStorage.setItem("aqua_large", large.checked ? "1" : "0"); });
  readAloud.addEventListener("change", () => {
    localStorage.setItem("aqua_read_aloud", readAloud.checked ? "1" : "0");
    if (!readAloud.checked) {
      if (currentAudio) { currentAudio.pause(); currentAudio = null; }
      if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    }
  });

  const voiceSelect = $("#voiceSelect");
  if (voiceSelect) {
    if (!("speechSynthesis" in window)) {
      voiceSelect.disabled = true;
    } else {
      voiceSelect.addEventListener("change", () => {
        localStorage.setItem("aqua_tts_voice", voiceSelect.value);
      });
    }
  }

  const parishSelect = $("#customerParishSelect");
  parishSelect.value = localStorage.getItem("aqua_customer_parish") || "";
  parishSelect.addEventListener("change", () => {
    localStorage.setItem("aqua_customer_parish", parishSelect.value);
    renderOutageBanners();
  });

  const territorySelect = $("#settingsTerritorySelect");
  populateSelect(territorySelect, state.config.territories);
  territorySelect.value = state.territory;
  territorySelect.addEventListener("change", () => {
    state.territory = territorySelect.value;
    localStorage.setItem("aqua_territory", state.territory);
    renderContactRow();
  });

  $("#newChatBtn").addEventListener("click", () => {
    state.messages = [];
    saveMessages();
    localStorage.removeItem("aqua_session_id");
    state.sessionId = null;
    renderChat();
  });
}

// ---------------------------------------------------------------------
// Staff portal
// ---------------------------------------------------------------------
function setupStaffPortal() {
  $("#staffLoginBtn").addEventListener("click", async () => {
    const passcode = $("#staffPasscodeInput").value;
    const res = await fetch(`${API}/api/staff/login`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode }),
    });
    const data = await res.json();
    if (data.ok) {
      state.staffPasscode = passcode;
      sessionStorage.setItem("aqua_staff_passcode", passcode);
      staffLoginSuccess();
    } else {
      $("#staffLoginError").style.display = "block";
    }
  });

  $("#staffLogoutBtn").addEventListener("click", () => {
    state.staffPasscode = "";
    sessionStorage.removeItem("aqua_staff_passcode");
    $("#staffLoginCard").style.display = "block";
    $("#staffDashboard").style.display = "none";
  });

  populateSelect($("#outageParish"), state.config.parishes);
  populateSelect($("#quickStatusNew"), state.config.status_stages);

  $("#outageForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      parish: $("#outageParish").value, message: $("#outageMessage").value.trim(),
      start_date: $("#outageStart").value, end_date: $("#outageEnd").value,
    };
    if (!body.message || !body.start_date || !body.end_date) return;
    await staffFetch("/api/outages", { method: "POST", body: JSON.stringify(body) });
    $("#outageForm").reset();
    loadOutages();
  });

  $("#quickStatusForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const ref = $("#quickStatusRef").value;
    const status = $("#quickStatusNew").value;
    if (!ref) return;
    await staffFetch(`/api/reports/${encodeURIComponent(ref)}`, { method: "PATCH", body: JSON.stringify({ status }) });
    loadReports();
  });
}

function staffLoginSuccess() {
  $("#staffLoginCard").style.display = "none";
  $("#staffDashboard").style.display = "block";
  loadReports();
  loadOutages();
  loadNotifySubscribers();
}

async function staffFetch(path, opts = {}) {
  opts.headers = Object.assign({ "Content-Type": "application/json", "X-Staff-Passcode": state.staffPasscode }, opts.headers || {});
  return fetch(`${API}${path}`, opts);
}

async function loadReports() {
  const res = await staffFetch("/api/reports");
  if (res.status === 401) { staffLogout(); return; }
  const reports = await res.json();
  renderStatusMetrics(reports);
  renderReportsTable(reports);
  renderStaffMap(reports);
  populateSelect($("#quickStatusRef"), reports.map((r) => r.reference));
}

function staffLogout() {
  state.staffPasscode = "";
  sessionStorage.removeItem("aqua_staff_passcode");
  $("#staffLoginCard").style.display = "block";
  $("#staffDashboard").style.display = "none";
}

function renderStatusMetrics(reports) {
  const stages = state.config.status_stages;
  const counts = {};
  stages.forEach((s) => (counts[s] = 0));
  reports.forEach((r) => { if (counts[r.status] !== undefined) counts[r.status]++; });
  const emoji = { Received: "🔴", Assigned: "🟠", "Crew Dispatched": "🟠", "In Progress": "🔵", Resolved: "🟢" };
  const wrap = $("#statusMetrics");
  wrap.innerHTML = `<div class="metric-box"><div class="num">${reports.length}</div><div class="label">Total reports</div></div>`;
  stages.forEach((s) => {
    wrap.innerHTML += `<div class="metric-box"><div class="num">${counts[s]}</div><div class="label">${emoji[s] || ""} ${s}</div></div>`;
  });
}

function renderReportsTable(reports) {
  const thead = $("#reportsTable thead"), tbody = $("#reportsTable tbody");
  const cols = ["reference", "timestamp", "name", "phone", "location", "issue_type", "severity", "status", "attachment"];
  thead.innerHTML = `<tr>${cols.map((c) => `<th>${c}</th>`).join("")}</tr>`;
  tbody.innerHTML = "";
  reports.slice().reverse().forEach((r) => {
    const tr = document.createElement("tr");
    tr.innerHTML = cols.map((c) => {
      if (c === "attachment") return `<td>${buildAttachmentCell(r.attachment_mime, r.attachment_data)}</td>`;
      return `<td>${escapeHtml(String(r[c] ?? ""))}</td>`;
    }).join("");
    tbody.appendChild(tr);
  });
}

// Builds the "attachment" cell for a staff report row directly from the
// inline base64 stored on the report — no separate file/URL to fetch, so
// this works the same whether the report was created 5 seconds ago or
// survives a redeploy.
function buildAttachmentCell(mime, dataB64) {
  if (!mime || !dataB64) return `<span class="hint-text">—</span>`;
  const dataUrl = `data:${mime};base64,${dataB64}`;
  if (mime.startsWith("image/")) {
    return `<a href="${dataUrl}" target="_blank" rel="noopener"><img src="${dataUrl}" class="report-thumb" alt="attachment" /></a>`;
  }
  if (mime.startsWith("video/")) {
    return `<a href="${dataUrl}" target="_blank" rel="noopener" class="attachment-link">🎥 View video</a>`;
  }
  if (mime.startsWith("audio/")) {
    return `<a href="${dataUrl}" target="_blank" rel="noopener" class="attachment-link">🎤 Play audio</a>`;
  }
  return `<a href="${dataUrl}" target="_blank" rel="noopener" class="attachment-link">📎 View file</a>`;
}

function renderStaffMap(reports) {
  if (!state.staffMap) {
    state.staffMap = L.map("staffMap").setView(state.config.grenada_center, 10);
    L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
      attribution: "© OpenStreetMap contributors, SRTM | © OpenTopoMap (CC-BY-SA)",
    }).addTo(state.staffMap);
    state.staffMapLayer = L.layerGroup().addTo(state.staffMap);
  }
  state.staffMapLayer.clearLayers();
  const statusColors = { Received: "red", Assigned: "orange", "Crew Dispatched": "orange", "In Progress": "blue", Resolved: "green" };
  const gpsRe = /GPS:\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)/;
  reports.forEach((r) => {
    const m = gpsRe.exec(r.location || "");
    if (!m) return;
    const lat = parseFloat(m[1]), lng = parseFloat(m[2]);
    L.circleMarker([lat, lng], { radius: 8, color: statusColors[r.status] || "gray", fillOpacity: 0.8 })
      .bindPopup(`<b>${r.reference}</b><br>${r.issue_type} — ${r.status}<br>Severity: ${r.severity}`)
      .addTo(state.staffMapLayer);
  });
}

async function loadOutages() {
  const res = await fetch(`${API}/api/outages`);
  const outages = await res.json();
  const list = $("#outageList");
  list.innerHTML = "";
  if (!outages.length) {
    list.innerHTML = `<p class="hint-text">No announcements posted.</p>`;
    return;
  }
  outages.forEach((o) => {
    const row = document.createElement("div");
    row.className = "outage-row";
    row.innerHTML = `<span><b>${escapeHtml(o.parish)}</b> (${o.start_date} – ${o.end_date}): ${escapeHtml(o.message)}</span>`;
    const btn = document.createElement("button");
    btn.textContent = "Remove";
    btn.addEventListener("click", async () => {
      await staffFetch(`/api/outages/${o.id}`, { method: "DELETE" });
      loadOutages();
    });
    row.appendChild(btn);
    list.appendChild(row);
  });
}

async function loadNotifySubscribers() {
  const res = await staffFetch("/api/notify");
  if (res.status === 401) return;
  const subs = await res.json();
  const thead = $("#notifyTable thead"), tbody = $("#notifyTable tbody");
  thead.innerHTML = `<tr><th>timestamp</th><th>contact</th><th>categories</th></tr>`;
  tbody.innerHTML = subs.map((s) => `<tr><td>${escapeHtml(s.timestamp)}</td><td>${escapeHtml(s.contact)}</td><td>${escapeHtml(s.categories)}</td></tr>`).join("");
}

init();
