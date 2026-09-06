// AquaAssist frontend — talks to the Flask backend over /api/*.
// No build step: plain fetch + DOM. Leaflet (via CDN) handles the maps.

const API = ""; // same-origin

const TERRITORY_TO_PARISH = {
  "Carriacou": "Carriacou and Petite Martinique",
  "Petit Martinique": "Carriacou and Petite Martinique",
};

const state = {
  config: null,
  sessionId: localStorage.getItem("aqua_session_id") || null,
  territory: localStorage.getItem("aqua_territory") || "Grenada",
  messages: JSON.parse(localStorage.getItem("aqua_messages") || "[]"),
  staffToken: sessionStorage.getItem("aqua_staff_token") || "",
  staffAccount: JSON.parse(sessionStorage.getItem("aqua_staff_account") || "null"),
  permissionDefs: [],
  staffAccountsCache: [],
  editingAccountId: null,
  faqsAdmin: [],
  currentLiveChatSession: null,
  reportPin: null,
  reportMap: null,
  reportMarker: null,
  staffMap: null,
  features: {},
  tips: [],
  tipIndex: 0,
  tipTimer: null,
  chatbotName: "AquaAssist",
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

function applyChatbotName(name) {
  const clean = (name || "AquaAssist").trim() || "AquaAssist";
  const changed = state.chatbotName !== clean;
  state.chatbotName = clean;
  ["chatbotNameWidgetHeader", "chatbotNameLoginTitle", "chatbotNameHeroTitle"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.textContent = clean;
  });
  if (changed && state.messages.length === 0) {
    renderChat();
  }
}

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
    setTimeout(() => {
      if (state.reportMap) state.reportMap.invalidateSize();
      if (state.staffMap) state.staffMap.invalidateSize();
    }, 260);
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

async function init() {
  setupWidgetToggle();
  applyPrefsFromStorage();
  const res = await fetch(`${API}/api/init`);
  state.config = await res.json();
  applyChatbotName(state.config.chatbot_name);

  populateSelect($("#territorySelect"), state.config.territories);
  $("#territorySelect").value = state.territory;

  setupSiteNav();
  await loadPermissionDefs();
  setupStaffPortal();
  setupVoiceTestButton();

  await loadFeatureFlags();
  applyFeatureVisibility();

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
    if (!localStorage.getItem("aqua_customer_parish") && TERRITORY_TO_PARISH[territory]) {
      localStorage.setItem("aqua_customer_parish", TERRITORY_TO_PARISH[territory]);
    }
    startApp();
  });
}

function setupSiteNav() {
  const showSite = (pushUrl = true) => {
    $("#mockSiteMain").style.display = "";
    $("#staffPortalSection").style.display = "none";
    $("#navStaffPortalLink").classList.remove("active");
    $("#navHomeLink").classList.add("active");
    if (pushUrl && window.location.pathname !== "/") {
      history.pushState({ view: "site" }, "", "/");
    }
  };
  const showStaffPortal = (pushUrl = true) => {
    $("#mockSiteMain").style.display = "none";
    $("#staffPortalSection").style.display = "block";
    $("#navStaffPortalLink").classList.add("active");
    $("#navHomeLink").classList.remove("active");
    window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
    if (pushUrl && window.location.pathname !== "/admin") {
      history.pushState({ view: "admin" }, "", "/admin");
    }
    if (state.staffToken && state.staffAccount) {
      staffLoginSuccess();
    }
  };
  $("#navStaffPortalLink").addEventListener("click", (e) => { e.preventDefault(); showStaffPortal(); });
  $("#navHomeLink").addEventListener("click", (e) => { e.preventDefault(); showSite(); });
  $("#navCustomerPortalCta").addEventListener("click", (e) => {
    e.preventDefault();
    showSite();
    const widget = $("#aquaWidget");
    if (!widget.classList.contains("expanded")) $("#widgetToggleBtn").click();
  });

  window.addEventListener("popstate", () => {
    if (window.location.pathname === "/admin") showStaffPortal(false);
    else showSite(false);
  });

  if (window.location.pathname === "/admin") {
    showStaffPortal(false);
  }
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
  renderForms();
  populateSelect($("#reportParish"), state.config.parishes);
  populateSelect($("#reportIssueType"), state.config.issue_types);
  populateSelect($("#reportSeverity"), state.config.severity_levels);
  populateSelect($("#customerParishSelect"), ["", ...state.config.parishes], (v) => v || "— none selected —");
  renderNotifyCategories();
  renderChat();
  renderOutageBanners();
  setupReportMap();

  setupTabs();
  setupChatForm();
  setupReportForm();
  setupTrackForm();
  setupNotifyForm();
  setupSettings();
  setupCamera();
  setupMic();
  setupLocationShare();
  loadTips();
  startStaffMessagePolling();

  setInterval(async () => {
    const res = await fetch(`${API}/api/business-hours`);
    state.config.business_hours = await res.json();
    renderHero();
  }, 60000);

  setInterval(async () => {
    try {
      const [featRes, initRes] = await Promise.all([
        fetch(`${API}/api/features`),
        fetch(`${API}/api/init`),
      ]);
      state.features = await featRes.json();
      const initData = await initRes.json();
      applyChatbotName(initData.chatbot_name);
      applyFeatureVisibility();
    } catch (err) { /* silent — best-effort background sync */ }
  }, 20000);
}

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

const FEATURE_DEFS = [
  { id: "faqs", label: "FAQs" },
  { id: "water_tips", label: "Water Tips" },
  { id: "report_issue", label: "Report an Issue" },
  { id: "whatsapp", label: "WhatsApp" },
  { id: "voice_notes", label: "Voice Notes" },
  { id: "notify", label: "Get Notified" },
  { id: "camera", label: "Camera" },
  { id: "quick_actions", label: "Quick Actions" },
  { id: "dark_mode", label: "Dark Mode Option" },
  { id: "high_contrast", label: "High Contrast Option" },
  { id: "large_text", label: "Larger Text Option" },
  { id: "read_aloud", label: "Read Aloud Option" },
  { id: "call_us", label: "Call Us" },
  { id: "website", label: "Website" },
  { id: "chatbot_available", label: "Chatbot Available" },
  { id: "settings", label: "Settings Tab" },
  { id: "forms", label: "Forms Tab" },
];

async function loadFeatureFlags() {
  try {
    const res = await fetch(`${API}/api/features`);
    state.features = await res.json();
  } catch (err) {
    state.features = {};
    FEATURE_DEFS.forEach((f) => { state.features[f.id] = true; });
  }
}

function featureEnabled(id) {
  return state.features[id] !== false;
}

function applyFeatureVisibility() {
  const faqTab = $('.tab-btn[data-tab="faq"]');
  if (faqTab) faqTab.style.display = featureEnabled("faqs") ? "" : "none";

  const reportTab = $('.tab-btn[data-tab="report"]');
  if (reportTab) reportTab.style.display = featureEnabled("report_issue") ? "" : "none";

  const notifyTab = $('.tab-btn[data-tab="notify"]');
  if (notifyTab) notifyTab.style.display = featureEnabled("notify") ? "" : "none";

  const settingsTab = $('.tab-btn[data-tab="settings"]');
  if (settingsTab) settingsTab.style.display = featureEnabled("settings") ? "" : "none";

  const formsTab = $('.tab-btn[data-tab="forms"]');
  if (formsTab) formsTab.style.display = featureEnabled("forms") ? "" : "none";

  const hasCameraSupport = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  const cameraEls = [$("#chatCameraBtn"), $("#reportCameraBtn")];
  cameraEls.forEach((el) => { if (el) el.style.display = (featureEnabled("camera") && hasCameraSupport) ? "" : "none"; });

  const waterTipCard = $("#waterTipCard");
  if (waterTipCard) {
    if (featureEnabled("water_tips")) {
      if (state.tips.length) waterTipCard.style.display = "flex";
      startTipRotation();
    } else {
      waterTipCard.style.display = "none";
      stopTipRotation();
    }
  }

  const waEls = [$("#contactWhatsappCard"), $("#whatsappFloat")];
  waEls.forEach((el) => { if (el) el.style.display = featureEnabled("whatsapp") ? "" : "none"; });

  const callCard = $("#contactCallCard");
  if (callCard) callCard.style.display = featureEnabled("call_us") ? "" : "none";
  const websiteCard = $("#contactWebsiteCard");
  if (websiteCard) websiteCard.style.display = featureEnabled("website") ? "" : "none";

  const micBtn = $("#chatMicBtn");
  if (micBtn) micBtn.style.display = featureEnabled("voice_notes") ? "" : "none";

  const qaLabel = $("#quickActionsLabel"), qaGrid = $("#quickActions");
  [qaLabel, qaGrid].forEach((el) => { if (el) el.style.display = featureEnabled("quick_actions") ? "" : "none"; });

  const toggleRow = (inputId) => { const input = $(inputId); return input ? input.closest("label") : null; };
  const darkRow = toggleRow("#darkModeToggle");
  if (darkRow) darkRow.style.display = featureEnabled("dark_mode") ? "" : "none";
  const hcRow = toggleRow("#highContrastToggle");
  if (hcRow) hcRow.style.display = featureEnabled("high_contrast") ? "" : "none";
  const largeRow = toggleRow("#largeTextToggle");
  if (largeRow) largeRow.style.display = featureEnabled("large_text") ? "" : "none";
  const readAloudRow = toggleRow("#readAloudToggle");
  if (readAloudRow) readAloudRow.style.display = featureEnabled("read_aloud") ? "" : "none";
  const voiceLabel = $("#voiceSelectLabel"), voiceSelectEl = $("#voiceSelect");
  [voiceLabel, voiceSelectEl].forEach((el) => { if (el) el.style.display = featureEnabled("read_aloud") ? "" : "none"; });

  document.querySelectorAll(".speak-btn").forEach((btn) => {
    btn.style.display = featureEnabled("read_aloud") ? "" : "none";
  });
  if (!featureEnabled("read_aloud")) {
    stopSpeaking();
  }

  const activeTab = $(".tab-btn.active");
  if (activeTab && activeTab.style.display === "none") {
    const chatTab = $('.tab-btn[data-tab="chat"]');
    if (chatTab) chatTab.click();
  }

  applyMaintenanceMode();
}

function applyMaintenanceMode() {
  const available = featureEnabled("chatbot_available");
  const screen = $("#maintenanceScreen");
  const normalContent = $("#chatNormalContent");
  const tabNav = $(".tab-nav");

  if (normalContent) normalContent.style.display = available ? "" : "none";
  if (tabNav) tabNav.style.display = available ? "" : "none";

  if (!available) {
    $$(".tab-btn").forEach((b) => b.classList.remove("active"));
    $$(".tab-panel").forEach((p) => p.classList.remove("active"));
    const chatTabBtn = $('.tab-btn[data-tab="chat"]');
    const chatPanel = $("#tab-chat");
    if (chatTabBtn) chatTabBtn.classList.add("active");
    if (chatPanel) chatPanel.classList.add("active");
  }

  if (screen) {
    screen.style.display = available ? "none" : "flex";
    if (!available) {
      const msg = (state.features && state.features.maintenance_message) ||
        "AquaAssist is temporarily unavailable.";
      const textEl = $("#maintenanceScreenText");
      if (textEl) textEl.textContent = msg;

      const callBtn = $("#maintenanceCallBtn");
      if (callBtn && state.config) {
        const phoneDigits = state.config.nawasa_phone.replace(/\D/g, "");
        callBtn.href = `tel:${phoneDigits}`;
      }
      const waBtn = $("#maintenanceWhatsappBtn");
      if (waBtn && state.config) {
        const wa = state.config.territory_whatsapp[state.territory] || state.config.territory_whatsapp.Grenada;
        waBtn.href = wa;
      }
    }
  }
}

async function loadTips() {
  try {
    const res = await fetch(`${API}/api/tips`);
    state.tips = await res.json();
  } catch (err) {
    state.tips = [];
  }
  state.tipIndex = 0;
  const card = $("#waterTipCard");
  if (state.tips.length && featureEnabled("water_tips")) {
    card.style.display = "flex";
    renderCurrentTip();
    startTipRotation();
  } else {
    card.style.display = "none";
  }
}

function renderCurrentTip() {
  const textEl = $("#waterTipText");
  if (!textEl || !state.tips.length) return;
  textEl.classList.remove("tip-fade-in");
  void textEl.offsetWidth;
  textEl.textContent = state.tips[state.tipIndex % state.tips.length].text;
  textEl.classList.add("tip-fade-in");
}

function startTipRotation() {
  stopTipRotation();
  if (!state.tips.length || !featureEnabled("water_tips")) return;
  state.tipTimer = setInterval(() => {
    state.tipIndex = (state.tipIndex + 1) % state.tips.length;
    renderCurrentTip();
  }, 25000);
}

function stopTipRotation() {
  if (state.tipTimer) {
    clearInterval(state.tipTimer);
    state.tipTimer = null;
  }
}

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

const QUICK_ACTIONS = [
  { label: "👷 Report a Leak", prompt: "I'd like to report a water leak." },
  { label: "🚰 Water Supply & Outages", prompt: "Are there any scheduled outages or planned maintenance in my area?" },
  { label: "💳 Pay My Bill", prompt: "What are my options for paying my NAWASA bill?" },
  { label: "📄 Check My Bill", prompt: "How can I check my current NAWASA bill balance and consumption?" },
  { label: "📍 Office Locations", prompt: "Where are NAWASA's office locations?" },
  { label: "👤 Speak to an Agent", prompt: "I'd like to speak with a customer service representative." },
  { label: "📄 Forms", tab: "forms" },
];
function renderQuickActions() {
  const grid = $("#quickActions");
  grid.innerHTML = "";
  QUICK_ACTIONS.forEach((qa) => {
    const btn = document.createElement("button");
    btn.className = "quick-action-btn";
    btn.textContent = qa.label;
    btn.addEventListener("click", () => {
      if (qa.tab) { const t = $(`.tab-btn[data-tab="${qa.tab}"]`); if (t) t.click(); return; }
      sendMessage(qa.prompt);
    });
    grid.appendChild(btn);
  });
}

function suggestFollowupChips() {
  const lastAssistant = [...state.messages].reverse().find((m) => m.role === "assistant");
  const lastUser = [...state.messages].reverse().find((m) => m.role === "user");
  const recent = `${lastUser ? lastUser.content : ""} ${lastAssistant ? lastAssistant.content : ""}`.toLowerCase();

  const topics = [
    { keys: ["leak", "burst", "hydrant", "drip"], chips: [
      ["📷 Send a photo", "I'd like to send a photo of the issue."],
      ["📍 Share my location", null, "location"],
      ["👤 Talk to an agent", "I'd like to speak with a customer service representative."],
    ]},
    { keys: ["bill", "payment", "arrears", "invoice", "estimated bill"], chips: [
      ["📄 Check my balance", "How can I check my current NAWASA bill balance and consumption?"],
      ["💳 Payment options", "What are my options for paying my NAWASA bill?"],
      ["❓ Another billing question", "I have another billing question."],
    ]},
    { keys: ["outage", "no water", "supply", "maintenance", "interruption", "low pressure"], chips: [
      ["🚰 Any reported interruptions?", "Would there be any reported interruption or outage in my area right now?"],
      ["📍 Office locations", "Where are NAWASA's office locations?"],
      ["👤 Talk to an agent", "I'd like to speak with a customer service representative."],
    ]},
    { keys: ["new connection", "new service", "connect my"], chips: [
      ["💰 Connection costs", "What does a new connection cost?"],
      ["⏱️ How long does it take?", "How long does it take NAWASA to install a new connection?"],
    ]},
    { keys: ["report logged", "reference number", "nw-"], chips: [
      ["📍 Track this report", null, "track"],
      ["📷 Add a photo later", "Can I add a photo to my report after submitting it?"],
    ]},
  ];

  for (const t of topics) {
    if (t.keys.some((k) => recent.includes(k))) return t.chips;
  }
  return null;
}

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
      if (featureEnabled("read_aloud")) {
        const speakBtn = document.createElement("button");
        speakBtn.type = "button";
        speakBtn.className = "speak-btn";
        speakBtn.title = "Read this answer aloud";
        speakBtn.textContent = "🔊";
        speakBtn.addEventListener("click", () => speakText(`${f.q}. ${f.a}`, speakBtn));
        item.appendChild(speakBtn);
      }
      list.appendChild(item);
    });
  });
  $("#faqSearch").oninput = (e) => renderFAQ(e.target.value);
}

// ---------------------------------------------------------------------
// Forms (customer-facing)
// ---------------------------------------------------------------------
function renderForms() {
  const list = $("#formsList");
  if (!list) return;
  const forms = (state.config && state.config.forms) || [];
  list.innerHTML = "";
  if (!forms.length) {
    list.innerHTML = `<p class="hint-text">No forms are available right now. Please contact NAWASA directly.</p>`;
    return;
  }
  forms.forEach((f) => {
    const item = document.createElement("div");
    item.className = "form-item";
    item.innerHTML = `
      <div class="form-item-name">${escapeHtml(f.name)}</div>
      <div class="form-item-desc">${escapeHtml(f.description)}</div>
      <a class="btn-primary form-open-btn" href="${escapeHtml(f.url)}" target="_blank" rel="noopener noreferrer">Open Form</a>
    `;
    list.appendChild(item);
  });
}

function buildFormCardsEl(cards) {
  const wrap = document.createElement("div");
  wrap.className = "form-cards-wrap";
  cards.forEach((f) => {
    const card = document.createElement("div");
    card.className = "form-card";
    card.innerHTML = `
      <div class="form-card-name">📄 ${escapeHtml(f.name)}</div>
      <div class="form-card-desc">${escapeHtml(f.description)}</div>
      <a class="btn-primary form-open-btn" href="${escapeHtml(f.url)}" target="_blank" rel="noopener noreferrer">Open Form</a>
    `;
    wrap.appendChild(card);
  });
  return wrap;
}

let pendingAttachment = null;
let pendingReportPhoto = null;

function renderChat() {
  const box = $("#chatMessages");
  box.innerHTML = "";
  if (!state.messages.length) {
    appendBubble("assistant", welcomeText());
  } else {
    state.messages.forEach((m) => appendBubble(m.role, m.content, m.attachmentName, m.reportCard, m.attachmentMime, m.locationCard, false, m.formCards));
  }
  $("#messageCount").textContent = `${state.messages.length} messages in this session.`;
  renderFollowupChips();
}

function welcomeText() {
  return `👋 **Welcome to ${state.chatbotName}**\n\nI'm NAWASA's official virtual assistant, available 24/7 to help with water outages, billing, new connections, reporting leaks, office locations, FAQs, and general support.\n\nHow may I assist you today?`;
}

function appendBubble(role, content, attachmentName, reportCard, attachmentMime, locationCard, isLive = true, formCards) {
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
  if (formCards && formCards.length) {
    bubble.appendChild(buildFormCardsEl(formCards));
  }
  let speakBtn = null;
  if (role === "assistant" && featureEnabled("read_aloud")) {
    speakBtn = document.createElement("button");
    speakBtn.type = "button";
    speakBtn.className = "speak-btn";
    speakBtn.title = "Read this reply aloud";
    speakBtn.textContent = "🔊";
    speakBtn.addEventListener("click", () => speakText(content, speakBtn));
    bubble.appendChild(speakBtn);
  }
  row.appendChild(avatar);
  row.appendChild(bubble);
  $("#chatMessages").appendChild(row);
  $("#chatMessages").scrollTop = $("#chatMessages").scrollHeight;
  if (isLive && role === "assistant" && featureEnabled("read_aloud") && localStorage.getItem("aqua_read_aloud") === "1") {
    speakText(content, speakBtn);
  }
}

let availableVoices = [];
let currentAudio = null;
let currentSpeakBtn = null;

const CARIBBEAN_LOCALES = [
  "en-gd", "en-jm", "en-tt", "en-bb", "en-lc", "en-vc", "en-ag", "en-kn", "en-dm", "en-bs", "en-bz", "en-gy",
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

function resetSpeakBtn(btn) {
  if (!btn) return;
  btn.classList.remove("speaking");
  btn.textContent = "🔊";
}

function stopSpeaking() {
  if (currentAudio) {
    currentAudio.pause();
    currentAudio = null;
  }
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  resetSpeakBtn(currentSpeakBtn);
  currentSpeakBtn = null;
}

function speakWithBrowserVoice(plainText, btn) {
  if (!("speechSynthesis" in window)) {
    resetSpeakBtn(btn);
    if (currentSpeakBtn === btn) currentSpeakBtn = null;
    return;
  }
  window.speechSynthesis.cancel();
  const utter = new SpeechSynthesisUtterance(plainText);
  utter.rate = 1;
  const savedVoiceName = localStorage.getItem("aqua_tts_voice");
  const voice = availableVoices.find((v) => v.name === savedVoiceName);
  if (voice) utter.voice = voice;
  utter.onend = () => { resetSpeakBtn(btn); if (currentSpeakBtn === btn) currentSpeakBtn = null; };
  utter.onerror = () => { resetSpeakBtn(btn); if (currentSpeakBtn === btn) currentSpeakBtn = null; };
  window.speechSynthesis.speak(utter);
}

async function speakText(text, btn) {
  if (btn && currentSpeakBtn === btn) {
    stopSpeaking();
    return;
  }
  stopSpeaking();

  if (btn) {
    currentSpeakBtn = btn;
    btn.classList.add("speaking");
    btn.textContent = "⏹️";
  }

  const plain = text.replace(/\*\*(.+?)\*\*/g, "$1").replace(/[#_*`]/g, "");

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
    currentAudio.addEventListener("ended", () => {
      URL.revokeObjectURL(url);
      currentAudio = null;
      resetSpeakBtn(btn);
      if (currentSpeakBtn === btn) currentSpeakBtn = null;
    });
    currentAudio.addEventListener("error", () => {
      resetSpeakBtn(btn);
      if (currentSpeakBtn === btn) currentSpeakBtn = null;
    });
    await currentAudio.play();
  } catch (err) {
    speakWithBrowserVoice(plain, btn);
  }
}

function setupVoiceTestButton() {
  const btn = $("#testVoiceBtn");
  if (!btn) return;
  const statusEl = $("#testVoiceStatus");
  const SAMPLE_TEXT = "Good day, this is AquaAssist, testing the NAWASA voice. If you can hear this clearly, the Caribbean-accent voice is working.";
  let testAudio = null;

  const reset = () => {
    btn.disabled = false;
    btn.textContent = "🔊 Test Caribbean Voice";
  };

  btn.addEventListener("click", async () => {
    if (testAudio && !testAudio.paused) {
      testAudio.pause();
      testAudio = null;
      reset();
      return;
    }

    btn.disabled = true;
    btn.textContent = "⏳ Loading voice...";
    if (statusEl) statusEl.textContent = "";

    try {
      const res = await fetch(`${API}/api/tts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: SAMPLE_TEXT }),
      });
      if (!res.ok) {
        reset();
        if (statusEl) statusEl.textContent = "⚠️ Server voice isn't configured — set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID.";
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      testAudio = new Audio(url);
      btn.disabled = false;
      btn.textContent = "⏹️ Stop test";
      testAudio.addEventListener("ended", () => {
        URL.revokeObjectURL(url);
        testAudio = null;
        reset();
      });
      await testAudio.play();
    } catch (err) {
      reset();
      if (statusEl) statusEl.textContent = "⚠️ Couldn't reach the voice endpoint.";
    }
  });
}

const PHONE_NUMBER_RE = /(\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b/g;

function linkifyPhoneNumbers(html) {
  const parts = html.split(/(<a\b[^>]*>.*?<\/a>)/g);
  return parts.map((part) => {
    if (part.startsWith("<a")) return part;
    return part.replace(PHONE_NUMBER_RE, (match) => {
      const digits = match.replace(/[^\d]/g, "");
      const telDigits = digits.length === 11 ? digits : `1${digits}`;
      return `<a href="tel:+${telDigits}">${match}</a>`;
    });
  }).join("");
}

function mdLite(text) {
  let html = escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\n/g, "<br>");
  html = html.replace(
    /(https?:\/\/[^\s<]+)/g,
    (url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`
  );
  html = linkifyPhoneNumbers(html);
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
  const chips = suggestFollowupChips();
  if (!chips) return;
  chips.forEach(([label, prompt, action]) => {
    const btn = document.createElement("button");
    btn.className = "chip-btn";
    btn.textContent = label;
    btn.addEventListener("click", () => {
      if (action === "location") { $("#chatLocationBtn").click(); return; }
      if (action === "track") { $('.tab-btn[data-tab="report"]').click(); return; }
      sendMessage(prompt);
    });
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

    if (data.paused) {
      const noticeKey = `aqua_paused_notice_${state.sessionId}`;
      if (!localStorage.getItem(noticeKey)) {
        const noticeText = "🧑‍💼 A NAWASA representative is now handling this conversation directly — they'll reply here shortly.";
        state.messages.push({ role: "assistant", content: noticeText });
        saveMessages();
        appendBubble("assistant", noticeText);
        localStorage.setItem(noticeKey, "1");
      }
      renderFollowupChips();
      return;
    }

    if (data.error) {
      appendBubble("assistant", `⚠️ ${data.error}`);
      return;
    }
    state.messages.push({ role: "assistant", content: data.reply, reportCard: data.report_card || null, formCards: data.form_cards || null });
    saveMessages();
    appendBubble("assistant", data.reply, null, data.report_card, null, null, true, data.form_cards);
    renderFollowupChips();
  } catch (err) {
    typingRow.remove();
    appendBubble("assistant", "⚠️ Something went wrong reaching AquaAssist. Please try again, or call/WhatsApp us directly.");
  }
}

let staffPollTimer = null;
function startStaffMessagePolling() {
  if (staffPollTimer) return;
  staffPollTimer = setInterval(async () => {
    if (!state.sessionId) return;
    const lastIdKey = `aqua_last_staff_id_${state.sessionId}`;
    const lastId = parseInt(localStorage.getItem(lastIdKey) || "0", 10);
    try {
      const res = await fetch(`${API}/api/chat/${state.sessionId}/updates?after=${lastId}`);
      if (!res.ok) return;
      const staffMsgs = await res.json();
      if (!staffMsgs.length) return;
      staffMsgs.forEach((m) => {
        const displayText = `🧑‍💼 **NAWASA Support Team:**\n${m.content}`;
        state.messages.push({ role: "assistant", content: displayText });
        appendBubble("assistant", displayText);
        localStorage.setItem(lastIdKey, String(m.id));
      });
      saveMessages();
      renderFollowupChips();
    } catch (err) { /* silent — this is a background poll */ }
  }, 6000);
}

function grenadaTodayISO() {
  const grenadaMs = Date.now() - 4 * 60 * 60 * 1000;
  return new Date(grenadaMs).toISOString().slice(0, 10);
}

async function renderOutageBanners() {
  const parish = localStorage.getItem("aqua_customer_parish");
  const wrap = $("#outageBanners");
  wrap.innerHTML = "";
  if (!parish) {
    wrap.innerHTML = `<div class="card" style="font-size:.8rem;">📍 <a href="#" id="setParishPromptLink">Set your parish</a> to see service notices for your area.</div>`;
    const link = $("#setParishPromptLink");
    if (link) {
      link.addEventListener("click", (e) => {
        e.preventDefault();
        $('.tab-btn[data-tab="settings"]').click();
      });
    }
    return;
  }
  const res = await fetch(`${API}/api/outages`);
  const outages = await res.json();
  const today = grenadaTodayISO();
  outages.filter((o) => o.parish === parish && o.start_date <= today && o.end_date >= today).forEach((o) => {
    const div = document.createElement("div");
    div.className = "card";
    div.style.borderLeft = "4px solid #F5A623";
    div.innerHTML = `⚠️ <b>Service notice for ${o.parish}:</b> ${escapeHtml(o.message)} (${o.start_date} – ${o.end_date})`;
    wrap.appendChild(div);
  });
}

function nearestParish(lat, lng) {
  const pointSets = state.config.parish_reference_points;
  if (pointSets) {
    let best = null, bestDist = Infinity;
    for (const [parish, points] of Object.entries(pointSets)) {
      for (const [plat, plng] of points) {
        const d = (lat - plat) ** 2 + (lng - plng) ** 2;
        if (d < bestDist) { bestDist = d; best = parish; }
      }
    }
    if (best) return best;
  }
  let best = null, bestDist = Infinity;
  for (const [parish, [plat, plng]] of Object.entries(state.config.parish_centers)) {
    const d = (lat - plat) ** 2 + (lng - plng) ** 2;
    if (d < bestDist) { bestDist = d; best = parish; }
  }
  return best;
}

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

let cameraStream = null;
let cameraFacingMode = "environment";
let cameraTarget = null;
let cameraMode = "photo";
let cameraRecorder = null;
let cameraChunks = [];
let capturedPhotoB64 = null;
let capturedVideoBlob = null;

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

let voiceRecorder = null;
let voiceChunks = [];
let voiceStartTime = null;

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
        if (seconds < 1) return;
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
  localStorage.setItem("aqua_customer_parish", parish);
  const parishSelect = $("#customerParishSelect");
  if (parishSelect) parishSelect.value = parish;
  renderOutageBanners();
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

function setupSettings() {
  const dark = $("#darkModeToggle"), hc = $("#highContrastToggle"), large = $("#largeTextToggle"), readAloud = $("#readAloudToggle");
  dark.checked = document.body.classList.contains("dark");
  hc.checked = document.body.classList.contains("high-contrast");
  large.checked = document.body.classList.contains("large-text");
  readAloud.checked = localStorage.getItem("aqua_read_aloud") === "1";

  if ("speechSynthesis" in window) {
    loadVoices();
    window.speechSynthesis.onvoiceschanged = loadVoices;
  }

  dark.addEventListener("change", () => { document.body.classList.toggle("dark", dark.checked); localStorage.setItem("aqua_dark", dark.checked ? "1" : "0"); });
  hc.addEventListener("change", () => { document.body.classList.toggle("high-contrast", hc.checked); localStorage.setItem("aqua_hc", hc.checked ? "1" : "0"); });
  large.addEventListener("change", () => { document.body.classList.toggle("large-text", large.checked); localStorage.setItem("aqua_large", large.checked ? "1" : "0"); });
  readAloud.addEventListener("change", () => {
    localStorage.setItem("aqua_read_aloud", readAloud.checked ? "1" : "0");
    if (!readAloud.checked) {
      stopSpeaking();
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
    if (TERRITORY_TO_PARISH[state.territory]) {
      localStorage.setItem("aqua_customer_parish", TERRITORY_TO_PARISH[state.territory]);
      if (parishSelect) parishSelect.value = TERRITORY_TO_PARISH[state.territory];
    }
    renderContactRow();
    applyMaintenanceMode();
    renderOutageBanners();
  });

  $("#newChatBtn").addEventListener("click", () => {
    state.messages = [];
    saveMessages();
    localStorage.removeItem("aqua_session_id");
    state.sessionId = null;
    renderChat();
  });
}

const SECTION_PERMISSIONS = {
  "overview": null,
  "website-alerts": ["manage_service_alerts", "view_website_management"],
  "website-tips": ["manage_water_tips", "view_website_management"],
  "website-preview": ["view_website_management"],
  "aqua-livechat": ["access_live_chat", "view_aquaassist_dashboard"],
  "aqua-kb": ["manage_faqs", "manage_knowledge_base", "sync_website_content"],
  "aqua-unanswered": ["review_unanswered_questions"],
  "aqua-forms": ["manage_forms", "manage_knowledge_base"],
  "aqua-settings": ["manage_chatbot_settings"],
  "reports-map": ["view_reporting_map"],
  "reports-table": ["view_reports"],
  "reports-notify": ["view_reports", "manage_subscribers"],
  "staff-accounts": ["manage_staff_accounts"],
  "audit-log": ["system_settings", "manage_staff_accounts"],
  "chatbot-name": "SUPER_ADMIN_ONLY",
};

function hasPerm(key) {
  const acct = state.staffAccount;
  if (!acct) return false;
  if (acct.is_super_admin) return true;
  return (acct.permissions || []).includes(key);
}
function hasAnyPerm(keys) {
  if (!keys) return true;
  return keys.some((k) => hasPerm(k));
}

async function loadPermissionDefs() {
  try {
    const res = await fetch(`${API}/api/staff/permission-defs`);
    state.permissionDefs = await res.json();
  } catch (err) {
    state.permissionDefs = [];
  }
}

function setupStaffSidebar() {
  $$(".staff-nav-btn[data-staff-section]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.style.display === "none") return;
      $$(".staff-nav-btn").forEach((b) => b.classList.remove("active"));
      $$(".staff-section").forEach((s) => s.classList.remove("active"));
      btn.classList.add("active");
      const section = $(`#staffSection-${btn.dataset.staffSection}`);
      if (section) section.classList.add("active");
      if (btn.dataset.staffSection === "reports-map" && state.staffMap) {
        setTimeout(() => state.staffMap.invalidateSize(), 50);
      }
      if (btn.dataset.staffSection === "staff-accounts") loadStaffAccountsAdmin();
      if (btn.dataset.staffSection === "audit-log") loadAuditLog();
      if (btn.dataset.staffSection === "chatbot-name") $("#chatbotNameInput").value = state.chatbotName;
      if (btn.dataset.staffSection === "aqua-kb") loadWebsiteContentAdmin();
      if (btn.dataset.staffSection === "aqua-forms") loadFormsAdmin();
    });
  });
}

function applyStaffRoleVisibility() {
  $$(".staff-nav-btn[data-staff-section]").forEach((btn) => {
    const perms = SECTION_PERMISSIONS[btn.dataset.staffSection];
    const visible = perms === "SUPER_ADMIN_ONLY"
      ? !!(state.staffAccount && state.staffAccount.is_super_admin)
      : hasAnyPerm(perms);
    btn.style.display = visible ? "" : "none";
  });
  $$(".staff-nav-group").forEach((g) => {
    let sib = g.nextElementSibling, anyVisible = false;
    while (sib && sib.classList.contains("staff-nav-btn")) {
      if (sib.style.display !== "none") anyVisible = true;
      sib = sib.nextElementSibling;
    }
    g.style.display = anyVisible ? "" : "none";
  });
  const active = $(".staff-nav-btn.active");
  if (active && active.style.display === "none") {
    const overviewBtn = $('.staff-nav-btn[data-staff-section="overview"]');
    if (overviewBtn) overviewBtn.click();
  }

  const label = $("#staffAccountLabel");
  if (label && state.staffAccount) {
    label.textContent = `${state.staffAccount.avatar || "🙂"} ${state.staffAccount.full_name} — ${state.staffAccount.role}`;
  }

  const pwSection = $("#changePasswordSection");
  if (pwSection) pwSection.style.display = (state.staffAccount && state.staffAccount.is_super_admin) ? "block" : "none";
}

async function refreshOverview() {
  try {
    const [reportsRes, outagesRes, tipsRes] = await Promise.all([
      staffFetch("/api/reports"), fetch(`${API}/api/outages`), staffFetch("/api/tips/all"),
    ]);
    const reports = reportsRes.ok ? await reportsRes.json() : [];
    const outages = outagesRes.ok ? await outagesRes.json() : [];
    const tips = tipsRes.ok ? await tipsRes.json() : [];
    renderOverviewCards(reports, outages, tips);
  } catch (err) { /* non-critical */ }
}

function renderOverviewCards(reports, outages, tips) {
  const wrap = $("#overviewCards");
  if (!wrap) return;
  const newCount = reports.filter((r) => r.status === "Received").length;
  const inProgress = reports.filter((r) => ["Assigned", "Crew Dispatched", "In Progress"].includes(r.status)).length;
  const resolved = reports.filter((r) => r.status === "Resolved").length;
  wrap.innerHTML = `
    <div class="metric-box"><div class="num">${outages.length}</div><div class="label">Active alerts</div></div>
    <div class="metric-box"><div class="num">${tips.filter((t) => t.enabled).length}</div><div class="label">Active water tips</div></div>
    <div class="metric-box"><div class="num">${newCount}</div><div class="label">🔴 New reports</div></div>
    <div class="metric-box"><div class="num">${inProgress}</div><div class="label">🔵 In progress</div></div>
    <div class="metric-box"><div class="num">${resolved}</div><div class="label">🟢 Resolved</div></div>
  `;
}

async function loadOverviewAquaStats() {
  const wrapAqua = $("#overviewCardsAqua");
  if (!wrapAqua) return;
  try {
    const [unansweredRes, statsRes, handoffsRes] = await Promise.all([
      staffFetch("/api/unanswered"),
      staffFetch("/api/chat-stats"),
      staffFetch("/api/handoffs"),
    ]);
    const unanswered = unansweredRes.ok ? await unansweredRes.json() : [];
    const stats = statsRes.ok ? await statsRes.json() : { conversations_today: 0, questions_answered_today: 0 };
    const handoffs = handoffsRes.ok ? await handoffsRes.json() : [];
    wrapAqua.innerHTML = `
      <div class="metric-box"><div class="num">${stats.conversations_today}</div><div class="label">💬 Conversations today</div></div>
      <div class="metric-box"><div class="num">${stats.questions_answered_today}</div><div class="label">✅ Replies sent today</div></div>
      <div class="metric-box"><div class="num">${unanswered.length}</div><div class="label">❓ Unanswered questions</div></div>
      <div class="metric-box"><div class="num" id="overviewHandoffMetric">${handoffs.length}</div><div class="label">🆘 Needs a human</div></div>
    `;
  } catch (err) {
    wrapAqua.innerHTML = "";
  }
}

async function loadFaqsAdmin() {
  const res = await staffFetch("/api/faqs");
  if (res.status === 401) { staffLogout(); return; }
  if (res.status === 403) return;
  const faqs = await res.json();
  state.faqsAdmin = faqs;
  renderFaqManageList(faqs);
}

function renderFaqManageList(faqs, query) {
  const wrap = $("#faqManageList");
  if (!wrap) return;
  const q = (query || "").toLowerCase();
  const filtered = faqs.filter((f) => !q || f.q.toLowerCase().includes(q) || f.a.toLowerCase().includes(q) || f.category.toLowerCase().includes(q));
  wrap.innerHTML = "";
  if (!filtered.length) { wrap.innerHTML = `<p class="hint-text">No FAQs found.</p>`; return; }
  filtered.forEach((f) => {
    const row = document.createElement("div");
    row.className = "tip-manage-row";
    const textSpan = document.createElement("span");
    textSpan.className = "tip-manage-text" + (f.enabled ? "" : " tip-disabled");
    textSpan.innerHTML = `<b>[${escapeHtml(f.category)}]</b> ${escapeHtml(f.q)}`;
    row.appendChild(textSpan);

    const actions = document.createElement("div");
    actions.className = "tip-manage-actions";

    const editBtn = document.createElement("button");
    editBtn.type = "button"; editBtn.className = "btn-secondary"; editBtn.textContent = "Edit";
    editBtn.addEventListener("click", async () => {
      const newAnswer = prompt("Edit answer:", f.a);
      if (newAnswer === null || !newAnswer.trim()) return;
      await staffFetch(`/api/faqs/${f.id}`, { method: "PATCH", body: JSON.stringify({ a: newAnswer.trim() }) });
      loadFaqsAdmin();
    });

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button"; toggleBtn.className = "btn-secondary";
    toggleBtn.textContent = f.enabled ? "Disable" : "Enable";
    toggleBtn.addEventListener("click", async () => {
      await staffFetch(`/api/faqs/${f.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !f.enabled }) });
      loadFaqsAdmin();
    });

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button"; deleteBtn.className = "btn-secondary tip-delete-btn"; deleteBtn.textContent = "Remove";
    deleteBtn.addEventListener("click", async () => {
      if (!confirm("Delete this FAQ?")) return;
      await staffFetch(`/api/faqs/${f.id}`, { method: "DELETE" });
      loadFaqsAdmin();
    });

    actions.appendChild(editBtn); actions.appendChild(toggleBtn); actions.appendChild(deleteBtn);
    row.appendChild(actions);
    wrap.appendChild(row);
  });
}

async function loadUnansweredAdmin() {
  const res = await staffFetch("/api/unanswered");
  if (res.status === 401) { staffLogout(); return; }
  if (res.status === 403) return;
  renderUnansweredList(await res.json());
}

function renderUnansweredList(items) {
  const wrap = $("#unansweredList");
  if (!wrap) return;
  wrap.innerHTML = "";
  if (!items.length) { wrap.innerHTML = `<p class="hint-text">Nothing outstanding — AquaAssist has answered everything asked recently.</p>`; return; }
  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "card";
    const info = document.createElement("p");
    info.style.margin = "0 0 .4rem"; info.style.fontWeight = "600";
    info.textContent = item.question;
    const meta = document.createElement("p");
    meta.className = "hint-text"; meta.style.margin = "0 0 .6rem";
    meta.textContent = `Asked ${item.timestamp}`;
    card.appendChild(info);
    card.appendChild(meta);

    const catInput = document.createElement("input");
    catInput.placeholder = "Category (e.g. Billing)";
    const ansInput = document.createElement("textarea");
    ansInput.rows = 2; ansInput.placeholder = "Write the answer to add to the FAQ...";
    const btnRow = document.createElement("div");
    btnRow.style.display = "flex"; btnRow.style.gap = ".5rem";

    const addBtn = document.createElement("button");
    addBtn.type = "button"; addBtn.className = "btn-primary"; addBtn.textContent = "Answer & add to FAQ";
    addBtn.addEventListener("click", async () => {
      const answer = ansInput.value.trim();
      if (!answer) return;
      await staffFetch(`/api/unanswered/${item.id}`, {
        method: "PATCH",
        body: JSON.stringify({ staff_answer: answer, add_to_faq: true, category: catInput.value.trim() || "General", question_text: item.question }),
      });
      loadUnansweredAdmin();
      loadFaqsAdmin();
    });

    const dismissBtn = document.createElement("button");
    dismissBtn.type = "button"; dismissBtn.className = "btn-secondary"; dismissBtn.textContent = "Dismiss";
    dismissBtn.addEventListener("click", async () => {
      await staffFetch(`/api/unanswered/${item.id}`, { method: "PATCH", body: JSON.stringify({ staff_answer: "" }) });
      loadUnansweredAdmin();
    });

    btnRow.appendChild(addBtn); btnRow.appendChild(dismissBtn);
    card.appendChild(catInput); card.appendChild(ansInput); card.appendChild(btnRow);
    wrap.appendChild(card);
  });
}

// ---------------------------------------------------------------------
// Forms admin (Staff Portal — view/edit/enable-disable only, no add/delete)
// ---------------------------------------------------------------------
async function loadFormsAdmin() {
  const res = await staffFetch("/api/forms/all");
  if (res.status === 401) { staffLogout(); return; }
  if (res.status === 403) return;
  renderFormManageList(await res.json());
}

function renderFormManageList(forms) {
  const wrap = $("#formsManageList");
  if (!wrap) return;
  wrap.innerHTML = "";
  if (!forms.length) { wrap.innerHTML = `<p class="hint-text">No forms found.</p>`; return; }
  forms.forEach((f) => {
    const row = document.createElement("div");
    row.className = "tip-manage-row";
    const textSpan = document.createElement("span");
    textSpan.className = "tip-manage-text" + (f.enabled ? "" : " tip-disabled");
    textSpan.innerHTML = `<b>${escapeHtml(f.name)}</b><br><span class="hint-text">${escapeHtml(f.description)}</span><br><span class="hint-text">${escapeHtml(f.url)}</span>`;
    row.appendChild(textSpan);

    const actions = document.createElement("div");
    actions.className = "tip-manage-actions";

    const editBtn = document.createElement("button");
    editBtn.type = "button"; editBtn.className = "btn-secondary"; editBtn.textContent = "Edit";
    editBtn.addEventListener("click", async () => {
      const newName = prompt("Form name:", f.name);
      if (newName === null || !newName.trim()) return;
      const newDesc = prompt("Description:", f.description);
      if (newDesc === null || !newDesc.trim()) return;
      const newUrl = prompt("Official PDF URL:", f.url);
      if (newUrl === null || !newUrl.trim()) return;
      if (!/^https?:\/\//i.test(newUrl.trim())) { alert("URL must start with http:// or https://"); return; }
      const res2 = await staffFetch(`/api/forms/${f.id}`, {
        method: "PATCH",
        body: JSON.stringify({ name: newName.trim(), description: newDesc.trim(), url: newUrl.trim() }),
      });
      const data = await res2.json();
      if (data.error) { alert(data.error); return; }
      loadFormsAdmin();
    });

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button"; toggleBtn.className = "btn-secondary";
    toggleBtn.textContent = f.enabled ? "Disable" : "Enable";
    toggleBtn.addEventListener("click", async () => {
      await staffFetch(`/api/forms/${f.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !f.enabled }) });
      loadFormsAdmin();
    });

    actions.appendChild(editBtn); actions.appendChild(toggleBtn);
    row.appendChild(actions);
    wrap.appendChild(row);
  });
}

let liveChatTranscriptTimer = null;
let liveChatSessionsTimer = null;
let handoffsTimer = null;
let staffAccountRefreshTimer = null;
let knownOpenHandoffIds = new Set();

function formatChatRole(role) {
  if (role === "user") return "🙂 Customer";
  if (role === "staff") return "💧 Staff";
  return "💧 AquaAssist";
}

async function loadLiveChatSessions() {
  const res = await staffFetch("/api/sessions");
  if (res.status === 401) { staffLogout(); return; }
  if (res.status === 403) return;
  const sessions = await res.json();
  renderLiveChatSessionList(sessions);
}

function renderLiveChatSessionList(sessions) {
  const wrap = $("#liveChatSessionList");
  if (!wrap) return;
  wrap.innerHTML = "";
  if (!sessions.length) {
    wrap.innerHTML = `<p class="hint-text">No conversations yet today.</p>`;
    return;
  }
  sessions.forEach((s) => {
    const row = document.createElement("div");
    row.className = "livechat-session-row" + (state.currentLiveChatSession === s.session_id ? " active" : "");
    row.innerHTML = `
      <button type="button" class="livechat-session-main">
        <div class="livechat-session-top">
          <span class="livechat-session-territory">${escapeHtml(s.territory || "Grenada")}${s.paused ? ' <span class="livechat-paused-tag">Paused</span>' : ""}</span>
          <span class="livechat-session-time">${escapeHtml(s.last_timestamp || "")}</span>
        </div>
        <div class="livechat-session-preview">${escapeHtml(formatChatRole(s.last_role))}: ${escapeHtml(s.last_message || "")}</div>
      </button>
      <button type="button" class="livechat-session-delete" title="Delete this conversation">🗑️</button>
    `;
    row.querySelector(".livechat-session-main").addEventListener("click", () => openLiveChatSession(s.session_id));
    row.querySelector(".livechat-session-delete").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this conversation? This can't be undone.")) return;
      await staffFetch(`/api/sessions/${encodeURIComponent(s.session_id)}`, { method: "DELETE" });
      if (state.currentLiveChatSession === s.session_id) {
        closeLiveChatSession();
      }
      loadLiveChatSessions();
      loadHandoffs();
    });
    wrap.appendChild(row);
  });
}

function closeLiveChatSession() {
  state.currentLiveChatSession = null;
  if (liveChatTranscriptTimer) { clearInterval(liveChatTranscriptTimer); liveChatTranscriptTimer = null; }
  $("#liveChatActive").style.display = "none";
  $("#liveChatEmptyState").style.display = "block";
  const suggWrap = $("#liveChatSuggestions");
  if (suggWrap) suggWrap.innerHTML = "";
}

async function refreshLiveChatPauseButton() {
  const btn = $("#liveChatPauseBtn");
  if (!btn || !state.currentLiveChatSession) return;
  const res = await staffFetch(`/api/sessions/${encodeURIComponent(state.currentLiveChatSession)}/status`);
  if (!res.ok) return;
  const data = await res.json();
  btn.dataset.paused = data.paused ? "1" : "0";
  btn.textContent = data.paused ? "▶ Resume AI" : "⏸ Pause AI";
  btn.classList.toggle("livechat-paused-active", data.paused);
}

function openLiveChatSession(sessionId) {
  state.currentLiveChatSession = sessionId;
  $("#liveChatEmptyState").style.display = "none";
  $("#liveChatActive").style.display = "flex";
  $("#liveChatSessionLabel").textContent = sessionId;
  loadLiveChatTranscript();
  refreshLiveChatPauseButton();
  loadLiveChatSuggestions();
  if (liveChatTranscriptTimer) clearInterval(liveChatTranscriptTimer);
  liveChatTranscriptTimer = setInterval(loadLiveChatTranscript, 5000);
  $$(".livechat-session-row").forEach((r) => r.classList.remove("active"));
}

async function loadLiveChatTranscript() {
  if (!state.currentLiveChatSession) return;
  const res = await staffFetch(`/api/sessions/${encodeURIComponent(state.currentLiveChatSession)}/messages`);
  if (res.status === 401) { staffLogout(); return; }
  if (res.status === 403) return;
  const messages = await res.json();
  renderLiveChatTranscript(messages);
}

function renderLiveChatTranscript(messages) {
  const wrap = $("#liveChatTranscript");
  if (!wrap) return;
  const wasAtBottom = wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 20;
  wrap.innerHTML = "";
  messages.forEach((m) => {
    const row = document.createElement("div");
    row.className = `livechat-msg livechat-msg-${m.role}`;
    row.innerHTML = `<div class="livechat-msg-role">${escapeHtml(formatChatRole(m.role))} · <span class="hint-text">${escapeHtml(m.timestamp)}</span></div><div class="livechat-msg-text">${escapeHtml(m.content)}</div>`;
    wrap.appendChild(row);
  });
  if (wasAtBottom || messages.length <= 2) wrap.scrollTop = wrap.scrollHeight;
}

async function loadLiveChatSuggestions() {
  const sessionId = state.currentLiveChatSession;
  if (!sessionId) return;
  const wrap = $("#liveChatSuggestions");
  if (!wrap) return;
  wrap.innerHTML = `<span class="hint-text livechat-suggestions-loading">💡 Thinking of reply ideas...</span>`;
  try {
    const res = await staffFetch(`/api/sessions/${encodeURIComponent(sessionId)}/suggestions`);
    if (sessionId !== state.currentLiveChatSession) return;
    if (!res.ok) { wrap.innerHTML = ""; return; }
    const data = await res.json();
    renderLiveChatSuggestions(data.suggestions || []);
  } catch (err) {
    if (sessionId === state.currentLiveChatSession) wrap.innerHTML = "";
  }
}

function renderLiveChatSuggestions(suggestions) {
  const wrap = $("#liveChatSuggestions");
  if (!wrap) return;
  if (!suggestions.length) {
    wrap.innerHTML = `<span class="hint-text">No suggestions right now — write your own reply below.</span>`;
    return;
  }
  wrap.innerHTML = "";
  suggestions.forEach((text) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "livechat-suggestion-chip";
    chip.textContent = text;
    chip.title = "Click to use this as a starting point — edit before sending";
    chip.addEventListener("click", () => {
      const input = $("#liveChatReplyText");
      input.value = text;
      input.focus();
      input.setSelectionRange(text.length, text.length);
    });
    wrap.appendChild(chip);
  });
}

function setupLiveChatMonitor() {
  const form = $("#liveChatReplyForm");
  if (!form) return;
  let liveChatSending = false;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.currentLiveChatSession) return;
    if (liveChatSending) return;
    const input = $("#liveChatReplyText");
    const text = input.value.trim();
    if (!text) return;
    const submitBtn = form.querySelector('button[type="submit"]');
    liveChatSending = true;
    input.disabled = true;
    if (submitBtn) submitBtn.disabled = true;
    try {
      await staffFetch(`/api/sessions/${encodeURIComponent(state.currentLiveChatSession)}/staff-message`, {
        method: "POST",
        body: JSON.stringify({ message: text }),
      });
      input.value = "";
    } finally {
      liveChatSending = false;
      input.disabled = false;
      if (submitBtn) submitBtn.disabled = false;
    }
    loadLiveChatTranscript();
    loadLiveChatSessions();
    loadHandoffs();
    loadLiveChatSuggestions();
  });

  const suggestBtn = $("#liveChatSuggestBtn");
  if (suggestBtn) suggestBtn.addEventListener("click", () => loadLiveChatSuggestions());

  $("#liveChatRefreshBtn").addEventListener("click", () => {
    loadLiveChatSessions();
    if (state.currentLiveChatSession) loadLiveChatTranscript();
  });

  $("#liveChatPauseBtn").addEventListener("click", async () => {
    if (!state.currentLiveChatSession) return;
    const btn = $("#liveChatPauseBtn");
    const currentlyPaused = btn.dataset.paused === "1";
    btn.disabled = true;
    await staffFetch(
      `/api/sessions/${encodeURIComponent(state.currentLiveChatSession)}/${currentlyPaused ? "resume" : "pause"}`,
      { method: "POST" }
    );
    btn.disabled = false;
    await refreshLiveChatPauseButton();
    loadLiveChatSessions();
    loadHandoffs();
  });

  $("#liveChatDeleteBtn").addEventListener("click", async () => {
    if (!state.currentLiveChatSession) return;
    if (!confirm("Delete this entire conversation? This can't be undone.")) return;
    await staffFetch(`/api/sessions/${encodeURIComponent(state.currentLiveChatSession)}`, { method: "DELETE" });
    closeLiveChatSession();
    loadLiveChatSessions();
    loadHandoffs();
  });

  $("#liveChatClearAllBtn").addEventListener("click", async () => {
    if (!confirm("Delete ALL conversations? This removes every transcript for every customer and can't be undone.")) return;
    await staffFetch("/api/sessions", { method: "DELETE" });
    closeLiveChatSession();
    loadLiveChatSessions();
    loadHandoffs();
  });
}

function playNotifyBeep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.2, ctx.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.4);
  } catch (err) { /* audio not available — ignore */ }
}

function updateHandoffBadge(handoffs) {
  const badge = $("#liveChatBadge");
  if (badge) {
    if (handoffs.length) {
      badge.textContent = String(handoffs.length);
      badge.style.display = "inline-flex";
    } else {
      badge.style.display = "none";
    }
  }
  const metric = $("#overviewHandoffMetric");
  if (metric) metric.textContent = String(handoffs.length);
}

async function loadHandoffs(isFirstLoad = false) {
  const res = await staffFetch("/api/handoffs");
  if (res.status === 401) { staffLogout(); return; }
  if (res.status === 403) return;
  const handoffs = await res.json();
  const currentIds = new Set(handoffs.map((h) => h.id));
  const isNew = [...currentIds].some((id) => !knownOpenHandoffIds.has(id));

  if (isNew && !isFirstLoad && localStorage.getItem("aqua_staff_notify") === "1") {
    playNotifyBeep();
    if ("Notification" in window && Notification.permission === "granted") {
      const latest = handoffs.find((h) => !knownOpenHandoffIds.has(h.id));
      new Notification("AquaAssist — customer needs a live agent", {
        body: latest ? latest.reason : "A conversation was flagged for staff.",
      });
    }
  }
  knownOpenHandoffIds = currentIds;
  updateHandoffBadge(handoffs);
  return handoffs;
}

function setupStaffNotifySetting() {
  const toggle = $("#staffNotifyToggle");
  if (!toggle) return;
  toggle.checked = localStorage.getItem("aqua_staff_notify") === "1";
  toggle.addEventListener("change", async () => {
    if (toggle.checked && "Notification" in window && Notification.permission === "default") {
      await Notification.requestPermission();
    }
    localStorage.setItem("aqua_staff_notify", toggle.checked ? "1" : "0");
  });
}

function permissionCheckboxesHTML(idPrefix, checkedKeys) {
  const checked = new Set(checkedKeys || []);
  const byCategory = {};
  state.permissionDefs.forEach((p) => {
    (byCategory[p.category] = byCategory[p.category] || []).push(p);
  });
  let html = "";
  Object.entries(byCategory).forEach(([category, perms]) => {
    html += `<div class="perm-category-label">${escapeHtml(category)}</div><div class="perm-grid">`;
    perms.forEach((p) => {
      const isChecked = checked.has(p.key) ? "checked" : "";
      html += `<label class="perm-check"><input type="checkbox" data-perm="${p.key}" id="${idPrefix}-${p.key}" ${isChecked} /> ${escapeHtml(p.label)}</label>`;
    });
    html += `</div>`;
  });
  return html;
}

function collectCheckedPermissions(container) {
  return $$(`#${container.id} input[data-perm]:checked`).map((el) => el.dataset.perm);
}

function accountCanBeManagedByMe(account) {
  if (!account.is_super_admin) return true;
  return state.staffAccount && state.staffAccount.id === account.id;
}

async function loadStaffAccountsAdmin() {
  const res = await staffFetch("/api/staff/accounts");
  if (res.status === 401) { staffLogout(); return; }
  if (res.status === 403) return;
  const accounts = await res.json();
  state.staffAccountsCache = accounts;
  renderStaffAccountsTable(accounts);
}

function renderStaffAccountsTable(accounts) {
  const thead = $("#staffAccountsTable thead"), tbody = $("#staffAccountsTable tbody");
  thead.innerHTML = `<tr><th>Name</th><th>Username</th><th>Role</th><th>Status</th><th>Access</th><th></th></tr>`;
  tbody.innerHTML = "";
  accounts.forEach((a) => {
    const tr = document.createElement("tr");
    const access = a.is_super_admin ? "Everything" : (a.permissions.length ? `${a.permissions.length} permission(s)` : "None yet");
    tr.innerHTML = `
      <td>${escapeHtml(a.avatar || "")} ${escapeHtml(a.full_name)}</td>
      <td>${escapeHtml(a.username)}</td>
      <td>${escapeHtml(a.role)}${a.is_super_admin ? " 👑" : ""}</td>
      <td>${a.status === "Active" ? '<span style="color:#2E9E5B;font-weight:700;">Active</span>' : '<span style="color:#D64545;font-weight:700;">Disabled</span>'}</td>
      <td>${escapeHtml(access)}</td>
      <td></td>
    `;
    const actionsTd = tr.lastElementChild;
    const canManage = accountCanBeManagedByMe(a);

    if (hasPerm("edit_accounts") && canManage) {
      const editBtn = document.createElement("button");
      editBtn.type = "button"; editBtn.className = "btn-secondary"; editBtn.textContent = "Edit";
      editBtn.style.marginRight = ".3rem";
      editBtn.addEventListener("click", () => openAccountEditor(a));
      actionsTd.appendChild(editBtn);
    }

    if (hasPerm("manage_permissions") && !a.is_super_admin) {
      const permBtn = document.createElement("button");
      permBtn.type = "button"; permBtn.className = "btn-secondary"; permBtn.textContent = "Permissions";
      permBtn.style.marginRight = ".3rem";
      permBtn.addEventListener("click", () => openPermissionsEditor(a));
      actionsTd.appendChild(permBtn);
    }

    if (state.staffAccount && state.staffAccount.is_super_admin) {
      const resetBtn = document.createElement("button");
      resetBtn.type = "button"; resetBtn.className = "btn-secondary"; resetBtn.textContent = "Reset Password";
      resetBtn.style.marginRight = ".3rem";
      resetBtn.addEventListener("click", async () => {
        const newPass = prompt(`New password for ${a.username} (min 6 characters):`);
        if (!newPass) return;
        const res2 = await staffFetch(`/api/staff/accounts/${a.id}/reset-password`, {
          method: "POST", body: JSON.stringify({ new_password: newPass }),
        });
        const data = await res2.json();
        if (data.error) { alert(data.error); return; }
        alert("Password reset.");
      });
      actionsTd.appendChild(resetBtn);
    }

    if (hasPerm("disable_accounts") && !a.is_super_admin) {
      const toggleBtn = document.createElement("button");
      toggleBtn.type = "button"; toggleBtn.className = "btn-secondary"; toggleBtn.style.marginRight = ".3rem";
      toggleBtn.textContent = a.status === "Active" ? "Disable" : "Enable";
      toggleBtn.addEventListener("click", async () => {
        const newStatus = a.status === "Active" ? "Disabled" : "Active";
        if (newStatus === "Disabled" && !confirm(`Disable ${a.username}? They'll immediately lose access to the Staff Portal.`)) return;
        await staffFetch(`/api/staff/accounts/${a.id}/status`, { method: "PATCH", body: JSON.stringify({ status: newStatus }) });
        loadStaffAccountsAdmin();
      });
      actionsTd.appendChild(toggleBtn);
    }

    if (hasPerm("delete_accounts") && !a.is_super_admin && (!state.staffAccount || a.id !== state.staffAccount.id)) {
      const delBtn = document.createElement("button");
      delBtn.type = "button"; delBtn.className = "btn-secondary tip-delete-btn"; delBtn.textContent = "Delete";
      delBtn.addEventListener("click", async () => {
        if (!confirm(`Permanently delete the account "${a.username}"? This can't be undone.`)) return;
        const res2 = await staffFetch(`/api/staff/accounts/${a.id}`, { method: "DELETE" });
        const data = await res2.json();
        if (data.error) { alert(data.error); return; }
        loadStaffAccountsAdmin();
      });
      actionsTd.appendChild(delBtn);
    }

    tbody.appendChild(tr);
  });
}

function openAccountEditor(account) {
  state.editingAccountId = account ? account.id : null;
  $("#accountFormTitle").textContent = account ? `Edit ${account.username}` : "Create staff account";
  $("#accountFullName").value = account ? account.full_name : "";
  $("#accountUsername").value = account ? account.username : "";
  $("#accountUsername").disabled = !!(account && account.is_super_admin);
  $("#accountRole").value = account ? account.role : "";
  $("#accountAvatar").value = account ? account.avatar : "💧";
  $("#accountPassword").value = "";
  const iAmSuperAdmin = !!(state.staffAccount && state.staffAccount.is_super_admin);
  const showPasswordField = !account || iAmSuperAdmin;
  $("#accountPassword").style.display = showPasswordField ? "" : "none";
  $("#accountPassword").previousElementSibling.style.display = showPasswordField ? "" : "none";
  $("#accountPasswordHint").style.display = showPasswordField ? "none" : "block";
  $("#accountPassword").placeholder = account ? "Leave blank to keep current password" : "Set an initial password";
  $("#accountPassword").required = !account;

  const permsWrap = $("#accountFormPermissions");
  const canAssignPerms = hasPerm("manage_permissions");
  if (!account && canAssignPerms) {
    permsWrap.innerHTML = permissionCheckboxesHTML("newacct", []);
    permsWrap.style.display = "block";
  } else {
    permsWrap.innerHTML = "";
    permsWrap.style.display = "none";
  }

  $("#accountFormCard").style.display = "block";
  $("#accountFormCard").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeAccountEditor() {
  state.editingAccountId = null;
  $("#accountFormCard").style.display = "none";
  $("#accountForm").reset();
}

async function submitAccountForm(e) {
  e.preventDefault();
  const full_name = $("#accountFullName").value.trim();
  const username = $("#accountUsername").value.trim();
  const role = $("#accountRole").value.trim();
  const avatar = $("#accountAvatar").value.trim() || "🙂";
  const password = $("#accountPassword").value;
  const errEl = $("#accountFormError");
  errEl.style.display = "none";

  if (state.editingAccountId) {
    const res = await staffFetch(`/api/staff/accounts/${state.editingAccountId}`, {
      method: "PATCH", body: JSON.stringify({ full_name, username, role, avatar }),
    });
    const data = await res.json();
    if (data.error) { errEl.textContent = data.error; errEl.style.display = "block"; return; }
    if (password) {
      const res2 = await staffFetch(`/api/staff/accounts/${state.editingAccountId}/reset-password`, {
        method: "POST", body: JSON.stringify({ new_password: password }),
      });
      const data2 = await res2.json();
      if (data2.error) { errEl.textContent = data2.error; errEl.style.display = "block"; return; }
    }
  } else {
    const permsWrap = $("#accountFormPermissions");
    const permissions = permsWrap.style.display !== "none" ? collectCheckedPermissions(permsWrap) : [];
    const res = await staffFetch("/api/staff/accounts", {
      method: "POST",
      body: JSON.stringify({ full_name, username, password, role, avatar, permissions }),
    });
    const data = await res.json();
    if (data.error) { errEl.textContent = data.error; errEl.style.display = "block"; return; }
  }
  closeAccountEditor();
  loadStaffAccountsAdmin();
}

function openPermissionsEditor(account) {
  $("#permissionsEditorTitle").textContent = `Permissions — ${account.full_name} (${account.username})`;
  $("#permissionsEditorBody").innerHTML = permissionCheckboxesHTML("permedit", account.permissions);
  $("#permissionsEditorCard").dataset.accountId = account.id;
  $("#permissionsEditorCard").style.display = "block";
  $("#permissionsEditorCard").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closePermissionsEditor() {
  $("#permissionsEditorCard").style.display = "none";
  $("#permissionsEditorBody").innerHTML = "";
}

async function submitPermissionsEditor() {
  const accountId = $("#permissionsEditorCard").dataset.accountId;
  const permissions = collectCheckedPermissions($("#permissionsEditorBody"));
  const res = await staffFetch(`/api/staff/accounts/${accountId}/permissions`, {
    method: "PATCH", body: JSON.stringify({ permissions }),
  });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }
  closePermissionsEditor();
  loadStaffAccountsAdmin();
}

function setupStaffAccountsUI() {
  const createBtn = $("#createAccountBtn");
  if (createBtn) createBtn.addEventListener("click", () => openAccountEditor(null));
  const cancelBtn = $("#accountFormCancelBtn");
  if (cancelBtn) cancelBtn.addEventListener("click", closeAccountEditor);
  const form = $("#accountForm");
  if (form) form.addEventListener("submit", submitAccountForm);

  const permCancelBtn = $("#permissionsEditorCancelBtn");
  if (permCancelBtn) permCancelBtn.addEventListener("click", closePermissionsEditor);
  const permSaveBtn = $("#permissionsEditorSaveBtn");
  if (permSaveBtn) permSaveBtn.addEventListener("click", submitPermissionsEditor);

  const changePwForm = $("#changePasswordForm");
  if (changePwForm) {
    changePwForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const errEl = $("#changePasswordError");
      const okEl = $("#changePasswordSuccess");
      errEl.style.display = "none"; okEl.style.display = "none";
      const current_password = $("#currentPasswordInput").value;
      const new_password = $("#newPasswordInput").value;
      const res = await staffFetch("/api/staff/change-password", {
        method: "POST", body: JSON.stringify({ current_password, new_password }),
      });
      const data = await res.json();
      if (data.error) { errEl.textContent = data.error; errEl.style.display = "block"; return; }
      okEl.style.display = "block";
      changePwForm.reset();
    });
  }

  const chatbotNameForm = $("#chatbotNameForm");
  if (chatbotNameForm) {
    chatbotNameForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const errEl = $("#chatbotNameError");
      const okEl = $("#chatbotNameSuccess");
      errEl.style.display = "none"; okEl.style.display = "none";
      const name = $("#chatbotNameInput").value.trim();
      if (!name) return;
      const res = await staffFetch("/api/settings/chatbot-name", {
        method: "PATCH", body: JSON.stringify({ name }),
      });
      const data = await res.json();
      if (data.error) { errEl.textContent = data.error; errEl.style.display = "block"; return; }
      applyChatbotName(data.chatbot_name);
      okEl.style.display = "block";
      setTimeout(() => { okEl.style.display = "none"; }, 2500);
    });
  }
}

async function loadAuditLog() {
  const res = await staffFetch("/api/audit-log");
  if (res.status === 401) { staffLogout(); return; }
  if (res.status === 403) return;
  const entries = await res.json();
  const thead = $("#auditLogTable thead"), tbody = $("#auditLogTable tbody");
  thead.innerHTML = `<tr><th>Timestamp</th><th>User</th><th>Action</th><th>Item</th><th>Details</th></tr>`;
  tbody.innerHTML = entries.map((e) => `
    <tr>
      <td>${escapeHtml(e.timestamp)}</td>
      <td>${escapeHtml(e.staff_user)}</td>
      <td>${escapeHtml(e.action)}</td>
      <td>${escapeHtml(e.item || "")}</td>
      <td>${escapeHtml(e.details || "")}</td>
    </tr>
  `).join("");
}

async function loadWebsiteContentAdmin() {
  const res = await staffFetch("/api/website-content");
  if (res.status === 401) { staffLogout(); return; }
  if (res.status === 403) { $("#websiteSyncSummary").textContent = ""; return; }
  const pages = await res.json();
  renderWebsiteContentList(pages);
}

function renderWebsiteContentList(pages) {
  const summaryEl = $("#websiteSyncSummary");
  const wrap = $("#websiteContentList");
  const captchaNote = $("#websiteCaptchaNote");
  const looksCaptchaBlocked = pages.some((p) => p.status !== "ok" && /captcha|anti-bot/i.test(p.error || ""));
  if (captchaNote) captchaNote.style.display = looksCaptchaBlocked ? "block" : "none";

  if (!pages.length) {
    if (summaryEl) summaryEl.textContent = "Not synced yet.";
    if (wrap) wrap.innerHTML = `<p class="hint-text">No pages synced yet — click "Sync now" below.</p>`;
    return;
  }
  const okCount = pages.filter((p) => p.status === "ok").length;
  const latest = pages.reduce((a, b) => (a.fetched_at > b.fetched_at ? a : b), pages[0]);
  if (summaryEl) summaryEl.textContent = `${okCount}/${pages.length} pages synced — last attempt ${latest.fetched_at}.`;
  if (!wrap) return;
  wrap.innerHTML = "";
  pages.forEach((p) => {
    const row = document.createElement("div");
    row.className = "tip-manage-row";
    const statusBadge = p.status === "ok" ? `<span style="color:#2E9E5B;">✓ ${p.chars} chars</span>` : p.status === "removed" ? `<span style="color:#999;">— removed</span>` : `<span style="color:#D64545;">✗ ${escapeHtml(p.error || "failed")}</span>`;
    const sourceBadge = p.source === "manual" ? ` <span style="color:#0072BC;">(manually added)</span>` : "";
    const previewHtml = p.status === "ok" && p.preview
      ? `<div class="hint-text" style="margin-top:.2rem;">"${escapeHtml(p.preview)}${p.chars > 220 ? "…" : ""}"</div>`
      : "";
    row.innerHTML = `
      <span class="tip-manage-text">
        <b>${escapeHtml(p.title)}</b> — ${statusBadge}${sourceBadge}<br>
        <span class="hint-text">${escapeHtml(p.url)} · last attempt ${escapeHtml(p.fetched_at)}</span>
        ${previewHtml}
      </span>
    `;
    wrap.appendChild(row);
  });
}

function setupWebsiteSync() {
  const btn = $("#websiteSyncBtn");
  if (btn) {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = "🔄 Syncing...";
      try {
        const res = await staffFetch("/api/website-content/sync", { method: "POST" });
        const data = await res.json();
        if (data.error) {
          alert(`Sync failed: ${data.error}`);
        } else if (data.kb_reseed_ok === false) {
          alert(`Fetched ${data.ok}/${data.total} pages from nawasa.gd successfully, but rebuilding the searchable knowledge base failed (${data.kb_reseed_error || "unknown error"}). The pages are saved — try syncing again in a moment to retry the knowledge-base rebuild.`);
        }
        await loadWebsiteContentAdmin();
        await loadFaqsAdmin();
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    });
  }

  const toggleBtn = $("#websiteManualToggleBtn");
  const manualForm = $("#websiteManualForm");
  if (toggleBtn && manualForm) {
    toggleBtn.addEventListener("click", () => {
      manualForm.style.display = manualForm.style.display === "none" ? "block" : "none";
    });
  }

  const saveBtn = $("#websiteManualSaveBtn");
  if (saveBtn) {
    saveBtn.addEventListener("click", async () => {
      const errEl = $("#websiteManualError");
      errEl.style.display = "none";
      const url = $("#websiteManualUrl").value.trim();
      const title = $("#websiteManualTitle").value.trim();
      const content = $("#websiteManualContent").value.trim();
      saveBtn.disabled = true;
      try {
        const res = await staffFetch("/api/website-content/manual", {
          method: "POST", body: JSON.stringify({ url, title, content }),
        });
        const data = await res.json();
        if (data.error) {
          errEl.textContent = data.error;
          errEl.style.display = "block";
          return;
        }
        $("#websiteManualUrl").value = "";
        $("#websiteManualTitle").value = "";
        $("#websiteManualContent").value = "";
        manualForm.style.display = "none";
        await loadWebsiteContentAdmin();
        await loadFaqsAdmin();
      } finally {
        saveBtn.disabled = false;
      }
    });
  }
}

function setupStaffPortal() {
  setupReportsTableActions();
  setupReportNotes();
  setupStaffSidebar();
  setupLiveChatMonitor();
  setupStaffNotifySetting();
  setupStaffAccountsUI();
  setupWebsiteSync();

  $("#staffLoginBtn").addEventListener("click", async () => {
    const username = $("#staffUsernameInput").value.trim();
    const password = $("#staffPasswordInputField").value;
    $("#staffLoginError").style.display = "none";
    if (!username || !password) {
      $("#staffLoginError").textContent = "Enter a username and password.";
      $("#staffLoginError").style.display = "block";
      return;
    }
    const res = await fetch(`${API}/api/staff/login`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    if (data.ok) {
      state.staffToken = data.token;
      state.staffAccount = data.account;
      sessionStorage.setItem("aqua_staff_token", data.token);
      sessionStorage.setItem("aqua_staff_account", JSON.stringify(data.account));
      staffLoginSuccess();
    } else {
      $("#staffLoginError").textContent = data.error || "Login failed.";
      $("#staffLoginError").style.display = "block";
    }
  });
  $("#staffPasswordInputField").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("#staffLoginBtn").click();
  });

  $("#staffLogoutBtn").addEventListener("click", () => {
    staffFetch("/api/staff/logout", { method: "POST" }).catch(() => {});
    staffLogout();
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
    refreshOverview();
  });

  $("#quickStatusForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const ref = $("#quickStatusRef").value;
    const status = $("#quickStatusNew").value;
    if (!ref) return;
    await staffFetch(`/api/reports/${encodeURIComponent(ref)}`, { method: "PATCH", body: JSON.stringify({ status }) });
    loadReports();
    refreshOverview();
  });

  $("#tipForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = $("#tipText").value.trim();
    if (!text) return;
    await staffFetch("/api/tips", { method: "POST", body: JSON.stringify({ text }) });
    $("#tipForm").reset();
    loadTipsAdmin();
    refreshOverview();
  });

  $("#faqForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const category = $("#faqCategory").value.trim();
    const q = $("#faqQuestion").value.trim();
    const a = $("#faqAnswer").value.trim();
    if (!category || !q || !a) return;
    await staffFetch("/api/faqs", { method: "POST", body: JSON.stringify({ category, q, a }) });
    $("#faqForm").reset();
    loadFaqsAdmin();
  });
  $("#faqSearchAdmin").addEventListener("input", (e) => renderFaqManageList(state.faqsAdmin || [], e.target.value));

  $("#saveMaintenanceMessageBtn").addEventListener("click", async () => {
    const text = $("#maintenanceMessageText").value.trim();
    if (!text) return;
    await staffFetch("/api/features", { method: "PATCH", body: JSON.stringify({ maintenance_message: text }) });
    state.features.maintenance_message = text;
    applyFeatureVisibility();
    const saved = $("#maintenanceMessageSaved");
    if (saved) {
      saved.style.display = "block";
      setTimeout(() => { saved.style.display = "none"; }, 2000);
    }
  });
}

async function refreshMyStaffAccount() {
  try {
    const res = await staffFetch("/api/staff/me");
    if (res.status === 401 || res.status === 403) {
      staffLogout();
      return;
    }
    if (!res.ok) return;
    const account = await res.json();
    const changed = JSON.stringify(account) !== JSON.stringify(state.staffAccount);
    if (changed) {
      state.staffAccount = account;
      sessionStorage.setItem("aqua_staff_account", JSON.stringify(account));
      applyStaffRoleVisibility();
    }
  } catch (err) { /* best-effort background sync — a network hiccup here is not worth surfacing */ }
}

function staffLoginSuccess() {
  $("#staffLoginCard").style.display = "none";
  $("#staffDashboard").style.display = "block";
  applyStaffRoleVisibility();
  loadReports();
  loadOutages();
  loadNotifySubscribers();
  loadTipsAdmin();
  loadFeaturesAdmin();
  loadFaqsAdmin();
  loadFormsAdmin();
  loadWebsiteContentAdmin();
  loadUnansweredAdmin();
  refreshOverview();
  loadOverviewAquaStats();
  loadLiveChatSessions();
  if (liveChatSessionsTimer) clearInterval(liveChatSessionsTimer);
  liveChatSessionsTimer = setInterval(loadLiveChatSessions, 8000);
  knownOpenHandoffIds = new Set();
  loadHandoffs(true);
  if (handoffsTimer) clearInterval(handoffsTimer);
  handoffsTimer = setInterval(() => loadHandoffs(false), 8000);
  if (staffAccountRefreshTimer) clearInterval(staffAccountRefreshTimer);
  staffAccountRefreshTimer = setInterval(refreshMyStaffAccount, 30000);
}

function staffLogout() {
  state.staffToken = "";
  state.staffAccount = null;
  state.currentLiveChatSession = null;
  if (liveChatTranscriptTimer) { clearInterval(liveChatTranscriptTimer); liveChatTranscriptTimer = null; }
  if (liveChatSessionsTimer) { clearInterval(liveChatSessionsTimer); liveChatSessionsTimer = null; }
  if (handoffsTimer) { clearInterval(handoffsTimer); handoffsTimer = null; }
  if (staffAccountRefreshTimer) { clearInterval(staffAccountRefreshTimer); staffAccountRefreshTimer = null; }
  knownOpenHandoffIds = new Set();
  sessionStorage.removeItem("aqua_staff_token");
  sessionStorage.removeItem("aqua_staff_account");
  $("#staffLoginCard").style.display = "block";
  $("#staffDashboard").style.display = "none";
  $("#staffUsernameInput").value = "";
  $("#staffPasswordInputField").value = "";
}

async function loadTipsAdmin() {
  const res = await staffFetch("/api/tips/all");
  if (res.status === 401) { staffLogout(); return; }
  if (res.status === 403) return;
  const tips = await res.json();
  renderTipManageList(tips);
}

function renderTipManageList(tips) {
  const wrap = $("#tipList");
  wrap.innerHTML = "";
  if (!tips.length) {
    wrap.innerHTML = `<p class="hint-text">No tips added yet.</p>`;
    return;
  }
  tips.forEach((tip) => {
    const row = document.createElement("div");
    row.className = "tip-manage-row";

    const textEl = document.createElement("span");
    textEl.className = "tip-manage-text" + (tip.enabled ? "" : " tip-disabled");
    textEl.textContent = tip.text;
    row.appendChild(textEl);

    const actions = document.createElement("div");
    actions.className = "tip-manage-actions";

    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "btn-secondary";
    editBtn.textContent = "Edit";
    editBtn.addEventListener("click", async () => {
      const updated = prompt("Edit tip text:", tip.text);
      if (updated === null) return;
      const trimmed = updated.trim();
      if (!trimmed) return;
      await staffFetch(`/api/tips/${tip.id}`, { method: "PATCH", body: JSON.stringify({ text: trimmed }) });
      loadTipsAdmin();
    });

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "btn-secondary";
    toggleBtn.textContent = tip.enabled ? "Disable" : "Enable";
    toggleBtn.addEventListener("click", async () => {
      await staffFetch(`/api/tips/${tip.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !tip.enabled }) });
      loadTipsAdmin();
    });

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "btn-secondary tip-delete-btn";
    deleteBtn.textContent = "Remove";
    deleteBtn.addEventListener("click", async () => {
      await staffFetch(`/api/tips/${tip.id}`, { method: "DELETE" });
      loadTipsAdmin();
    });

    actions.appendChild(editBtn);
    actions.appendChild(toggleBtn);
    actions.appendChild(deleteBtn);
    row.appendChild(actions);
    wrap.appendChild(row);
  });
}

async function loadFeaturesAdmin() {
  const res = await staffFetch("/api/features");
  if (res.status === 401) { staffLogout(); return; }
  if (res.status === 403) return;
  const flags = await res.json();
  state.features = flags;
  renderFeatureToggleList(flags);
  const msgField = $("#maintenanceMessageText");
  if (msgField) msgField.value = flags.maintenance_message || "";
}

function renderFeatureToggleList(flags) {
  const wrap = $("#featureToggleList");
  wrap.innerHTML = "";
  FEATURE_DEFS.forEach((f) => {
    const label = document.createElement("label");
    label.className = "toggle-row";
    const checked = flags[f.id] !== false;
    label.innerHTML = `<input type="checkbox" data-feature="${f.id}" ${checked ? "checked" : ""} /> ${f.label}`;
    label.querySelector("input").addEventListener("change", async (e) => {
      const enabled = e.target.checked;
      await staffFetch("/api/features", { method: "PATCH", body: JSON.stringify({ [f.id]: enabled }) });
      state.features[f.id] = enabled;
      applyFeatureVisibility();
      if (f.id === "water_tips") loadTips();
    });
    wrap.appendChild(label);
  });
}

async function staffFetch(path, opts = {}) {
  opts.headers = Object.assign({ "Content-Type": "application/json", "X-Staff-Token": state.staffToken }, opts.headers || {});
  return fetch(`${API}${path}`, opts);
}

async function loadReports() {
  const res = await staffFetch("/api/reports");
  if (res.status === 401) { staffLogout(); return; }
  if (res.status === 403) return;
  const reports = await res.json();
  renderStatusMetrics(reports);
  renderReportsTable(reports);
  renderStaffMap(reports);
  populateSelect($("#quickStatusRef"), reports.map((r) => r.reference));
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
  const cols = ["reference", "timestamp", "name", "phone", "location", "issue_type", "severity", "status", "attachment", "notes", "delete"];
  thead.innerHTML = `<tr>${cols.map((c) => `<th>${c === "delete" || c === "notes" ? "" : c}</th>`).join("")}</tr>`;
  tbody.innerHTML = "";
  staffReportsCache = {};
  reports.slice().reverse().forEach((r) => {
    staffReportsCache[r.reference] = r;
    const tr = document.createElement("tr");
    tr.innerHTML = cols.map((c) => {
      if (c === "attachment") return `<td>${buildAttachmentCell(r)}</td>`;
      if (c === "notes") return `<td><button type="button" class="report-notes-btn btn-secondary" data-ref="${escapeHtml(r.reference)}" title="Internal notes" style="padding:.3rem .5rem;font-size:.78rem;">📝</button></td>`;
      if (c === "delete") return `<td>${hasPerm("edit_reports") ? `<button type="button" class="delete-report-btn" data-ref="${escapeHtml(r.reference)}" title="Delete this report">🗑️</button>` : ""}</td>`;
      return `<td>${escapeHtml(String(r[c] ?? ""))}</td>`;
    }).join("");
    tbody.appendChild(tr);
  });
}

let staffReportsCache = {};

function buildAttachmentCell(r) {
  const mime = r.attachment_mime, dataB64 = r.attachment_data;
  if (!mime || !dataB64) return `<span class="hint-text">—</span>`;
  const ref = escapeHtml(r.reference);
  if (mime.startsWith("image/")) {
    return `<button type="button" class="attachment-cell-btn" data-ref="${ref}" title="View full size"><img src="data:${mime};base64,${dataB64}" class="report-thumb" alt="attachment" /></button>`;
  }
  const label = mime.startsWith("video/") ? "🎥 View video" : mime.startsWith("audio/") ? "🎤 Play audio" : "📎 View file";
  return `<button type="button" class="attachment-cell-btn attachment-link" data-ref="${ref}">${label}</button>`;
}

function openAttachmentViewer(mime, dataB64, refLabel) {
  const dataUrl = `data:${mime};base64,${dataB64}`;
  const body = $("#attachmentViewerBody");
  $("#attachmentViewerTitle").textContent = refLabel ? `Attachment — ${refLabel}` : "Attachment";
  if (mime.startsWith("image/")) {
    body.innerHTML = `<img src="${dataUrl}" class="attachment-viewer-media" alt="attachment" />`;
  } else if (mime.startsWith("video/")) {
    body.innerHTML = `<video src="${dataUrl}" class="attachment-viewer-media" controls autoplay></video>`;
  } else if (mime.startsWith("audio/")) {
    body.innerHTML = `<audio src="${dataUrl}" controls autoplay style="width:100%;"></audio>`;
  } else {
    body.innerHTML = `<div style="text-align:center;"><p class="hint-text">This file type can't be previewed here.</p><a href="${dataUrl}" download class="btn-primary" style="display:inline-block;margin-top:.6rem;">⬇ Download file</a></div>`;
  }
  $("#attachmentViewerModal").style.display = "flex";
}

function closeAttachmentViewer() {
  $("#attachmentViewerBody").innerHTML = "";
  $("#attachmentViewerModal").style.display = "none";
}

function setupReportsTableActions() {
  $("#attachmentViewerCloseBtn").addEventListener("click", closeAttachmentViewer);
  $("#attachmentViewerModal").addEventListener("click", (e) => {
    if (e.target.id === "attachmentViewerModal") closeAttachmentViewer();
  });
  $("#reportsTable").addEventListener("click", async (e) => {
    const attachBtn = e.target.closest(".attachment-cell-btn");
    if (attachBtn) {
      const report = staffReportsCache[attachBtn.dataset.ref];
      if (!report || !report.attachment_mime || !report.attachment_data) return;
      openAttachmentViewer(report.attachment_mime, report.attachment_data, report.reference);
      return;
    }
    const notesBtn = e.target.closest(".report-notes-btn");
    if (notesBtn) {
      openReportNotes(notesBtn.dataset.ref);
      return;
    }
    const deleteBtn = e.target.closest(".delete-report-btn");
    if (deleteBtn) {
      const ref = deleteBtn.dataset.ref;
      if (!confirm(`Delete report ${ref}? This can't be undone.`)) return;
      deleteBtn.disabled = true;
      await staffFetch(`/api/reports/${encodeURIComponent(ref)}`, { method: "DELETE" });
      loadReports();
      refreshOverview();
    }
  });
}

let reportNotesCurrentRef = null;

async function openReportNotes(reference) {
  reportNotesCurrentRef = reference;
  $("#reportNotesTitle").textContent = `Internal notes — ${reference}`;
  $("#reportNotesInput").value = "";
  const form = $("#reportNotesForm");
  form.style.display = hasPerm("add_internal_notes") ? "" : "none";
  $("#reportNotesModal").style.display = "flex";
  await loadReportNotes(reference);
}

async function loadReportNotes(reference) {
  const listEl = $("#reportNotesList");
  listEl.innerHTML = `<p class="hint-text">Loading...</p>`;
  const res = await staffFetch(`/api/reports/${encodeURIComponent(reference)}/notes`);
  if (!res.ok) {
    listEl.innerHTML = `<p class="hint-text">Couldn't load notes.</p>`;
    return;
  }
  const notes = await res.json();
  renderReportNotesList(notes);
}

function renderReportNotesList(notes) {
  const listEl = $("#reportNotesList");
  if (!notes.length) {
    listEl.innerHTML = `<p class="hint-text">No internal notes yet.</p>`;
    return;
  }
  listEl.innerHTML = notes.map((n) => `
    <div class="tip-manage-row" style="display:block;">
      <div class="tip-manage-text">${escapeHtml(n.note)}</div>
      <div class="hint-text" style="margin-top:.25rem;">— ${escapeHtml(n.author)} · ${escapeHtml(n.timestamp)}</div>
    </div>
  `).join("");
}

function closeReportNotes() {
  $("#reportNotesModal").style.display = "none";
  reportNotesCurrentRef = null;
}

function setupReportNotes() {
  $("#reportNotesCloseBtn").addEventListener("click", closeReportNotes);
  $("#reportNotesModal").addEventListener("click", (e) => {
    if (e.target.id === "reportNotesModal") closeReportNotes();
  });
  $("#reportNotesForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!reportNotesCurrentRef) return;
    const input = $("#reportNotesInput");
    const note = input.value.trim();
    if (!note) return;
    const submitBtn = e.target.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    try {
      const res = await staffFetch(`/api/reports/${encodeURIComponent(reportNotesCurrentRef)}/notes`, {
        method: "POST", body: JSON.stringify({ note }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        alert(data.error || "Couldn't add note.");
        return;
      }
      input.value = "";
      await loadReportNotes(reportNotesCurrentRef);
    } finally {
      submitBtn.disabled = false;
    }
  });
}

function renderStaffMap(reports) {
  if (!state.staffMap) {
    state.staffMap = L.map("staffMap").setView(state.config.grenada_center, 10);
    L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
      attribution: "© OpenStreetMap contributors, SRTM | © OpenTopoMap (CC-BY-SA)",
    }).addTo(state.staffMap);
    state.staffMapLayer = L.layerGroup().addTo(state.staffMap);
    setTimeout(() => { if (state.staffMap) state.staffMap.invalidateSize(); }, 200);
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
      refreshOverview();
    });
    row.appendChild(btn);
    list.appendChild(row);
  });
}

async function loadNotifySubscribers() {
  const res = await staffFetch("/api/notify");
  if (res.status === 401) return;
  if (res.status === 403) return;
  const subs = await res.json();
  renderSubscribersTable(subs);
}

function renderSubscribersTable(subs) {
  const countEl = $("#subscriberCount");
  if (countEl) countEl.textContent = `${subs.length} subscriber${subs.length === 1 ? "" : "s"}`;

  const thead = $("#notifyTable thead"), tbody = $("#notifyTable tbody");
  const canManage = hasPerm("manage_subscribers");
  thead.innerHTML = `<tr><th>Subscribed</th><th>Contact</th><th>Categories</th>${canManage ? "<th></th>" : ""}</tr>`;
  tbody.innerHTML = "";
  if (!subs.length) {
    tbody.innerHTML = `<tr><td colspan="4"><span class="hint-text">No subscribers yet.</span></td></tr>`;
    return;
  }
  subs.slice().reverse().forEach((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(s.timestamp)}</td>
      <td>${escapeHtml(s.contact)}</td>
      <td>${escapeHtml(s.categories)}</td>
      ${canManage ? "<td></td>" : ""}
    `;
    if (canManage) {
      const delBtn = document.createElement("button");
      delBtn.type = "button"; delBtn.className = "btn-secondary tip-delete-btn"; delBtn.textContent = "Delete";
      delBtn.addEventListener("click", async () => {
        if (!confirm(`Remove subscriber "${s.contact}"? This can't be undone.`)) return;
        delBtn.disabled = true;
        const res2 = await staffFetch(`/api/notify/${s.id}`, { method: "DELETE" });
        if (!res2.ok) {
          const data = await res2.json().catch(() => ({}));
          alert(data.error || "Couldn't delete this subscriber.");
          delBtn.disabled = false;
          return;
        }
        loadNotifySubscribers();
      });
      tr.lastElementChild.appendChild(delBtn);
    }
    tbody.appendChild(tr);
  });
}

init();
