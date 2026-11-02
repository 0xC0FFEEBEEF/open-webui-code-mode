/*
 * code-toggle.js — the "Chat | Code" toggle for Open WebUI.
 *
 * Adds a small pill to the top bar. In Code mode, requests for the current
 * chat are rewritten so they go to the coding agent instead of the selected
 * chat model (the model picker itself is left untouched). Mode is stored per
 * chat in localStorage.
 *
 * Feature chats: the ✦ Feature button starts a chat in which Ornith extends Code
 * mode itself on a branch; such chats are marked in the sidebar, and the Features
 * panel (served by code-deploy on this host, port 8095) deploys and rolls them back.
 *
 * GitHub button: next to the thinking pill; its panel signs in to GitHub, lists, creates and switches repos, and
 * makes the chat work on one ("Work on it": the Code pipe clones it into the chat's project, or gives a chat's own
 * project an empty repo). In such a chat it pushes to a branch, lists the versions and rolls back. The pipe does
 * the GitHub work and keeps the token.
 *
 * Thinking pill: a small Off / Low / Med / High (or Off / On) control in the message bar for the picked model,
 * remembered per model and sent with each request (Chat: reasoning_effort; Code: an opencode model variant).
 *
 * Context ring: before the send button, how full the chat's context is and where compaction starts; it spins
 * while a chat is compacted and drains when that is done (numbers from the auto_compact filter and the Code pipe).
 *
 * Plain browser JS (ES2020), one file, no libraries, no build step. Its only
 * network calls are the preview's version poll and the feature-chat list. Nothing
 * here may be allowed to break Open WebUI: every section is defensive and swallows
 * its own errors.
 */
(function () {
  if (window.__codeToggle) return;
  window.__codeToggle = {}; // filled in below: setMode, startCoding (used by the "Desktop and Code" tool)

  // ---------------------------------------------------------------- constants
  var CODE_MODEL = "code_mode.ornith-1.5-35b-a3b";
  var MY_VERSION = (function () {   // loader.js loads this file as /static/code-toggle.js?v=<version>
    try {
      var m = /[?&]v=([^&#]+)/.exec((document.currentScript && document.currentScript.src) || "");
      return m ? decodeURIComponent(m[1]) : "";
    } catch (e) { return ""; }
  })();

  var BUTTON_SELECTOR = "#model-selector-0-button"; // the model picker, unchanged
  var PILL_ID = "code-toggle-pill";

  var KEY_MODE_PREFIX = "codeToggle:mode:";
  var KEY_NEW_CHAT = "codeToggle:newChat";
  var KEY_MSGS_PREFIX = "codeToggle:msgs:";
  var MAX_MSGS = 500;

  var TITLE =
    "Chat: answers from the model you picked. " +
    "Code: the coding agent works on this chat's project.";

  var KEY_FEATURE_PREFIX = "codeToggle:feature:";   // a chat started with ✦ Feature
  var KEY_NEW_FEATURE = "codeToggle:newFeature";    // ✦ Feature pressed: the next new chat is a feature chat
  var KEY_NEW_REPO = "codeToggle:newRepo";          // "Work on it" pressed on a new chat: its first message starts work on this repo
  var KEY_REPO_PREFIX = "codeToggle:repo:";         // the repo a chat works on (sent with its Code turns as code_repo)
  var KEY_FEATURE_CHATS = "codeToggle:featureChats"; // last list from code-deploy, for instant sidebar marks
  var KEY_PICK_PREFIX = "codeToggle:pick:";         // the model last picked in a chat (a Code turn's code_model)

  // turnModel: the model a Code turn runs on, the one picked in the model picker (a separate name from
  // pickedModel() below, which reads the picker for the thinking pill). A continue or regenerate of a reply saved
  // under the Code pipe's own name sends that name instead; the chat's last picked model stands in for it, and with
  // none known nothing is sent, so the pipe asks before it uses a model.
  function turnModel(chatId, model) {
    var key = chatId ? KEY_PICK_PREFIX + chatId : null;
    if (model && String(model).split(".")[0] !== CODE_MODEL.split(".")[0]) {
      if (key) safeSet(key, model);
      return model;
    }
    return (key && safeGet(key)) || "";
  }
  var DEPLOY_ORIGIN = window.__codeDeployOrigin || location.protocol + "//" + location.hostname + ":8095";
  var DEPLOY_ORIGIN_ONLY = (function () { try { return new URL(DEPLOY_ORIGIN).origin; } catch (e) { return DEPLOY_ORIGIN; } })();
  var FEATURE_TITLE = "Start a feature chat: ask Ornith to add a feature to Code mode itself. " +
    "Nothing goes live until you deploy it from Features.";
  var FIXER_TITLE = "Fixer: have the bug fixer look into this chat";
  var WRENCH = "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z";

  // --------------------------------------------------------------- utilities
  var warned = {};
  function warnOnce(tag, e) {
    if (warned[tag]) return;
    warned[tag] = true;
    try {
      console.warn("[code-toggle] " + tag, e);
    } catch (_) {}
  }

  function safeGet(key) {
    try {
      return localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  function safeSet(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (e) {
      /* storage may be unavailable (private mode, full quota) — ignore */
    }
  }

  function isMode(v) {
    return v === "code" || v === "chat";
  }

  function modeKey(chatId) {
    return KEY_MODE_PREFIX + chatId;
  }

  function msgsKey(chatId) {
    return KEY_MSGS_PREFIX + chatId;
  }

  function chatIdFromUrl() {
    try {
      var m = /^\/c\/([^/]+)(\/?)$/.exec(decodeURIComponent(location.pathname));
      return m ? m[1] : null;
    } catch (e) {
      return null;
    }
  }

  // ------------------------------------------------------------------ state
  var state = { mode: "chat", pendingFeature: false }; // pill mode ("chat" | "code"); ✦ Feature pressed
  var currentChatId = null; // chat id from the URL, or null on a new chat

  function loadCurrentMode() {
    var saved = currentChatId ? safeGet(modeKey(currentChatId)) : safeGet(KEY_NEW_CHAT);
    state.mode = isMode(saved) ? saved : "chat";
    updatePill();
  }

  function setPillMode(mode) {
    if (!isMode(mode)) return;
    state.mode = mode;
    updatePill();
    if (currentChatId) {
      safeSet(modeKey(currentChatId), mode);
    } else {
      safeSet(KEY_NEW_CHAT, mode); // remembered in memory for the next new chat
    }
  }

  // -------------------------------------------------------------------- pill
  function getPill() {
    return document.getElementById(PILL_ID);
  }

  function injectStyle() {
    if (document.getElementById("code-toggle-style")) return;
    var css = [
      "#code-toggle-pill{display:inline-flex;align-items:center;gap:2px;box-sizing:border-box;height:30px;padding:2px;border-radius:999px;background:#f3f4f6;border:1px solid #e5e7eb;position:absolute;z-index:40}",
      "html.dark #code-toggle-pill{background:#262626;border:1px solid #333}",
      "#code-toggle-band{position:absolute;left:0;right:0;top:0;z-index:-1;pointer-events:none}#code-toggle-band[hidden]{display:none}",
      ".code-round-folded{display:none!important}",
      ".code-rounds-fold{flex:1 1 100%;display:block;box-sizing:border-box;width:100%;text-align:left;margin:0;padding:8px 14px;border-radius:14px;border:1px solid #1f1f23;background:#0a0a0b;color:#85858f;font:500 12px/1.4 Inter,system-ui,-apple-system,\"Segoe UI\",Roboto,sans-serif;cursor:pointer}",
      ".code-rounds-fold:hover{color:#e4e4e7;border-color:#2b3a57}.code-rounds-fold:focus-visible{outline:2px solid #4f8cff;outline-offset:1px}",
      "#code-toggle-pill .ct-seg{appearance:none;-moz-appearance:none;-webkit-appearance:none;border:none;cursor:pointer;font:600 13px/1 system-ui,-apple-system,\"Segoe UI\",Roboto,Helvetica,Arial,sans-serif;padding:0 12px;height:26px;border-radius:999px;color:#6b7280;background:transparent;transition:background .12s ease,color .12s ease,box-shadow .12s ease;white-space:nowrap;display:inline-flex;align-items:center;gap:5px}",
      "#code-toggle-pill .ct-seg:hover{color:#111}",
      "html.dark #code-toggle-pill .ct-seg:hover{color:#fff}",
      "#code-toggle-pill .ct-seg[aria-checked=\"true\"]{background:#fff;color:#111;box-shadow:0 1px 2px rgba(0,0,0,.16),0 1px 3px rgba(0,0,0,.10)}",
      "html.dark #code-toggle-pill .ct-seg[aria-checked=\"true\"]{background:#3a3a3a;color:#fff}",
      "#code-toggle-pill .ct-seg:focus-visible{outline:2px solid #4f8cff;outline-offset:1px}",
      "#code-toggle-pill .ct-dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#4f8cff;flex:none}",
      "#code-toggle-pill .ct-sep{width:1px;height:16px;background:#e5e7eb;margin:0 3px;flex:none}",
      "html.dark #code-toggle-pill .ct-sep{background:#3a3a3a}",
      "#code-toggle-pill .ct-feat .ct-star,.ct-feat-mark,.ct-nav-star{color:#d97706}",
      "html.dark #code-toggle-pill .ct-feat .ct-star,html.dark .ct-feat-mark,html.dark .ct-nav-star{color:#f5a524}",
      "#code-toggle-pill .ct-feat .ct-fsub{font-weight:500;color:#9ca3af}",
      "#code-toggle-pill .ct-fnum{display:none}",
      "#code-toggle-pill[data-compact=\"1\"] .ct-flabel,#code-toggle-pill[data-compact=\"1\"] .ct-fsub{display:none}",
      "#code-toggle-pill[data-compact=\"1\"] .ct-fnum{display:inline}",
      "#code-toggle-pill[data-compact=\"1\"] .ct-feat{padding:0 9px}",
      "#code-toggle-pill[data-feature=\"1\"] .ct-chat,#code-toggle-pill[data-feature=\"1\"] .ct-code,#code-toggle-pill[data-feature=\"1\"] .ct-sep{display:none}",
      "#code-toggle-pill[data-features=\"0\"] .ct-feat,#code-toggle-pill[data-features=\"0\"] .ct-sep{display:none}",
      // The Fixer button: after ✦ Feature, on a chat that exists, for admins (the same ones who see ✦ Feature).
      "#code-toggle-pill .ct-fix .ct-wrench{width:13px;height:13px;flex:none;color:#d97706}",
      "html.dark #code-toggle-pill .ct-fix .ct-wrench{color:#f5a524}",
      "#code-toggle-pill[data-fixer=\"0\"] .ct-fix{display:none}",
      "#code-toggle-pill[data-watch=\"0\"] .ct-watch,#code-toggle-pill:not([data-watch]) .ct-watch{display:none}",
      "#code-toggle-pill .ct-watch .ct-eye{width:14px;height:14px;flex:none;color:#4f8cff}",
      "#code-toggle-pill[data-compact=\"1\"] .ct-wlabel{display:none}",
      "#code-toggle-pill[data-compact=\"1\"] .ct-watch{padding:0 9px}",
      "#code-toggle-pill[data-compact=\"1\"] .ct-xlabel{display:none}",
      "#code-toggle-pill[data-compact=\"1\"] .ct-fix{padding:0 9px}",
      "#code-toggle-pill .ct-fix[aria-expanded=\"true\"]{color:#111}html.dark #code-toggle-pill .ct-fix[aria-expanded=\"true\"]{color:#fff}",
      "#code-toggle-pill[data-fixing=\"1\"] .ct-fix .ct-wrench{animation:ctFixPulse 1.2s ease-in-out infinite}",
      "@keyframes ctFixPulse{0%,100%{opacity:.35}50%{opacity:1}}",
      // Desktop and Upload (Feature #31): on a chat that exists, in Code mode
      '#code-toggle-pill[data-desk="0"] .ct-desk,#code-toggle-pill:not([data-desk]) .ct-desk,#code-toggle-pill[data-desk="0"] .ct-up,#code-toggle-pill:not([data-desk]) .ct-up{display:none}',
      "#code-toggle-pill .ct-desk svg,#code-toggle-pill .ct-up svg{width:14px;height:14px;flex:none;color:#4f8cff}",
      '#code-toggle-pill[data-compact="1"] .ct-desk,#code-toggle-pill[data-compact="1"] .ct-up{padding:0 9px}',
      '#code-toggle-pill .ct-up[aria-expanded="true"]{color:#111}html.dark #code-toggle-pill .ct-up[aria-expanded="true"]{color:#fff}',
      '#code-toggle-pill[data-uploading="1"] .ct-up svg{animation:ctFixPulse 1.2s ease-in-out infinite}',
      '#code-up-pop,#code-desk-note{position:fixed;z-index:60;box-sizing:border-box;border-radius:14px;border:1px solid #e5e7eb;background:#fff;color:#111;box-shadow:0 10px 30px rgba(0,0,0,.14);font:13px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}',
      "html.dark #code-up-pop,html.dark #code-desk-note{background:#1f1f1f;border-color:#333;color:#e5e5e5;box-shadow:0 10px 30px rgba(0,0,0,.6)}",
      "#code-up-pop{z-index:70;display:flex;flex-direction:column;gap:10px;padding:12px}#code-desk-note{max-width:360px;padding:10px 12px}",
      "#code-up-pop[hidden],#code-up-pop [hidden],#code-desk-note[hidden]{display:none}",
      "#code-up-pop .up-title{font-weight:600;display:flex;align-items:center;gap:6px}#code-up-pop .up-title svg{width:14px;height:14px;color:#4f8cff}",
      "#code-up-pop .up-where{font-size:12px;color:#6b7280}html.dark #code-up-pop .up-where{color:#9ca3af}",
      "#code-up-pop .up-where code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}",
      "#code-up-pop .up-btns{display:flex;gap:8px}",
      '#code-up-pop .up-btns button{flex:1;appearance:none;-webkit-appearance:none;cursor:pointer;padding:8px 10px;border-radius:9px;border:1px solid #e5e7eb;background:#f9fafb;color:#111;font:600 13px/1 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}',
      "html.dark #code-up-pop .up-btns button{border-color:#333;background:#262626;color:#e5e5e5}",
      "#code-up-pop .up-btns button:hover:not(:disabled){border-color:#4f8cff}#code-up-pop .up-btns button:disabled{opacity:.5;cursor:default}",
      "#code-up-pop .up-drop{border:1.5px dashed #d1d5db;border-radius:10px;padding:14px;text-align:center;font-size:12px;color:#6b7280}",
      "html.dark #code-up-pop .up-drop{border-color:#3f3f46;color:#9ca3af}",
      "#code-up-pop .up-drop.over{border-color:#4f8cff;color:#4f8cff;background:rgba(79,140,255,.06)}",
      "#code-up-pop .up-prog{display:flex;flex-direction:column;gap:6px}",
      "#code-up-pop .up-bar{height:4px;border-radius:2px;background:#e5e7eb;overflow:hidden}html.dark #code-up-pop .up-bar{background:#333}",
      "#code-up-pop .up-bar i{display:block;height:100%;width:0;background:#4f8cff;transition:width .2s}",
      "#code-up-pop .up-status,#code-up-pop .up-done{font-size:12px;color:#6b7280}html.dark #code-up-pop .up-status,html.dark #code-up-pop .up-done{color:#9ca3af}",
      "#code-up-pop .up-err{font-size:12px;color:#dc2626;max-height:96px;overflow:auto;white-space:pre-wrap}html.dark #code-up-pop .up-err{color:#f87171}",
      "#fixer-ask{position:fixed;z-index:60;display:flex;flex-direction:column;gap:10px;box-sizing:border-box;padding:12px;border-radius:14px;border:1px solid #e5e7eb;background:#fff;color:#111;box-shadow:0 10px 30px rgba(0,0,0,.14);font:13px/1.45 system-ui,-apple-system,\"Segoe UI\",Roboto,Helvetica,Arial,sans-serif}",
      "html.dark #fixer-ask{background:#1f1f1f;border-color:#333;color:#e5e5e5;box-shadow:0 10px 30px rgba(0,0,0,.6)}",
      "#fixer-ask[hidden],#fixer-ask [hidden]{display:none}",
      "#fixer-ask .fx-title{font-weight:600;display:flex;align-items:center;gap:6px}",
      "#fixer-ask .fx-title svg{width:14px;height:14px;color:#d97706}html.dark #fixer-ask .fx-title svg{color:#f5a524}",
      "#fixer-ask textarea{box-sizing:border-box;width:100%;min-height:84px;max-height:240px;padding:8px 10px;border-radius:8px;border:1px solid #e5e7eb;background:transparent;color:inherit;font:inherit;line-height:1.45;resize:vertical;outline:none}",
      "html.dark #fixer-ask textarea{border-color:#333}",
      "#fixer-ask textarea:focus{border-color:#4f8cff}",
      "#fixer-ask .fx-dim{color:#6b7280;font-size:12px}html.dark #fixer-ask .fx-dim{color:#9ca3af}",
      "#fixer-ask .fx-warn{color:#b45309;font-size:12px}html.dark #fixer-ask .fx-warn{color:#f5a524}",
      "#fixer-ask .fx-error{color:#dc2626;font-size:12px;overflow-wrap:anywhere}html.dark #fixer-ask .fx-error{color:#f87171}",
      "#fixer-ask .fx-foot{display:flex;align-items:center;justify-content:flex-end;gap:12px}",
      "#fixer-ask .fx-go{appearance:none;-webkit-appearance:none;height:32px;padding:0 14px;border-radius:8px;border:0;background:#111;color:#fff;font:inherit;font-weight:600;line-height:1;cursor:pointer;white-space:nowrap}",
      "html.dark #fixer-ask .fx-go{background:#fff;color:#111}",
      "#fixer-ask .fx-go:disabled{opacity:.5;cursor:default}",
      "#fixer-ask .fx-link{appearance:none;-webkit-appearance:none;border:0;background:none;padding:0;color:#6b7280;font:inherit;cursor:pointer}",
      "html.dark #fixer-ask .fx-link{color:#9ca3af}",
      "#fixer-ask .fx-link:hover{color:#111}html.dark #fixer-ask .fx-link:hover{color:#fff}",
      "#fixer-ask .fx-last{margin-right:auto;text-decoration:underline;text-underline-offset:2px}",
      ".ct-feat-mark{flex:none;align-self:center;margin-right:6px;font:600 11px/1 system-ui,sans-serif}",
      ".ct-feat-mark.deployed{color:#16a34a}html.dark .ct-feat-mark.deployed{color:#3ecf8e}",
      ".ct-feat-mark.rolled_back,html.dark .ct-feat-mark.rolled_back{color:#9ca3af}",
      ".ct-nav-count{margin-left:auto;font:600 11px/1 ui-monospace,Menlo,monospace;color:#d97706;padding-right:2px}",
      "html.dark .ct-nav-count{color:#f5a524}",
      "#ct-feature-hint{position:fixed;z-index:30;transform:translateX(-50%);display:flex;align-items:center;gap:6px;white-space:nowrap;max-width:90vw;overflow:hidden;text-overflow:ellipsis;font:500 12px/16px system-ui,-apple-system,\"Segoe UI\",Roboto,sans-serif;color:#4b5563;background:#fffbeb;border:1px solid #fde68a;padding:4px 12px;border-radius:999px;animation:ctFade .25s ease;pointer-events:none}",
      "html.dark #ct-feature-hint{color:#d1d5db;background:#1d1a14;border-color:rgba(245,165,36,0.28)}",
      "#ct-feature-hint b{font-weight:600;color:#92400e}html.dark #ct-feature-hint b{color:#f5a524}",
      "@keyframes ctFade{from{opacity:0;transform:translate(-50%,4px)}to{opacity:1;transform:translateX(-50%)}}",
      ".code-toggle-badge{display:inline-flex;align-items:center;font:600 11px/1 system-ui,-apple-system,\"Segoe UI\",Roboto,Helvetica,Arial,sans-serif;color:#6b7280;background:#eef0f2;border:1px solid #e5e7eb;padding:1px 6px;border-radius:999px;margin-left:6px;height:18px;flex:none}",
      "html.dark .code-toggle-badge{color:#9ca3af;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.12)}",
      "#effort-pill{display:inline-flex;align-items:center;gap:1px;box-sizing:border-box;height:28px;padding:2px;margin-left:6px;border-radius:999px;background:#f3f4f6;border:1px solid #e5e7eb;flex:none}",
      "html.dark #effort-pill{background:#262626;border-color:#333}",
      "#effort-pill .ef-ico{width:15px;height:15px;margin:0 2px 0 5px;color:#9ca3af;flex:none}",
      "#effort-pill .ef-seg{appearance:none;-moz-appearance:none;-webkit-appearance:none;border:none;cursor:pointer;font:600 12px/1 system-ui,-apple-system,\"Segoe UI\",Roboto,Helvetica,Arial,sans-serif;padding:0 9px;height:22px;border-radius:999px;color:#6b7280;background:transparent;transition:background .12s ease,color .12s ease,box-shadow .12s ease;white-space:nowrap}",
      "#effort-pill .ef-seg:hover{color:#111}",
      "html.dark #effort-pill .ef-seg:hover{color:#fff}",
      "#effort-pill .ef-seg[aria-checked=\"true\"]{background:#fff;color:#111;box-shadow:0 1px 2px rgba(0,0,0,.16),0 1px 3px rgba(0,0,0,.10)}",
      "html.dark #effort-pill .ef-seg[aria-checked=\"true\"]{background:#3a3a3a;color:#fff}",
      "#effort-pill .ef-seg:focus-visible{outline:2px solid #4f8cff;outline-offset:1px}",
      "#effort-pill[data-compact=\"1\"] .ef-seg:not([aria-checked=\"true\"]){display:none}",
      "#effort-pill[data-compact=\"1\"] .ef-seg[aria-checked=\"true\"]::after{content:\"\";display:inline-block;margin-left:5px;border:4px solid transparent;border-top-color:currentColor;border-bottom:0;vertical-align:2px;opacity:.55}",
      "#effort-pop{position:fixed;z-index:70;display:flex;flex-direction:column;gap:2px;padding:4px;border-radius:12px;background:#fff;border:1px solid #e5e7eb;box-shadow:0 8px 24px rgba(0,0,0,.14)}",
      "html.dark #effort-pop{background:#262626;border-color:#333;box-shadow:0 8px 24px rgba(0,0,0,.5)}",
      "#effort-pop button{appearance:none;-webkit-appearance:none;border:none;cursor:pointer;text-align:left;min-width:104px;padding:9px 14px;border-radius:9px;font:600 14px/1 system-ui,-apple-system,\"Segoe UI\",Roboto,Helvetica,Arial,sans-serif;color:#6b7280;background:transparent}",
      "html.dark #effort-pop button{color:#a1a1aa}",
      "#effort-pop button[aria-checked=\"true\"]{background:#f3f4f6;color:#111}html.dark #effort-pop button[aria-checked=\"true\"]{background:#3a3a3a;color:#fff}",
    ].join("");
    var s = document.createElement("style");
    s.id = "code-toggle-style";
    s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
  }

  function createPill() {
    var pill = document.createElement("div");
    pill.id = PILL_ID;
    pill.setAttribute("role", "radiogroup");
    pill.setAttribute("title", TITLE);

    var chatBtn = document.createElement("button");
    chatBtn.type = "button";
    chatBtn.className = "ct-seg ct-chat";
    chatBtn.setAttribute("role", "radio");
    chatBtn.setAttribute("aria-checked", "true");
    chatBtn.textContent = "Chat";

    var codeBtn = document.createElement("button");
    codeBtn.type = "button";
    codeBtn.className = "ct-seg ct-code";
    codeBtn.setAttribute("role", "radio");
    codeBtn.setAttribute("aria-checked", "false");
    codeBtn.textContent = "Code";

    chatBtn.addEventListener("click", function () {
      cancelPendingFeature();
      if (state.mode !== "chat") setPillMode("chat");
    });
    codeBtn.addEventListener("click", function () {
      cancelPendingFeature();
      if (state.mode !== "code") setPillMode("code");
    });

    var sep = document.createElement("span");
    sep.className = "ct-sep";
    var featBtn = document.createElement("button");
    featBtn.type = "button";
    featBtn.className = "ct-seg ct-feat";
    featBtn.setAttribute("aria-checked", "false");
    featBtn.title = FEATURE_TITLE;
    featBtn.innerHTML = '<span class="ct-star" aria-hidden="true">\u2726</span><span class="ct-flabel">Feature</span><span class="ct-fsub"></span><span class="ct-fnum"></span>';
    featBtn.addEventListener("click", function () {
      var f = currentChatId && featureOf(currentChatId);
      if (f) openFeatures(f.n || null);
      else startFeatureChat();
    });

    var fixBtn = document.createElement("button");
    fixBtn.type = "button";
    fixBtn.className = "ct-seg ct-fix";
    fixBtn.title = FIXER_TITLE;
    fixBtn.setAttribute("aria-haspopup", "dialog");
    fixBtn.setAttribute("aria-expanded", "false");
    fixBtn.appendChild(wrenchIcon("ct-wrench"));
    var fixLabel = document.createElement("span");
    fixLabel.className = "ct-xlabel";
    fixLabel.textContent = "Fixer";
    fixBtn.appendChild(fixLabel);
    fixBtn.addEventListener("click", function () {
      try {
        fixerButton();
      } catch (e) {
        warnOnce("fixer-button", e);
      }
    });

    var deskBtn = document.createElement("button");
    deskBtn.type = "button";
    deskBtn.className = "ct-seg ct-desk";
    deskBtn.title = "Desktop: this chat's desktop and browser, any time (you can take control there)";
    deskBtn.innerHTML = DESK_ICON + '<span class="ct-xlabel">Desktop</span>';
    deskBtn.addEventListener("click", function () {
      try { openDesktop(); } catch (e) { warnOnce("desktop-open", e); }
    });
    var upBtn = document.createElement("button");
    upBtn.type = "button";
    upBtn.className = "ct-seg ct-up";
    upBtn.title = "Upload files or folders into this chat's project folder, where the agent can read them";
    upBtn.setAttribute("aria-haspopup", "dialog");
    upBtn.setAttribute("aria-expanded", "false");
    upBtn.innerHTML = UP_ICON + '<span class="ct-xlabel">Upload</span>';
    upBtn.addEventListener("click", function () {
      try { upButton(); } catch (e) { warnOnce("upload-button", e); }
    });

    pill.appendChild(chatBtn);
    pill.appendChild(codeBtn);
    var watchBtn = document.createElement("button");
    watchBtn.type = "button";
    watchBtn.className = "ct-seg ct-watch";
    watchBtn.title = "Watch: this chat's work full window, with its preview, checklist and engine beside it";
    watchBtn.innerHTML = '<svg class="ct-eye" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"></path><circle cx="12" cy="12" r="3"></circle></svg><span class="ct-wlabel">Watch</span>';
    watchBtn.addEventListener("click", function () {
      try { wtOpen(); } catch (e) { warnOnce("watch-open", e); }
    });

    pill.appendChild(sep);
    pill.appendChild(featBtn);
    pill.appendChild(fixBtn);
    pill.appendChild(deskBtn);
    pill.appendChild(upBtn);
    pill.appendChild(watchBtn);
    return pill;
  }

  function updatePill() {
    var pill = getPill();
    if (!pill) return;
    var chatBtn = pill.querySelector(".ct-chat");
    var codeBtn = pill.querySelector(".ct-code");
    var f = currentChatId && featureOf(currentChatId);
    var featureNew = !currentChatId && state.pendingFeature;
    if (chatBtn) chatBtn.setAttribute("aria-checked", !featureNew && state.mode === "chat" ? "true" : "false");
    if (codeBtn) codeBtn.setAttribute("aria-checked", !featureNew && state.mode === "code" ? "true" : "false");
    setAttr(pill, "data-feature", f ? "1" : "0");
    setAttr(pill, "data-features", features.enabled || f ? "1" : "0");
    setAttr(pill, "title", f ? "" : TITLE);
    var featBtn = pill.querySelector(".ct-feat");
    if (featBtn) {
      setAttr(featBtn, "aria-checked", f || featureNew ? "true" : "false");
      setAttr(featBtn, "title", f ? "Feature" + (f.n ? " #" + f.n : "") + ": open it in Features (deploy, roll back, history)"
                                  : FEATURE_TITLE);
      setText(featBtn.querySelector(".ct-flabel"), f && f.n ? "Feature #" + f.n : "Feature");
      setText(featBtn.querySelector(".ct-fsub"), f ? STATUS_WORD[f.status] || "" : "");
      setText(featBtn.querySelector(".ct-fnum"), f && f.n ? "#" + f.n : "");
    }
    setAttr(pill, "data-watch", currentChatId && state.mode === "code" && document.querySelector(LIVE_FRAMES) ? "1" : "0");
    setAttr(pill, "data-desk", currentChatId && state.mode === "code" ? "1" : "0");
    setAttr(pill, "data-uploading", up && up.busy ? "1" : "0");
    var upBtn = pill.querySelector(".ct-up");
    if (upBtn) setAttr(upBtn, "aria-expanded", up && up.open ? "true" : "false");
    var fixBtn = pill.querySelector(".ct-fix");
    if (fixBtn) {
      var running = fixerRunning();
      setAttr(pill, "data-fixer", currentChatId && features.enabled ? "1" : "0");
      setAttr(pill, "data-fixing", running ? "1" : "0");
      setAttr(fixBtn, "title", running ? "The fixer is looking into this chat: show it" : FIXER_TITLE);
      setAttr(fixBtn, "aria-expanded", fixAsk && fixAsk.open ? "true" : "false");
    }
    // accent dot before "Code" when in Code mode
    if (codeBtn) {
      var existing = codeBtn.querySelector(".ct-dot");
      if (state.mode === "code") {
        if (!existing) {
          var d = document.createElement("span");
          d.className = "ct-dot";
          codeBtn.appendChild(d);
        }
      } else if (existing) {
        codeBtn.removeChild(existing);
      }
    }
  }

  function positionPill() {
    var pill = getPill();
    if (!pill) return;
    var btn = document.querySelector(BUTTON_SELECTOR);
    var nav = btn && btn.closest("nav");
    if (!btn || !nav) return;
    var b = btn.getBoundingClientRect();
    var n = nav.getBoundingClientRect();

    // The bar's own controls on the right (chat menu, controls): the pill must end before them. When the full
    // pill doesn't fit (a long model name, or the side panel open) it goes compact: ✦ without its words.
    var rightLimit = n.right;
    Array.prototype.forEach.call(nav.querySelectorAll("button, a"), function (el) {
      if (pill.contains(el)) return;
      var r = el.getBoundingClientRect();
      // (skipping the picker's own "+" right next to it)
      if (r.width && r.left > b.right + 48 && r.left < rightLimit) rightLimit = r.left;
    });
    if (pill.getAttribute("data-compact") !== "1") pill.__fullWidth = pill.getBoundingClientRect().width;
    else pill.__compactWidth = pill.getBoundingClientRect().width;

    // A phone: not even the compact pill fits between the model picker and the bar's own controls, so it takes a
    // row of its own under the bar (the checklist button beside it) instead of covering the model's name.
    var row2 = rightLimit - 12 - (b.right + 12) < (pill.__compactWidth || 150);
    document.documentElement.classList.toggle("code-narrow", row2);
    var band = document.getElementById("code-toggle-band");
    if (row2) return placePillRow2(pill, nav, n);
    if (band) band.hidden = true;

    var room = rightLimit - 12 - Math.max(n.left + (n.width - (pill.__fullWidth || 0)) / 2, b.right + 12);
    setAttr(pill, "data-compact", pill.__fullWidth && room < pill.__fullWidth ? "1" : "0");
    var p = pill.getBoundingClientRect();

    // vertical: centre the pill on the button's row (clamped to the nav).
    var top = b.top + (b.height - p.height) / 2;
    top = Math.max(n.top, Math.min(top, n.top + n.height - p.height));

    // horizontal: centred in the nav by default...
    var leftCenter = (n.width - p.width) / 2;
    var leftAbs = n.left + leftCenter;

    // ...unless it would overlap the model picker; then sit just past it.
    var overlaps = leftAbs < b.right && leftAbs + p.width > b.left;
    var left;
    if (overlaps) {
      left = b.right - n.left + 12;
    } else {
      left = leftCenter;
    }
    left = Math.max(0, Math.min(left, rightLimit - n.left - 12 - p.width)); // never under the bar's own controls

    pill.style.top = top - n.top + "px";
    pill.style.left = left + "px";
  }

  // The pill's own row under the bar (a phone): full words when they fit beside the checklist button, centred in
  // what is left. A band in the page's colour sits behind the bar and this row (under their buttons: z-index -1 in
  // the bar's own stacking context), so the chat scrolls under both instead of showing through them.
  function placePillRow2(pill, nav, n) {
    var root = document.documentElement;
    var rowTop = Math.round(n.bottom + 6) + "px";  // the bar is sticky at the top; the checklist button is fixed
    if (root.style.getPropertyValue("--code-row2-top") !== rowTop) root.style.setProperty("--code-row2-top", rowTop);
    var rail = document.getElementById("code-ck-rail");
    var railW = rail && !rail.hidden ? rail.getBoundingClientRect().width + 8 : 0;
    var room = n.width - 20 - railW;
    setAttr(pill, "data-compact", pill.__fullWidth && pill.__fullWidth > room ? "1" : "0");
    var p = pill.getBoundingClientRect();
    pill.style.top = Math.round(n.height + 6) + "px";
    pill.style.left = Math.round(10 + Math.max(0, (room - p.width) / 2)) + "px";
    var band = document.getElementById("code-toggle-band");
    if (!band) {
      band = document.createElement("div");
      band.id = "code-toggle-band";
      band.setAttribute("aria-hidden", "true");
    }
    if (band.parentNode !== nav) nav.appendChild(band);
    band.hidden = false;
    var bg = getComputedStyle(document.body).backgroundColor || "transparent";
    var look = "linear-gradient(to bottom, " + bg + " 0%, " + bg + " 84%, transparent 100%)";
    if (band.style.backgroundImage !== look) band.style.backgroundImage = look;
    var bandH = Math.round(n.height + 6 + p.height + 16) + "px";
    if (band.style.height !== bandH) band.style.height = bandH;
  }

  function reconcile() {
    injectStyle();
    try {
      reconcileEffort();
    } catch (e) {
      warnOnce("effort", e);
    }
    try {
      reconcileGithub();
    } catch (e) {
      warnOnce("github", e);
    }
    try {
      reconcileContext();
    } catch (e) {
      warnOnce("context", e);
    }
    try {
      placeTaskStrip();  // Open WebUI may have redrawn the reply under it, or the chat changed
    } catch (e) {
      warnOnce("task-strip", e);
    }
    try {
      foldRounds();
    } catch (e) {
      warnOnce("fold-rounds", e);
    }
    try {
      cqCapture(); // a message Open WebUI just queued goes to cryoai
      cqRender();
    } catch (e) {
      warnOnce("queue", e);
    }
    var btn = document.querySelector(BUTTON_SELECTOR);
    if (!btn) {
      // No model picker on this page (settings, etc.) — keep the pill away.
      var pillNoBtn = getPill();
      if (pillNoBtn && pillNoBtn.parentNode) pillNoBtn.parentNode.removeChild(pillNoBtn);
      return;
    }
    var nav = btn.closest("nav");
    if (!nav) {
      var pillNoNav = getPill();
      if (pillNoNav && pillNoNav.parentNode) pillNoNav.parentNode.removeChild(pillNoNav);
      return;
    }
    var pill = getPill();
    if (!pill) {
      pill = createPill();
      nav.appendChild(pill);
    } else if (pill.parentNode !== nav) {
      nav.appendChild(pill);
    }
    inferFromReplies();
    updatePill();
    positionPill();
    applyBadges();
    decoratePreviewLinks();
    decorateFriendlyLinks();
    decorateFixerLinks();
    decorateFeatureLinks();
    decorateSidebar();
    placeFeatureHint();
  }

  // ------------------------------------------------------------------ badges
  function readMsgs(chatId) {
    var raw;
    try {
      raw = JSON.parse(safeGet(msgsKey(chatId)) || "[]");
    } catch (e) {
      return [];
    }
    return Array.isArray(raw) ? raw : [];
  }

  function recordMsg(chatId, msgId) {
    if (!chatId || msgId == null) return;
    var arr = readMsgs(chatId);
    arr.push(String(msgId));
    if (arr.length > MAX_MSGS) arr = arr.slice(arr.length - MAX_MSGS);
    safeSet(msgsKey(chatId), JSON.stringify(arr));
  }

  // The mode and the Code badges live in this browser's storage, so a Code chat opened in another browser, or
  // started by a script, showed as Chat (2026-10-09). Every Code reply carries the Code pipe's marks in the chat
  // itself (its Preview link, a feature chip, the live window): when this browser has no record of the chat's Code
  // replies, take them from there, and if the latest reply is a Code reply, the chat is in Code mode.
  var inferredChats = {};

  function isCodeReply(el) {
    return !!(el && el.querySelector('a[href^="' + PREVIEW_ORIGIN + '/p/"], a.code-preview-chip, ' +
                                      'a[href*=":8095/features/#f"], iframe[src*=":4099/w/"]'));
  }

  function inferFromReplies() {
    if (!currentChatId || inferredChats[currentChatId]) return;
    var replies = Array.prototype.filter.call(document.querySelectorAll('[id^="message-"]'), function (el) {
      return MESSAGE_ID_RE.test(el.id) && el.querySelector("#response-message-model-name");
    });
    if (!replies.length) return; // not rendered yet: the next reconcile tries again
    inferredChats[currentChatId] = true;
    if (readMsgs(currentChatId).length) return; // this browser already knows this chat's Code replies
    var code = replies.filter(isCodeReply);
    if (!code.length) return;
    code.forEach(function (el) { recordMsg(currentChatId, el.id.slice("message-".length)); });
    if (isCodeReply(replies[replies.length - 1])) {
      state.mode = "code";
      safeSet(modeKey(currentChatId), "code");
      updatePill();
    }
  }

  function applyBadges() {
    if (!currentChatId) return;
    var arr = readMsgs(currentChatId);
    for (var i = 0; i < arr.length; i++) {
      var id = arr[i];
      var nameEl = document.querySelector("#message-" + id + " #response-message-model-name");
      if (!nameEl || !nameEl.parentNode) continue;
      // insert once, right after the model-name span
      if (!nameEl.parentNode.querySelector(".code-toggle-badge")) {
        var badge = document.createElement("span");
        badge.className = "code-toggle-badge";
        badge.textContent = "Code";
        nameEl.parentNode.insertBefore(badge, nameEl.nextSibling);
      }
    }
  }

  // -------------------------------------------------------------- reconciliation
  var rafId = 0;
  function scheduleReconcile() {
    if (rafId) return;
    rafId = requestAnimationFrame (function () {
      rafId = 0;
      try {
        reconcile();
      } catch (e) {
        warnOnce("reconcile", e);
      }
    });
  }

  function startObserver() {
    try {
      if (typeof MutationObserver === "undefined") return;
      var observer = new MutationObserver(function () {
        scheduleReconcile();
      });
      observer.observe(document.body, { subtree: true, childList: true });
    } catch (e) {
      warnOnce("observer", e);
    }
  }

  // ---------------------------------------------------------- URL navigation
  function onUrlChange() {
    var previous = currentChatId;
    currentChatId = chatIdFromUrl();
    if (!previous && currentChatId && !isMode(safeGet(modeKey(currentChatId)))) {
      safeSet(modeKey(currentChatId), state.mode);
    }
    if (previous && previous !== currentChatId) resetPreviewForChat();
    if (previous !== currentChatId) featureLinks = { count: 0, since: Date.now() };
    if (previous !== currentChatId) ghChatChanged();
    if (previous !== currentChatId) checklistChatChanged();
    if (previous !== currentChatId) fixAskClose();
    if (previous !== currentChatId) deskChatChanged();
    if (previous !== currentChatId && wt.on) wtRetarget();
    if (previous !== currentChatId) {
      if (!wt.on) wfClear();
      try { qbRender(); } catch (e) { warnOnce("ask-box", e); }   // this chat's approvals as a dialog, the rest as notices
      qbPoll();
    }
    loadCurrentMode();
    scheduleReconcile(); // rebuild pill for the new chat + re-apply badges
  }

  function patchHistory(name, original) {
    try {
      history[name] = function () {
        var r = original.apply(history, arguments);
        scheduleUrlChange();
        return r;
      };
    } catch (e) {
      warnOnce("history:" + name, e);
    }
  }

  var urlRaf = 0;
  function scheduleUrlChange() {
    if (urlRaf) return;
    urlRaf = requestAnimationFrame (function () {
      urlRaf = 0;
      try {
        onUrlChange();
      } catch (e) {
        warnOnce("urlchange", e);
      }
    });
  }

  // ------------------------------------------------------------- thinking pill
  // Sits at the end of the message bar's left group (+ / integrations / tools) and applies to the model in the
  // picker, in Chat and Code mode alike. Chat sends params.reasoning_effort, which Open WebUI copies into the
  // request: llama.cpp reads "none" as thinking off and hands a level to the model's chat template. Code also
  // sends code_effort, which the Code pipe turns into an opencode model variant. "on" sends nothing (the
  // template's default). The options come from the slerm gateway, which reads each model's served chat template
  // (slerm.thinking in /v1/models, passed through by Open WebUI's /api/models): a new model gets the right pill
  // by itself, and models that can't steer thinking get none.
  var EFFORT_ID = "effort-pill";
  var KEY_EFFORT_PREFIX = "effortPill:";
  // Each level is shown as the model's own name for it (Qwen3.8 tops out at xhigh, gpt-oss at high).
  var EFFORT_LABELS = { none: "Off", on: "On", minimal: "Min", low: "Low", medium: "Med", high: "High",
                        xhigh: "XHigh", max: "Max" };
  // Used only while the gateway sends no slerm.thinking at all (an older slerm-orch).
  var EFFORT_RULES = [
    { re: /^qwen3\.8-/, choices: ["none", "low", "medium", "xhigh"], dflt: "medium" },
    { re: /^gpt-oss-/, choices: ["low", "medium", "high"], dflt: "medium" },
    { re: /^(ornith-|qwen3\.[5-7]-|holo|kat-coder-|nemotron-|gemma4-|bonsai-|ui-venus-|qwen3-(8b|14b)$|Qwen3-8B-|qwen3-30b-a3b-iq2$)/,
      choices: ["none", "on"], dflt: "on" },
  ];
  var thinkingCaps = { map: null, at: 0, busy: false }; // base model name -> {choices, dflt}; map null = not loaded

  function loadThinkingCaps() {
    if (thinkingCaps.busy || Date.now() - thinkingCaps.at < 10000) return;
    thinkingCaps.busy = true;
    thinkingCaps.at = Date.now();
    var token = safeGet("token");
    _fetch("/api/models", { headers: token ? { Authorization: "Bearer " + token } : {} })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var list = d && (d.data || d);
        if (!Array.isArray(list)) return;
        var map = {}, any = false;
        list.forEach(function (m) {
          if (m && m.slerm && m.slerm.ctx) modelCtx[baseModel(m.id)] = m.slerm.ctx; // the context ring
          var th = m && m.slerm && m.slerm.thinking;
          if (m && m.slerm && "thinking" in m.slerm) any = true;
          if (th && Array.isArray(th.levels) && th.levels.length) {
            any = true;
            map[baseModel(m.id)] = { choices: th.levels.filter(function (v) { return v in EFFORT_LABELS; }),
                                     dflt: th["default"] };
          }
        });
        thinkingCaps.map = any ? map : null;
        scheduleReconcile();
      })
      .catch(function (e) { warnOnce("thinking-caps", e); })
      .then(function () { thinkingCaps.busy = false; });
  }

  var LIGHTBULB = "M12 18v-5.25m0 0a6.01 6.01 0 0 0 1.5-.189m-1.5.189a6.01 6.01 0 0 1-1.5-.189m3.75 7.478a12.06 12.06 0 0 1-4.5 0m3.75 2.383a14.406 14.406 0 0 1-3 0M14.25 18v-.192c0-.983.658-1.823 1.508-2.316a7.5 7.5 0 1 0-7.517 0c.85.493 1.509 1.333 1.509 2.316V18";

  function baseModel(id) {
    return String(id || "").trim().replace(/^(slerm-turing|code_mode)\./, "");
  }

  function effortRule(model) {
    if (thinkingCaps.map) {
      var c = thinkingCaps.map[model];
      return c && c.choices.length ? c : null;
    }
    for (var i = 0; i < EFFORT_RULES.length; i++) {
      if (EFFORT_RULES[i].re.test(model)) return EFFORT_RULES[i];
    }
    return null;
  }

  function effortLevel(model, rule) {
    var saved = safeGet(KEY_EFFORT_PREFIX + model);
    return rule.choices.indexOf(saved) >= 0 ? saved : rule.dflt;
  }

  // The level to send for a request's model, or null when the pill doesn't apply to it.
  function effortFor(modelId) {
    var model = baseModel(modelId);
    var rule = model && effortRule(model);
    return rule ? effortLevel(model, rule) : null;
  }

  function pickedModel() {
    var btn = document.querySelector(BUTTON_SELECTOR);
    if (!btn) return null;
    return baseModel((btn.innerText || btn.textContent || "").split("\n")[0]);
  }

  function effortGroup() {
    var menu = document.getElementById("input-menu-button");
    var row = menu && menu.closest(".justify-between");
    return row && row.firstElementChild && row.firstElementChild.contains(menu) ? row.firstElementChild : null;
  }

  function createEffortPill() {
    var pill = document.createElement("div");
    pill.id = EFFORT_ID;
    pill.setAttribute("role", "radiogroup");
    pill.setAttribute("aria-label", "Thinking");
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.6");
    svg.setAttribute("class", "ef-ico");
    svg.setAttribute("aria-hidden", "true");
    var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    path.setAttribute("d", LIGHTBULB);
    svg.appendChild(path);
    pill.appendChild(svg);
    pill.addEventListener("mousedown", function (e) {
      e.preventDefault(); // keep the message box focused
    });
    pill.addEventListener("click", function (e) {
      try {
        var seg = e.target && e.target.closest ? e.target.closest(".ef-seg") : null;
        e.preventDefault();
        e.stopPropagation();
        if (!seg) return;
        if (pill.getAttribute("data-compact") === "1" && seg.getAttribute("aria-checked") === "true") {
          openEffortPop(pill);  // a narrow box shows only the chosen level: tapping it offers the others
          return;
        }
        var model = pill.getAttribute("data-model");
        var rule = model && effortRule(model);
        if (!rule) return;
        safeSet(KEY_EFFORT_PREFIX + model, seg.getAttribute("data-v"));
        updateEffortPill(pill, model, rule);
      } catch (err) {
        warnOnce("effort-click", err);
      }
    });
    return pill;
  }

  // Idempotent: only touches the DOM when something changed, so the body observer settles.
  function updateEffortPill(pill, model, rule) {
    var choices = rule.choices.join(",");
    if (pill.getAttribute("data-choices") !== choices) {
      Array.prototype.forEach.call(pill.querySelectorAll(".ef-seg"), function (b) { b.remove(); });
      rule.choices.forEach(function (v) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "ef-seg";
        b.setAttribute("role", "radio");
        b.setAttribute("data-v", v);
        b.textContent = EFFORT_LABELS[v];
        pill.appendChild(b);
      });
      pill.setAttribute("data-choices", choices);
    }
    if (pill.getAttribute("data-model") !== model) pill.setAttribute("data-model", model);
    var level = effortLevel(model, rule);
    Array.prototype.forEach.call(pill.querySelectorAll(".ef-seg"), function (b) {
      var on = b.getAttribute("data-v") === level ? "true" : "false";
      if (b.getAttribute("aria-checked") !== on) b.setAttribute("aria-checked", on);
    });
    var off = rule.choices.indexOf("none") >= 0 ? "Off answers straight away" : "";
    var title = rule.choices.indexOf("on") >= 0
      ? "Thinking for " + model + ": " + off + "."
      : "Thinking effort for " + model + ": " + (off ? off + ", " : "") + "the highest level thinks longest.";
    if (pill.title !== title) pill.title = title;
  }

  function reconcileEffort() {
    var pill = document.getElementById(EFFORT_ID);
    var model = pickedModel();
    // Load the options once, and look again (at most once a minute) for a model they don't cover yet.
    if (model && (!thinkingCaps.map ? !thinkingCaps.at : !(model in thinkingCaps.map) && Date.now() - thinkingCaps.at > 60000)) {
      loadThinkingCaps();
    }
    var rule = model ? effortRule(model) : null;
    var group = rule ? effortGroup() : null;
    if (!group) {
      if (pill && pill.parentNode) pill.parentNode.removeChild(pill);
      return;
    }
    if (!pill) pill = createEffortPill();
    if (pill.parentNode !== group) group.appendChild(pill);
    updateEffortPill(pill, model, rule);
    fitEffort(pill);
  }

  // When the message box's left group can't hold every level (a phone), the pill shows only the chosen one.
  function fitEffort(pill) {
    var row = pill.parentElement;
    if (!row) return;
    var compact = pill.getAttribute("data-compact") === "1";
    var w = pill.getBoundingClientRect().width;
    if (!compact) pill.__fullWidth = w;
    var over = row.scrollWidth - row.clientWidth;  // how far the group's contents run past its own width
    var want = compact ? over + (pill.__fullWidth || 0) - w > 0 : over > 0;
    setAttr(pill, "data-compact", want ? "1" : "0");
    if (!want) closeEffortPop();
  }

  function openEffortPop(pill) {
    closeEffortPop();
    var pop = document.createElement("div");
    pop.id = "effort-pop";
    pop.setAttribute("role", "menu");
    Array.prototype.forEach.call(pill.querySelectorAll(".ef-seg"), function (seg) {
      var b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "menuitemradio");
      b.setAttribute("data-v", seg.getAttribute("data-v"));
      b.setAttribute("aria-checked", seg.getAttribute("aria-checked") || "false");
      b.textContent = seg.textContent;
      pop.appendChild(b);
    });
    pop.addEventListener("mousedown", function (e) { e.preventDefault(); });  // keep the message box focused
    pop.addEventListener("click", function (e) {
      var b = e.target && e.target.closest ? e.target.closest("button[data-v]") : null;
      e.preventDefault();
      e.stopPropagation();
      if (!b) return;
      var seg = pill.querySelector('.ef-seg[data-v="' + b.getAttribute("data-v") + '"]');
      closeEffortPop();
      if (seg && seg.getAttribute("aria-checked") !== "true") seg.click();  // the pill's own handler saves it
    });
    document.body.appendChild(pop);
    var r = pill.getBoundingClientRect(), pr = pop.getBoundingClientRect();
    pop.style.left = Math.round(Math.max(8, Math.min(r.left, window.innerWidth - pr.width - 8))) + "px";
    pop.style.top = Math.round(Math.max(8, r.top - pr.height - 8)) + "px";
    // (it opens on a click, so the tap that opened it is already over: listening now can't close it straight away)
    document.addEventListener("pointerdown", effortPopOutside, true);
    document.addEventListener("keydown", effortPopKey, true);
  }

  function effortPopOutside(e) {
    var pop = document.getElementById("effort-pop");
    if (pop && !pop.contains(e.target)) closeEffortPop();
  }

  function effortPopKey(e) {
    if (e.key === "Escape") closeEffortPop();
  }

  function closeEffortPop() {
    var pop = document.getElementById("effort-pop");
    if (pop) pop.remove();
    document.removeEventListener("pointerdown", effortPopOutside, true);
    document.removeEventListener("keydown", effortPopKey, true);
  }

  // ------------------------------------------------------------------- the chat's "scroll down" arrow
  // While a Code turn runs, its live window can sit well above the bottom of the chat (a continued reply keeps every
  // earlier round's window below it). The arrow then brings the running window to the middle of the screen instead
  // of the very bottom; pressed again with that window already there, it goes to the bottom as before. Each live
  // window says whether it is the running one in its iframe:height messages (live.html report()).
  var LIVE_FRAMES = 'iframe[src*=":4099/w/"]:not(.wt-live)';   // the Watch stage's copy is not one of the chat's

  function liveFrameOf(src) {
    var frames = document.querySelectorAll(LIVE_FRAMES);
    for (var i = 0; i < frames.length; i++) if (frames[i].contentWindow === src) return frames[i];
    return null;
  }

  function onLiveFrameMessage(e) {
    var d = e.data;
    if (wt.frame && e.source === wt.frame.contentWindow) {   // the Watch stage: its header line
      if (d && d.type === "code:watch-state") {
        wt.st = d;
        wt.stateAt = Date.now();
        if (!d.review) wt.builderSrc = wt.src;   // the Tools tab counts the builder's session, also while a review shows
        if (wt.tab === "tools") {
          try { wtToolsRender(); } catch (err) { warnOnce("watch-tools", err); }
        }
      }
      return;
    }
    if (d && d.type === "code:watch") {
      if (liveFrameOf(e.source)) {
        try { wtOpen(); } catch (err) { warnOnce("watch-open", err); }
      }
      return;
    }
    if (d && (d.type === "code:compaction" || d.type === "code:review" || d.type === "code:review-handback")) {
      var lf = liveFrameOf(e.source);   // only a live window of this page may draw these overlays
      if (lf) {
        try {
          if (d.type === "code:compaction") cmpOnMessage(lf, d);
          else if (d.type === "code:review") rvOnMessage(lf, d);
          else rvOnHandback(lf, d.findings);
        } catch (err) {
          warnOnce(d.type, err);
        }
      }
      return;
    }
    if (!d || d.type !== "iframe:height" || typeof d.live !== "boolean") return;
    var frames = document.querySelectorAll(LIVE_FRAMES);
    for (var i = 0; i < frames.length; i++) {
      if (frames[i].contentWindow === e.source) {
        setAttr(frames[i], "data-code-live", d.live ? "1" : "0");
        try { placeTaskStrip(); } catch (err) { warnOnce("task-strip", err); }
        try { foldRounds(); } catch (err) { warnOnce("fold-rounds", err); }
        return;
      }
    }
  }

  // ------------------------------------------------------------------- earlier rounds, folded
  // A long reply gets a live window per round (each builder run, each review), and the finished ones pile up in it.
  // In each reply they fold into one line, "N earlier rounds", that shows them again.  While a round runs every
  // finished one folds; once the turn has ended the newest finished box stays in view.  A box counts as finished once
  // its window said so (data-code-live="0"); the newest is told by opencode's message id in its link (they sort by time).
  var FOLDED = "code-round-folded";

  function frameMessageId(f) {
    var m = /[?&]m=([^&#]+)/.exec(f.getAttribute("src") || "");
    return m ? m[1] : "";
  }

  function foldRounds() {
    var rows = [];
    Array.prototype.forEach.call(document.querySelectorAll(LIVE_FRAMES), function (f) {
      var row = f.parentElement && f.parentElement.parentElement;   // Open WebUI: a reply's row of embed boxes
      if (row && rows.indexOf(row) < 0) rows.push(row);
    });
    rows.forEach(function (row) {
      var boxes = [];
      Array.prototype.forEach.call(row.children, function (c) {
        var f = c.querySelector ? c.querySelector(LIVE_FRAMES) : null;
        if (f) boxes.push({ box: c, live: f.getAttribute("data-code-live"), id: frameMessageId(f) });
      });
      var done = boxes.filter(function (b) { return b.live === "0"; });
      var running = boxes.some(function (b) { return b.live === "1"; });
      var keep = null;
      if (!running) done.forEach(function (b) { if (!keep || b.id > keep.id) keep = b; });
      var fold = done.filter(function (b) { return b !== keep; });
      var line = null;
      Array.prototype.forEach.call(row.children, function (c) { if (c.classList && c.classList.contains("code-rounds-fold")) line = c; });
      if (fold.length < 2) {   // one finished box is no clutter
        boxes.forEach(function (b) { b.box.classList.remove(FOLDED); });
        if (line) line.remove();
        return;
      }
      if (!line) {
        line = document.createElement("button");
        line.type = "button";
        line.className = "code-rounds-fold";
        line.addEventListener("click", function () {
          line.setAttribute("data-open", line.getAttribute("data-open") === "1" ? "0" : "1");
          try { foldRounds(); } catch (e) { warnOnce("fold-rounds", e); }
        });
      }
      var open = line.getAttribute("data-open") === "1";
      boxes.forEach(function (b) { b.box.classList.toggle(FOLDED, !open && fold.indexOf(b) >= 0); });
      setText(line, (open ? "\u25be " : "\u25b8 ") + fold.length + " earlier round" + (fold.length === 1 ? "" : "s") +
                    (open ? " \u00b7 fold them" : ""));
      setAttr(line, "aria-expanded", open ? "true" : "false");
      var last = null;   // the line goes after the last box still in view
      boxes.forEach(function (b) { if (!b.box.classList.contains(FOLDED)) last = b.box; });
      var anchor = last || boxes[0].box;
      if (line.previousElementSibling !== anchor || line.parentNode !== row) row.insertBefore(line, anchor.nextSibling);
    });
  }

  function runningLiveFrame() {
    var all = document.querySelectorAll(LIVE_FRAMES + '[data-code-live="1"]');
    for (var i = all.length - 1; i >= 0; i--) {
      if (all[i].getClientRects().length) return all[i];  // the newest one that is on the page
    }
    return null;
  }

  function isScrollDownArrow(btn) {
    var p = btn && btn.parentElement;
    return !!(p && /(^|\s)-top-12(\s|$)/.test(p.className || "") && /rounded-full/.test(btn.className || "") &&
              btn.querySelector('path[d^="M10 3a.75.75 0 01.75.75v10.638"]'));
  }

  function onScrollDownArrow(e) {
    try {
      var btn = e.target && e.target.closest ? e.target.closest("button") : null;
      if (!isScrollDownArrow(btn)) return;
      var frame = runningLiveFrame();
      if (!frame) return;
      var r = frame.getBoundingClientRect(), vh = window.innerHeight;
      if (Math.abs(r.top + Math.min(r.height, vh) / 2 - vh / 2) < 60) return;  // already there: to the bottom
      e.preventDefault();
      e.stopPropagation();
      frame.scrollIntoView({ block: r.height > vh - 140 ? "start" : "center", behavior: "smooth" });
    } catch (err) {
      warnOnce("scroll-arrow", err);
    }
  }


  // ------------------------------------------------------------------- compaction overlay
  // When opencode compacts a Code session it reads the whole conversation, writes a summary of it, then reads the new
  // and much shorter context back in: minutes in which the chat shows nothing else. The running live window posts
  // what it knows ("code:compaction", live.html cPost: the phase, the history it reads as tape lines, the summary as
  // it is written, the engine's read position) and this draws it in a dialog over the chat, like Open WebUI's
  // question dialog (10-25, the user's idea). Hide, Esc or a click beside it fold it into a pill above the message
  // box. Once the model has read the new context it says so and folds into the context ring. It only shows: nothing
  // here is sent anywhere, and every text from the model or the history is set as text, never markup.
  var CMP_ID = "code-cmp", CMP_PILL = "code-cmp-pill";
  var CMP_SYS_CHARS = 60000;   // the system prompt, tools and AGENTS.md before the history, in characters (~17k tokens)
  var CMP_ROW = 22;            // tape row height, px (the CSS)
  var CMP_TAGS = { sys: "system", rules: "rules", sum: "summary", you: "you", says: "says", think: "thinks", sh: "run",
                   edit: "edit", write: "write", read: "read", tool: "tool", ask: "task" };
  var CMP_INST = [{ k: "sys", s: "opencode's system prompt and its tool definitions", w: 3 },
                  { k: "rules", s: "Code mode's standing rules (AGENTS.md)", w: 4 },
                  { k: "rules", s: "The project's own AGENTS.md, when it has one", w: 1 }];
  var cmp = { frame: null, st: null, key: "", tape: null, rows: null, inRows: null, hidden: false, done: false, last: 0,
              tick: 0, endTimer: 0, toastTimer: 0, before: null, readTotal: null, inTotal: null, toastText: "" };

  function cmpFmt(n) { return n == null || isNaN(n) ? "–" : Math.round(n).toLocaleString("en-US"); }
  function cmpClock(ms) { var s = Math.max(0, Math.floor(ms / 1000)); return Math.floor(s / 60) + ":" + (s % 60 < 10 ? "0" : "") + (s % 60); }
  function cmpTook(ms) { var s = Math.max(0, Math.round(ms / 1000)); return s < 60 ? s + " s" : Math.floor(s / 60) + " min " + (s % 60) + " s"; }
  function cmpPlain(s) { return String(s || "").replace(/\*\*|`/g, ""); }
  function cmpEl(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function cmpOnMessage(frame, d) {
    var st = d.state;
    if (!st) {
      if (cmp.frame === frame && !cmp.done) cmpClose();
      return;
    }
    var key = String(st.since || "");
    if (cmp.key !== key || cmp.frame !== frame) {   // a new compaction (or the first message of this one)
      if (cmp.st) cmpClose();
      cmp.key = key; cmp.frame = frame;
      cmp.before = ctx.source === "code" && ctx.used ? ctx.used : null;
    }
    if (d.tape) { cmp.tape = d.tape; cmp.rows = null; cmp.inRows = null; }
    cmp.last = Date.now();
    if (cmp.done) return;
    cmp.st = st;
    if (st.ph === "read" && st.prog && st.prog.state === "reading" && st.prog.total) cmp.readTotal = st.prog.total;
    if (st.ph === "done" || st.ph === "fail") { cmpFinish(st); return; }
    cmpRender();
  }

  function cmpInjectStyle() {
    if (document.getElementById("code-cmp-style")) return;
    var css = [
      ".cmp-ov{position:fixed;inset:0;z-index:60;display:flex;align-items:center;justify-content:center;padding:16px;pointer-events:none}",
      ".cmp-ov.is-hidden{display:none}",
      ".cmp-back{position:absolute;inset:0;background:rgba(0,0,0,.5);-webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px);pointer-events:auto;animation:cmp-fade .3s ease both;transition:opacity .6s}",
      ".cmp-card{position:relative;width:100%;max-width:600px;max-height:calc(100vh - 32px);overflow:hidden;box-sizing:border-box;background:#111113;border:1px solid #2a2a30;border-radius:22px;box-shadow:0 30px 80px rgba(0,0,0,.55);padding:20px 22px 22px;color:#ececf1;font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,\"Segoe UI\",Roboto,sans-serif;display:flex;flex-direction:column;gap:16px;pointer-events:auto;text-align:left;animation:cmp-pop .38s cubic-bezier(.2,.9,.3,1.2) both;transition:transform .9s cubic-bezier(.55,0,.7,.2),opacity .9s ease-in}",
      ".cmp-ov.is-flying .cmp-card,.cmp-ov.is-flying .cmp-back{opacity:0}",
      "@keyframes cmp-pop{from{opacity:0;transform:translateY(10px) scale(.96)}}",
      "@keyframes cmp-fade{from{opacity:0}}",
      ".cmp-hd{display:flex;align-items:center;gap:12px;flex-wrap:wrap;row-gap:10px}",
      ".cmp-spin{width:22px;height:22px;flex:none;animation:cmp-rot 1.1s linear infinite}",
      ".cmp-spin circle{fill:none;stroke:#2a2a30;stroke-width:2.5}",
      ".cmp-spin path{fill:none;stroke:#6ea8ff;stroke-width:2.5;stroke-linecap:round}",
      "@keyframes cmp-rot{to{transform:rotate(360deg)}}",
      ".cmp-tt{flex:1 1 150px;min-width:0}",
      ".cmp-title{overflow-wrap:normal;word-break:normal}",
      ".cmp-title{font-size:16px;font-weight:600;letter-spacing:-.01em}",
      ".cmp-sub{font-size:12px;color:#8a8a95;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      ".cmp-clock{font:500 13px ui-monospace,\"JetBrains Mono\",Menlo,monospace;color:#a6a6b0}",
      ".cmp-hide{height:32px;padding:0 12px;border-radius:99px;border:1px solid #2a2a30;background:#18181c;color:#c9c9d1;font:500 12px Inter,system-ui,sans-serif;cursor:pointer}",
      ".cmp-hide:hover{background:#222228;color:#fff}",
      ".cmp-steps{display:flex;align-items:center;gap:10px}",
      ".cmp-steps b{flex:1;min-width:12px;height:1px;background:#26262c}",
      ".cmp-step{display:flex;align-items:center;gap:8px;color:#8a8a95;font-size:12.5px;font-weight:500;white-space:nowrap}",
      ".cmp-step i{width:20px;height:20px;border-radius:50%;border:1px solid #34343b;display:flex;align-items:center;justify-content:center;font:500 10.5px ui-monospace,monospace;flex:none}",
      ".cmp-step.is-on{color:#ececf1}",
      ".cmp-step.is-on i{border-color:#6ea8ff;background:#6ea8ff;color:#0d0d0f;animation:cmp-ping 1.6s ease-out infinite}",
      ".cmp-step.is-done{color:#a6a6b0}",
      ".cmp-step.is-done i{border-color:#3d6b4a;color:#7bd88f}",
      "@keyframes cmp-ping{0%{box-shadow:0 0 0 0 rgba(110,168,255,.45)}80%,100%{box-shadow:0 0 0 7px rgba(110,168,255,0)}}",
      ".cmp-card section{display:none;flex-direction:column;gap:10px}",
      ".cmp-ov[data-ph=read] .cmp-s-read,.cmp-ov[data-ph=write] .cmp-s-write,.cmp-ov[data-ph=readin] .cmp-s-readin,.cmp-ov[data-ph=done] .cmp-s-done,.cmp-ov[data-ph=fail] .cmp-s-fail{display:flex}",
      ".cmp-ov[data-ph=done] .cmp-steps,.cmp-ov[data-ph=fail] .cmp-steps,.cmp-ov[data-ph=done] .cmp-hide,.cmp-ov[data-ph=fail] .cmp-hide{display:none}",
      ".cmp-ov[data-ph=done] .cmp-spin,.cmp-ov[data-ph=fail] .cmp-spin{animation:none;opacity:.35}",
      ".cmp-row{display:flex;align-items:baseline;justify-content:space-between;gap:12px}",
      ".cmp-h{font-size:14px;font-weight:600}",
      ".cmp-num{font:12px ui-monospace,\"JetBrains Mono\",Menlo,monospace;color:#a6a6b0;white-space:nowrap}",
      ".cmp-small{font-size:12px;color:#a6a6b0}",
      ".cmp-ssdnote{color:#4fd1c5;white-space:nowrap}",
      ".cmp-bar{position:relative;height:6px;border-radius:99px;background:#202026;overflow:hidden}",
      ".cmp-bar i{position:absolute;top:0;bottom:0;left:0;width:0;border-radius:99px;transition:width .3s linear}",
      ".cmp-fill{background:linear-gradient(90deg,#4f86d9,#6ea8ff)}",
      ".cmp-ssd{background:#4fd1c5}",
      ".cmp-tape{position:relative;height:232px;overflow:hidden;border-radius:12px;background:#0b0b0d;border:1px solid #1f1f24;flex:none}",
      ".cmp-tape::before,.cmp-tape::after{content:\"\";position:absolute;left:0;right:0;height:38px;z-index:1;pointer-events:none}",
      ".cmp-tape::before{top:0;background:linear-gradient(#0b0b0d,rgba(11,11,13,0))}",
      ".cmp-tape::after{bottom:0;background:linear-gradient(rgba(11,11,13,0),#0b0b0d)}",
      ".cmp-roll{padding:8px 0;transition:transform .45s cubic-bezier(.2,.7,.2,1)}",
      ".cmp-ln{display:flex;gap:10px;align-items:center;height:22px;padding:0 12px;font:11.5px/22px ui-monospace,\"JetBrains Mono\",Menlo,monospace;white-space:nowrap;overflow:hidden;color:#5d5d67;transition:color .3s,background .3s}",
      ".cmp-k{flex:none;width:46px;text-align:right;font-size:10px;letter-spacing:.06em;text-transform:uppercase}",
      ".cmp-t{overflow:hidden;text-overflow:ellipsis;min-width:0}",
      ".cmp-ln.k-sh .cmp-t::before{content:\"$ \";color:#7bd88f}",
      ".cmp-ln.k-you .cmp-k{color:#e5b567}",
      ".cmp-ln.is-read{color:#a6a6b0}",
      ".cmp-ln.is-ssd{color:#8fd9cf}",
      ".cmp-ln.is-ssd .cmp-k{color:#4fd1c5}",
      ".cmp-ln.is-head{color:#fff;background:linear-gradient(90deg,rgba(110,168,255,.26),rgba(110,168,255,.04));box-shadow:inset 2px 0 0 #6ea8ff}",
      ".cmp-ln.is-head .cmp-k{color:#6ea8ff}",
      ".cmp-fold{display:flex;align-items:center;gap:10px;min-height:34px;padding:0 12px;border-radius:10px;background:#0b0b0d;border:1px solid #1f1f24;font:12px ui-monospace,\"JetBrains Mono\",Menlo,monospace;color:#a6a6b0;animation:cmp-fold .6s cubic-bezier(.2,.7,.2,1) both}",
      ".cmp-fold svg{width:14px;height:14px;flex:none;fill:none;stroke:#7bd88f;stroke-width:2}",
      "@keyframes cmp-fold{from{min-height:232px;opacity:.2}}",
      ".cmp-doc{height:236px;overflow:hidden;display:flex;flex-direction:column;justify-content:flex-end;gap:5px;padding:12px 14px;border-radius:12px;background:#0b0b0d;border:1px solid #1f1f24;font-size:12.5px;line-height:1.55;color:#c9c9d1;box-sizing:border-box;flex:none}",
      ".cmp-doc.cmp-doc-top{justify-content:flex-start}",
      ".cmp-d-h{font-weight:600;color:#fff;font-size:13px;margin-top:6px}",
      ".cmp-d-li{padding-left:12px;text-indent:-12px;overflow-wrap:anywhere}",
      ".cmp-d-new{animation:cmp-in .35s ease-out both}",
      "@keyframes cmp-in{from{opacity:0;transform:translateY(6px)}}",
      ".cmp-caret{display:inline-block;width:7px;height:14px;margin-left:2px;vertical-align:-2px;background:#6ea8ff;animation:cmp-blink 1s steps(2) infinite}",
      "@keyframes cmp-blink{50%{opacity:0}}",
      ".cmp-took{color:#fff;background:rgba(110,168,255,.16);border-radius:3px}",
      ".cmp-ahead{color:#5d5d67}",
      ".cmp-sweep{display:inline-block;width:2px;height:15px;vertical-align:-3px;background:#6ea8ff;box-shadow:0 0 10px #6ea8ff}",
      ".cmp-seg{display:flex;height:10px;gap:2px;border-radius:99px;overflow:hidden}",
      ".cmp-seg>div{position:relative;height:100%;background:#202026;overflow:hidden;min-width:4px}",
      ".cmp-seg i{position:absolute;top:0;bottom:0;left:0;width:0;background:#6ea8ff;transition:width .3s linear}",
      ".cmp-seg .cmp-mid i{background:#b48bff}",
      ".cmp-seg>div:nth-child(3) i{background:#4fd1c5}",
      ".cmp-seglbl{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:11.5px;color:#a6a6b0}",
      ".cmp-seglbl span{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}",
      ".cmp-seglbl span::before{content:\"\";width:8px;height:8px;border-radius:2px;background:#6ea8ff}",
      ".cmp-seglbl span:nth-child(2)::before{background:#b48bff}",
      ".cmp-seglbl span:nth-child(3)::before{background:#4fd1c5}",
      ".cmp-s-done{align-items:center;text-align:center;padding:24px 0 14px}",
      ".cmp-check{width:56px;height:56px;border-radius:50%;background:rgba(123,216,143,.12);border:1px solid rgba(123,216,143,.4);display:flex;align-items:center;justify-content:center;animation:cmp-pop .5s cubic-bezier(.2,1.4,.4,1) both}",
      ".cmp-check svg{width:26px;height:26px;fill:none;stroke:#7bd88f;stroke-width:1.8}",
      ".cmp-done-h{font-size:18px;font-weight:600}",
      ".cmp-done-n{font:14px ui-monospace,\"JetBrains Mono\",Menlo,monospace}",
      ".cmp-done-l{font-size:12.5px;color:#a6a6b0;max-width:460px;line-height:1.6}",
      ".cmp-fail{color:#f19a9a;font-size:13px;line-height:1.55}",
      ".cmp-pill{position:absolute;left:12px;bottom:calc(100% + 10px);z-index:20;display:flex;align-items:center;gap:10px;height:36px;box-sizing:border-box;padding:0 6px 0 12px;border-radius:99px;background:#1c1c20;border:1px solid #2e2e34;box-shadow:0 10px 30px rgba(0,0,0,.35);color:#ececf1;font:12.5px Inter,system-ui,sans-serif;white-space:nowrap;max-width:calc(100% - 24px);animation:cmp-in .3s ease-out both}",
      ".cmp-pill .cmp-spin{width:16px;height:16px}",
      ".cmp-pill-t{overflow:hidden;text-overflow:ellipsis;min-width:0}",
      ".cmp-pill-b{height:26px;padding:0 10px;border-radius:99px;border:1px solid #2e2e34;background:#26262b;color:#c9c9d1;font:500 12px Inter,system-ui,sans-serif;cursor:pointer}",
      ".cmp-pill.is-done{padding:0 14px;background:#13201a;border-color:#2f5a3c;color:#d6f5dd}",
      ".cmp-pill.is-done .cmp-spin,.cmp-pill.is-done .cmp-pill-b{display:none}",
      "@media (max-width:560px){.cmp-card{padding:16px;gap:12px;border-radius:18px}.cmp-tape,.cmp-doc{height:180px}.cmp-step{font-size:11.5px}.cmp-sub{display:none}}",
      "@media (prefers-reduced-motion:reduce){.cmp-ov *,.cmp-pill,.cmp-pill *{animation:none!important;transition:none!important}}",
    ].join("\n");
    var s = document.createElement("style");
    s.id = "code-cmp-style";
    s.textContent = css;
    document.head.appendChild(s);
  }

  var CMP_SPIN = '<svg class="cmp-spin" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 3a9 9 0 0 1 9 9"></path></svg>';
  var CMP_CHECK = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3 8.5 3.2 3L13 4.5"></path></svg>';

  function cmpBuild() {
    cmpInjectStyle();
    var ov = cmpEl("div", "cmp-ov");
    ov.id = CMP_ID;
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-label", "Compacting the session");
    ov.innerHTML =   // fixed markup only: the model's and the history's text goes in with textContent
      '<div class="cmp-back"></div><div class="cmp-card">' +
      '<div class="cmp-hd">' + CMP_SPIN + '<div class="cmp-tt"><div class="cmp-title">Compacting the session</div><div class="cmp-sub"></div></div>' +
      '<div class="cmp-clock">0:00</div><button type="button" class="cmp-hide" title="Keep it working in the background; the pill above the message box brings it back">Hide</button></div>' +
      '<div class="cmp-steps"><span class="cmp-step"><i>1</i>Reading</span><b></b><span class="cmp-step"><i>2</i>Writing</span><b></b><span class="cmp-step"><i>3</i>Reading it in</span></div>' +
      '<section class="cmp-s-read"><div class="cmp-row"><span class="cmp-h">Reading the conversation</span><span class="cmp-num"></span></div>' +
      '<div class="cmp-bar"><i class="cmp-fill"></i><i class="cmp-ssd"></i></div>' +
      '<div class="cmp-row cmp-small"><span class="cmp-note"></span><span class="cmp-ssdnote"></span></div>' +
      '<div class="cmp-tape"><div class="cmp-roll"></div></div></section>' +
      '<section class="cmp-s-write"><div class="cmp-fold">' + CMP_CHECK + '<span></span></div>' +
      '<div class="cmp-row"><span class="cmp-h">Writing the summary</span><span class="cmp-num"></span></div><div class="cmp-doc"></div></section>' +
      '<section class="cmp-s-readin"><div class="cmp-row"><span class="cmp-h">Reading it back in</span><span class="cmp-num"></span></div>' +
      '<div class="cmp-seg"><div><i></i></div><div class="cmp-mid"><i></i></div><div><i></i></div></div>' +
      '<div class="cmp-seglbl"><span></span><span></span><span></span></div><div class="cmp-small cmp-note"></div>' +
      '<div class="cmp-doc cmp-doc-top"></div><div class="cmp-tape cmp-tape-in"><div class="cmp-roll"></div></div></section>' +
      '<section class="cmp-s-done"><div class="cmp-check">' + CMP_CHECK + '</div><div class="cmp-done-h">Compacted</div>' +
      '<div class="cmp-done-n"></div><div class="cmp-done-l"></div></section>' +
      '<section class="cmp-s-fail"><div class="cmp-fail"></div></section></div>';
    ov.querySelector(".cmp-hide").addEventListener("click", function () { cmpHide(true); });
    ov.querySelector(".cmp-back").addEventListener("click", function () { cmpHide(true); });
    (wtHost() || document.body).appendChild(ov);
    return ov;
  }

  function cmpPill() {   // the pill above the message box: the hidden overlay, then the "Compacted" toast
    var host = wtHost() || document.getElementById("message-input-container");
    var p = document.getElementById(CMP_PILL);
    if (!host) {
      if (p) p.remove();
      return null;
    }
    cmpInjectStyle();
    if (!p) {
      p = cmpEl("div", "cmp-pill");
      p.id = CMP_PILL;
      p.innerHTML = CMP_SPIN + '<span class="cmp-pill-t"></span><button type="button" class="cmp-pill-b">Show</button>';
      p.querySelector(".cmp-pill-b").addEventListener("click", function () { cmpHide(false); });
    }
    if (p.parentNode !== host) {
      if (window.getComputedStyle(host).position === "static") host.style.position = "relative";
      host.insertBefore(p, host.firstChild);
    }
    return p;
  }

  function cmpHide(on) {
    cmp.hidden = !!on;
    cmpRender();
  }

  function cmpKey(e) {
    if (e.key === "Escape" && cmp.st && !cmp.hidden && !cmp.done) { e.codeHandled = true; cmpHide(true); }
  }

  function cmpStartTick() {
    if (cmp.tick) return;
    cmp.tick = setInterval(function () {
      try {
        if (!cmp.st) { cmpStopTick(); return; }
        if (cmp.frame && !cmp.frame.isConnected) { cmpClose(); return; }   // another chat, or the reply went away
        if (!cmp.done && Date.now() - cmp.last > 90000) { cmpClose(); return; }   // the window stopped talking
        cmpClockUpdate();
      } catch (e) {
        warnOnce("compaction-tick", e);
      }
    }, 1000);
    document.addEventListener("keydown", cmpKey, true);
  }

  function cmpStopTick() {
    clearInterval(cmp.tick);
    cmp.tick = 0;
    document.removeEventListener("keydown", cmpKey, true);
  }

  function cmpClockUpdate() {
    var ov = document.getElementById(CMP_ID), st = cmp.st;
    if (ov && st) setText(ov.querySelector(".cmp-clock"), cmpClock((st.until || Date.now()) - (st.since || Date.now())));
  }

  function cmpClose() {
    clearTimeout(cmp.endTimer);
    cmpStopTick();
    var ov = document.getElementById(CMP_ID);
    if (ov) ov.remove();
    var p = document.getElementById(CMP_PILL);
    if (p && !p.classList.contains("is-done")) p.remove();
    cmp.frame = null; cmp.st = null; cmp.key = ""; cmp.tape = null; cmp.rows = null; cmp.inRows = null; cmp.hidden = false;
    cmp.done = false; cmp.before = null; cmp.readTotal = null; cmp.inTotal = null; cmp.toastText = "";
  }

  function cmpRender() {
    var st = cmp.st;
    if (!st || cmp.done) return;
    var ov = document.getElementById(CMP_ID) || cmpBuild();
    cmpStartTick();
    setAttr(ov, "data-ph", st.ph);
    ov.classList.toggle("is-hidden", cmp.hidden);
    var n = st.ph === "read" ? 0 : st.ph === "write" ? 1 : 2;
    Array.prototype.forEach.call(ov.querySelectorAll(".cmp-step"), function (s, i) {
      var c = "cmp-step" + (i < n ? " is-done" : i === n ? " is-on" : "");
      if (s.className !== c) s.className = c;
    });
    setText(ov.querySelector(".cmp-sub"), [st.model || "", ctx.limit ? ctxK(ctx.limit) + " context" : ""].filter(Boolean).join(" · "));
    cmpClockUpdate();
    var pr = st.prog || {};
    if (st.ph === "read") cmpRenderRead(ov, st, pr);
    else if (st.ph === "write") cmpRenderWrite(ov, st, pr);
    else if (st.ph === "readin") cmpRenderReadIn(ov, st, pr);
    cmpRenderPill(st, pr);
  }

  function cmpRows(roll, lines) {   // a tape's rows, built once per tape
    roll.textContent = "";
    roll.style.transform = "";
    var cum = [0];
    lines.forEach(function (l) {
      var r = cmpEl("div", "cmp-ln k-" + String(l.k || "").replace(/[^a-z]/g, ""));
      r.appendChild(cmpEl("span", "cmp-k", CMP_TAGS[l.k] || String(l.k || "")));
      r.appendChild(cmpEl("span", "cmp-t", String(l.s || "")));
      roll.appendChild(r);
      cum.push(cum[cum.length - 1] + Math.max(1, +l.w || 0));
    });
    return { lines: lines, cum: cum, els: Array.prototype.slice.call(roll.children), head: -1, ssd: -1, roll: roll };
  }

  function cmpTapeAt(T, frac, ssdFrac, viewH) {   // the read head at this fraction of the tape's weight
    var all = T.cum[T.cum.length - 1], pos = Math.max(0, Math.min(1, frac)) * all, ssdPos = Math.max(0, ssdFrac) * all;
    var head = 0;
    while (head < T.lines.length - 1 && T.cum[head + 1] <= pos) head++;
    if (head === T.head && ssdPos === T.ssd) return;
    T.head = head; T.ssd = ssdPos;
    T.els.forEach(function (r, i) {
      var mark = i === head ? " is-head" : i < head ? (T.cum[i + 1] <= ssdPos ? " is-ssd" : " is-read") : "";
      var c = r.className.replace(/ is-(head|ssd|read)/g, "") + mark;
      if (r.className !== c) r.className = c;
    });
    var max = Math.max(0, T.lines.length * CMP_ROW + 16 - viewH);
    T.roll.style.transform = "translateY(-" + Math.max(0, Math.min(max, head * CMP_ROW - 92)) + "px)";
  }

  function cmpRenderRead(ov, st, pr) {
    var sec = ov.querySelector(".cmp-s-read");
    var total = pr.state === "reading" && pr.total ? pr.total : cmp.readTotal;
    var read = pr.state === "reading" && pr.read != null ? pr.read : pr.state === "generating" && total ? total : 0;
    setText(sec.querySelector(".cmp-num"), total ? cmpFmt(read) + " / " + cmpFmt(total) + " tokens" : "");
    sec.querySelector(".cmp-fill").style.width = (total ? Math.min(100, read / total * 100) : 0) + "%";
    sec.querySelector(".cmp-ssd").style.width = (total && st.ssd ? Math.min(100, st.ssd / total * 100) : 0) + "%";
    var rate = pr.pps || 0;
    setText(sec.querySelector(".cmp-note"), !total
      ? (Date.now() - st.since < 30000 ? "Setting the conversation aside and finding what the engine already has" : "Waiting for the engine")
      : "Reading" + (rate && read < total ? " at " + Math.round(rate) + " tok/s · about " + cmpClock((total - read) / rate * 1000) + " left" : ""));
    setText(sec.querySelector(".cmp-ssdnote"), st.ssd ? cmpFmt(st.ssd) + " already cached" : "");
    var tape = sec.querySelector(".cmp-tape"), roll = tape.querySelector(".cmp-roll");
    if (!cmp.rows) {
      if (!cmp.tape) {
        if (!roll.firstChild) roll.appendChild(cmpEl("div", "cmp-ln", "Gathering the history it reads…"));
        return;
      }
      cmp.rows = cmpRows(roll, [{ k: "sys", s: "System prompt, tools and standing rules", w: CMP_SYS_CHARS }].concat(cmp.tape,
                         [{ k: "ask", s: "Summarize what the user and the assistant said and did", w: 400 }]));
    }
    cmpTapeAt(cmp.rows, total ? read / total : 0, total && st.ssd ? st.ssd / total : 0, tape.clientHeight || 232);
  }

  function cmpRenderWrite(ov, st, pr) {
    var sec = ov.querySelector(".cmp-s-write");
    setText(sec.querySelector(".cmp-fold span"), (cmp.readTotal ? cmpFmt(cmp.readTotal) + " tokens read" : "The conversation is read") +
            (cmp.tape ? " · " + cmp.tape.length + " messages and tool calls" : ""));
    var text = st.text || "";
    var tok = pr.state === "generating" && pr.gen ? pr.gen : text.length / 3.6;
    setText(sec.querySelector(".cmp-num"), cmpFmt(tok) + " tokens" + (pr.state === "generating" && pr.rate ? " · " + Math.round(pr.rate) + " tok/s" : ""));
    var box = sec.querySelector(".cmp-doc");
    if (box.getAttribute("data-len") === String(text.length)) return;
    box.setAttribute("data-len", String(text.length));
    var all = text.split("\n").filter(function (l) { return l.trim(); });
    var grew = String(all.length) !== box.getAttribute("data-n");
    box.setAttribute("data-n", String(all.length));
    var lines = all.slice(-10);
    box.textContent = "";
    if (!lines.length) lines = [""];
    lines.forEach(function (l, i) {
      var h = /^#{1,4}\s+/.test(l);
      var row = cmpEl("div", h ? "cmp-d-h" : "cmp-d-li", cmpPlain(h ? l.replace(/^#{1,4}\s+/, "") : l.replace(/^\s*[-*]\s+/, "• ")));
      if (i === lines.length - 1) {
        row.appendChild(cmpEl("span", "cmp-caret"));
        if (grew) row.classList.add("cmp-d-new");
      }
      box.appendChild(row);
    });
  }

  function cmpSweep(box, text, f) {   // the summary as the model reads it back: read part lit, a cursor where it is
    var k = Math.floor(text.length * Math.max(0, Math.min(1, f)));
    if (box.getAttribute("data-k") === String(k)) return;
    box.setAttribute("data-k", String(k));
    var off = 0, rows = [], cur = 0;
    text.split("\n").forEach(function (l) {
      var a = off;
      off += l.length + 1;
      if (!l.trim()) return;
      var m = /^#{1,4}\s+/.exec(l), cut = m ? m[0].length : 0;
      var txt = cmpPlain(m ? l.slice(cut) : l.replace(/^\s*[-*]\s+/, "• "));
      var took = Math.max(0, Math.min(txt.length, k - a - cut));
      var here = k >= a && k < off;
      if (here) cur = rows.length;
      rows.push({ h: !!m, took: txt.slice(0, took), rest: txt.slice(took), here: here });
    });
    box.textContent = "";
    rows.slice(Math.max(0, cur - 3), Math.max(0, cur - 3) + 8).forEach(function (r) {
      var d = cmpEl("div", r.h ? "cmp-d-h" : "cmp-d-li");
      d.appendChild(cmpEl("span", "cmp-took", r.took));
      if (r.here) d.appendChild(cmpEl("span", "cmp-sweep"));
      d.appendChild(cmpEl("span", "cmp-ahead", r.rest));
      box.appendChild(d);
    });
  }

  function cmpRecent(st) {   // the newest tape lines that add up to the "recent" part opencode keeps word for word
    var tape = cmp.tape || [], want = st.recent || 0, out = [], sum = 0;
    for (var i = tape.length - 1; i >= 0 && (sum < want || !out.length); i--) {
      out.unshift(tape[i]);
      sum += +tape[i].w || 0;
    }
    return out.length ? out.slice(-60) : [{ k: "says", s: "The latest messages, kept word for word", w: 1 }];
  }

  function cmpRenderReadIn(ov, st, pr) {
    var sec = ov.querySelector(".cmp-s-readin");
    var sumTok = (st.tokens && st.tokens.output) || Math.round((st.text || "").length / 3.6);
    var recTok = Math.round((st.recent || 0) / 3.6);
    if (pr.state === "reading" && pr.total) cmp.inTotal = pr.total;
    var total = cmp.inTotal, read = pr.state === "reading" ? pr.read || 0 : 0;
    var A = 0.34, B = 0.44;
    if (total) {
      var inst = Math.max(0, total - sumTok - recTok);
      A = inst / total;
      B = Math.min(1, (inst + sumTok) / total);
    }
    var p = total ? Math.min(1, read / total) : 0;
    setText(sec.querySelector(".cmp-num"), total ? cmpFmt(read) + " / " + cmpFmt(total) + " tokens" : "");
    var spans = [[0, A], [A, B], [B, 1]];
    var segs = sec.querySelectorAll(".cmp-seg > div"), lbls = sec.querySelectorAll(".cmp-seglbl span");
    spans.forEach(function (s, i) {
      var grow = String(Math.max(0.02, s[1] - s[0]));
      segs[i].style.flex = grow + " 1 0";
      var w = s[1] > s[0] ? Math.max(0, Math.min(1, (p - s[0]) / (s[1] - s[0]))) : (p >= s[1] ? 1 : 0);
      segs[i].querySelector("i").style.width = (w * 100) + "%";
    });
    setText(lbls[0], "Instructions" + (total ? " ≈ " + ctxK(A * total) : ""));
    setText(lbls[1], "Summary " + ctxK(sumTok));
    setText(lbls[2], "Recent messages, kept as they were" + (recTok ? " ≈ " + ctxK(recTok) : ""));
    setText(sec.querySelector(".cmp-note"), !total ? "Waiting for the model to start reading the new context"
      : p < A ? "The standing instructions, read fresh: the new context shares nothing with the old one"
      : p < B ? "Now the summary it just wrote"
      : "Then the latest messages, kept word for word" + (pr.pps ? " · " + Math.round(pr.pps) + " tok/s" : ""));
    var doc = sec.querySelector(".cmp-doc"), tape = sec.querySelector(".cmp-tape-in");
    var inSum = !!total && p >= A && p < B;
    doc.style.display = inSum ? "" : "none";
    tape.style.display = inSum ? "none" : "";
    if (inSum) {
      cmpSweep(doc, st.text || "", (p - A) / Math.max(1e-6, B - A));
      return;
    }
    var kind = !total || p < A ? "inst" : "recent";
    if (!cmp.inRows || tape.getAttribute("data-kind") !== kind) {
      tape.setAttribute("data-kind", kind);
      cmp.inRows = cmpRows(tape.querySelector(".cmp-roll"), kind === "inst" ? CMP_INST : cmpRecent(st));
    }
    cmpTapeAt(cmp.inRows, kind === "inst" ? (A ? p / A : 0) : (p - B) / Math.max(1e-6, 1 - B), 0, tape.clientHeight || 236);
  }

  function cmpRenderPill(st, pr) {
    var p = document.getElementById(CMP_PILL);
    if (!cmp.hidden) {
      if (p && !p.classList.contains("is-done")) p.remove();
      return;
    }
    p = cmpPill();
    if (!p) return;
    p.classList.remove("is-done");
    var t;
    if (st.ph === "read") {
      var tot = pr.state === "reading" && pr.total ? pr.total : cmp.readTotal;
      t = tot ? "reading " + cmpFmt(pr.state === "reading" ? pr.read : tot) + " / " + cmpFmt(tot) : "reading the conversation";
    } else if (st.ph === "write") {
      t = "writing the summary · " + cmpFmt(pr.state === "generating" && pr.gen ? pr.gen : (st.text || "").length / 3.6) + " tokens";
    } else {
      t = "reading it back in" + (cmp.inTotal && pr.state === "reading" ? " · " + cmpFmt(pr.read) + " / " + cmpFmt(cmp.inTotal) : "");
    }
    setText(p.querySelector(".cmp-pill-t"), "Compacting · " + t);
  }

  function cmpFinish(st) {
    cmp.st = st; cmp.done = true;
    var ov = document.getElementById(CMP_ID);
    if (st.ph === "fail") {
      if (!ov || cmp.hidden) { cmpClose(); return; }
      setAttr(ov, "data-ph", "fail");
      ov.classList.remove("is-hidden");
      setText(ov.querySelector(".cmp-title"), "Compaction stopped");
      setText(ov.querySelector(".cmp-fail"), "It did not finish (" + (st.err || "unknown error") + "). The session goes on as it was.");
      cmpClockUpdate();
      cmp.endTimer = setTimeout(cmpClose, 4000);
      return;
    }
    var before = cmp.before || (ctx.burst && ctx.burst.before) || null;
    var after = cmp.inTotal || (ctx.source === "code" && ctx.used && (!before || ctx.used < before) ? ctx.used : null);
    var head = (before && after ? cmpFmt(before) + " → " + cmpFmt(after) + " tokens" : after ? cmpFmt(after) + " tokens now" : "Done") +
               " · " + cmpTook((st.until || Date.now()) - (st.since || Date.now()));
    var sumTok = st.tokens && st.tokens.output;
    var detail = (cmp.readTotal ? "Read " + cmpFmt(cmp.readTotal) + (st.ssd ? " (" + cmpFmt(st.ssd) + " of them already cached)" : "") + ", wrote" : "Wrote") +
                 " a " + (sumTok ? cmpFmt(sumTok) + "-token " : "") + "summary, then read the new context" +
                 (cmp.inTotal ? " (" + cmpFmt(cmp.inTotal) + " tokens)" : "") + ".";
    cmp.toastText = "Compacted · " + head;
    if (!ov || cmp.hidden) { cmpLand(before, after); return; }
    setAttr(ov, "data-ph", "done");
    setText(ov.querySelector(".cmp-title"), "Compaction finished");
    setText(ov.querySelector(".cmp-done-n"), head);
    setText(ov.querySelector(".cmp-done-l"), detail);
    cmpClockUpdate();
    cmp.endTimer = setTimeout(function () {
      try { cmpFly(ov); } catch (e) { warnOnce("compaction-fly", e); }
      cmp.endTimer = setTimeout(function () { cmpLand(before, after); }, 900);
    }, 1600);
  }

  function cmpFly(ov) {   // the card shrinks into the context ring
    var card = ov.querySelector(".cmp-card"), ring = (wt.on && wt.el && wt.el.querySelector(".wt-ring")) || document.getElementById(CTX_ID);
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (card && ring && !still && ring.getClientRects().length) {
      var a = card.getBoundingClientRect(), b = ring.getBoundingClientRect();
      var dx = b.left + b.width / 2 - (a.left + a.width / 2), dy = b.top + b.height / 2 - (a.top + a.height / 2);
      card.style.transform = "translate(" + Math.round(dx) + "px," + Math.round(dy) + "px) scale(.04)";
    }
    ov.classList.add("is-flying");
  }

  function cmpLand(before, after) {   // the ring sparks, a short "Compacted" note sits above the message box
    var text = cmp.toastText;
    cmpClose();
    var ring = document.getElementById(CTX_ID);
    if (ring) ctxBurst(ring, before, after || ctx.used);
    var p = text ? cmpPill() : null;
    if (!p) return;
    p.classList.add("is-done");
    setText(p.querySelector(".cmp-pill-t"), text);
    clearTimeout(cmp.toastTimer);
    cmp.toastTimer = setTimeout(function () {
      var q = document.getElementById(CMP_PILL);
      if (q && q.classList.contains("is-done")) q.remove();
    }, 6000);
  }

  // ------------------------------------------------------------------- review overlay
  // Code mode reviews a builder turn in a session of its own (the reviewer did not watch the work): it reads a brief,
  // checks the project with read-only commands, writes a worksheet (ASKED, HAD, ADDED, TESTING, CLAIM lines) and a
  // verdict, and what it finds goes back to the builder. The review's live window posts its round ("code:review",
  // live.html rUpdate); this shows it in a dialog like the compaction overlay (10-25, the user: a full dialog every
  // time, with a "Keep minimized" switch that holds across chats and can be switched back). Minimized, a pill above
  // the message box stands for it and the verdict pops up for a moment. When the findings reach the builder (its next
  // window posts "code:review-handback") the card folds into that window. Nothing here is sent anywhere.
  var RV_ID = "code-rv", RV_PILL = "code-rv-pill", RV_POP = "code-rv-pop", RV_KEEP = "code-mode.review-minimized";
  var RV_TAGS = { rules: "rules", task: "task", asked: "asked", answers: "answers", claims: "claims", files: "files",
                  before: "before", record: "record", recheck: "recheck", reply: "reply" };
  var rv = { frame: null, st: null, key: "", hidden: false, peek: false, done: false, last: 0, tick: 0, endTimer: 0,
             popTimer: 0, toastTimer: 0, handback: null, briefRows: null };

  function rvKeep() {
    try { return window.localStorage.getItem(RV_KEEP) === "1"; } catch (e) { return false; }
  }
  function rvSetKeep(on) {
    try {
      if (on) window.localStorage.setItem(RV_KEEP, "1");
      else window.localStorage.removeItem(RV_KEEP);
    } catch (e) { /* private mode: it holds for this page only */ }
  }
  function rvMinimized() { return (rvKeep() && !rv.peek) || rv.hidden; }

  function rvOnMessage(frame, d) {
    var st = d.state;
    if (!st) return;
    var key = String(st.since || "") + ":" + (st.round || 0);
    if (rv.key !== key || rv.frame !== frame) {   // a new round
      if (rv.st) rvClose();
      rv.key = key; rv.frame = frame; rv.hidden = false; rv.peek = false;
    }
    rv.last = Date.now();
    if (rv.done) return;
    rv.st = st;
    if (st.ended) { rvFinish(); return; }
    rvRender();
  }

  function rvOnHandback(frame, findings) {   // the builder's next window: the findings it was given
    if (!rv.st || !rv.done || rv.handback) return;
    rv.handback = { frame: frame, findings: (findings || []).map(String) };
    clearTimeout(rv.endTimer);
    rvRenderVerdict(document.getElementById(RV_ID), rv.st);
    rvPopUpdate();
    rv.endTimer = setTimeout(function () {
      var ov = document.getElementById(RV_ID);
      if (ov && !rvMinimized()) {
        try { rvFly(ov, wt.on && wt.frame ? wt.frame : frame); } catch (e) { warnOnce("review-fly", e); }
        rv.endTimer = setTimeout(rvLand, 900);
      } else {
        rvLand();
      }
    }, 2600);
  }

  function rvInjectStyle() {
    cmpInjectStyle();
    if (document.getElementById("code-rv-style")) return;
    var css = [
      ".rv-ov .cmp-spin path,.rv-pop .cmp-spin path,#code-rv-pill .cmp-spin path{stroke:#b48bff}",
      ".rv-ov .cmp-step.is-on i{border-color:#b48bff;background:#b48bff;animation:rv-ping 1.6s ease-out infinite}",
      "@keyframes rv-ping{0%{box-shadow:0 0 0 0 rgba(180,139,255,.45)}80%,100%{box-shadow:0 0 0 7px rgba(180,139,255,0)}}",
      ".rv-ov .cmp-fill{background:linear-gradient(90deg,#8a63d9,#b48bff)}",
      ".rv-ov .cmp-ln.is-head{background:linear-gradient(90deg,rgba(180,139,255,.24),rgba(180,139,255,.04));box-shadow:inset 2px 0 0 #b48bff}",
      ".rv-ov .cmp-ln.is-head .cmp-k{color:#c4a8ff}",
      ".rv-ov .cmp-k{width:58px}",
      ".rv-ov[data-ph=brief] .rv-s-brief,.rv-ov[data-ph=check] .rv-s-check,.rv-ov[data-ph=sheet] .rv-s-sheet,.rv-ov[data-ph=verdict] .rv-s-verdict{display:flex}",
      ".rv-ov[data-ph=verdict] .cmp-spin{animation:none;opacity:.35}",
      ".rv-hd-icon{width:24px;height:24px;flex:none;fill:none;stroke:#b48bff;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}",
      ".rv-ov .cmp-hd{flex-wrap:wrap;row-gap:10px}",
      ".rv-ov .cmp-hd .rv-switch{margin-left:auto}",
      ".rv-ov .cmp-steps{gap:8px}",
      ".rv-ov .cmp-step{font-size:12px}",
      ".rv-brief{height:auto;padding:6px 0}",
      ".rv-brief::before,.rv-brief::after{display:none}",
      ".rv-box{position:relative;overflow:hidden;border-radius:12px;background:#0b0b0d;border:1px solid #1f1f24;display:flex;flex-direction:column;justify-content:flex-end;box-sizing:border-box;flex:none}",
      ".rv-box::after{content:\"\";position:absolute;left:0;right:0;top:0;height:30px;background:linear-gradient(#0b0b0d,rgba(11,11,13,0));pointer-events:none}",
      ".rv-cmds{height:268px;padding:6px 0}",
      ".rv-ln{display:flex;gap:10px;align-items:center;min-height:24px;padding:0 12px;font:11.5px/24px ui-monospace,\"JetBrains Mono\",Menlo,monospace;white-space:nowrap;overflow:hidden;color:#a6a6b0}",
      ".rv-ln .cmp-k{width:58px;flex:none;text-align:right;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:#6e6e78}",
      ".rv-ln .cmp-t{overflow:hidden;text-overflow:ellipsis;min-width:0}",
      ".rv-ln.k-sh .cmp-t::before{content:\"$ \";color:#7bd88f}",
      ".rv-ln.is-now{color:#fff;background:linear-gradient(90deg,rgba(180,139,255,.24),rgba(180,139,255,.04));box-shadow:inset 2px 0 0 #b48bff}",
      ".rv-ln.is-now .cmp-k{color:#c4a8ff}",
      ".rv-ln.is-now .cmp-t::after{content:\"\";display:inline-block;width:7px;height:13px;margin-left:4px;vertical-align:-2px;background:#b48bff;animation:cmp-blink 1s steps(2) infinite}",
      ".rv-sheet{height:274px}",
      ".rv-row{display:flex;align-items:center;gap:10px;min-height:28px;padding:0 12px;font-size:12.5px;color:#c9c9d1;border-top:1px solid #16161a}",
      ".rv-row:first-child{border-top:0}",
      ".rv-row.is-new{animation:cmp-in .4s ease-out both;background:rgba(180,139,255,.06)}",
      ".rv-chip{flex:none;min-width:58px;box-sizing:border-box;text-align:center;padding:2px 6px;border-radius:6px;font:600 9.5px ui-monospace,\"JetBrains Mono\",Menlo,monospace;letter-spacing:.06em;background:#1d1d23;color:#a6a6b0}",
      ".rv-row .rv-it{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".rv-st{flex:none;max-width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:500}",
      ".st-ok{background:rgba(123,216,143,.12);color:#8fe0a1}",
      ".st-warn{background:rgba(229,181,103,.14);color:#eec27e}",
      ".st-bad{background:rgba(240,138,138,.14);color:#f4a3a3}",
      ".st-plain{background:#1d1d23;color:#a6a6b0}",
      ".rv-vhead{display:flex;align-items:center;gap:16px;flex-wrap:wrap}",
      ".rv-stamp{display:inline-flex;align-items:center;gap:10px;padding:10px 18px;border-radius:14px;font:700 20px ui-monospace,\"JetBrains Mono\",Menlo,monospace;letter-spacing:.08em;animation:rv-stamp .5s cubic-bezier(.2,1.5,.4,1) both}",
      ".rv-stamp svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round}",
      "@keyframes rv-stamp{from{opacity:0;transform:scale(1.5)}}",
      ".v-incomplete{color:#eec27e;background:rgba(229,181,103,.1);border:1.5px solid rgba(229,181,103,.55)}",
      ".v-pass{color:#8fe0a1;background:rgba(123,216,143,.1);border:1.5px solid rgba(123,216,143,.55)}",
      ".v-fail{color:#f4a3a3;background:rgba(240,138,138,.1);border:1.5px solid rgba(240,138,138,.55)}",
      ".v-none{color:#c9c9d1;background:#1d1d23;border:1.5px solid #34343b}",
      ".rv-vline{flex:1;min-width:200px;font-size:13px;color:#c9c9d1;line-height:1.5}",
      ".rv-tally{display:flex;flex-direction:column;gap:6px;padding:10px 12px;border-radius:10px;background:#0b0b0d;border:1px solid #1f1f24}",
      ".rv-tally div{display:flex;gap:10px;font-size:12px;color:#c9c9d1}",
      ".rv-tally b{flex:none;width:74px;font:600 10px ui-monospace,\"JetBrains Mono\",Menlo,monospace;letter-spacing:.06em;color:#8a8a95;padding-top:2px}",
      ".rv-fh{font-size:13px;font-weight:600}",
      ".rv-find{display:flex;gap:10px;padding:10px 12px;border-radius:10px;background:#0b0b0d;border:1px solid #1f1f24;font-size:12px;line-height:1.5;color:#c9c9d1}",
      ".rv-find .rv-chip{background:rgba(240,138,138,.14);color:#f4a3a3;align-self:flex-start;margin-top:1px}",
      ".rv-find span:last-child{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}",
      ".rv-finds{display:flex;flex-direction:column;gap:8px}",
      ".rv-hb{display:flex;align-items:center;gap:10px;font-size:12.5px;color:#6e6e78}",
      ".rv-hb.is-done{color:#c9c9d1}",
      ".rv-hb.is-now{color:#fff}",
      ".rv-dot{width:16px;height:16px;flex:none;border-radius:50%;border:1.5px solid #34343b;box-sizing:border-box}",
      ".rv-hb.is-done .rv-dot{border-color:#7bd88f;background:#7bd88f}",
      ".rv-hb.is-now .rv-dot{border-color:#b48bff;border-top-color:transparent;animation:cmp-rot .9s linear infinite}",
      ".rv-switch{display:flex;align-items:center;gap:8px;height:32px;padding:0 10px 0 6px;border-radius:99px;border:1px solid #2a2a30;background:#18181c;color:#c9c9d1;font:500 12px Inter,system-ui,sans-serif;cursor:pointer}",
      ".rv-track{position:relative;width:28px;height:16px;border-radius:99px;background:#34343b;transition:background .2s;flex:none}",
      ".rv-knob{position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:50%;background:#ececf1;transition:left .2s}",
      ".rv-switch[aria-checked=true] .rv-track{background:#b48bff}",
      ".rv-switch[aria-checked=true] .rv-knob{left:14px}",
      ".rv-pop{position:absolute;left:12px;bottom:calc(100% + 10px);z-index:21;width:340px;max-width:calc(100% - 24px);box-sizing:border-box;display:flex;flex-direction:column;gap:8px;padding:14px 16px;border-radius:16px;background:#111113;border:1px solid #2a2a30;box-shadow:0 16px 40px rgba(0,0,0,.5);color:#ececf1;font:12.5px/1.5 Inter,system-ui,sans-serif;animation:rv-pop .45s cubic-bezier(.2,1.3,.4,1) both}",
      "@keyframes rv-pop{from{opacity:0;transform:translateY(14px) scale(.92)}}",
      ".rv-pop .rv-pst{padding:4px 10px;border-radius:8px;font:700 12px ui-monospace,\"JetBrains Mono\",Menlo,monospace;letter-spacing:.08em}",
      ".rv-pop .rv-psub{font-size:12px;color:#8a8a95}",
      "@media (max-width:560px){.rv-cmds,.rv-sheet{height:200px}.rv-ov .cmp-step span{display:none}.rv-switch{font-size:11px}}",
      "@media (prefers-reduced-motion:reduce){.rv-pop,.rv-pop *,.rv-ov *{animation:none!important;transition:none!important}}",
    ].join("\n");
    var s = document.createElement("style");
    s.id = "code-rv-style";
    s.textContent = css;
    document.head.appendChild(s);
  }

  var RV_ICON = '<svg class="rv-hd-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"></circle><path d="m15.5 15.5 4.5 4.5"></path><path d="m7.8 10.6 1.9 1.9 3.6-3.8"></path></svg>';

  function rvBuild() {
    rvInjectStyle();
    var ov = cmpEl("div", "cmp-ov rv-ov");
    ov.id = RV_ID;
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-label", "Reviewing the turn");
    ov.innerHTML =   // fixed markup only: the reviewer's and the brief's text goes in with textContent
      '<div class="cmp-back"></div><div class="cmp-card">' +
      '<div class="cmp-hd">' + RV_ICON + '<div class="cmp-tt"><div class="cmp-title">Reviewing the turn</div><div class="cmp-sub"></div></div>' +
      '<div class="cmp-clock">0:00</div>' +
      '<button type="button" class="rv-switch" role="switch" aria-checked="false" title="Show reviews as a pill above the message box, in every chat; the verdict still pops up for a moment">' +
      '<span class="rv-track"><span class="rv-knob"></span></span>Keep minimized</button>' +
      '<button type="button" class="cmp-hide" title="Hide this review; the pill above the message box brings it back">Hide</button></div>' +
      '<div class="cmp-steps"><span class="cmp-step"><i>1</i><span>Reading the brief</span></span><b></b><span class="cmp-step"><i>2</i><span>Checking</span></span><b></b>' +
      '<span class="cmp-step"><i>3</i><span>Worksheet</span></span><b></b><span class="cmp-step"><i>4</i><span>Verdict</span></span></div>' +
      '<section class="rv-s-brief"><div class="cmp-row"><span class="cmp-h">Reading the brief</span><span class="cmp-num"></span></div>' +
      '<div class="cmp-bar"><i class="cmp-fill"></i></div>' +
      '<div class="cmp-small">What Code mode hands the reviewer. It did not watch the builder work, so it starts from this and the project itself.</div>' +
      '<div class="cmp-tape rv-brief"><div class="cmp-roll"></div></div></section>' +
      '<section class="rv-s-check"><div class="cmp-row"><span class="cmp-h">Checking the work</span><span class="cmp-num"></span></div>' +
      '<div class="cmp-small">Comparing the project with how it was before the turn, and the builder\'s claims with the code and its logs. Read-only.</div>' +
      '<div class="rv-box rv-cmds"></div></section>' +
      '<section class="rv-s-sheet"><div class="cmp-fold">' + CMP_CHECK + '<span></span></div>' +
      '<div class="cmp-row"><span class="cmp-h">Writing the worksheet</span><span class="cmp-num"></span></div><div class="rv-box rv-sheet"></div></section>' +
      '<section class="rv-s-verdict"><div class="rv-vhead"><div class="rv-stamp"><svg viewBox="0 0 16 16" aria-hidden="true"><path></path></svg><span></span></div>' +
      '<div class="rv-vline"></div></div><div class="rv-tally"></div><div class="rv-finds"></div></section></div>';
    ov.querySelector(".cmp-hide").addEventListener("click", function () { rvHide(true); });
    ov.querySelector(".cmp-back").addEventListener("click", function () { rvHide(true); });
    ov.querySelector(".rv-switch").addEventListener("click", function () {
      var on = !rvKeep();
      rvSetKeep(on);
      if (on) rv.peek = false;   // from now on reviews stay in the pill, this one too
      rvRender();
    });
    (wtHost() || document.body).appendChild(ov);
    return ov;
  }

  function rvHide(on) {
    rv.hidden = !!on;
    if (!on) rv.peek = true;
    rvRender();
  }

  function rvKey(e) {
    if (e.key === "Escape" && rv.st && !rv.done && !rvMinimized()) { e.codeHandled = true; rvHide(true); }
  }

  function rvStartTick() {
    if (rv.tick) return;
    rv.tick = setInterval(function () {
      try {
        if (!rv.st) { rvStopTick(); return; }
        if (rv.frame && !rv.frame.isConnected && !rv.done) { rvClose(); return; }
        if (!rv.done && Date.now() - rv.last > 120000) { rvClose(); return; }
        rvClockUpdate();
      } catch (e) {
        warnOnce("review-tick", e);
      }
    }, 1000);
    document.addEventListener("keydown", rvKey, true);
  }

  function rvStopTick() {
    clearInterval(rv.tick);
    rv.tick = 0;
    document.removeEventListener("keydown", rvKey, true);
  }

  function rvClockUpdate() {
    var ov = document.getElementById(RV_ID), st = rv.st;
    if (ov && st) setText(ov.querySelector(".cmp-clock"), cmpClock((st.until || Date.now()) - (st.since || Date.now())));
  }

  function rvClose() {
    clearTimeout(rv.endTimer);
    rvStopTick();
    var ov = document.getElementById(RV_ID);
    if (ov) ov.remove();
    var p = document.getElementById(RV_PILL);
    if (p && !p.classList.contains("is-done")) p.remove();
    rv.frame = null; rv.st = null; rv.key = ""; rv.hidden = false; rv.peek = false; rv.done = false; rv.handback = null;
    rv.briefRows = null;
  }

  function rvStatusCls(r) {
    if (r.kind) return "rv-st st-bad";
    if (r.uv || /unverified|half|could not/i.test(r.st)) return "rv-st st-warn";
    if (/^(built|still there|true|cleaned up|asked|needed|kept)/i.test(r.st)) return "rv-st st-ok";
    return "rv-st st-plain";
  }

  function rvFindings(st) {   // the worksheet's findings, as the Code pipe would send them (its own list wins: handback)
    return (st.rows || []).filter(function (r) { return r.kind; }).map(function (r) { return { kind: r.kind, text: r.s + " (" + r.st + ")" }; });
  }

  function rvRender() {
    var st = rv.st;
    if (!st) return;
    var ov = document.getElementById(RV_ID) || rvBuild();
    rvStartTick();
    var min = rvMinimized();
    setAttr(ov, "data-ph", st.ph);
    ov.classList.toggle("is-hidden", min);
    var n = { brief: 0, check: 1, sheet: 2, verdict: 3 }[st.ph] || 0;
    Array.prototype.forEach.call(ov.querySelectorAll(".cmp-step"), function (s, i) {
      var c = "cmp-step" + (i < n ? " is-done" : i === n && !rv.done ? " is-on" : i === n ? " is-done" : "");
      if (s.className !== c) s.className = c;
    });
    setText(ov.querySelector(".cmp-title"), rv.done ? "Review finished" : st.recheck ? "Rechecking the fixes" : "Reviewing the turn");
    setText(ov.querySelector(".cmp-sub"), ["Round " + (st.round || 1), st.recheck ? "a recheck of the fixes" : "an independent check of the builder's turn",
                                           st.model || ""].filter(Boolean).join(" · "));
    setAttr(ov.querySelector(".rv-switch"), "aria-checked", rvKeep() ? "true" : "false");
    rvClockUpdate();
    if (st.ph === "brief") rvRenderBrief(ov, st);
    else if (st.ph === "check") rvRenderCheck(ov, st);
    else if (st.ph === "sheet") rvRenderSheet(ov, st);
    else rvRenderVerdict(ov, st);
    rvRenderPill(st, min);
  }

  function rvRenderBrief(ov, st) {
    var sec = ov.querySelector(".rv-s-brief"), pr = st.prog || {};
    var total = pr.state === "reading" && pr.total ? pr.total : null, read = total ? pr.read || 0 : 0;
    setText(sec.querySelector(".cmp-num"), total ? cmpFmt(read) + " / " + cmpFmt(total) + " tokens" : "");
    sec.querySelector(".cmp-fill").style.width = (total ? Math.min(100, read / total * 100) : 0) + "%";
    var roll = sec.querySelector(".cmp-roll");
    if (!rv.briefRows) rv.briefRows = cmpRows(roll, (st.brief || []).map(function (b) { return { k: b.k, s: b.s, w: b.w }; }));
    rv.briefRows.els.forEach(function (r, i) {   // the brief's own words for its tags
      var k = r.querySelector(".cmp-k");
      if (k) setText(k, RV_TAGS[rv.briefRows.lines[i].k] || rv.briefRows.lines[i].k);
    });
    cmpTapeAt(rv.briefRows, total ? read / total : 0, 0, 100000);
    rv.briefRows.roll.style.transform = "";
  }

  function rvLines(box, items, make) {   // a bottom-aligned list, rebuilt only when it changed
    var sig = JSON.stringify(items);
    if (box.getAttribute("data-sig") === sig) return;
    box.setAttribute("data-sig", sig);
    box.textContent = "";
    items.forEach(function (it, i) { box.appendChild(make(it, i, items.length)); });
  }

  function rvRenderCheck(ov, st) {
    var sec = ov.querySelector(".rv-s-check");
    setText(sec.querySelector(".cmp-num"), (st.nCmds || 0) + " command" + (st.nCmds === 1 ? "" : "s"));
    var cmds = (st.cmds || []).slice(-10), base = (st.nCmds || 0) - cmds.length;
    rvLines(sec.querySelector(".rv-cmds"), cmds, function (c, i, n) {
      var r = cmpEl("div", "rv-ln k-" + String(c.k || "").replace(/[^a-z]/g, "") + (i === n - 1 && c.st !== "completed" && c.st !== "error" ? " is-now" : ""));
      r.appendChild(cmpEl("span", "cmp-k", i === n - 1 && c.st !== "completed" && c.st !== "error" ? "now" : "#" + (base + i + 1)));
      r.appendChild(cmpEl("span", "cmp-t", String(c.s || "")));
      return r;
    });
  }

  function rvRenderSheet(ov, st) {
    var sec = ov.querySelector(".rv-s-sheet");
    var took = st.sheetFrom && st.checkFrom ? cmpTook(st.sheetFrom - st.checkFrom) : "";
    setText(sec.querySelector(".cmp-fold span"), (st.nCmds || 0) + " command" + (st.nCmds === 1 ? "" : "s") + " run" + (took ? " · " + took + " of checking" : ""));
    var rows = st.rows || [];
    setText(sec.querySelector(".cmp-num"), rows.length + " line" + (rows.length === 1 ? "" : "s"));
    rvLines(sec.querySelector(".rv-sheet"), rows.slice(-9), function (r, i, n) {
      var row = cmpEl("div", "rv-row" + (i === n - 1 ? " is-new" : ""));
      row.appendChild(cmpEl("span", "rv-chip", r.tag));
      row.appendChild(cmpEl("span", "rv-it", r.s));
      row.appendChild(cmpEl("span", rvStatusCls(r), r.kind ? r.kind : r.st));
      if (r.kind) row.lastChild.title = r.st;
      return row;
    });
  }

  var RV_STAMPS = {
    PASS: ["v-pass", "m3 8.5 3.2 3L13 4.5", "No problems found in what was asked, what the project had, or the builder's claims."],
    INCOMPLETE: ["v-incomplete", "M8 3.5v5.5M8 12v.5", "Some evidence was not available, and nothing was proved wrong."],
    FAIL: ["v-fail", "M4.5 4.5l7 7M11.5 4.5l-7 7", "It found work that is missing or not true."],
    NONE: ["v-none", "M4 8h8", "The review ended without a verdict."]
  };

  function rvRenderVerdict(ov, st) {
    if (!ov) return;
    var sec = ov.querySelector(".rv-s-verdict"), v = st.verdict || (st.ended ? "NONE" : "");
    var s = RV_STAMPS[v] || RV_STAMPS.NONE;
    var stamp = sec.querySelector(".rv-stamp");
    stamp.className = "rv-stamp " + s[0];
    setText(stamp.querySelector("span"), v && v !== "NONE" ? v : "NO VERDICT");
    setAttr(stamp.querySelector("path"), "d", s[1]);
    setText(sec.querySelector(".rv-vline"), v ? s[2] : "Writing its verdict…");
    var groups = [["ASKED", "Asked"], ["HAD", "Had"], ["ADDED", "Added"], ["TESTING", "Testing"], ["CLAIM", "Claims"]];
    var tally = [];
    groups.forEach(function (g) {
      var counts = {}, order = [];
      (st.rows || []).forEach(function (r) {
        if (r.tag !== g[0]) return;
        var k = (r.kind ? r.kind : String(r.st).split(/ \(| \/|,|;/)[0].trim().toLowerCase()) || "?";
        if (!counts[k]) { counts[k] = 0; order.push(k); }
        counts[k]++;
      });
      if (order.length) tally.push({ tag: g[1].toUpperCase() + " " + order.reduce(function (a, k) { return a + counts[k]; }, 0),
                                     text: order.map(function (k) { return counts[k] + " " + k; }).join(" · ") });
    });
    rvLines(sec.querySelector(".rv-tally"), tally, function (x) {
      var d = cmpEl("div");
      d.appendChild(cmpEl("b", "", x.tag));
      d.appendChild(cmpEl("span", "", x.text));
      return d;
    });
    sec.querySelector(".rv-tally").style.display = tally.length ? "" : "none";
    var sent = rv.handback, finds = sent ? sent.findings.map(function (f) {
      var m = /^\[([^\]]+)\]\s*(.*)$/.exec(f);
      return { kind: m ? m[1] : "finding", text: (m ? m[2] : f).replace(/[`*]/g, "") };
    }) : rvFindings(st);
    var box = sec.querySelector(".rv-finds"), items = [];
    if (finds.length || (rv.done && v === "FAIL")) {
      items.push({ h: sent ? (finds.length === 1 ? "1 finding" : finds.length + " findings") + " sent back to the builder"
                            : (finds.length === 1 ? "1 finding" : finds.length + " findings") + " in the worksheet" });
      finds.slice(0, 4).forEach(function (f) { items.push({ f: f }); });
      if (rv.done) {
        items.push({ hb: "Handing it to the builder", st: sent ? "done" : "now" });
        items.push({ hb: "The builder has it", st: sent ? "now" : "" });
      }
    } else if (rv.done && v) {
      items.push({ ok: "Nothing goes back to the builder." });
    }
    rvLines(box, items, function (it) {
      if (it.h) return cmpEl("div", "rv-fh", it.h);
      if (it.ok) return cmpEl("div", "rv-fh", it.ok);
      if (it.f) {
        var d = cmpEl("div", "rv-find");
        d.appendChild(cmpEl("span", "rv-chip", it.f.kind));
        d.appendChild(cmpEl("span", "", it.f.text));
        return d;
      }
      var h = cmpEl("div", "rv-hb" + (it.st === "done" ? " is-done" : it.st === "now" ? " is-now" : ""));
      h.appendChild(cmpEl("span", "rv-dot"));
      h.appendChild(cmpEl("span", "", it.hb));
      return h;
    });
  }

  function rvPillHost(id, cls) {
    var host = wtHost() || document.getElementById("message-input-container");
    var p = document.getElementById(id);
    if (!host) { if (p) p.remove(); return null; }
    if (!p) {
      p = cmpEl("div", cls);
      p.id = id;
    }
    if (p.parentNode !== host) {
      if (window.getComputedStyle(host).position === "static") host.style.position = "relative";
      host.insertBefore(p, host.firstChild);
    }
    p.style.bottom = document.getElementById(CMP_PILL) && id !== CMP_PILL ? (wtHost() ? "60px" : "calc(100% + 56px)") : "";
    return p;
  }

  function rvRenderPill(st, min) {
    var p = document.getElementById(RV_PILL);
    if (!min || rv.done) {
      if (p && !p.classList.contains("is-done")) p.remove();
      return;
    }
    rvInjectStyle();
    p = rvPillHost(RV_PILL, "cmp-pill");
    if (!p) return;
    if (!p.querySelector(".cmp-pill-b")) {
      p.innerHTML = CMP_SPIN + '<span class="cmp-pill-t"></span><button type="button" class="cmp-pill-b">Show</button>';
      p.querySelector(".cmp-pill-b").addEventListener("click", function () { rvHide(false); });
    }
    p.classList.remove("is-done");
    var pr = st.prog || {}, t;
    if (st.ph === "brief") t = "reading the brief" + (pr.state === "reading" && pr.total ? " · " + cmpFmt(pr.read) + " / " + cmpFmt(pr.total) : "");
    else if (st.ph === "check") t = "checking · " + (st.nCmds || 0) + " command" + (st.nCmds === 1 ? "" : "s");
    else if (st.ph === "sheet") t = "writing the worksheet · " + (st.rows || []).length + " lines";
    else t = "verdict: " + (st.verdict || "…");
    setText(p.querySelector(".cmp-pill-t"), "Reviewing · round " + (st.round || 1) + " · " + t);
  }

  function rvSummary() {   // one line for the toast and the pop: the verdict, what went back, how long
    var st = rv.st || {}, v = st.verdict;
    var n = rv.handback ? rv.handback.findings.length : rvFindings(st).length;
    var took = cmpTook((st.until || Date.now()) - (st.since || Date.now()));
    if (n) return { head: (v || "Review") + "", line: (n === 1 ? "1 finding goes" : n + " findings go") + " back to the builder.",
                    toast: "Review: " + (n === 1 ? "1 finding" : n + " findings") + " sent back to the builder · " + took };
    if (v === "PASS") return { head: v, line: "Nothing goes back to the builder.", toast: "Review passed · round " + (st.round || 1) + " · " + took };
    if (v) return { head: v, line: "Nothing goes back to the builder.", toast: "Review: " + v.toLowerCase() + " · nothing sent back · " + took };
    return { head: "NO VERDICT", line: "The review ended without a verdict.", toast: "Review ended without a verdict · " + took };
  }

  function rvPop() {   // minimized: the verdict pops up above the message box for a moment
    rvInjectStyle();
    var host = wtHost() || document.getElementById("message-input-container");
    if (!host) return;
    var p = document.getElementById(RV_POP) || cmpEl("div", "rv-pop");
    p.id = RV_POP;
    p.innerHTML = '<div style="display:flex;align-items:center;gap:10px"><span class="rv-pst"></span><span class="rv-psub"></span></div><div class="rv-pl"></div>';
    if (p.parentNode !== host) host.insertBefore(p, host.firstChild);
    rvPopUpdate();
    clearTimeout(rv.popTimer);
    rv.popTimer = setTimeout(function () { var q = document.getElementById(RV_POP); if (q) q.remove(); }, 6000);
  }

  function rvPopUpdate() {
    var p = document.getElementById(RV_POP);
    if (!p || !rv.st) return;
    var s = rvSummary(), v = rv.st.verdict || "NONE", c = (RV_STAMPS[v] || RV_STAMPS.NONE)[0];
    var pst = p.querySelector(".rv-pst");
    pst.className = "rv-pst " + c;
    setText(pst, v === "NONE" ? "NO VERDICT" : v);
    setText(p.querySelector(".rv-psub"), "Review · round " + (rv.st.round || 1) + " · " +
            cmpClock((rv.st.until || Date.now()) - (rv.st.since || Date.now())));
    setText(p.querySelector(".rv-pl"), s.line);
  }

  function rvFinish() {   // the review's turn has ended: the verdict, then the hand-back (or a toast)
    rv.done = true;
    var st = rv.st, min = rvMinimized();
    var ov = document.getElementById(RV_ID) || rvBuild();
    rvStartTick();
    setAttr(ov, "data-ph", "verdict");
    ov.classList.toggle("is-hidden", min);
    rvRender();
    var pill = document.getElementById(RV_PILL);
    if (pill && !pill.classList.contains("is-done")) pill.remove();
    if (min) rvPop();
    var expect = rvFindings(st).length > 0 || st.verdict === "FAIL" || st.verdict === "INCOMPLETE";
    clearTimeout(rv.endTimer);
    rv.endTimer = setTimeout(function () {   // nothing to wait for, or the builder never picked it up
      var o = document.getElementById(RV_ID);
      if (o && !rvMinimized()) {
        o.classList.add("is-flying");
        rv.endTimer = setTimeout(rvLand, 700);
      } else {
        rvLand();
      }
    }, expect ? 45000 : 3500);
  }

  function rvFly(ov, frame) {   // the card shrinks into the builder's window
    var card = ov.querySelector(".cmp-card");
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (card && frame && !still && frame.getClientRects().length) {
      var a = card.getBoundingClientRect(), b = frame.getBoundingClientRect();
      var dx = b.left + b.width / 2 - (a.left + a.width / 2), dy = b.top + Math.min(b.height, 200) / 2 - (a.top + a.height / 2);
      card.style.transform = "translate(" + Math.round(dx) + "px," + Math.round(dy) + "px) scale(.08)";
    }
    ov.classList.add("is-flying");
  }

  function rvLand() {
    var s = rv.st ? rvSummary() : null, popShown = !!document.getElementById(RV_POP);
    rvPopUpdate();
    rvClose();
    if (!s || popShown) return;   // the pop already says it
    rvInjectStyle();
    var p = rvPillHost(RV_PILL, "cmp-pill");
    if (!p) return;
    p.innerHTML = CMP_SPIN + '<span class="cmp-pill-t"></span>';
    p.classList.add("is-done");
    setText(p.querySelector(".cmp-pill-t"), s.toast);
    clearTimeout(rv.toastTimer);
    rv.toastTimer = setTimeout(function () {
      var q = document.getElementById(RV_PILL);
      if (q && q.classList.contains("is-done")) q.remove();
    }, 6000);
  }

  // ------------------------------------------------------------------- watch mode
  // The chat's work full window (10-25, the user keeps the chat centred on the live window with the checklist open,
  // and the slerm panel's AI tab beside it): the running live window as the stage (a second copy of it, live.html
  // #fill=1, which follows the turn from round to round), the agent's task above, a panel at the right with Preview,
  // Checklist or Engine, and the engine strip at the bottom. No message box. The compaction and review overlays
  // dock over the stage only, so the panel stays in view.
  // The panel picks itself (the user: a preview that does not change should not hold the screen): Checklist, or
  // Engine while compacting; Preview only for a few minutes at a time (WT_MIN_KEY, default 2) when it has just
  // changed, and Preview again when the turn is over. A tab picked by hand stays until something new happens (the
  // preview changes, a compaction starts or ends, the turn ends), 10 minutes at most (Preview: its minutes).
  var WT_ID = "code-watch", WT_MIN_KEY = "code-mode.watch-preview-min", WT_HAND_MS = 10 * 60000, WT_ENGINE_MS = 2000;
  var WT_GAP_MS = 8000;   // no live window this long after one was: the turn is over (rounds hand over in seconds)
  var WT_PANEL_URL = window.__codeSlermPanel || "http://slerm-head:8080/";
  var wt = { on: false, el: null, stage: null, frame: null, src: "", tab: "", pick: null, pickAt: 0, prevAt: 0,
             ended: false, sawLive: false, noLiveAt: 0, st: null, eng: null, engAt: 0, engBusy: false, engTried: false,
             tick: 0, ckSig: "", ckTicked: null, pframe: "", imp: "",
             tools: null, toolsAt: 0, toolsBusy: false, toolsTried: false, builderSrc: "",
             stateAt: 0, loadAt: 0, reloadAt: 0 };
  var WT_STALL_MS = 20000;     // the stage said nothing this long: reload it (10-26, the user: never blank)
  var WT_REOPEN_KEY = "code-mode.watch-reopen";
  var WT_TOOLS_MS = 20000;   // the Tools tab asks code-live again this often while it shows

  function wtHost() { return wt.on && wt.stage ? wt.stage : null; }

  function wtMinutes() {
    var v = parseFloat(safeGet(WT_MIN_KEY));
    return isFinite(v) && v > 0 ? Math.min(60, v) : 2;
  }

  function wtClock(ms) {
    var s = Math.max(0, Math.floor(ms / 1000)), h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, r = s % 60;
    return (h ? h + ":" + (m < 10 ? "0" : "") : "") + m + ":" + (r < 10 ? "0" : "") + r;
  }

  function wtPlain(s) { return String(s || "").replace(/\*\*|`/g, "").replace(/\s+/g, " ").trim(); }

  function wtTitle() {
    var t = String(document.title || "").replace(/\s*[|•·–-]\s*Open WebUI\s*$/i, "").trim();
    return t && !/^Open WebUI$/i.test(t) ? t : "This chat";
  }

  function wtInjectStyle() {
    cmpInjectStyle();
    if (document.getElementById("code-watch-style")) return;
    var css = [
      "#code-watch{position:fixed;inset:0;z-index:70;display:flex;flex-direction:column;background:#0b0b0d;color:#ececf1;font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,\"Segoe UI\",Roboto,sans-serif}",
      "html.code-watching,html.code-watching body{overflow:hidden}",
      ".wt-top{flex:none;display:flex;align-items:center;gap:14px;flex-wrap:wrap;row-gap:8px;min-height:56px;padding:8px 18px;box-sizing:border-box;border-bottom:1px solid #1a1a1f}",
      ".wt-dot{width:8px;height:8px;border-radius:50%;flex:none;background:#6ea8ff;animation:wt-pulse 1.6s ease-out infinite}",
      "#code-watch[data-mode=compact] .wt-dot{background:#4fd1c5}#code-watch[data-mode=review] .wt-dot{background:#b48bff}",
      "#code-watch[data-mode=done] .wt-dot,#code-watch[data-mode=idle] .wt-dot{background:#6e6e78;animation:none}",
      "@keyframes wt-pulse{0%{box-shadow:0 0 0 0 rgba(110,168,255,.5)}80%,100%{box-shadow:0 0 0 6px rgba(110,168,255,0)}}",
      ".wt-title{font-size:15px;font-weight:600;max-width:280px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      ".wt-chip{flex:none;padding:3px 8px;border-radius:6px;font:600 10px ui-monospace,\"JetBrains Mono\",Menlo,monospace;letter-spacing:.08em;background:rgba(110,168,255,.14);color:#8fbaff}",
      "#code-watch[data-mode=compact] .wt-chip{background:rgba(79,209,197,.14);color:#6fe0d4}",
      "#code-watch[data-mode=review] .wt-chip{background:rgba(180,139,255,.16);color:#c4a8ff}",
      "#code-watch[data-mode=done] .wt-chip,#code-watch[data-mode=idle] .wt-chip{background:#1d1d23;color:#a6a6b0}",
      ".wt-model{font-size:12.5px;color:#8a8a95;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}",
      ".wt-clock{font:500 13px ui-monospace,\"JetBrains Mono\",Menlo,monospace;color:#c9c9d1}",
      ".wt-round{font-size:12px;color:#8a8a95;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:0 1 auto}",
      ".wt-sp{flex:1}",
      ".wt-pmin{display:flex;align-items:center;gap:6px;font-size:12px;color:#8a8a95}",
      ".wt-pmin select{height:30px;border-radius:8px;border:1px solid #2a2a30;background:#16161a;color:#c9c9d1;font:12px Inter,system-ui,sans-serif;padding:0 6px}",
      ".wt-seg{display:flex;padding:3px;border-radius:10px;background:#16161a;border:1px solid #232329}",
      ".wt-seg button{height:30px;padding:0 14px;border:0;border-radius:7px;background:transparent;color:#a6a6b0;font:500 12.5px Inter,system-ui,sans-serif;cursor:pointer}",
      ".wt-seg button[aria-pressed=true]{background:#2a2a31;color:#fff}",
      ".wt-btn{height:34px;padding:0 12px;border-radius:9px;border:1px solid #2a2a30;background:#16161a;color:#c9c9d1;font:500 12.5px Inter,system-ui,sans-serif;cursor:pointer;display:inline-flex;align-items:center;gap:8px;text-decoration:none}",
      ".wt-btn:hover{background:#1f1f24;color:#fff}",
      ".wt-btn svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round}",
      ".wt-kbd{font:500 10.5px ui-monospace,monospace;color:#8a8a95;border:1px solid #34343b;border-radius:5px;padding:1px 5px}",
      ".wt-task{flex:none;display:flex;align-items:center;gap:12px;min-height:44px;padding:6px 18px;box-sizing:border-box;border-bottom:1px solid #1a1a1f;font-size:13px;overflow:hidden}",
      ".wt-task[hidden]{display:none}",
      ".wt-tl,.wt-dl{flex:none;padding:3px 8px;border-radius:6px;font:600 10px ui-monospace,monospace;letter-spacing:.08em;background:rgba(110,168,255,.12);color:#8fbaff}",
      ".wt-dl{background:#1d1d23;color:#a6a6b0}.wt-dl[hidden]{display:none}",
      ".wt-tt{flex:0 1 auto;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;max-width:40%}",
      ".wt-td{flex:1;min-width:0;color:#c9c9d1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      ".wt-ckn{flex:none;display:flex;align-items:center;gap:8px;font:500 12px ui-monospace,monospace;color:#a6a6b0}",
      ".wt-bar{position:relative;display:block;height:5px;border-radius:99px;background:#202026;overflow:hidden}",
      ".wt-bar b{position:absolute;left:0;top:0;bottom:0;border-radius:99px;background:#7bd88f;transition:width .6s}",
      ".wt-body{flex:1;min-height:0;display:flex;gap:16px;padding:16px 18px}",
      ".wt-stage{position:relative;flex:1;min-width:0;border-radius:16px;overflow:hidden;border:1px solid #1f1f24;background:#0a0a0b}",
      ".wt-stage iframe{position:absolute;inset:0;width:100%;height:100%;border:0;background:#0a0a0b}",
      ".wt-stage .cmp-ov{position:absolute;z-index:5}",
      ".wt-stage>.cmp-pill,.wt-stage>.rv-pop{left:14px;bottom:14px;z-index:6}",
      ".wt-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#8a8a95;font-size:13px}",
      ".wt-panel{width:430px;flex:none;display:flex;flex-direction:column;border-radius:16px;overflow:hidden;border:1px solid #1f1f24;background:#111113;min-height:0}",
      ".wt-p{display:none;flex:1;min-height:0;flex-direction:column}",
      "#code-watch[data-tab=preview] .wt-p-preview,#code-watch[data-tab=checklist] .wt-p-checklist,#code-watch[data-tab=engine] .wt-p-engine,#code-watch[data-tab=tools] .wt-p-tools{display:flex}",
      ".wt-tbars{display:flex;flex-direction:column;gap:5px;margin-top:8px}",
      ".wt-tbar{display:grid;grid-template-columns:minmax(0,150px) 1fr 52px 40px;align-items:center;gap:10px;font-size:12px;color:#c9c9d1}",
      ".wt-tbar .n{font:12px ui-monospace,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".wt-tbar .c{font:500 12px ui-monospace,monospace;text-align:right}.wt-tbar .s{font-size:11px;color:#8a8a95;text-align:right}",
      ".wt-tbar .wt-bar b{background:#6ea8ff}.wt-tbar.mcp .wt-bar b{background:#b48bff}",
      ".wt-mcp{display:flex;align-items:center;gap:10px;min-height:28px;font-size:12.5px;color:#c9c9d1;border-top:1px solid #18181d}",
      ".wt-mcp:first-child{border-top:0}",
      ".wt-mcp i{width:7px;height:7px;border-radius:50%;background:#6e6e78;flex:none}.wt-mcp i.on{background:#7bd88f}",
      ".wt-mcp .n{flex:1;font:12.5px ui-monospace,monospace}.wt-mcp .st{font-size:11.5px;color:#8a8a95}",
      ".wt-mcp .k{font:500 12px ui-monospace,monospace;min-width:84px;text-align:right}.wt-mcp .k.none{color:#e5b567;font-family:inherit;font-weight:400}",
      ".wt-kinds{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}",
      ".wt-kind{padding:3px 9px;border-radius:99px;font-size:11.5px;background:rgba(240,138,138,.12);color:#f4a3a3}",
      ".wt-kind b{font:600 11px ui-monospace,monospace;margin-left:4px}",
      ".wt-purl{flex:none;display:flex;align-items:center;gap:8px;height:46px;padding:0 12px;border-bottom:1px solid #1a1a1f}",
      ".wt-purl span{flex:1;min-width:0;height:30px;line-height:30px;padding:0 10px;border-radius:8px;background:#0b0b0d;border:1px solid #1f1f24;font:12px ui-monospace,monospace;color:#a6a6b0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      ".wt-purl .wt-btn{height:30px;padding:0 9px}",
      ".wt-pframe{flex:1;min-height:0;width:100%;border:0;background:#fff}",
      ".wt-pnone{flex:1;display:flex;align-items:center;justify-content:center;padding:24px;text-align:center;color:#8a8a95;font-size:13px}",
      ".wt-pnone[hidden],.wt-pframe[hidden]{display:none}",
      ".wt-pnote{flex:none;padding:8px 14px;font-size:11.5px;color:#8a8a95;border-top:1px solid #1a1a1f}",
      ".wt-ck{flex:1;min-height:0;overflow-y:auto;padding:16px 18px 20px;color:#e5e7eb;font:13px/1.5 Inter,system-ui,sans-serif}",
      ".wt-ck .ck-sub,.wt-ck .ck-meta,.wt-ck .ck-row.done .ck-label,.wt-ck .ck-detail{color:#a1a1aa}",
      ".wt-ck .ck-bar{background:#26262d}.wt-ck .ck-bar i{background:#3ecf8e}.wt-ck .ck-now{background:#161b26}",
      ".wt-ck .ck-h,.wt-ck .ck-id{color:#8a8a94}.wt-ck .ck-now .ck-id,.wt-ck .cp-ck-count{color:#a9c8ff}",
      ".wt-ck .ck-label code,.wt-ck .ck-detail code{color:#f0d9a8}.wt-ck .ck-ev,.wt-ck .ck-foot{color:#7c7c86}",
      ".wt-eng{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:16px;padding:16px 18px;box-sizing:border-box}",
      ".wt-ehd{display:flex;align-items:center;gap:10px}",
      ".wt-ehd>span:first-child{flex:1;display:flex;flex-direction:column;min-width:0}",
      ".wt-ename{font-size:14px;font-weight:600}.wt-esub{font-size:11.5px;color:#8a8a95}",
      ".wt-big{display:flex;align-items:baseline;gap:10px}",
      ".wt-big b{font:600 30px ui-monospace,\"JetBrains Mono\",Menlo,monospace;letter-spacing:-.02em}",
      ".wt-big span{font-size:13px;color:#a6a6b0}",
      ".wt-lbl{font:600 10px ui-monospace,monospace;letter-spacing:.08em;color:#8a8a95;text-transform:uppercase}",
      ".wt-num{font:500 12px ui-monospace,\"JetBrains Mono\",Menlo,monospace;color:#c9c9d1}",
      ".wt-row{display:flex;justify-content:space-between;align-items:baseline;gap:10px}",
      ".wt-ctx{position:relative;height:8px;border-radius:99px;background:#202026}",
      ".wt-ctx i{position:absolute;left:0;top:0;bottom:0;border-radius:99px;background:linear-gradient(90deg,#4f86d9,#6ea8ff);transition:width .6s}",
      ".wt-ctx b{position:absolute;top:-3px;bottom:-3px;width:2px;background:#e5b567}",
      ".wt-small{font-size:11.5px;color:#8a8a95}",
      ".wt-bars{display:flex;align-items:flex-end;gap:5px;height:72px}",
      ".wt-bars i{flex:1;min-height:2px;border-radius:3px 3px 0 0;background:#3b5f99}",
      ".wt-bars i:last-child{background:#6ea8ff}",
      ".wt-reads{height:44px}.wt-reads i{background:#4fd1c5}.wt-reads i.big{background:#e5b567}",
      ".wt-gpu{display:flex;align-items:center;gap:10px;font-size:12px;color:#a6a6b0}",
      ".wt-gpu .wt-bar{flex:1}.wt-gpu .wt-bar b{background:#6ea8ff}",
      ".wt-reqs{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:4px 6px;font:11.5px ui-monospace,monospace;color:#c9c9d1}",
      ".wt-reqs span.h{color:#8a8a95}",
      ".wt-strip{flex:none;display:flex;align-items:center;gap:20px;min-height:54px;padding:6px 18px;box-sizing:border-box;border-top:1px solid #1a1a1f;font-size:12px;color:#a6a6b0;flex-wrap:wrap;row-gap:6px}",
      ".wt-echip{flex:none;padding:3px 8px;border-radius:6px;font:600 10px ui-monospace,monospace;letter-spacing:.08em;background:#1d1d23;color:#a6a6b0}",
      ".wt-echip[data-s=generating]{background:rgba(110,168,255,.14);color:#8fbaff}",
      ".wt-echip[data-s=reading]{background:rgba(79,209,197,.14);color:#6fe0d4}",
      ".wt-etext{color:#ececf1;white-space:nowrap}",
      ".wt-spark{width:120px;height:26px;flex:none}",
      ".wt-spark polyline{fill:none;stroke:#6ea8ff;stroke-width:1.6;stroke-linejoin:round;stroke-linecap:round}",
      ".wt-div{width:1px;height:22px;background:#232329;flex:none}",
      ".wt-mgpu{display:flex;align-items:center;gap:8px}.wt-mgpu .wt-bar{width:52px}.wt-mgpu .wt-bar b{background:#6ea8ff}",
      ".wt-ring{width:24px;height:24px;flex:none}",
      ".wt-ring circle{fill:none;stroke-width:4}",
      ".wt-ring .bg{stroke:#2a2a30}.wt-ring .fg{stroke:#7bd88f;stroke-linecap:round;transition:stroke-dashoffset .8s}",
      ".wt-ring.near .fg{stroke:#e5b567}.wt-ring.over .fg{stroke:#f08a8a}",
      "@media (max-width:860px){.wt-body{flex-direction:column;padding:10px;gap:10px}.wt-stage{flex:1 1 52%}.wt-panel{width:auto;flex:1 1 48%}",
      ".wt-top{padding:8px 12px;gap:10px}.wt-model,.wt-round,.wt-pmin,.wt-kbd{display:none}.wt-task{padding:6px 12px}.wt-tt{max-width:none}.wt-td{display:none}",
      ".wt-strip{padding:6px 12px;gap:12px}.wt-spark,.wt-div,.wt-mgpu+.wt-mgpu,.wt-toeng{display:none}.wt-seg button{padding:0 10px}}",
      "@media (prefers-reduced-motion:reduce){#code-watch *{animation:none!important;transition:none!important}}",
    ].join("\n");
    var s = document.createElement("style");
    s.id = "code-watch-style";
    s.textContent = css;
    document.head.appendChild(s);
  }

  var WT_EYE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"></path><circle cx="12" cy="12" r="3"></circle></svg>';

  function wtBuild() {
    var el = cmpEl("div");
    el.id = WT_ID;
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-label", "Watch mode");
    el.innerHTML =   // fixed markup; every text from the chat, the agent or the engine goes in with textContent
      '<header class="wt-top"><span class="wt-dot"></span><span class="wt-title"></span><span class="wt-chip">WORKING</span>' +
      '<span class="wt-model"></span><span class="wt-clock"></span><span class="wt-round"></span><span class="wt-sp"></span>' +
      '<label class="wt-pmin" title="How long the preview may hold the panel before the checklist (or the engine) takes it back">Preview' +
      '<select><option value="1">1 min</option><option value="2">2 min</option><option value="5">5 min</option><option value="10">10 min</option></select></label>' +
      '<div class="wt-seg" role="group" aria-label="Side panel"><button type="button" data-tab="preview" aria-pressed="false">Preview</button>' +
      '<button type="button" data-tab="checklist" aria-pressed="false">Checklist</button><button type="button" data-tab="engine" aria-pressed="false">Engine</button>' +
      '<button type="button" data-tab="tools" aria-pressed="false">Tools</button></div>' +
      '<button type="button" class="wt-btn wt-full" aria-label="Full screen" title="Full screen"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10"></path></svg></button>' +
      '<button type="button" class="wt-btn wt-exit">Exit <span class="wt-kbd">Esc</span></button></header>' +
      '<div class="wt-task" hidden><span class="wt-tl">TASK</span><span class="wt-tt"></span><span class="wt-dl">DOING</span><span class="wt-td"></span>' +
      '<span class="wt-ckn"><span class="wt-ckc"></span><span class="wt-bar" style="width:64px"><b></b></span></span></div>' +
      '<div class="wt-body"><div class="wt-stage"><div class="wt-empty">Waiting for this chat\'s live window…</div><iframe class="wt-live" title="This chat\'s turn, live"></iframe></div>' +
      '<aside class="wt-panel">' +
      '<div class="wt-p wt-p-preview"><div class="wt-purl"><span></span>' +
      '<button type="button" class="wt-btn wt-preload" aria-label="Reload the preview"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v3h-3"></path></svg></button>' +
      '<a class="wt-btn wt-popen" target="_blank" rel="noopener" aria-label="Open the preview in a new tab"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M9 3h4v4M13 3 7.5 8.5M12 9.5V13H3V4h3.5"></path></svg></a></div>' +
      '<iframe class="wt-pframe" title="The project\'s preview"></iframe><div class="wt-pnone" hidden>No preview for this chat yet.</div>' +
      '<div class="wt-pnote"></div></div>' +
      '<div class="wt-p wt-p-checklist"><div class="wt-ck cp-ck"></div></div>' +
      '<div class="wt-p wt-p-engine"><div class="wt-eng">' +
      '<div class="wt-ehd"><span><span class="wt-ename"></span><span class="wt-esub"></span></span><span class="wt-echip wt-estate"></span></div>' +
      '<div class="wt-big"><b class="wt-etok"></b><span class="wt-ephase"></span></div>' +
      '<div><div class="wt-row"><span class="wt-lbl">Context</span><span class="wt-num wt-ectx"></span></div>' +
      '<div class="wt-ctx" style="margin-top:6px"><i></i><b></b></div><div class="wt-row wt-small" style="margin-top:6px"><span class="wt-elast"></span><span class="wt-eat" style="color:#e5b567"></span></div></div>' +
      '<div><div class="wt-row"><span class="wt-lbl">Decode speed · last requests</span><span class="wt-num wt-erange" style="color:#8a8a95"></span></div><div class="wt-bars wt-dec" style="margin-top:6px"></div></div>' +
      '<div><div class="wt-row"><span class="wt-lbl">Prompt read per request</span><span class="wt-small">the rest came from the cache</span></div><div class="wt-bars wt-reads" style="margin-top:6px"></div></div>' +
      '<div class="wt-gpus" style="display:flex;flex-direction:column;gap:8px"></div><div class="wt-small wt-node"></div>' +
      '<div class="wt-reqs"></div>' +
      '<a class="wt-small wt-plink" target="_blank" rel="noopener" style="color:#6ea8ff;margin-top:auto">Open the AI tab in the slerm panel</a>' +
      '</div></div>' +
      '<div class="wt-p wt-p-tools"><div class="wt-eng wt-tools">' +
      '<div><div class="wt-row"><span class="wt-lbl">This turn</span><span class="wt-num wt-tturn"></span></div><div class="wt-tbars wt-turnbars"></div></div>' +
      '<div><div class="wt-row"><span class="wt-lbl">Whole chat</span><span class="wt-num wt-tall"></span></div><div class="wt-tbars wt-allbars"></div></div>' +
      '<div><div class="wt-lbl">MCP servers</div><div class="wt-mcps"></div></div>' +
      '<div><div class="wt-row"><span class="wt-lbl">Review send-backs</span><span class="wt-num wt-tsb"></span></div><div class="wt-kinds"></div></div>' +
      '</div></div></aside></div>' +
      '<footer class="wt-strip"><span style="display:flex;align-items:center;gap:10px"><span class="wt-echip wt-sstate">ENGINE</span><span class="wt-etext"></span>' +
      '<svg class="wt-spark" viewBox="0 0 120 26" aria-hidden="true"><polyline points=""></polyline></svg></span><span class="wt-div"></span>' +
      '<span class="wt-mgpus" style="display:flex;align-items:center;gap:20px"></span><span class="wt-div"></span>' +
      '<span style="display:flex;align-items:center;gap:8px"><svg class="wt-ring" viewBox="0 0 36 36" aria-hidden="true"><circle class="bg" cx="18" cy="18" r="15"></circle>' +
      '<circle class="fg" cx="18" cy="18" r="15" pathLength="100" stroke-dasharray="100" stroke-dashoffset="100" transform="rotate(-90 18 18)"></circle></svg>' +
      '<span class="wt-num wt-rctx"></span><span class="wt-rat"></span></span><span class="wt-sp"></span>' +
      '<button type="button" class="wt-btn wt-toeng" style="height:30px">Engine</button></footer>';
    wt.stage = el.querySelector(".wt-stage");
    wt.frame = el.querySelector(".wt-live");
    el.querySelector(".wt-exit").addEventListener("click", wtClose);
    el.querySelector(".wt-full").addEventListener("click", function () {
      try {
        if (document.fullscreenElement) document.exitFullscreen();
        else if (el.requestFullscreen) el.requestFullscreen();
      } catch (e) { /* not allowed here */ }
    });
    Array.prototype.forEach.call(el.querySelectorAll(".wt-seg button"), function (b) {
      b.addEventListener("click", function () { wtPick(b.getAttribute("data-tab")); });
    });
    el.querySelector(".wt-toeng").addEventListener("click", function () { wtPick("engine"); });
    el.querySelector(".wt-preload").addEventListener("click", function () { wtPreviewLoad(true); });
    var sel = el.querySelector(".wt-pmin select");
    sel.value = String(wtMinutes());
    if (!sel.value) { var o = cmpEl("option", "", wtMinutes() + " min"); o.value = String(wtMinutes()); sel.appendChild(o); sel.value = o.value; }
    sel.addEventListener("change", function () { safeSet(WT_MIN_KEY, sel.value); wtDecide(); });
    el.querySelector(".wt-plink").setAttribute("href", WT_PANEL_URL);
    document.body.appendChild(el);
    return el;
  }

  function wtOpen(force) {
    if (wt.on || !currentChatId || (!force && !document.querySelector(LIVE_FRAMES))) return;
    wtInjectStyle();
    wt.on = true; wt.ended = false; wt.sawLive = false; wt.noLiveAt = 0; wt.pick = null; wt.pickAt = 0; wt.prevAt = 0;
    wt.src = ""; wt.st = null; wt.tab = ""; wt.ckSig = ""; wt.ckTicked = null; wt.pframe = ""; wt.eng = null; wt.engAt = 0;
    wt.engTried = false;
    wt.imp = "";
    wt.tools = null; wt.toolsAt = 0; wt.toolsTried = false; wt.builderSrc = "";
    wt.stateAt = 0; wt.loadAt = 0; wt.reloadAt = 0;
    wt.el = wtBuild();
    document.documentElement.classList.add("code-watching");
    ["code-cmp", "code-rv"].forEach(function (id) { var o = document.getElementById(id); if (o) wt.stage.appendChild(o); });
    [CMP_PILL, RV_PILL, RV_POP].forEach(function (id) { var p = document.getElementById(id); if (p && !p.classList.contains("is-done")) p.remove(); });
    if (cmp.st && !cmp.done) cmpRender();
    if (rv.st && !rv.done) rvRender();
    wf.pollAt = 0;   // ask at once whether the Fixer is on this chat
    if (wf.url && !wf.done) wfRender();
    document.addEventListener("keydown", wtKey, true);
    if (!document.querySelector(LIVE_FRAMES)) wtEmpty("Nothing is running in this chat yet. Watch picks it up when it starts.");
    wtNoteReopen();
    wtTick();
    wt.tick = setInterval(wtTick, 1000);
    try { wt.el.querySelector(".wt-exit").focus(); } catch (e) { /* fine */ }
  }

  function wtClose() {
    if (!wt.on) return;
    wt.on = false;
    try { sessionStorage.removeItem(WT_REOPEN_KEY); } catch (e) { /* fine */ }
    clearInterval(wt.tick);
    wt.tick = 0;
    document.removeEventListener("keydown", wtKey, true);
    ["code-cmp", "code-rv"].forEach(function (id) { var o = document.getElementById(id); if (o) document.body.appendChild(o); });
    [CMP_PILL, RV_PILL, RV_POP].forEach(function (id) { var p = document.getElementById(id); if (p) p.remove(); });
    clearTimeout(wf.foldTimer);
    if (wf.done) wfClear();   // a verdict already shown: the next Watch starts on the builder
    if (wt.el) wt.el.remove();
    wt.el = null; wt.stage = null; wt.frame = null;
    document.documentElement.classList.remove("code-watching");
    try { if (document.fullscreenElement) document.exitFullscreen(); } catch (e) { /* fine */ }
    if (cmp.st && !cmp.done) cmpRender();
    if (rv.st && !rv.done) rvRender();
  }

  function wtKey(e) {
    if (e.key !== "Escape" || e.codeHandled) return;
    if ((cmp.st && !cmp.hidden && !cmp.done) || (rv.st && !rv.done && !rvMinimized())) return;   // that dialog closes first
    e.preventDefault();
    if (wfShowing() && !wf.done) { wfHide(true); return; }   // the Fixer folds into its pill first
    wtClose();
  }

  function wtTick() {
    if (!wt.on) return;
    if (!wt.el || !wt.el.isConnected) { wtClose(); return; }
    try { wtSync(); } catch (e) { warnOnce("watch-sync", e); }
    try { wtDecide(); } catch (e) { warnOnce("watch-decide", e); }
    try { wtRender(); } catch (e) { warnOnce("watch-render", e); }
    try { wfTick(); } catch (e) { warnOnce("watch-fixer", e); }
    try { wtStallCheck(); } catch (e) { warnOnce("watch-stall", e); }
    if (Date.now() - wt.engAt >= WT_ENGINE_MS) wtEngineFetch();
    if (wt.tab === "tools" && Date.now() - wt.toolsAt >= WT_TOOLS_MS) wtToolsFetch();
  }

  function wtSync() {   // the stage follows the chat's running window (the builder's, a review's, the next round's)
    var frames = document.querySelectorAll(LIVE_FRAMES), live = runningLiveFrame(), now = Date.now(), target = null;
    var last = frames.length ? frames[frames.length - 1] : null;
    if (live) {
      target = live;
      wt.sawLive = true;
      wt.noLiveAt = 0;
      wt.ended = false;
    } else {
      if (!wt.noLiveAt) wt.noLiveAt = now;
      if (!wt.src && last) target = last;
      var over = wt.sawLive ? now - wt.noLiveAt > WT_GAP_MS : !last || last.getAttribute("data-code-live") === "0";
      if (over && !wt.ended) {
        wt.ended = true;
        wt.pick = null;   // the turn is over: the panel finishes on the preview
      }
    }
    if (target) {
      var src = target.getAttribute("src") || "";
      if (src && src !== wt.src) {
        wt.src = src;
        wt.st = null;
        wt.loadAt = Date.now();
        wt.stateAt = 0;
        wt.frame.setAttribute("src", src + (src.indexOf("#") >= 0 ? "&" : "#") + "fill=1");
        var empty = wt.stage.querySelector(".wt-empty");
        if (empty) empty.remove();
      }
    }
  }

  // The stage tells the page its state every second (live.html #fill=1). When it falls silent - a lost connection,
  // a page that never loaded - it is reloaded, at most every 30 s (10-26, the user: "autoreload on its own").
  function wtStallCheck() {
    if (!wt.on || !wt.src || !wt.frame || document.hidden) return;
    var now = Date.now(), since = Math.max(wt.stateAt, wt.loadAt);
    if (now - since < WT_STALL_MS || now - wt.reloadAt < 30000) return;
    wt.reloadAt = now;
    wt.loadAt = now;
    var src = wt.frame.getAttribute("src");
    wt.frame.setAttribute("src", "about:blank");
    setTimeout(function () { if (wt.on && wt.frame) wt.frame.setAttribute("src", src); }, 80);
  }

  // Watch moves with you (10-26, the user): another chat keeps Watch open on that chat, waiting for its work.
  function wtRetarget() {
    if (!wt.on || !wt.el) return;
    wfClear();
    wf.pollAt = 0;
    wt.src = ""; wt.st = null; wt.ended = false; wt.sawLive = false; wt.noLiveAt = 0; wt.pick = null; wt.pickAt = 0;
    wt.prevAt = 0; wt.ckSig = ""; wt.ckTicked = null; wt.pframe = ""; wt.tools = null; wt.toolsAt = 0;
    wt.builderSrc = ""; wt.stateAt = 0; wt.loadAt = 0;
    wt.frame.setAttribute("src", "about:blank");
    wtEmpty(currentChatId ? "Nothing is running in this chat yet. Watch picks it up when it starts."
      : "Open a chat to watch it.");
    wtNoteReopen();
  }

  function wtEmpty(text) {
    if (!wt.stage) return;
    var e = wt.stage.querySelector(".wt-empty");
    if (!e) {
      e = cmpEl("div", "wt-empty");
      wt.stage.insertBefore(e, wt.stage.firstChild);
    }
    setText(e, text);
  }

  function wtNoteReopen() {   // Watch comes back after a reload of this tab (a new version, or the user's F5)
    try {
      if (wt.on && currentChatId) sessionStorage.setItem(WT_REOPEN_KEY, JSON.stringify({ chat: currentChatId, at: Date.now() }));
    } catch (e) { /* fine */ }
  }

  function wtHasChecklist() {
    return !!(checklist.data && checklist.chat === currentChatId && (checklist.data.items || []).length);
  }

  function wtDecide() {   // which tab the panel shows now
    if (!wt.on || !wt.el) return;
    var now = Date.now(), min = wtMinutes() * 60000;
    var important = cmp.st && !cmp.done ? "engine" : wtHasChecklist() ? "checklist" : "engine";
    if (important !== wt.imp) {   // a compaction started or ended: news beats a tab picked before it
      if (wt.imp) wt.pick = null;
      wt.imp = important;
    }
    var want;
    if (wt.pick && now - wt.pickAt < (wt.pick === "preview" ? min : WT_HAND_MS)) want = wt.pick;
    else {
      wt.pick = null;
      if (wt.ended && preview.base) want = "preview";                              // the turn is over: its result
      else if (preview.base && wt.prevAt && now - wt.prevAt < min) want = "preview";   // the preview just changed
      else want = important;
    }
    if (want === "preview" && !preview.base) want = important;
    if (want !== wt.tab) wtSetTab(want);
  }

  function wtPick(tab) {
    wt.pick = tab;
    wt.pickAt = Date.now();
    wtSetTab(tab);
  }

  function wtSetTab(tab) {
    wt.tab = tab;
    setAttr(wt.el, "data-tab", tab);
    Array.prototype.forEach.call(wt.el.querySelectorAll(".wt-seg button"), function (b) {
      setAttr(b, "aria-pressed", b.getAttribute("data-tab") === tab ? "true" : "false");
    });
    if (tab === "preview") wtPreviewLoad(false);
    else if (tab === "checklist") wtChecklist();
    else if (tab === "tools") { wtToolsRender(); wtToolsFetch(); }
    else wtEngineRender();
  }

  function wtPreviewChanged() {
    wt.prevAt = Date.now();
    wt.pick = null;   // news beats a tab picked before it
    if (wt.tab === "preview") wtPreviewLoad(true);
    else wt.pframe = "";   // load it fresh when it comes up
    wtDecide();
  }

  function wtPreviewLoad(force) {
    if (!wt.el) return;
    var f = wt.el.querySelector(".wt-pframe"), url = preview.page || preview.base || "";
    setText(wt.el.querySelector(".wt-purl span"), url ? "Preview · " + (url.replace(/^https?:\/\/[^/]+\/p\/[^/]+\/[0-9a-f]+\//, "/") || "/") : "");
    wt.el.querySelector(".wt-pnone").hidden = !!url;
    f.hidden = !url;
    var open = wt.el.querySelector(".wt-popen");
    if (url) open.setAttribute("href", url); else open.removeAttribute("href");
    if (url && (force || wt.pframe !== url)) {
      wt.pframe = url;
      f.setAttribute("src", url);
    }
    var min = wtMinutes(), left = wt.pick === "preview" ? min * 60000 - (Date.now() - wt.pickAt)
      : wt.prevAt ? min * 60000 - (Date.now() - wt.prevAt) : 0;
    setText(wt.el.querySelector(".wt-pnote"), wt.ended ? "The turn is over: this is what it built."
      : left > 0 ? "Changed just now · the checklist comes back in " + wtClock(left) : "Shown for " + min + " min when the page changes.");
  }

  function wtChecklist() {   // the checklist panel's own drawing, in the Watch panel
    var box = wt.el && wt.el.querySelector(".wt-ck");
    if (!box) return;
    if (!(checklist.data && checklist.chat === currentChatId)) {
      if (!box.querySelector(".ck-empty")) box.innerHTML = '<div class="ck-empty">No checklist for this chat yet.</div>';
      wt.ckSig = "";
      return;
    }
    var s0 = checklist.sig, t0 = checklist.ticked;
    checklist.sig = wt.ckSig;
    checklist.ticked = wt.ckTicked;
    try {
      renderChecklist(box);
    } finally {
      wt.ckSig = checklist.sig;
      wt.ckTicked = checklist.ticked;
      checklist.sig = s0;
      checklist.ticked = t0;
    }
  }

  function wtTask() {
    var d = checklist.data && checklist.chat === currentChatId ? checklist.data : null;
    if (!d) return null;
    var cur = d.current && d.current.task ? d.current : null;
    if (cur) return { label: "TASK", task: wtPlain(cur.task), doing: wtPlain(cur.doing) };
    var first = (d.items || []).filter(function (it) { return !it.done; })[0];
    return first ? { label: "NEXT", task: wtPlain((first.id ? first.id + " " : "") + first.label), doing: "" } : null;
  }

  function wtRender() {
    if (!wt.on || !wt.el) return;
    var el = wt.el, st = wt.st || {}, now = Date.now();
    var mode = wfActive() ? "fixer" : cmp.st && !cmp.done ? "compact" : rv.st && !rv.done ? "review" : wt.ended ? "done"
      : runningLiveFrame() || st.live ? "work" : "idle";
    setAttr(el, "data-mode", mode);
    setText(el.querySelector(".wt-chip"), { fixer: wf.phase === "claude" ? "CLAUDE" : "FIXER", compact: "COMPACTING", review: "REVIEWING", done: "DONE", work: "WORKING",
                                            idle: "WAITING" }[mode]);
    setText(el.querySelector(".wt-title"), wtTitle());
    setText(el.querySelector(".wt-model"), [st.model || "", ctx.limit ? ctxK(ctx.limit) + " context" : ""].filter(Boolean).join(" · "));
    setText(el.querySelector(".wt-clock"), st.since ? wtClock((!st.live && st.until ? st.until : now) - st.since) : "");
    setText(el.querySelector(".wt-round"), st.review ? "review round " + st.review.round + (st.now ? " · " + st.now : "") : st.now || "");
    var task = wtTask(), strip = el.querySelector(".wt-task");
    strip.hidden = !task;
    if (task) {
      setText(strip.querySelector(".wt-tl"), task.label);
      setText(strip.querySelector(".wt-tt"), task.task);
      setText(strip.querySelector(".wt-td"), task.doing);
      strip.querySelector(".wt-dl").hidden = !task.doing;
      var d = checklist.data;
      setText(strip.querySelector(".wt-ckc"), d && d.total ? d.done + "/" + d.total : "");
      strip.querySelector(".wt-bar b").style.width = d && d.total ? Math.round(d.done / d.total * 100) + "%" : "0%";
    }
    var sel = el.querySelector(".wt-pmin select");
    if (sel && document.activeElement !== sel && sel.value !== String(wtMinutes())) sel.value = String(wtMinutes());
    if (wt.tab === "checklist") wtChecklist();
    else if (wt.tab === "preview") wtPreviewLoad(false);
    wtStripRender();
  }

  function wtEngineUrl() {   // code-live's /engine, next to the live window the stage shows
    var src = String(wt.src || ""), base = src.split("#")[0].split("?")[0];
    return /\/w\/[^/]+\/[0-9a-f]{32}\/$/.test(base) ? base + "engine" : "";
  }

  async function wtEngineFetch() {
    var url = wtEngineUrl();
    if (wt.engBusy || !url) return;
    wt.engBusy = true;
    wt.engAt = Date.now();
    try {
      var r = await _fetch(url, { cache: "no-store" });
      if (r.ok) wt.eng = ((await r.json()) || {}).engine || null;
    } catch (e) {
      wt.eng = null;
    } finally {
      wt.engBusy = false;
      wt.engTried = true;
    }
    if (!wt.on) return;
    try {
      wtStripRender();
      if (wt.tab === "engine") wtEngineRender();
    } catch (e) {
      warnOnce("watch-engine", e);
    }
  }

  function wtEngineState(e) {
    if (!e) return { s: "", chip: "ENGINE", text: wt.engTried ? "no reading from the slerm panel" : "…" };
    if (e.state === "reading") return { s: "reading", chip: "READING", text: e.prompt_total ? cmpFmt(e.prompt_read) + " / " + cmpFmt(e.prompt_total) + " tokens" : "the prompt" };
    if (e.state === "generating") return { s: "generating", chip: "GENERATING", text: (e.phase ? e.phase + " · " : "") + (e.tok_s != null ? e.tok_s.toFixed(1) + " tok/s" : "") };
    return { s: e.state || "", chip: String(e.state || "idle").toUpperCase(), text: e.model || "" };
  }

  function wtGpuName(g, i) { return "GPU " + i; }

  function wtStripRender() {
    if (!wt.el) return;
    var el = wt.el, e = wt.eng, es = wtEngineState(e);
    var chip = el.querySelector(".wt-sstate");
    setText(chip, es.chip);
    setAttr(chip, "data-s", es.s);
    setText(el.querySelector(".wt-etext"), es.text);
    var reqs = e && e.requests ? e.requests.slice().reverse() : [];
    var sp = reqs.filter(function (q) { return q.tok_s != null; }).map(function (q) { return q.tok_s; });
    var lo = Math.min.apply(null, sp.concat([1e9])), hi = Math.max.apply(null, sp.concat([0]));
    var pts = sp.map(function (v, i) {
      return (sp.length > 1 ? i * 120 / (sp.length - 1) : 60).toFixed(1) + "," + (hi > lo ? 23 - (v - lo) / (hi - lo) * 20 : 13).toFixed(1);
    }).join(" ");
    setAttr(el.querySelector(".wt-spark polyline"), "points", pts);
    var gbox = el.querySelector(".wt-mgpus"), gpus = e && e.gpus ? e.gpus : [];
    if (gbox.children.length !== gpus.length) {
      gbox.textContent = "";
      gpus.forEach(function () {
        var s = cmpEl("span", "wt-mgpu");
        s.innerHTML = '<span class="wt-lbl"></span><span class="wt-bar"><b></b></span><span class="wt-num"></span><span class="wt-t"></span>';
        gbox.appendChild(s);
      });
    }
    gpus.forEach(function (g, i) {
      var s = gbox.children[i];
      setText(s.querySelector(".wt-lbl"), wtGpuName(g, i));
      s.querySelector(".wt-bar b").style.width = (g.util || 0) + "%";
      setText(s.querySelector(".wt-num"), g.util != null ? g.util + "%" : "–");
      setText(s.querySelector(".wt-t"), g.temp != null ? g.temp + "°C" : "");
    });
    var used = ctx.used != null ? ctx.used : e && e.prompt_tokens != null ? e.prompt_tokens : null;   // the engine's, until the chat says
    var limit = ctx.limit || (e && e.max_context) || 0;
    var pct = limit && used != null ? Math.min(100, used / limit * 100) : 0;
    var ring = el.querySelector(".wt-ring");
    setAttr(ring.querySelector(".fg"), "stroke-dashoffset", String(Math.round((100 - pct) * 10) / 10));
    ring.classList.toggle("near", !!(ctx.at && used >= ctx.at * 0.85 && used < ctx.at));
    ring.classList.toggle("over", !!(ctx.at && used >= ctx.at));
    setText(el.querySelector(".wt-rctx"), used != null && limit ? ctxK(used) + " / " + ctxK(limit) : "");
    setText(el.querySelector(".wt-rat"), ctx.at ? "compacts at " + ctxK(ctx.at) : "");
  }

  function wtEngineRender() {   // the slerm panel's AI tab, folded in
    if (!wt.el) return;
    var el = wt.el, e = wt.eng, es = wtEngineState(e);
    var gpus = e && e.gpus ? e.gpus : [];
    setText(el.querySelector(".wt-ename"), e ? [e.engine === "strata" ? "Strata" : e.engine || "Engine", e.node || ""].filter(Boolean).join(" · ") : "Engine");
    setText(el.querySelector(".wt-esub"), e ? [e.model || "", gpus.length ? gpus.length + "× " + String(gpus[0].name || "GPU").replace(/^Tesla\s+/, "") : ""].filter(Boolean).join(" · ")
      : wt.engTried ? "The slerm panel did not answer." : "Asking the slerm panel…");
    var chip = el.querySelector(".wt-estate");
    setText(chip, es.chip);
    setAttr(chip, "data-s", es.s);
    setText(el.querySelector(".wt-etok"), e && e.state === "generating" && e.tok_s != null ? e.tok_s.toFixed(1)
      : e && e.state === "reading" && e.prompt_total ? Math.round(e.prompt_read / e.prompt_total * 100) + "%" : "–");
    setText(el.querySelector(".wt-ephase"), e && e.state === "generating" ? "tok/s" + (e.phase ? " · " + e.phase : "") + (e.tok_s_mean ? " · mean " + e.tok_s_mean.toFixed(1) : "")
      : e && e.state === "reading" ? "of the prompt read" + (e.pps ? " · " + Math.round(e.pps) + " tok/s" : "") : e ? es.chip.toLowerCase() : "");
    var used = (e && e.prompt_tokens) || ctx.used, max = (e && e.max_context) || ctx.limit;
    setText(el.querySelector(".wt-ectx"), used && max ? cmpFmt(used) + " / " + cmpFmt(max) : "");
    el.querySelector(".wt-ctx i").style.width = used && max ? Math.min(100, used / max * 100) + "%" : "0%";
    var mark = el.querySelector(".wt-ctx b");
    mark.style.display = ctx.at && max ? "" : "none";
    if (ctx.at && max) mark.style.left = Math.min(100, ctx.at / max * 100) + "%";
    setText(el.querySelector(".wt-eat"), ctx.at ? "compacts at " + ctxK(ctx.at) : "");
    var q0 = e && e.requests && e.requests[0];
    setText(el.querySelector(".wt-elast"), q0 ? "last request: " + cmpFmt(q0.reused) + " reused · " + cmpFmt(q0.read) + " read" : "");
    var reqs = e && e.requests ? e.requests.slice().reverse() : [];
    var dec = reqs.map(function (q) { return q.tok_s || 0; }), hi = Math.max.apply(null, dec.concat([1])), lo = Math.min.apply(null, dec.concat([hi]));
    setText(el.querySelector(".wt-erange"), dec.length ? Math.round(lo) + "–" + Math.round(hi) + " tok/s" : "");
    wtBars(el.querySelector(".wt-dec"), dec.map(function (v) { return { h: hi ? Math.max(4, v / hi * 100) : 4, t: v.toFixed(1) + " tok/s" }; }));
    var reads = reqs.map(function (q) { return q.read || 0; }), rmax = Math.sqrt(Math.max.apply(null, reads.concat([1])));
    wtBars(el.querySelector(".wt-reads"), reads.map(function (v) { return { h: Math.max(4, Math.sqrt(v) / rmax * 100), t: cmpFmt(v) + " tokens read", big: v > 1000 }; }));
    var gbox = el.querySelector(".wt-gpus");
    if (gbox.children.length !== gpus.length) {
      gbox.textContent = "";
      gpus.forEach(function () {
        var r = cmpEl("div", "wt-gpu");
        r.innerHTML = '<span class="wt-lbl" style="width:44px"></span><span class="wt-bar"><b></b></span><span class="wt-num" style="width:36px;text-align:right"></span>' +
          '<span class="wt-gm" style="width:80px;text-align:right"></span><span class="wt-gt" style="width:40px;text-align:right"></span><span class="wt-gp" style="width:46px;text-align:right"></span>';
        gbox.appendChild(r);
      });
    }
    gpus.forEach(function (g, i) {
      var r = gbox.children[i];
      setText(r.querySelector(".wt-lbl"), wtGpuName(g, i));
      r.querySelector(".wt-bar b").style.width = (g.util || 0) + "%";
      setText(r.querySelector(".wt-num"), g.util != null ? g.util + "%" : "–");
      setText(r.querySelector(".wt-gm"), g.mem_used != null && g.mem_total ? (g.mem_used / 1024).toFixed(1) + "/" + Math.round(g.mem_total / 1024) + " GB" : "");
      setText(r.querySelector(".wt-gt"), g.temp != null ? g.temp + "°C" : "");
      setText(r.querySelector(".wt-gp"), g.power != null ? Math.round(g.power) + " W" : "");
    });
    var h = e && e.host;
    setText(el.querySelector(".wt-node"), h && h.mem_total ? "Node: RAM " + (h.mem_used / 1024).toFixed(1) + " / " + (h.mem_total / 1024).toFixed(1) + " GB" +
      (h.cpu != null ? " · CPU " + Math.round(h.cpu) + "%" + (h.cpus ? " of " + h.cpus : "") : "") + (h.disk_read != null ? " · disk " + h.disk_read.toFixed(1) + " MB/s" : "") : "");
    var table = el.querySelector(".wt-reqs"), rows = (e && e.requests ? e.requests : []).slice(0, 5);
    var sig = JSON.stringify(rows);
    if (table.getAttribute("data-sig") !== sig) {
      table.setAttribute("data-sig", sig);
      table.textContent = "";
      if (rows.length) ["prompt", "read", "out", "tok/s", "time"].forEach(function (x) { table.appendChild(cmpEl("span", "h", x)); });
      rows.forEach(function (q) {
        [cmpFmt(q.prompt), cmpFmt(q.read), cmpFmt(q.out), q.tok_s != null ? q.tok_s.toFixed(1) : "–", q.secs != null ? cmpTook(q.secs * 1000) : "–"]
          .forEach(function (x) { table.appendChild(cmpEl("span", "", x)); });
      });
    }
  }


  function wtToolsUrl() {   // code-live's /tools for the builder's session (the stage's, until a builder window spoke)
    var src = String(wt.builderSrc || wt.src || ""), base = src.split("#")[0], i = base.indexOf("?");
    if (i < 0 || !/\/w\/[^/]+\/[0-9a-f]{32}\/$/.test(base.slice(0, i))) return "";
    return base.slice(0, i) + "tools" + base.slice(i);
  }

  async function wtToolsFetch() {
    var url = wtToolsUrl();
    if (wt.toolsBusy || !url) return;
    wt.toolsBusy = true;
    wt.toolsAt = Date.now();
    try {
      var r = await _fetch(url, { cache: "no-store" });
      if (r.ok) wt.tools = await r.json();
    } catch (e) {
      /* keep what is shown */
    } finally {
      wt.toolsBusy = false;
      wt.toolsTried = true;
    }
    if (wt.on && wt.tab === "tools") {
      try { wtToolsRender(); } catch (e) { warnOnce("watch-tools", e); }
    }
  }

  function wtToolBars(box, pairs, mcpNames, limit) {
    var top = pairs.slice(0, limit), max = top.length ? top[0][1] : 1, total = pairs.reduce(function (a, p) { return a + p[1]; }, 0);
    var sig = JSON.stringify(top);
    if (box.getAttribute("data-sig") === sig) return;
    box.setAttribute("data-sig", sig);
    box.textContent = "";
    if (!top.length) { box.appendChild(cmpEl("div", "wt-small", "No tool calls yet.")); return; }
    top.forEach(function (p) {
      var mcp = mcpNames.some(function (m) { return m && (p[0].indexOf(m + "_") === 0 || p[0].indexOf(m + ":") === 0); });
      var row = cmpEl("div", "wt-tbar" + (mcp ? " mcp" : ""));
      row.appendChild(cmpEl("span", "n", p[0]));
      var bar = cmpEl("span", "wt-bar");
      var b = cmpEl("b");
      b.style.width = Math.max(2, p[1] / max * 100) + "%";
      bar.appendChild(b);
      row.appendChild(bar);
      row.appendChild(cmpEl("span", "c", cmpFmt(p[1])));
      row.appendChild(cmpEl("span", "s", total ? Math.round(p[1] / total * 100) + "%" : ""));
      box.appendChild(row);
    });
    if (pairs.length > top.length) {
      var rest = pairs.slice(limit).reduce(function (a, p) { return a + p[1]; }, 0);
      box.appendChild(cmpEl("div", "wt-small", (pairs.length - limit) + " more tools · " + cmpFmt(rest) + " calls"));
    }
  }

  function wtToolsRender() {   // most used tools this turn and over the chat, the MCP servers, the review send-backs
    if (!wt.el) return;
    var el = wt.el, doc = wt.tools, st = wt.st || {};
    var mcpNames = doc && doc.mcp ? doc.mcp.map(function (m) { return m.name; }) : [];
    var turn = Object.keys(st.tools || {}).map(function (k) { return [k, st.tools[k]]; }).sort(function (a, b) { return b[1] - a[1] || (a[0] < b[0] ? -1 : 1); });
    var tcalls = turn.reduce(function (a, p) { return a + p[1]; }, 0);
    setText(el.querySelector(".wt-tturn"), tcalls + " call" + (tcalls === 1 ? "" : "s") + (st.steps ? " · " + st.steps + " step" + (st.steps === 1 ? "" : "s") : ""));
    wtToolBars(el.querySelector(".wt-turnbars"), turn, mcpNames, 6);
    setText(el.querySelector(".wt-tall"), doc ? cmpFmt(doc.calls) + " calls · " + cmpFmt(doc.steps) + " steps" : wt.toolsTried ? "code-live did not answer" : "counting…");
    wtToolBars(el.querySelector(".wt-allbars"), doc ? doc.tools : [], mcpNames, 8);
    var mbox = el.querySelector(".wt-mcps"), msig = JSON.stringify(doc ? doc.mcp : null);
    if (mbox.getAttribute("data-sig") !== msig) {
      mbox.setAttribute("data-sig", msig);
      mbox.textContent = "";
      if (!doc || !doc.mcp.length) mbox.appendChild(cmpEl("div", "wt-small", doc ? "opencode has no MCP servers." : "…"));
      (doc ? doc.mcp : []).forEach(function (m) {
        var row = cmpEl("div", "wt-mcp");
        row.appendChild(cmpEl("i", m.status === "connected" ? "on" : ""));
        row.appendChild(cmpEl("span", "n", m.name));
        row.appendChild(cmpEl("span", "st", m.status || "unknown"));
        row.appendChild(cmpEl("span", "k" + (m.calls ? "" : " none"), m.calls ? cmpFmt(m.calls) + " call" + (m.calls === 1 ? "" : "s") : "never used"));
        mbox.appendChild(row);
      });
    }
    setText(el.querySelector(".wt-tsb"), doc ? doc.sendbacks + (doc.sendbacks === 1 ? " time" : " times") : "");
    var kbox = el.querySelector(".wt-kinds"), ksig = JSON.stringify(doc ? doc.kinds : null);
    if (kbox.getAttribute("data-sig") !== ksig) {
      kbox.setAttribute("data-sig", ksig);
      kbox.textContent = "";
      if (doc && !doc.kinds.length) kbox.appendChild(cmpEl("div", "wt-small", "The reviewer has not sent anything back in this chat."));
      (doc ? doc.kinds : []).forEach(function (k) {
        var chip = cmpEl("span", "wt-kind", k[0]);
        chip.appendChild(cmpEl("b", "", String(k[1])));
        kbox.appendChild(chip);
      });
    }
  }

  function wtBars(box, bars) {
    if (box.children.length !== bars.length) {
      box.textContent = "";
      bars.forEach(function () { box.appendChild(cmpEl("i")); });
    }
    bars.forEach(function (b, i) {
      var n = box.children[i];
      n.style.height = Math.round(b.h) + "%";
      n.title = b.t;
      n.classList.toggle("big", !!b.big);
    });
  }

  // ------------------------------------------------------------------- context ring
  // A small ring before the send button: how full the chat's context is. Chat: the auto_compact filter sends
  // {used, limit, compact_at} (an estimate) and the reply's own usage gives the exact count. Code: the Code pipe
  // sends opencode's session size after every step. Both send a live "slerm:context" event (Open WebUI forwards
  // it over its socket, saves nothing) and save one hidden "slerm_context" status with the reply, which is what a
  // reload reads. While a chat is compacted the ring spins; when it is done it squeezes, sparks and drains to the
  // new size. The dot on the ring is where compaction starts.
  var CTX_ID = "ctx-ring";
  var ctx = { chat: undefined, used: null, limit: 0, at: 0, source: "", est: false, phase: "", seq: 0, timer: 0, liveAt: 0 };
  var modelCtx = {}; // base model name -> its context (from /api/models slerm.ctx)

  function ctxK(n) {
    if (n == null) return "?";
    return n >= 100000 ? Math.round(n / 1000) + "k" : n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(n);
  }

  function usageTotal(u) {
    if (!u || typeof u !== "object") return 0;
    if (u.total_tokens) return u.total_tokens;
    if (u.prompt_tokens || u.completion_tokens) return (u.prompt_tokens || 0) + (u.completion_tokens || 0);
    if (u.prompt_n || u.predicted_n) return (u.cache_n || 0) + (u.prompt_n || 0) + (u.predicted_n || 0);
    return 0;
  }

  // Open WebUI's socket (engine.io sets ws.onmessage): watch it for this page's chat events, without touching them.
  (function tapSocket() {
    try {
      var proto = window.WebSocket && window.WebSocket.prototype;
      var d = proto && Object.getOwnPropertyDescriptor(proto, "onmessage");
      if (!d || !d.set || proto.__ctxTap) return;
      Object.defineProperty(proto, "onmessage", {
        configurable: true,
        enumerable: d.enumerable,
        get: d.get,
        set: function (fn) {
          if (typeof fn !== "function") return d.set.call(this, fn);
          return d.set.call(this, function (ev) {
            try { ctxOnSocket(ev); } catch (e) { warnOnce("ctx-socket", e); }
            return fn.apply(this, arguments);
          });
        },
      });
      proto.__ctxTap = true;
    } catch (e) {
      warnOnce("ctx-tap", e);
    }
  })();

  function ctxOnSocket(ev) {
    var s = ev && ev.data;
    // socket.io event frames are "42[...]"; only parse the ones that can matter here.
    if (typeof s !== "string" || s.charCodeAt(0) !== 52 || s.charCodeAt(1) !== 50) return;
    if (s.indexOf("slerm:context") < 0 && s.indexOf('"usage"') < 0) return;
    var i = s.indexOf("[");
    if (i < 0) return;
    var arr = JSON.parse(s.slice(i));
    if (!Array.isArray(arr) || (arr[0] !== "events" && arr[0] !== "chat-events")) return;
    var p = arr[1] || {};
    if (!p.chat_id || p.chat_id !== currentChatId) return;
    var e = p.data || {};
    if (e.type === "slerm:context") {
      ctxApply(e.data || {});
    } else if (e.type === "chat:completion" && e.data && e.data.usage && ctx.source !== "code") {
      var u = usageTotal(e.data.usage);
      if (u) ctxApply({ used: u, estimate: false });
    }
  }

  function ctxApply(info) {
    var phase = info.phase || "";
    ctx.liveAt = Date.now();
    if (info.limit) ctx.limit = info.limit;
    if (info.compact_at) ctx.at = info.compact_at;
    if (info.source) ctx.source = info.source;
    if ("used" in info) ctx.used = info.used;
    ctx.est = !!info.estimate;
    var el = document.getElementById(CTX_ID);
    if (phase === "compacting") {
      ctx.phase = "compacting";
    } else if (phase === "compacted") {
      ctx.phase = "";
      ctx.burst = { before: info.before || null, after: info.used };   // the compaction overlay's numbers too
      ctxBurst(el, info.before, info.used);
    } else if (phase === "compact_failed" || phase === "step") {
      ctx.phase = "";
    }
    ctxRender(el);
  }

  function ctxBurst(el, before, after) {
    if (!el) return;
    var label = el.querySelector(".cr-float");
    if (label) label.textContent = before ? "−" + ctxK(Math.max(0, before - (after || 0))) : "compacted";
    el.classList.remove("is-compacted");
    void el.offsetWidth; // restart the animation
    el.classList.add("is-compacted");
    clearTimeout(ctx.burstTimer);
    ctx.burstTimer = setTimeout(function () { el.classList.remove("is-compacted"); }, 2000);
  }

  function ctxRender(el) {
    el = el || document.getElementById(CTX_ID);
    if (!el) return;
    var limit = ctx.limit || 0;
    var used = ctx.used;
    var pct = limit && used != null ? Math.min(100, used / limit * 100) : 0;
    var arc = el.querySelector(".cr-arc");
    var off = String(Math.round((100 - pct) * 10) / 10);
    if (arc && arc.getAttribute("stroke-dashoffset") !== off) arc.setAttribute("stroke-dashoffset", off);
    var near = ctx.at && used != null && used >= ctx.at * 0.85;
    var over = ctx.at && used != null && used >= ctx.at;
    var tone = over ? "over" : near ? "near" : "ok";
    if (el.getAttribute("data-tone") !== tone) el.setAttribute("data-tone", tone);
    var busy = ctx.phase === "compacting";
    if (el.classList.contains("is-compacting") !== busy) el.classList.toggle("is-compacting", busy);
    var num = el.querySelector(".cr-num");
    var txt = used == null ? (busy ? "" : "–") : String(Math.round(pct));
    if (num && num.textContent !== txt) num.textContent = txt;
    var mark = el.querySelector(".cr-mark");
    if (mark) {
      var on = !!(ctx.at && limit);
      var a = on ? ctx.at / limit * 2 * Math.PI : 0;
      var cx = String(Math.round((18 + 15 * Math.sin(a)) * 100) / 100), cy = String(Math.round((18 - 15 * Math.cos(a)) * 100) / 100);
      if (mark.getAttribute("cx") !== cx) mark.setAttribute("cx", cx);
      if (mark.getAttribute("cy") !== cy) mark.setAttribute("cy", cy);
      var vis = on ? "visible" : "hidden";
      if (mark.getAttribute("visibility") !== vis) mark.setAttribute("visibility", vis);
    }
    var who = ctx.source === "code" ? "Code session" : "Context";
    var lines = [busy ? "Compacting…" :
      who + ": " + (used == null ? "updating…" : (ctx.est ? "~" : "") + ctxK(used)) + " of " + ctxK(limit) + " tokens" +
      (used == null ? "" : " (" + Math.round(pct) + "%)")];
    if (ctx.at && limit) {
      lines.push((ctx.source === "code" ? "opencode compacts" : "Compacts") + " at " + ctxK(ctx.at) +
                 " (" + Math.round(ctx.at / limit * 100) + "%)");
    }
    var title = lines.join("\n");
    if (el.title !== title) el.title = title;
    if (el.getAttribute("aria-label") !== title) el.setAttribute("aria-label", title);
  }

  function ctxSvg(tag, attrs) {
    var n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attrs).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }

  function ctxCreate() {
    var el = document.createElement("div");
    el.id = CTX_ID;
    el.setAttribute("role", "img");
    var svg = ctxSvg("svg", { viewBox: "0 0 36 36", "aria-hidden": "true" });
    svg.appendChild(ctxSvg("circle", { "class": "cr-track", cx: "18", cy: "18", r: "15" }));
    svg.appendChild(ctxSvg("circle", { "class": "cr-arc", cx: "18", cy: "18", r: "15", pathLength: "100",
                                       "stroke-dasharray": "100", "stroke-dashoffset": "100", transform: "rotate(-90 18 18)" }));
    svg.appendChild(ctxSvg("circle", { "class": "cr-comet", cx: "18", cy: "18", r: "15", pathLength: "100",
                                       "stroke-dasharray": "24 76" }));
    svg.appendChild(ctxSvg("circle", { "class": "cr-mark", cx: "18", cy: "3", r: "2.1", visibility: "hidden" }));
    el.appendChild(svg);
    var num = document.createElement("span");
    num.className = "cr-num";
    el.appendChild(num);
    for (var i = 0; i < 8; i++) {
      var sp = document.createElement("span");
      sp.className = "cr-spark";
      sp.style.setProperty("--a", (i * 45 + 22) + "deg");
      el.appendChild(sp);
    }
    var fl = document.createElement("span");
    fl.className = "cr-float";
    el.appendChild(fl);
    return el;
  }

  function ctxInjectStyle() {
    if (document.getElementById("ctx-ring-style")) return;
    var css = [
      "#ctx-ring{position:relative;flex:none;align-self:center;width:28px;height:28px;margin:0 4px 0 2px;display:inline-flex;align-items:center;justify-content:center;color:#9ca3af;--cr:#4f8cff;--cr-glow:#8b5cf6}",
      "#ctx-ring[data-tone=near]{--cr:#f59e0b}",
      "#ctx-ring[data-tone=over]{--cr:#ef4444}",
      "#ctx-ring svg{width:26px;height:26px;overflow:visible;transition:transform .2s}",
      "#ctx-ring .cr-track{fill:none;stroke:currentColor;stroke-width:3;opacity:.25}",
      "#ctx-ring .cr-arc{fill:none;stroke:var(--cr);stroke-width:3;stroke-linecap:round;transition:stroke-dashoffset 1.1s cubic-bezier(.22,1,.36,1),stroke .4s,opacity .3s}",
      "#ctx-ring .cr-comet{fill:none;stroke:var(--cr-glow);stroke-width:3;stroke-linecap:round;opacity:0;transform-box:fill-box;transform-origin:center;transition:opacity .3s}",
      "#ctx-ring .cr-mark{fill:currentColor;opacity:.8;stroke:#fff;stroke-width:1}",
      "html.dark #ctx-ring .cr-mark{stroke:#171717}",
      "#ctx-ring .cr-num{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font:650 8.5px/1 system-ui,-apple-system,\"Segoe UI\",Roboto,sans-serif;letter-spacing:-.03em;color:#4b5563;font-variant-numeric:tabular-nums;pointer-events:none;transition:opacity .3s}",
      "html.dark #ctx-ring .cr-num{color:#d1d5db}",
      "#ctx-ring.is-compacting .cr-arc{opacity:.2}",
      "#ctx-ring.is-compacting .cr-comet{opacity:1;animation:cr-spin .9s linear infinite}",
      "#ctx-ring.is-compacting svg{animation:cr-glow 1.4s ease-in-out infinite}",
      "#ctx-ring.is-compacting .cr-num{opacity:0}",
      "#ctx-ring.is-compacted svg{animation:cr-squeeze .75s cubic-bezier(.34,1.56,.64,1)}",
      "#ctx-ring .cr-spark{position:absolute;left:50%;top:50%;width:4px;height:4px;margin:-2px 0 0 -2px;border-radius:50%;background:var(--cr-glow);opacity:0;pointer-events:none}",
      "#ctx-ring .cr-spark:nth-of-type(even){background:var(--cr);width:3px;height:3px;margin:-1.5px 0 0 -1.5px}",
      "#ctx-ring.is-compacted .cr-spark{animation:cr-spark .85s cubic-bezier(.2,.8,.3,1) .12s forwards}",
      "#ctx-ring .cr-float{position:absolute;bottom:100%;left:50%;font:700 10px/1 system-ui,-apple-system,sans-serif;color:var(--cr-glow);white-space:nowrap;opacity:0;pointer-events:none;transform:translate(-50%,4px)}",
      "#ctx-ring.is-compacted .cr-float{animation:cr-float 1.9s ease-out forwards}",
      "@keyframes cr-spin{to{transform:rotate(360deg)}}",
      "@keyframes cr-glow{0%,100%{filter:drop-shadow(0 0 1px rgba(139,92,246,.35))}50%{filter:drop-shadow(0 0 5px rgba(139,92,246,.95))}}",
      "@keyframes cr-squeeze{0%{transform:scale(1)}30%{transform:scale(.68)}100%{transform:scale(1)}}",
      "@keyframes cr-spark{0%{opacity:1;transform:rotate(var(--a)) translateY(-6px) scale(1)}100%{opacity:0;transform:rotate(var(--a)) translateY(-20px) scale(.2)}}",
      "@keyframes cr-float{0%{opacity:0;transform:translate(-50%,4px)}15%,70%{opacity:1;transform:translate(-50%,-2px)}100%{opacity:0;transform:translate(-50%,-12px)}}",
      "@media (prefers-reduced-motion:reduce){#ctx-ring *,#ctx-ring svg{animation:none!important;transition:none!important}#ctx-ring.is-compacting .cr-comet{opacity:1}}",
    ].join("\n");
    var st = document.createElement("style");
    st.id = "ctx-ring-style";
    st.textContent = css;
    (document.head || document.documentElement).appendChild(st);
  }

  // The latest numbers saved with this chat's replies: the newest reply on the current branch, its usage for
  // the exact count (Chat), and the newest hidden "slerm_context" status for the limit and compaction point.
  function ctxLoad(chatId) {
    var seq = ++ctx.seq;
    var started = Date.now();
    var token = safeGet("token");
    _fetch("/api/v1/chats/" + encodeURIComponent(chatId), { headers: token ? { Authorization: "Bearer " + token } : {} })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (c) {
        if (!c || seq !== ctx.seq || chatId !== currentChatId) return;
        var h = (c.chat && c.chat.history) || {};
        var msgs = h.messages || {};
        var cur = h.currentId, guard = 0, latest = null, info = null;
        while (cur && msgs[cur] && guard++ < 5000) {
          var m = msgs[cur];
          if (m.role === "assistant") {
            if (!latest) latest = m;
            var st = (m.statusHistory || []).filter(function (s) { return s && s.action === "slerm_context"; });
            if (st.length) { info = st[st.length - 1]; break; }
          }
          cur = m.parentId;
        }
        if (!latest) return;
        var fresh = !!(info && latest.statusHistory && latest.statusHistory.indexOf(info) >= 0);
        var exact = usageTotal(latest.usage);
        var liveSince = ctx.liveAt > started; // a live event came in meanwhile: its numbers are newer
        if (info && (!liveSince || !ctx.limit)) {
          ctx.limit = info.limit || ctx.limit;
          ctx.at = info.compact_at || 0;
          ctx.source = info.source || "";
        }
        if (!liveSince) {
          if (exact && ctx.source !== "code") {
            ctx.used = exact;
            ctx.est = false;
          } else if (fresh) {
            ctx.used = info.used;
            ctx.est = !!info.estimate;
          } else if (ctx.used == null && info) { // the newest reply saved no numbers (yet): the last known ones
            ctx.used = info.used;
            ctx.est = true;
          }
        }
        if (!ctx.limit) ctx.limit = modelCtx[pickedModel()] || 0;
        scheduleReconcile();
      })
      .catch(function (e) { warnOnce("ctx-load", e); });
  }

  function ctxSoon(ms) {
    clearTimeout(ctx.timer);
    ctx.timer = setTimeout(function () { if (currentChatId) ctxLoad(currentChatId); }, ms);
  }

  function ctxRightGroup() {
    var menu = document.getElementById("input-menu-button");
    var row = menu && menu.closest(".justify-between");
    var right = row && row.lastElementChild;
    return right && right !== row.firstElementChild && !right.contains(menu) ? right : null;
  }

  function reconcileContext() {
    var el = document.getElementById(CTX_ID);
    if (ctx.chat !== currentChatId) { // another chat: start from what it saved
      ctx.chat = currentChatId;
      ctx.used = currentChatId ? null : 0;
      ctx.limit = 0;
      ctx.at = 0;
      ctx.source = "";
      ctx.est = false;
      ctx.phase = "";
      if (currentChatId) ctxLoad(currentChatId);
    }
    if (!currentChatId) { // a new chat follows the picked model (Chat; Code's limit is opencode's, known after a turn)
      var lim = state.mode === "chat" ? modelCtx[pickedModel()] || 0 : 0;
      if (ctx.limit !== lim) {
        ctx.limit = lim;
        ctx.at = Math.floor(lim * 0.75); // the filter's default trigger
      }
    }
    var group = ctxRightGroup();
    if (!group || !ctx.limit) {
      if (el) el.remove();
      return;
    }
    ctxInjectStyle();
    if (!el) el = ctxCreate();
    if (el.parentNode !== group || group.firstElementChild !== el) group.insertBefore(el, group.firstChild);
    ctxRender(el);
  }

  // ------------------------------------------------------------------- GitHub button
  // A button in the message bar (after the thinking pill) and its panel: sign in to GitHub with a one-time code,
  // list the repos, create one, switch one between private and public. The Code pipe does all the GitHub work and
  // keeps the sign-in token (code_mode.py, "GitHub button"); this side only draws. Each call is a POST to
  // /api/chat/completions with code_github = {op, ...} and NO chat id, which Open WebUI answers inline with the
  // pipe's dict. Everything is built with createElement / textContent: repo names and descriptions are other
  // people's text. Controls carry data-gh="..." names, which tests/page/test_github_panel.cjs clicks through.
  var GH_PILL_ID = "github-pill";
  var GH_PANEL_ID = "github-panel";
  var GH_CHIP_ID = "github-repo-chip";
  var GH_MARK = "M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12";
  var GH_CSS = [
    "#github-pill{display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box;width:28px;height:28px;margin-left:6px;padding:0;border-radius:999px;background:#f3f4f6;border:1px solid #e5e7eb;color:#6b7280;cursor:pointer;flex:none;transition:color .12s ease}",
    "html.dark #github-pill{background:#262626;border-color:#333;color:#9ca3af}",
    "#github-pill:hover,#github-pill[aria-expanded=\"true\"]{color:#111}",
    "html.dark #github-pill:hover,html.dark #github-pill[aria-expanded=\"true\"]{color:#fff}",
    "#github-pill:focus-visible{outline:2px solid #4f8cff;outline-offset:1px}",
    "#github-panel{position:fixed;z-index:60;display:flex;flex-direction:column;gap:10px;box-sizing:border-box;padding:12px;border-radius:14px;border:1px solid #e5e7eb;background:#fff;color:#111;box-shadow:0 10px 30px rgba(0,0,0,.14);font-family:inherit;font-size:13px;line-height:1.45;overflow:hidden}",
    "html.dark #github-panel{background:#1f1f1f;border-color:#333;color:#e5e5e5;box-shadow:0 10px 30px rgba(0,0,0,.6)}",
    "#github-panel[hidden],#github-panel [hidden]{display:none}",
    "#github-panel .gh-title{flex:none;font-weight:600}",
    "#github-panel .gh-dim{color:#6b7280}",
    "html.dark #github-panel .gh-dim{color:#9ca3af}",
    "#github-panel ol{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:2px;list-style:decimal}",
    "#github-panel a{color:inherit;text-decoration:underline;text-underline-offset:2px}",
    "#github-panel input[type=text]{flex:none;min-width:0;width:100%;box-sizing:border-box;height:32px;padding:0 10px;border-radius:8px;border:1px solid #e5e7eb;background:transparent;color:inherit;font:inherit;outline:none}",
    "html.dark #github-panel input[type=text]{border-color:#333}",
    "#github-panel input[type=text]:focus{border-color:#4f8cff}",
    "#github-panel .gh-btn{appearance:none;-webkit-appearance:none;flex:none;height:32px;padding:0 12px;border-radius:8px;border:0;background:#111;color:#fff;font:inherit;font-weight:600;line-height:1;cursor:pointer;white-space:nowrap}",
    "html.dark #github-panel .gh-btn{background:#fff;color:#111}",
    "#github-panel .gh-btn:disabled{opacity:.5;cursor:default}",
    "#github-panel .gh-link{appearance:none;-webkit-appearance:none;border:0;background:none;padding:0;color:#6b7280;font:inherit;cursor:pointer;text-decoration:underline;text-underline-offset:2px}",
    "html.dark #github-panel .gh-link{color:#9ca3af}",
    "#github-panel .gh-link:hover{color:#111}",
    "html.dark #github-panel .gh-link:hover{color:#fff}",
    "#github-panel .gh-line{display:flex;align-items:center;gap:8px;flex:none}",
    "#github-panel .gh-line input[type=text]{flex:1 1 auto;width:auto}",
    "#github-panel .gh-col{display:flex;flex-direction:column;gap:8px;flex:none}",
    "#github-panel .gh-code{font:600 22px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:3px;text-align:center;padding:8px 0;user-select:all;-webkit-user-select:all}",
    "#github-panel .gh-list{display:flex;flex-direction:column;flex:1 1 auto;min-height:0;overflow-y:auto;margin:0 -6px}",
    "#github-panel .gh-row{display:flex;align-items:center;gap:8px;flex:none;min-height:34px;padding:0 6px;border-radius:8px}",
    "#github-panel .gh-row:hover{background:#f3f4f6}",
    "html.dark #github-panel .gh-row:hover{background:#222}",
    "#github-panel .gh-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    "#github-panel .gh-owner,#github-panel .gh-time{color:#9ca3af}",
    "#github-panel .gh-time{flex:none;font-size:12px}",
    "#github-panel .gh-vis{appearance:none;-webkit-appearance:none;flex:none;height:22px;padding:0 8px;border-radius:999px;border:1px solid #e5e7eb;background:none;color:#6b7280;font:inherit;font-size:11px;font-weight:600;line-height:1;cursor:pointer}",
    "html.dark #github-panel .gh-vis{border-color:#333;color:#9ca3af}",
    "#github-panel .gh-vis[aria-pressed=\"false\"]{border-color:rgba(217,119,6,.5);color:#d97706}",
    "html.dark #github-panel .gh-vis[aria-pressed=\"false\"]{border-color:rgba(245,158,11,.5);color:#f59e0b}",
    "#github-panel .gh-vis:disabled{opacity:.5;cursor:default}",
    "#github-panel .gh-empty{padding:10px 6px;color:#9ca3af}",
    "#github-panel .gh-sep{flex:none;height:1px;margin:0 -12px;background:#e5e7eb}",
    "html.dark #github-panel .gh-sep{background:#2a2a2a}",
    "#github-panel .gh-check{display:inline-flex;align-items:center;gap:6px;flex:none;cursor:pointer}",
    "#github-panel .gh-error{flex:none;color:#dc2626;font-size:12px}",
    "html.dark #github-panel .gh-error{color:#f87171}",
    "#github-panel .gh-chat{display:flex;flex-direction:column;gap:6px;flex:none}",
    "#github-panel .gh-chat-text{overflow-wrap:anywhere}",
    "#github-panel .gh-spark{appearance:none;-webkit-appearance:none;display:inline-flex;align-items:center;justify-content:center;flex:none;width:32px;height:32px;padding:0;border-radius:8px;border:1px solid #e5e7eb;background:none;color:#6b7280;cursor:pointer}",
    "html.dark #github-panel .gh-spark{border-color:#333;color:#9ca3af}",
    "#github-panel .gh-spark:hover{color:#111}",
    "html.dark #github-panel .gh-spark:hover{color:#fff}",
    "#github-panel .gh-spark:disabled{cursor:default}",
    "#github-panel .gh-spark.gh-busy svg{animation:gh-pulse 1s ease-in-out infinite}",
    "@keyframes gh-pulse{0%,100%{opacity:.25}50%{opacity:1}}",
    "#github-panel .gh-head{display:flex;align-items:center;justify-content:space-between;gap:8px;flex:none}",
    "#github-panel .gh-info{flex:none;font-size:12px;color:#6b7280}",
    "html.dark #github-panel .gh-info{color:#9ca3af}",
    "#github-panel .gh-files{display:flex;flex-direction:column;flex:0 1 auto;min-height:20px;max-height:112px;overflow-y:auto;font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;color:#6b7280}",
    "html.dark #github-panel .gh-files{color:#9ca3af}",
    "#github-panel .gh-file{display:flex;gap:8px;flex:none;white-space:nowrap}",
    "#github-panel .gh-file span:first-child{flex:none;width:18px;color:#9ca3af}",
    "#github-panel .gh-file span:last-child{overflow:hidden;text-overflow:ellipsis}",
    "#github-panel select{flex:1;min-width:0;box-sizing:border-box;height:32px;padding:0 8px;border-radius:8px;border:1px solid #e5e7eb;background:#fff;color:inherit;font:inherit;outline:none}",
    "html.dark #github-panel select{border-color:#333;background:#1f1f1f}",
    "html.dark #github-panel select option{background:#1f1f1f;color:#e5e5e5}",
    "#github-panel select:focus{border-color:#4f8cff}",
    "#github-panel .gh-commit{display:flex;align-items:center;gap:8px;flex:none;min-height:40px;padding:2px 6px;border-radius:8px}",
    "#github-panel .gh-commit:hover{background:#f3f4f6}",
    "html.dark #github-panel .gh-commit:hover{background:#222}",
    "#github-panel .gh-commit-text{display:flex;flex-direction:column;flex:1;min-width:0}",
    "#github-panel .gh-finding-text{flex:1;min-width:0;overflow-wrap:anywhere}",
    "#github-panel .gh-commit-subject{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    "#github-panel .gh-commit-meta{font-size:11px;color:#9ca3af;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    "#github-panel .gh-work{flex:none;font-size:12px}",
    "#github-repo-chip{display:inline-flex;align-items:center;box-sizing:border-box;height:28px;max-width:200px;margin-left:6px;padding:0 4px 0 10px;border-radius:999px;border:1px solid #e5e7eb;background:#f3f4f6;color:#374151;font:600 12px/1 system-ui,-apple-system,'Segoe UI',sans-serif;flex:none}",
    "html.dark #github-repo-chip{background:#262626;border-color:#333;color:#d1d5db}",
    "#github-repo-chip .gh-chip-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-right:6px}",
    "#github-repo-chip .gh-chip-x{appearance:none;-webkit-appearance:none;flex:none;width:20px;height:20px;padding:0;border:0;border-radius:999px;background:none;color:#9ca3af;font:400 16px/1 system-ui,sans-serif;cursor:pointer}",
    "#github-repo-chip .gh-chip-x:hover{color:#111}",
    "html.dark #github-repo-chip .gh-chip-x:hover{color:#fff}",
    "#github-panel .gh-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;flex:none;font-size:12px;color:#6b7280}",
    "html.dark #github-panel .gh-foot{color:#9ca3af}"
  ].join("");

  var gh = {
    open: false,
    view: "loading", // loading | setup | signin | code | repos | blocked
    login: "",
    repos: [],
    loading: false,  // the repo list is being fetched
    filter: "",
    error: "",
    code: "", uri: "", interval: 5, // a sign-in under way: the one-time code, where to enter it, seconds between polls
    signingIn: false, pollTimer: 0,
    asking: "",      // the repo whose "Make public?" question is showing
    chat: null,      // the repo the open chat works on: {linked, repo, branch, changed, ahead, project} (chat_status)
    info: "",        // a plain line under the view (what a push or a roll back did)
    push: null,      // the push view: {repo, files, more, ahead, branches, def, current, done}
    history: null,   // the versions view: {commits, branch, asking}
    diag: null,      // the diagnose view: {chatId, loading, data} (what Diagnose found, from the pipe)
    wired: false
  };

  /** One call to the pipe: resolves to its {ok, ...} dict, or {ok: false, error} when the request itself failed. */
  function ghCall(args) {
    var token = safeGet("token");
    return _fetch("/api/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + (token || "") },
      body: JSON.stringify({ model: CODE_MODEL, stream: false, messages: [{ role: "user", content: "github" }], code_github: args })
    }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (d) {
      return d && typeof d === "object" && typeof d.ok === "boolean" ? d : { ok: false, error: "GitHub request failed" };
    }).catch(function () {
      return { ok: false, error: "GitHub request failed" };
    });
  }

  /** createElement with a few shorthands: text, cls (class), gh (data-gh), click; other keys are attributes. */
  function ghEl(tag, props, kids) {
    var n = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      var v = props[k];
      if (v == null || v === false) return;
      if (k === "text") n.textContent = v;
      else if (k === "cls") n.className = v;
      else if (k === "gh") n.setAttribute("data-gh", v);
      else if (k === "click") n.addEventListener("click", v);
      else n.setAttribute(k, v === true ? "" : v);
    });
    (kids || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  function ghMark(size) {
    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", size);
    svg.setAttribute("height", size);
    svg.setAttribute("fill", "currentColor");
    svg.setAttribute("aria-hidden", "true");
    var path = document.createElementNS(NS, "path");
    path.setAttribute("d", GH_MARK);
    svg.appendChild(path);
    return svg;
  }

  function ghAgo(ts) {
    var t = ts ? new Date(ts).getTime() : NaN;
    if (isNaN(t)) return "";
    var s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 3600) return Math.max(1, Math.floor(s / 60)) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    if (s < 86400 * 60) return Math.floor(s / 86400) + "d ago";
    if (s < 86400 * 365) return Math.floor(s / (86400 * 30)) + "mo ago";
    return Math.floor(s / (86400 * 365)) + "y ago";
  }

  /** Copy text; Open WebUI is usually served over plain http, where navigator.clipboard does not exist. */
  function ghCopy(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(function () {});
        return;
      }
      var ta = ghEl("textarea", { style: "position:fixed;left:-9999px;top:0;opacity:0" });
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      if (document.execCommand) document.execCommand("copy");
      ta.remove();
    } catch (e) {
      warnOnce("github-copy", e);
    }
  }

  function ghInjectStyle() {
    if (document.getElementById("github-style")) return;
    var s = document.createElement("style");
    s.id = "github-style";
    s.textContent = GH_CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  /** Keep the button in the message bar, right after the thinking pill when there is one. Runs on every reconcile,
   *  so it moves the button only when it is in the wrong place (a move is a DOM change, which reconciles again). */
  function reconcileGithub() {
    var group = effortGroup();
    var pill = document.getElementById(GH_PILL_ID);
    if (!group) { // no message bar on this page
      if (gh.open) ghClose();
      if (pill) pill.remove();
      return;
    }
    if (!pill) {
      ghInjectStyle();
      pill = ghEl("button", { id: GH_PILL_ID, type: "button", title: "GitHub", "aria-label": "GitHub", "aria-haspopup": "dialog", "aria-expanded": "false" }, [ghMark(15)]);
      pill.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        if (gh.open) ghClose();
        else ghOpen();
      });
    }
    var effort = document.getElementById(EFFORT_ID);
    if (effort && effort.parentNode === group) {
      if (effort.nextElementSibling !== pill) group.insertBefore(pill, effort.nextSibling);
    } else if (pill.parentNode !== group) {
      group.appendChild(pill);
    }
    ghReconcileChip(group, pill);
    if (gh.open) ghPlace();
  }

  /** The repo this chat works on, or (on a new chat) the one picked with "Work on it", which the first message
   *  starts work on; shown as a chip after the button. The picked one has an × to drop it. */
  function ghReconcileChip(group, pill) {
    var pending = !currentChatId;
    var repo = (pending ? (state.pendingFeature ? "" : safeGet(KEY_NEW_REPO)) : safeGet(KEY_REPO_PREFIX + currentChatId)) || "";
    var chip = document.getElementById(GH_CHIP_ID);
    if (!repo) {
      if (chip) chip.remove();
      return;
    }
    var key = repo + (pending ? "|new" : "");
    if (!chip || chip.getAttribute("data-key") !== key) { // rebuilt only when it changes: reconcile runs on every DOM change
      if (chip) chip.remove();
      chip = ghEl("span", {
        id: GH_CHIP_ID, "data-key": key,
        title: pending ? "Your first message starts work on this GitHub repository" : "This chat works on this GitHub repository"
      }, [
        ghEl("span", { cls: "gh-chip-name", text: repo }),
        pending ? ghEl("button", {
          type: "button", cls: "gh-chip-x", gh: "drop-repo", "aria-label": "Don't work on " + repo, text: "×",
          click: function (e) { e.preventDefault(); e.stopPropagation(); ghDropPendingRepo(); }
        }) : null
      ]);
    }
    if (pill.nextElementSibling !== chip) group.insertBefore(chip, pill.nextSibling);
  }

  function ghChatChanged() { // another chat: what the panel knew about the last one no longer applies
    gh.chat = null;
    gh.push = null;
    gh.history = null;
    gh.diag = null;
    if (gh.open) ghClose();
  }

  function ghDropPendingRepo() {
    try { localStorage.removeItem(KEY_NEW_REPO); } catch (e) { /* storage unavailable: nothing was kept */ }
    scheduleReconcile();
  }

  /** "Work on it". In a chat that exists, the pipe clones the repo into the chat's project right now and the chat
   *  switches to Code. On a new chat there is no chat to clone into yet: the pick waits as a chip (with an ×) and the
   *  first message makes the pipe clone it (ghAdoptRepo). Either way the user stays where they are. */
  function ghWorkOn(fullName, button) {
    var chatId = currentChatId;
    if (!chatId) {
      safeSet(KEY_NEW_REPO, fullName);
      ghClose();
      setPillMode("code");
      scheduleReconcile();
      ghFocusBox();
      return;
    }
    if (featureOf(chatId)) return ghError("This is a feature chat: it works on Code mode itself. Pick the repo in another chat.");
    button.disabled = true;
    setText(button, "Cloning…");
    ghError("");
    ghCall({ op: "attach", chat_id: chatId, full_name: fullName }).then(function (res) {
      button.disabled = false;
      setText(button, "Work on it");
      if (res.signed_out) return ghSignedOut(res.error);
      if (!res.ok) return ghError(res.error);
      safeSet(KEY_REPO_PREFIX + chatId, res.repo || fullName);
      if (chatId === currentChatId) {
        gh.chat = Object.assign({ chatId: chatId }, res);
        setPillMode("code");
        if (res.own) { // the chat's own project now has a repo, but nothing is on GitHub until it is pushed
          gh.info = "This chat's project now goes to " + (res.repo || fullName) + ". Press Push to put it on GitHub.";
          ghRender();
        } else {
          ghClose();
          ghFocusBox();
        }
      } else {
        safeSet(modeKey(chatId), "code"); // the user moved to another chat while it cloned
      }
      scheduleReconcile();
    });
  }

  function ghFocusBox() {
    setTimeout(function () {
      var box = document.getElementById("chat-input");
      if (box) try { editorOf(box).focus(); } catch (e) { /* no editor yet */ }
    }, 300);
  }

  /** The first message of a new chat after "Work on it" was pressed there: from now on the chat works on that repo,
   *  in Code mode. A message sent anywhere else first (an existing chat, a feature chat) just drops the choice. */
  function ghAdoptRepo(chatId, messages) {
    var picked = safeGet(KEY_NEW_REPO);
    if (!chatId || !picked) return;
    if (userTurns(messages) === 1 && !safeGet(KEY_REPO_PREFIX + chatId) && !state.pendingFeature && !featureOf(chatId)) {
      safeSet(KEY_REPO_PREFIX + chatId, picked);
      safeSet(modeKey(chatId), "code");
    }
    ghDropPendingRepo();
  }

  /** The panel hangs above the button: fixed to the window, lined up with the button's left edge. */
  function ghPlace() {
    var pill = document.getElementById(GH_PILL_ID);
    var panel = document.getElementById(GH_PANEL_ID);
    if (!pill || !panel) return;
    var r = pill.getBoundingClientRect();
    var width = Math.min(380, window.innerWidth - 16);
    panel.style.width = width + "px";
    panel.style.left = Math.max(8, Math.min(r.left, window.innerWidth - width - 8)) + "px";
    panel.style.bottom = Math.max(8, window.innerHeight - r.top + 8) + "px";
    panel.style.maxHeight = Math.max(220, Math.min(520, r.top - 24)) + "px";
  }

  function ghWire() {
    if (gh.wired) return;
    gh.wired = true;
    document.addEventListener("mousedown", function (e) {
      if (!gh.open) return;
      var panel = document.getElementById(GH_PANEL_ID);
      var pill = document.getElementById(GH_PILL_ID);
      if ((panel && panel.contains(e.target)) || (pill && pill.contains(e.target))) return;
      ghClose();
    }, true);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && gh.open) ghClose();
    });
    window.addEventListener("resize", function () {
      if (gh.open) ghPlace();
    });
  }

  function ghOpen() {
    ghWire();
    ghInjectStyle();
    var panel = document.getElementById(GH_PANEL_ID);
    if (!panel) {
      panel = ghEl("div", { id: GH_PANEL_ID, role: "dialog", "aria-label": "GitHub" });
      document.body.appendChild(panel);
    }
    gh.open = true;
    gh.error = "";
    gh.info = "";
    gh.push = null;
    gh.history = null;
    gh.diag = null;
    gh.asking = "";
    panel.hidden = false;
    setAttr(document.getElementById(GH_PILL_ID), "aria-expanded", "true");
    if (gh.signingIn) { // closed while waiting for the approval on GitHub: show the code again
      gh.view = "code";
      ghRender();
      return;
    }
    gh.view = "loading";
    ghRender();
    ghCall({ op: "status" }).then(function (s) {
      if (gh.signingIn) return;
      if (!s.ok) {
        gh.view = "blocked";
        gh.error = s.error || "GitHub request failed";
      } else if (!s.client_id_set) {
        gh.view = "setup";
      } else if (!s.signed_in) {
        gh.view = "signin";
      } else {
        gh.login = s.login || "";
        gh.view = "repos";
        ghLoadRepos();
        ghLoadChat();
      }
      ghRender();
    });
  }

  function ghClose() {
    gh.open = false;
    gh.asking = "";
    var panel = document.getElementById(GH_PANEL_ID);
    if (panel) panel.hidden = true;
    setAttr(document.getElementById(GH_PILL_ID), "aria-expanded", "false");
  }

  /** Show (or clear) the panel's one error line without redrawing anything else. */
  function ghError(message) {
    gh.error = message || "";
    var line = document.querySelector("#" + GH_PANEL_ID + ' [data-gh="error"]');
    if (!line) return;
    line.textContent = gh.error;
    line.hidden = !gh.error;
  }

  /** The same for the panel's plain line (what just happened). */
  function ghInfo(message) {
    gh.info = message || "";
    var line = document.querySelector("#" + GH_PANEL_ID + ' [data-gh="info"]');
    if (!line) return;
    line.textContent = gh.info;
    line.hidden = !gh.info;
  }

  // --- views

  function ghRender() {
    var panel = document.getElementById(GH_PANEL_ID);
    if (!panel || !gh.open) return;
    while (panel.firstChild) panel.removeChild(panel.firstChild);
    var kids = [];
    if (gh.view === "loading") kids = [ghEl("div", { cls: "gh-dim", text: "Checking GitHub…" })];
    else if (gh.view === "setup") kids = ghViewSetup();
    else if (gh.view === "signin") kids = ghViewSignIn();
    else if (gh.view === "code") kids = ghViewCode();
    else if (gh.view === "repos") kids = ghViewRepos();
    else if (gh.view === "push") kids = ghViewPush();
    else if (gh.view === "history") kids = ghViewHistory();
    else if (gh.view === "diagnose") kids = ghViewDiagnose();
    var error = ghEl("div", { cls: "gh-error", gh: "error", role: "alert", text: gh.error });
    error.hidden = !gh.error;
    var info = ghEl("div", { cls: "gh-info", gh: "info", text: gh.info });
    info.hidden = !gh.info;
    var foot = kids.length && kids[kids.length - 1].className === "gh-foot" ? kids.pop() : null;
    kids.concat([info, error, foot]).forEach(function (k) { if (k) panel.appendChild(k); });
    if (gh.view === "repos") {
      ghRenderList();
      ghRenderChat();
    }
    ghPlace();
    var first = panel.querySelector("[data-gh-focus]");
    if (first) {
      try { first.focus({ preventScroll: true }); } catch (e) { /* not focusable yet */ }
    }
  }

  function ghViewSetup() {
    var input = ghEl("input", { type: "text", gh: "client-id", placeholder: "Client ID", autocomplete: "off", spellcheck: "false", "data-gh-focus": true });
    function save() {
      var id = input.value.trim();
      if (!id) return;
      ghCall({ op: "set_client", client_id: id }).then(function (res) {
        if (!res.ok) return ghError(res.error);
        gh.error = "";
        gh.view = "signin";
        ghRender();
      });
    }
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") save();
    });
    return [
      ghEl("div", { cls: "gh-title", text: "Connect GitHub" }),
      ghEl("div", { cls: "gh-dim", text: "Signing in needs the Client ID of an OAuth App on your GitHub account. Once:" }),
      ghEl("ol", {}, [
        ghEl("li", {}, [
          document.createTextNode("Open "),
          ghEl("a", { href: "https://github.com/settings/developers", target: "_blank", rel: "noopener noreferrer", text: "github.com/settings/developers" }),
          document.createTextNode(" and choose New OAuth App.")
        ]),
        ghEl("li", { text: "Any homepage and callback URL will do; they are not used." }),
        ghEl("li", { text: "Tick Enable Device Flow, register the app, and copy its Client ID." })
      ]),
      ghEl("div", { cls: "gh-line" }, [input, ghEl("button", { type: "button", cls: "gh-btn", gh: "save-client", text: "Save", click: save })])
    ];
  }

  function ghViewSignIn() {
    return [
      ghEl("div", { cls: "gh-title", text: "GitHub" }),
      ghEl("div", { cls: "gh-line" }, [
        ghEl("button", { type: "button", cls: "gh-btn", gh: "sign-in", text: "Sign in with GitHub", click: ghStartSignIn, "data-gh-focus": true }),
        ghEl("button", {
          type: "button", cls: "gh-link", gh: "change-client", text: "Use another Client ID",
          click: function () { gh.error = ""; gh.view = "setup"; ghRender(); }
        })
      ])
    ];
  }

  function ghViewCode() {
    return [
      ghEl("div", { cls: "gh-title", text: "Enter this code on GitHub" }),
      ghEl("div", { cls: "gh-code", gh: "user-code", text: gh.code }),
      ghEl("div", { cls: "gh-line" }, [
        ghEl("button", {
          type: "button", cls: "gh-btn", gh: "open-github", text: "Copy code and open GitHub", "data-gh-focus": true,
          click: function () { // opened from the click itself, or the browser's pop-up blocker stops it
            ghCopy(gh.code);
            window.open(gh.uri, "_blank", "noopener");
          }
        }),
        ghEl("button", {
          type: "button", cls: "gh-link", gh: "cancel-sign-in", text: "Cancel",
          click: function () { ghStopSignIn(); gh.error = ""; gh.view = "signin"; ghRender(); }
        })
      ]),
      ghEl("div", { cls: "gh-dim", text: "Waiting for you to approve it there. This panel can be closed meanwhile." })
    ];
  }

  function ghViewRepos() {
    var search = ghEl("input", { type: "text", gh: "search", placeholder: "Find a repository", autocomplete: "off", spellcheck: "false", "data-gh-focus": true });
    search.value = gh.filter;
    search.addEventListener("input", function () { // redraw the rows only: the box keeps the cursor
      gh.filter = search.value;
      ghRenderList();
    });
    var name = ghEl("input", { type: "text", gh: "new-name", placeholder: "New repository name", autocomplete: "off", spellcheck: "false" });
    var desc = ghEl("input", { type: "text", gh: "new-desc", placeholder: "Description (optional)", autocomplete: "off" });
    var priv = ghEl("input", { type: "checkbox", gh: "new-private" });
    priv.checked = true;
    var create = ghEl("button", { type: "button", cls: "gh-btn", gh: "create", text: "Create" });
    function make() {
      var n = name.value.trim();
      if (!n || create.disabled) return;
      create.disabled = true;
      ghError("");
      ghCall({ op: "create_repo", name: n, description: desc.value.trim(), private: priv.checked }).then(function (res) {
        create.disabled = false;
        if (res.signed_out) return ghSignedOut(res.error);
        if (!res.ok) return ghError(res.error);
        name.value = "";
        desc.value = "";
        priv.checked = true;
        if (res.repo) gh.repos.unshift(res.repo);
        ghRenderList();
      });
    }
    create.addEventListener("click", make);
    name.addEventListener("keydown", function (e) {
      if (e.key === "Enter") make();
    });
    return [
      ghEl("div", { cls: "gh-chat", gh: "chat-repo", hidden: true }), // the repo this chat works on: ghRenderChat fills it
      search,
      ghEl("div", { cls: "gh-list", "data-gh-list": true }),
      ghEl("div", { cls: "gh-sep" }),
      ghEl("div", { cls: "gh-col" }, [
        ghEl("div", { cls: "gh-line" }, [name, create]),
        ghEl("div", { cls: "gh-line" }, [desc, ghEl("label", { cls: "gh-check" }, [priv, document.createTextNode("Private")])])
      ]),
      ghEl("div", { cls: "gh-foot" }, [
        ghEl("span", { text: gh.login ? "Signed in as " + gh.login : "Signed in" }),
        ghEl("button", { type: "button", cls: "gh-link", gh: "sign-out", text: "Sign out", click: ghSignOut })
      ])
    ];
  }

  function ghRenderList() {
    var list = document.querySelector("#" + GH_PANEL_ID + " [data-gh-list]");
    if (!list) return;
    while (list.firstChild) list.removeChild(list.firstChild);
    var q = gh.filter.trim().toLowerCase();
    var rows = gh.repos.filter(function (r) {
      return !q || r.full_name.toLowerCase().indexOf(q) >= 0 || (r.description || "").toLowerCase().indexOf(q) >= 0;
    });
    if (!rows.length) {
      list.appendChild(ghEl("div", {
        cls: "gh-empty",
        text: gh.loading ? "Loading your repositories…" : gh.repos.length ? "Nothing matches." : "No repositories yet."
      }));
      return;
    }
    rows.forEach(function (repo) { list.appendChild(ghRepoRow(repo)); });
  }

  function ghRepoRow(repo) {
    var slash = repo.full_name.indexOf("/");
    var nameCell = ghEl("span", { cls: "gh-name", title: repo.description || repo.full_name }, [
      ghEl("span", { cls: "gh-owner", text: repo.full_name.slice(0, slash + 1) }),
      document.createTextNode(repo.full_name.slice(slash + 1))
    ]);
    var row = ghEl("div", { cls: "gh-row", gh: "repo", "data-repo": repo.full_name, "data-private": String(!!repo.private) }, [nameCell]);
    if (gh.asking === repo.full_name) {
      row.appendChild(ghEl("span", { cls: "gh-dim", text: "Make public?" }));
      row.appendChild(ghEl("button", { type: "button", cls: "gh-link", gh: "confirm-yes", text: "Yes", click: function () { gh.asking = ""; ghSetPrivate(repo, false); } }));
      row.appendChild(ghEl("button", { type: "button", cls: "gh-link", gh: "confirm-cancel", text: "Cancel", click: function () { gh.asking = ""; ghRenderList(); } }));
      return row;
    }
    var work = ghEl("button", {
      type: "button", cls: "gh-link gh-work", gh: "work", text: "Work on it",
      title: "Work on this repository with the coding agent in this chat"
    });
    work.addEventListener("click", function () { ghWorkOn(repo.full_name, work); });
    row.appendChild(work);
    row.appendChild(ghEl("span", { cls: "gh-time", text: ghAgo(repo.pushed_at) }));
    row.appendChild(ghEl("button", {
      type: "button", cls: "gh-vis", gh: "visibility", "aria-pressed": String(!!repo.private),
      title: repo.private ? "Private. Click to make it public (asks first)." : "Public. Click to make it private.",
      text: repo.private ? "Private" : "Public",
      click: function () {
        if (repo.private) { // going public is the risky direction: ask in the row first
          gh.asking = repo.full_name;
          ghRenderList();
        } else {
          ghSetPrivate(repo, true);
        }
      }
    }));
    return row;
  }

  /** Above the search box, in a chat that works on a repo: "owner/name · main · 3 changed files · 2 to push" and
   *  Pull / Push / Versions. In a chat whose project is not on GitHub: how to put it there. */
  function ghRenderChat() {
    var line = document.querySelector("#" + GH_PANEL_ID + ' [data-gh="chat-repo"]');
    if (!line) return;
    while (line.firstChild) line.removeChild(line.firstChild);
    var c = gh.chat;
    line.hidden = !(c && (c.linked || c.project));
    if (line.hidden) return;
    if (!c.linked) {
      line.appendChild(ghEl("div", { cls: "gh-dim", gh: "chat-hint", text: "This chat's project is not on GitHub. To put it there, press Work on it on an empty repository (make one below), then Push." }));
      line.appendChild(ghEl("div", { cls: "gh-line" }, [
        ghEl("button", { type: "button", cls: "gh-vis", gh: "diagnose", text: "Diagnose", title: "Look this project over and say what it needs before it can go to GitHub", click: ghOpenDiagnose })
      ]));
      return;
    }
    var n = c.changed || 0;
    var text = c.repo + " · " + (c.branch || "no branch") + " · " + (n === 0 ? "no changes" : n === 1 ? "1 changed file" : n + " changed files");
    if (c.ahead) text += " · " + c.ahead + " to push";
    line.appendChild(ghEl("div", { cls: "gh-chat-text", title: "The repository this chat works on", text: text }));
    var chatId = c.chatId;
    var pull = ghEl("button", { type: "button", cls: "gh-vis", gh: "pull", text: "Pull", title: "Bring in new commits from GitHub (fast-forward only: your work is never overwritten)" });
    pull.addEventListener("click", function () {
      pull.disabled = true;
      pull.textContent = "Pulling…";
      ghError("");
      ghCall({ op: "pull", chat_id: chatId }).then(function (res) {
        if (res.signed_out) return ghSignedOut(res.error);
        if (res.ok && chatId === currentChatId) gh.chat = Object.assign({ chatId: chatId }, res);
        ghRenderChat();
        if (!res.ok) ghError(res.error);
      });
    });
    line.appendChild(ghEl("div", { cls: "gh-line" }, [
      pull,
      ghEl("button", { type: "button", cls: "gh-vis", gh: "push", text: "Push", title: "Save the changes as a version and push to a branch on GitHub", click: ghOpenPush }),
      ghEl("button", { type: "button", cls: "gh-vis", gh: "history", text: "Versions", title: "The project's versions; roll back to one", click: ghOpenHistory }),
      ghEl("button", { type: "button", cls: "gh-vis", gh: "diagnose", text: "Diagnose", title: "Compare this project with GitHub and say what is out of step (read-only)", click: ghOpenDiagnose })
    ]));
  }

  // --- pushing, history, rolling back (a chat that works on a repo)

  function ghBack() {
    gh.view = "repos";
    gh.error = "";
    gh.info = "";
    gh.push = null;
    gh.history = null;
    gh.diag = null;
    ghRender();
    ghLoadChat();
  }

  function ghHead(title) {
    return ghEl("div", { cls: "gh-head" }, [
      ghEl("span", { cls: "gh-title gh-name", text: title, title: title }),
      ghEl("button", { type: "button", cls: "gh-link", gh: "back", text: "Back", click: ghBack })
    ]);
  }

  /** Open the push view: what would be committed, and where it can go. */
  function ghOpenPush() {
    var chatId = currentChatId;
    if (!chatId) return;
    gh.view = "push";
    gh.error = "";
    gh.info = "";
    gh.push = { chatId: chatId, loading: true };
    ghRender();
    Promise.all([ghCall({ op: "changes", chat_id: chatId }), ghCall({ op: "branches", chat_id: chatId })]).then(function (r) {
      if (gh.view !== "push" || !gh.push || gh.push.chatId !== chatId) return;
      var bad = r[0].signed_out ? r[0] : r[1].signed_out ? r[1] : null;
      if (bad) return ghSignedOut(bad.error);
      if (!r[0].ok || !r[1].ok) {
        gh.push = null;
        gh.view = "repos";
        gh.error = (!r[0].ok ? r[0].error : r[1].error) || "GitHub request failed";
        return ghRender();
      }
      gh.push = {
        chatId: chatId, repo: r[0].repo, files: r[0].files || [], more: r[0].more || 0, ahead: r[0].ahead || 0,
        branches: r[1].branches || [], def: r[1]["default"] || "main", current: r[1].current || ""
      };
      ghRender();
    });
  }

  /** Where a push goes unless the user says otherwise: an empty repo gets its default branch; a chat already on a
   *  branch of its own keeps it; from the default branch it is a NEW branch, so main is never changed by accident. */
  function ghPushTarget(p) {
    if (!p.branches.length) return { value: p.def, fresh: "" };
    if (p.current && p.current !== p.def) {
      return p.branches.indexOf(p.current) >= 0 ? { value: p.current, fresh: "" } : { value: "__new__", fresh: p.current };
    }
    var d = new Date();
    var two = function (n) { return (n < 10 ? "0" : "") + n; };
    return { value: "__new__", fresh: "code-mode-" + two(d.getMonth() + 1) + two(d.getDate()) + "-" + two(d.getHours()) + two(d.getMinutes()) };
  }

  function ghViewPush() {
    var p = gh.push || {};
    if (p.loading) return [ghHead("Push"), ghEl("div", { cls: "gh-dim", text: "Looking at the project…" })];
    if (p.done) {
      return [
        ghHead("Push " + p.repo),
        ghEl("div", { gh: "pushed", text: "Pushed to " + p.done.branch + "." }),
        ghEl("div", { cls: "gh-line" }, [
          ghEl("a", { gh: "pushed-link", href: p.done.url, target: "_blank", rel: "noopener noreferrer", text: "Open it on GitHub" })
        ])
      ];
    }
    var kids = [ghHead("Push " + p.repo)];
    var message = null;
    if (p.files.length) {
      var total = p.files.length + p.more;
      kids.push(ghEl("div", { cls: "gh-dim", text: (total === 1 ? "1 changed file" : total + " changed files") + " will be saved as one version:" }));
      kids.push(ghEl("div", { cls: "gh-files", gh: "files" }, p.files.map(function (f) {
        return ghEl("div", { cls: "gh-file" }, [ghEl("span", { text: f.status }), ghEl("span", { text: f.path, title: f.path })]);
      }).concat(p.more ? [ghEl("div", { cls: "gh-file" }, [ghEl("span", { text: "" }), ghEl("span", { text: "and " + p.more + " more" })])] : [])));
      message = ghEl("input", { type: "text", gh: "commit-message", placeholder: "What changed (the version's name)", autocomplete: "off", "data-gh-focus": true });
      kids.push(ghEl("div", { cls: "gh-line" }, [message, ghSuggestButton(p, message)]));
    } else {
      kids.push(ghEl("div", { cls: "gh-dim", gh: "nothing-new", text: p.ahead ? (p.ahead === 1 ? "1 saved version" : p.ahead + " saved versions") + " to push. No unsaved changes." : "No unsaved changes, and GitHub already has every version. Pushing to another branch still works." }));
    }
    var target = ghPushTarget(p);
    var select = ghEl("select", { gh: "branch", "aria-label": "Branch to push to" });
    var names = p.branches.length ? p.branches.slice() : [p.def];
    names.forEach(function (b) {
      select.appendChild(ghEl("option", { value: b, text: b + (!p.branches.length ? " (first push)" : b === p.def ? " (default)" : "") }));
    });
    select.appendChild(ghEl("option", { value: "__new__", text: "New branch…" }));
    select.value = target.value;
    var fresh = ghEl("input", { type: "text", gh: "new-branch", placeholder: "new-branch-name", autocomplete: "off", spellcheck: "false" });
    fresh.value = target.fresh;
    fresh.hidden = select.value !== "__new__";
    select.addEventListener("change", function () {
      fresh.hidden = select.value !== "__new__";
      if (!fresh.hidden) fresh.focus();
    });
    kids.push(ghEl("div", { cls: "gh-line" }, [ghEl("span", { cls: "gh-dim", text: "To" }), select]));
    kids.push(fresh);
    var go = ghEl("button", { type: "button", cls: "gh-btn", gh: "do-push", text: "Push" });
    function push() {
      var branch = select.value === "__new__" ? fresh.value.trim() : select.value;
      if (!branch) return ghError("Give the new branch a name.");
      if (go.disabled) return;
      go.disabled = true;
      setText(go, "Pushing…");
      ghError("");
      ghCall({ op: "push", chat_id: p.chatId, branch: branch, message: message ? message.value.trim() : "" }).then(function (res) {
        go.disabled = false;
        setText(go, "Push");
        if (res.signed_out) return ghSignedOut(res.error);
        if (!res.ok) return ghError(res.error);
        if (p.chatId === currentChatId) gh.chat = Object.assign({ chatId: p.chatId }, res);
        if (gh.push === p) {
          p.done = { branch: res.pushed || branch, url: res.url || "" };
          ghRender();
        }
      });
    }
    go.addEventListener("click", push);
    [message, fresh].forEach(function (box) {
      if (box) box.addEventListener("keydown", function (e) { if (e.key === "Enter") push(); });
    });
    kids.push(ghEl("div", { cls: "gh-line" }, [go]));
    return kids;
  }

  /** The spark beside the "What changed" box: the model that writes chat titles and suggestions names the version
   *  from the changed files (the pipe's suggest_message), and the box is filled in for the user to keep or edit. */
  function ghSuggestButton(p, box) {
    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", 15);
    svg.setAttribute("height", 15);
    svg.setAttribute("fill", "currentColor");
    svg.setAttribute("aria-hidden", "true");
    var path = document.createElementNS(NS, "path");
    path.setAttribute("d", "M12 2l2.3 6.9L21 11l-6.7 2.1L12 20l-2.3-6.9L3 11l6.7-2.1L12 2zm7 13l.9 2.6L22.5 18.5l-2.6.9L19 22l-.9-2.6-2.6-.9 2.6-.9L19 15z");
    svg.appendChild(path);
    var button = ghEl("button", { type: "button", cls: "gh-spark", gh: "suggest-message", title: "Suggest a name from the changes (the small model that writes titles and suggestions)", "aria-label": "Suggest a name from the changes" }, [svg]);
    button.addEventListener("click", function () {
      if (button.disabled) return;
      button.disabled = true;
      button.classList.add("gh-busy");
      ghError("");
      ghInfo("");
      ghCall({ op: "suggest_message", chat_id: p.chatId }).then(function (res) {
        button.disabled = false;
        button.classList.remove("gh-busy");
        if (res.signed_out) return ghSignedOut(res.error);
        if (!res.ok) return ghError(res.error);
        box.value = res.message || "";
        box.focus();
        if (!res.from_model) ghInfo("The model did not answer, so this is put together from the file names.");
      });
    });
    return button;
  }

  function ghOpenHistory() {
    var chatId = currentChatId;
    if (!chatId) return;
    gh.view = "history";
    gh.error = "";
    gh.info = "";
    gh.history = { chatId: chatId, loading: true, commits: [], asking: "" };
    ghRender();
    ghLoadHistory();
  }

  function ghLoadHistory() {
    var h = gh.history;
    if (!h) return;
    ghCall({ op: "history", chat_id: h.chatId }).then(function (res) {
      if (gh.history !== h) return;
      if (res.signed_out) return ghSignedOut(res.error);
      h.loading = false;
      if (!res.ok) return ghError(res.error);
      h.commits = res.commits || [];
      h.branch = res.branch || "";
      if (h.chatId === currentChatId) gh.chat = Object.assign({ chatId: h.chatId }, { ok: true, linked: res.linked, repo: res.repo, branch: res.branch, changed: res.changed, ahead: res.ahead });
      ghRender();
    });
  }

  function ghViewHistory() {
    var h = gh.history || { commits: [] };
    var kids = [ghHead("Versions" + (h.branch ? " · " + h.branch : ""))];
    if (h.loading) return kids.concat([ghEl("div", { cls: "gh-dim", text: "Reading the history…" })]);
    if (!h.commits.length) return kids.concat([ghEl("div", { cls: "gh-dim", text: "No versions yet: nothing has been saved in this project." })]);
    kids.push(ghEl("div", { cls: "gh-list", "data-gh-history": true }, h.commits.map(function (c, i) {
      var row = ghEl("div", { cls: "gh-commit", gh: "commit", "data-sha": c.sha }, [
        ghEl("div", { cls: "gh-commit-text" }, [
          ghEl("span", { cls: "gh-commit-subject", text: c.subject || "(no description)", title: c.subject }),
          ghEl("span", { cls: "gh-commit-meta", text: c.short + " · " + ghAgo(c.time * 1000) + (c.author ? " · " + c.author : "") + (c.pushed ? "" : " · not pushed") })
        ])
      ]);
      if (h.asking === c.sha) {
        row.appendChild(ghEl("span", { cls: "gh-dim", text: "Go back to this?" }));
        row.appendChild(ghEl("button", { type: "button", cls: "gh-link", gh: "rollback-yes", text: "Yes", click: function () { ghRollback(c); } }));
        row.appendChild(ghEl("button", { type: "button", cls: "gh-link", gh: "rollback-cancel", text: "Cancel", click: function () { h.asking = ""; ghRender(); } }));
      } else if (i > 0) { // the first row is the version the project is at
        row.appendChild(ghEl("button", {
          type: "button", cls: "gh-link gh-work", gh: "rollback", text: "Roll back",
          title: "Make the project's files what they were at this version. It is saved as a new version on top: nothing is lost.",
          click: function () { h.asking = c.sha; gh.info = ""; ghRender(); }
        }));
      } else {
        row.appendChild(ghEl("span", { cls: "gh-time", text: "now" }));
      }
      return row;
    })));
    return kids;
  }

  function ghRollback(commit) {
    var h = gh.history;
    if (!h || h.busy) return;
    h.busy = true;
    ghError("");
    ghCall({ op: "rollback", chat_id: h.chatId, sha: commit.sha }).then(function (res) {
      h.busy = false;
      if (gh.history !== h) return;
      h.asking = "";
      if (res.signed_out) return ghSignedOut(res.error);
      if (!res.ok) {
        ghRender();
        return ghError(res.error);
      }
      gh.info = res.same ? "The project is already at that version." :
        "Rolled back to " + commit.short + (res.saved ? ". Your unsaved changes were saved as a version first." : ".") + " This is a new version on top: nothing was lost. Push when you want GitHub to have it.";
      ghLoadHistory();
    });
  }

  // --- diagnosing (read-only): what the chat's project and its GitHub repo have out of step

  function ghOpenDiagnose() {
    var chatId = currentChatId;
    if (!chatId) return;
    gh.view = "diagnose";
    gh.error = "";
    gh.info = "";
    gh.diag = { chatId: chatId, loading: true, data: null };
    ghRender();
    ghLoadDiagnose();
  }

  function ghLoadDiagnose() {
    var d = gh.diag;
    if (!d) return;
    ghCall({ op: "diagnose", chat_id: d.chatId }).then(function (res) {
      if (gh.diag !== d) return;
      if (res.signed_out) return ghSignedOut(res.error);
      d.loading = false;
      d.data = res;
      if (!res.ok) { d.data = null; return ghError(res.error); }
      if (res.linked && d.chatId === currentChatId) { // the chat row should show what the pipe just re-read
        gh.chat = Object.assign({ chatId: d.chatId }, { ok: true, linked: true, repo: res.repo, branch: res.branch,
                                                   changed: res.changed, ahead: res.ahead });
      }
      ghRender();
    });
  }

  function ghViewDiagnose() {
    var d = gh.diag || { loading: true, data: null };
    var res = d.data || {};
    var kids = [ghHead("Diagnose" + (res.repo ? " · " + res.repo : "") + (res.branch ? " · " + res.branch : ""))];
    if (d.loading) return kids.concat([ghEl("div", { cls: "gh-dim", text: "Comparing this project with GitHub…" })]);
    var findings = res.findings || [];
    kids.push(ghEl("div", { cls: findings.length ? "gh-chat-text" : "gh-dim", text: res.summary || "The project and GitHub match." }));
    if (findings.length) kids.push(ghEl("div", { cls: "gh-list", "data-gh-findings": true }, findings.map(function (f) {
      var row = ghEl("div", { cls: "gh-row", gh: "finding", "data-finding": f.id }, [
        ghEl("span", { cls: "gh-finding-text", text: f.text })
      ]);
      if (f.action === "pull") {
        row.appendChild(ghEl("button", { type: "button", cls: "gh-link gh-work", gh: "diag-pull", text: "Pull",
          title: "Bring in the commits GitHub has (fast-forward only: your work is never overwritten)",
          click: function () { ghDiagPull(); } }));
      } else if (f.action === "push") {
        row.appendChild(ghEl("button", { type: "button", cls: "gh-link gh-work", gh: "diag-push", text: "Push",
          title: "Send this project's work to GitHub", click: ghOpenPush }));
      }
      if (f.action === "agent") {
        row.appendChild(ghEl("button", { type: "button", cls: "gh-link gh-work", gh: "ask-agent", text: "Ask the agent",
          title: "Send these findings to this chat's coding agent to investigate and fix, with your say-so",
          click: ghAskAgent }));
      }
      return row;
    })));
    kids.push(ghEl("button", { type: "button", cls: "gh-link", gh: "diagnose-again", text: "Check again",
                              title: "Compare again (read-only)", click: ghOpenDiagnose }));
    return kids;
  }

  /** Pull from the diagnose view, then compare again: the findings should shrink to what a pull cannot fix. */
  function ghDiagPull() {
    var d = gh.diag;
    if (!d || d.busy) return;
    var chatId = d.chatId;
    d.busy = true;
    ghError("");
    ghCall({ op: "pull", chat_id: chatId }).then(function (res) {
      if (!d || gh.diag !== d) return;
      d.busy = false;
      if (res.signed_out) return ghSignedOut(res.error);
      if (res.ok && chatId === currentChatId) gh.chat = Object.assign({ chatId: chatId }, res);
      if (!res.ok) return ghError(res.error);
      gh.info = "Pulled. What is left:";
      ghLoadDiagnose();
    });
  }

  // --- actions

  /** Ask the pipe what the open chat's project is (it looks at the folder on the VM). Also teaches this browser
   *  the chat's repo when the chat was started elsewhere, so the chip shows and its Code turns carry code_repo. */
  function ghLoadChat() {
    var chatId = currentChatId;
    gh.chat = null;
    if (!chatId) return;
    ghCall({ op: "chat_status", chat_id: chatId }).then(function (res) {
      if (chatId !== currentChatId || !res.ok) return;
      gh.chat = Object.assign({ chatId: chatId }, res);
      var known = safeGet(KEY_REPO_PREFIX + chatId);
      if (res.linked && known !== res.repo) { // the folder is the truth
        safeSet(KEY_REPO_PREFIX + chatId, res.repo);
        scheduleReconcile();
      } else if (!res.linked && known) { // e.g. the first message's clone failed: stop asking for it every turn
        try { localStorage.removeItem(KEY_REPO_PREFIX + chatId); } catch (e) { /* storage unavailable */ }
        scheduleReconcile();
      }
      ghRenderChat();
    });
  }

  function ghSignedOut(message) {
    gh.repos = [];
    gh.chat = null;
    gh.push = null;
    gh.history = null;
    gh.diag = null;
    gh.info = "";
    gh.login = "";
    gh.view = "signin";
    gh.error = message || "";
    ghRender();
  }

  function ghLoadRepos() {
    gh.loading = true;
    ghCall({ op: "repos" }).then(function (res) {
      gh.loading = false;
      if (res.signed_out) return ghSignedOut(res.error);
      if (!res.ok) {
        ghRenderList();
        return ghError(res.error);
      }
      gh.repos = res.repos || [];
      if (res.login) gh.login = res.login;
      ghRenderList();
      var who = document.querySelector("#" + GH_PANEL_ID + " .gh-foot span");
      if (who && gh.login) who.textContent = "Signed in as " + gh.login;
    });
  }

  function ghSetPrivate(repo, makePrivate) {
    ghError("");
    ghCall({ op: "set_visibility", full_name: repo.full_name, private: makePrivate }).then(function (res) {
      if (res.signed_out) return ghSignedOut(res.error);
      if (!res.ok) {
        ghRenderList();
        return ghError(res.error);
      }
      repo.private = res.repo ? !!res.repo.private : makePrivate;
      ghRenderList();
    });
  }

  function ghStartSignIn() {
    ghError("");
    ghCall({ op: "login_start" }).then(function (res) {
      if (!res.ok) return ghError(res.error);
      gh.code = res.user_code || "";
      gh.uri = res.verification_uri || "https://github.com/login/device";
      gh.interval = res.interval > 0 ? res.interval : 5;
      gh.signingIn = true;
      gh.view = "code";
      ghRender();
      ghPollSoon();
    });
  }

  /** Ask the pipe again after `interval` seconds, the pace GitHub sets (and raises when polled too fast). */
  function ghPollSoon() {
    clearTimeout(gh.pollTimer);
    gh.pollTimer = setTimeout(function () {
      ghCall({ op: "login_poll" }).then(function (res) {
        if (!gh.signingIn) return; // cancelled meanwhile
        if (res.ok && res.pending) {
          if (res.interval > 0) gh.interval = res.interval;
          return ghPollSoon();
        }
        ghStopSignIn();
        if (res.ok && res.signed_in) {
          gh.login = res.login || "";
          gh.error = "";
          gh.view = "repos";
          ghLoadRepos();
          ghLoadChat();
        } else {
          gh.view = "signin";
          gh.error = res.error || "The sign-in did not finish.";
        }
        ghRender();
      });
    }, gh.interval * 1000);
  }

  function ghStopSignIn() {
    clearTimeout(gh.pollTimer);
    gh.pollTimer = 0;
    gh.signingIn = false;
    gh.code = "";
  }

  function ghSignOut() {
    ghCall({ op: "logout" }).then(function (res) {
      if (!res.ok) return ghError(res.error);
      ghSignedOut("");
    });
  }

  // --------------------------------------------------------------- routing / fetch
  function jsonFromBody(body) {
    if (typeof body === "string") {
      try {
        return JSON.parse(body);
      } catch (e) {
        return null;
      }
    }
    return null;
  }

  function handleRequest(urlStr, method, bodyStr, init, args) {
    var pathname;
    try {
      pathname = new URL(urlStr, location.href).pathname;
    } catch (e) {
      return _fetch.apply(null, args);
    }
    if (method === "POST" && pathname === "/api/chat/completed") { // a reply finished: read what it saved
      var completed = _fetch.apply(null, args);
      completed.then(function () { ctxSoon(1500); }, function () {});
      return completed;
    }
    if (method !== "POST" || pathname !== "/api/chat/completions") {
      return _fetch.apply(null, args); // only act on the chat endpoint
    }
    var parsed = jsonFromBody(bodyStr);
    if (!parsed || typeof parsed !== "object") {
      return _fetch.apply(null, args); // body is not a JSON string
    }

    var chatId = parsed.chat_id != null ? String(parsed.chat_id) : null;
    var picked = turnModel(chatId, parsed.model);
    var effort = null; // the thinking pill's level for the picked model, or null when it has no pill
    try {
      effort = effortFor(picked || parsed.model);
    } catch (e) {
      warnOnce("effort-request", e);
    }

    try {
      ghAdoptRepo(chatId, parsed.messages); // "Work on it" was pressed and this is the new chat's first message
    } catch (e) {
      warnOnce("github-adopt", e);
    }

    // Decide the mode for this chat: a saved mode wins, else the pill's mode.
    var saved = chatId ? safeGet(modeKey(chatId)) : null;
    var mode = isMode(saved) ? saved : state.mode;

    // ✦ Feature was pressed and this is the new chat's first message: it becomes a feature chat.
    if (chatId && state.pendingFeature && !featureOf(chatId)) {
      if (userTurns(parsed.messages) === 1) adoptFeature(chatId);
      else cancelPendingFeature(); // the user went back to an existing chat instead
    }
    var feature = chatId ? featureOf(chatId) : null;
    if (feature) {
      // Feature chat: always the coding agent, in the feature's workspace (the Code pipe sees code_feature).
      parsed.code_model = picked;
      parsed.code_feature = true;
      if (effort) parsed.code_effort = effort;
      parsed.model = CODE_MODEL;
      ["model_item", "tool_ids", "skill_ids", "terminal_id", "tool_servers", "features", "filter_ids"].forEach(
        function (k) { delete parsed[k]; });
      if (parsed.id != null) recordMsg(chatId, parsed.id);
      scheduleReconcile();
      setTimeout(refreshFeatureChats, 4000); // the pipe registers the feature on its first turn
      return _fetch(urlStr, Object.assign({}, init, { method: "POST", body: JSON.stringify(parsed) }));
    }

    // Carry a brand-new chat's choice over once it gets an id.
    if (chatId) {
      if (!(isMode(safeGet(modeKey(chatId))))) {
        safeSet(modeKey(chatId), state.mode);
      }
    } else {
      safeSet(KEY_NEW_CHAT, state.mode);
    }

    if (mode === "chat") {
      if (!effort || effort === "on") return _fetch.apply(null, args); // Chat mode: untouched
      // Open WebUI copies params into the request body; "none" turns thinking off in llama.cpp.
      parsed.params = parsed.params && typeof parsed.params === "object" ? parsed.params : {};
      parsed.params.reasoning_effort = effort;
      return _fetch(urlStr, Object.assign({}, init, { method: "POST", body: JSON.stringify(parsed) }));
    }

    // Code mode: rewrite the request for the coding agent.
    withStashedTask(parsed, chatId);
    if (effort) parsed.code_effort = effort; // the Code pipe sets it as the session's opencode variant
    parsed.code_model = picked; // the picked model; the Code pipe runs the agent on it when it can
    var chatRepo = chatId ? safeGet(KEY_REPO_PREFIX + chatId) : null;
    if (chatRepo) parsed.code_repo = chatRepo; // the Code pipe keeps the project folder a clone of this repo
    parsed.model = CODE_MODEL;
    delete parsed.model_item;
    delete parsed.tool_ids;
    delete parsed.skill_ids;
    delete parsed.terminal_id;
    delete parsed.tool_servers;
    delete parsed.features;
    delete parsed.filter_ids;

    var serialized = JSON.stringify(parsed);
    if (parsed.id != null) recordMsg(chatId, parsed.id);

    var newInit = Object.assign({}, init, { method: "POST", body: serialized });
    scheduleReconcile(); // apply badges once the reply message lands
    return _fetch(urlStr, newInit);
  }

  var _fetch = window.fetch;
  try {
    window.fetch = function () {
      try {
        var args = arguments;
        var input = args[0];
        var init = args[1] || {};
        var method = (init.method || (input && input.method) || "GET").toUpperCase();
        var urlStr;
        if (typeof input === "string") {
          urlStr = input;
        } else if (input && typeof input.url === "string") {
          urlStr = input.url; // new Request(...)
        } else {
          return _fetch.apply(null, args);
        }
        // Only a JSON string body can be inspected; anything else passes through.
        var bodyStr = init && typeof init.body === "string" ? init.body : null;
        return handleRequest(urlStr, method, bodyStr, init, args);
      } catch (e) {
        warnOnce("fetch", e);
        return _fetch.apply(null, arguments);
      }
    };
  } catch (e) {
    warnOnce("fetch-install", e);
  }

  // ----------------------------------------------------------------- preview
  // Code replies start with a [Preview](<PREVIEW_ORIGIN>/p/<chat>/<sig>/) link from the Code pipe. It becomes a
  // chip that opens a side panel showing the chat's project; while the chat is in Code mode the panel pops up
  // by itself when the project changes, and reloads as files keep changing.
  var PREVIEW_ORIGIN = window.__codePreviewOrigin || "http://ai-sandbox:4097";  // override only for tests
  var PREVIEW_POLL_MS = 2500;
  var preview = { base: null, page: null, links: 0, version: null, dismissed: false, timer: 0, seq: 0, down: false, retry: 0 };

  function previewLinks() {   // the chat's own links (Watch mode's "open in a tab" button is not one)
    return document.querySelectorAll('a[href^="' + PREVIEW_ORIGIN + '/p/"]:not(.wt-popen)');
  }

  function previewBaseOf(href) {
    var m = /^(https?:\/\/[^/]+\/p\/[A-Za-z0-9_-]+\/[0-9a-f]+\/)/.exec(href || "");
    return m ? m[1] : null;
  }

  function injectPreviewStyle() {
    if (document.getElementById("code-preview-style")) return;
    var css = [
      "a.code-preview-chip{display:inline-flex;align-items:center;gap:6px;font:600 12px/1 system-ui,-apple-system,\"Segoe UI\",Roboto,Helvetica,Arial,sans-serif;color:#374151;background:#f3f4f6;border:1px solid #e5e7eb;padding:5px 10px;border-radius:999px;text-decoration:none!important;cursor:pointer}",
      "html.dark a.code-preview-chip{color:#d1d5db;background:rgba(255,255,255,0.06);border-color:rgba(255,255,255,0.10)}",
      "a.code-preview-chip::before{content:'';width:6px;height:6px;border-radius:50%;background:#4f8cff}",
      "#code-preview-panel{position:fixed;top:0;right:0;bottom:0;z-index:60;display:flex;flex-direction:column;background:#fff;border-left:1px solid #e5e7eb;box-shadow:-8px 0 24px rgba(0,0,0,.08);transform:translateX(100%);transition:transform .18s ease}",
      "html.dark #code-preview-panel{background:#171717;border-left-color:rgba(255,255,255,0.08);box-shadow:-8px 0 24px rgba(0,0,0,.4)}",
      "#code-preview-panel.open{transform:none}",
      "#code-preview-panel .cp-head{display:flex;align-items:center;gap:6px;height:44px;padding:0 8px 0 14px;border-bottom:1px solid #e5e7eb;font:600 13px/1 system-ui,-apple-system,\"Segoe UI\",Roboto,Helvetica,Arial,sans-serif;color:#111}",
      "html.dark #code-preview-panel .cp-head{border-bottom-color:rgba(255,255,255,0.08);color:#fff}",
      "#code-preview-panel .cp-title{flex:1;display:flex;align-items:center;gap:8px}",
      "#code-preview-panel .cp-live{width:6px;height:6px;border-radius:50%;background:#4f8cff;opacity:.35;transition:opacity .3s}",
      "#code-preview-panel .cp-live.on{opacity:1}",
      "#code-preview-panel .cp-btn{appearance:none;border:0;background:transparent;color:#6b7280;cursor:pointer;width:30px;height:30px;border-radius:8px;font:500 15px/1 system-ui,sans-serif}",
      "#code-preview-panel .cp-btn:hover{background:#f3f4f6;color:#111}",
      "html.dark #code-preview-panel .cp-btn{color:#9ca3af}",
      "html.dark #code-preview-panel .cp-btn:hover{background:rgba(255,255,255,0.08);color:#fff}",
      "#code-preview-panel iframe{flex:1;width:100%;border:0;background:#fff}",
      "#code-preview-panel .cp-grip{position:absolute;left:-4px;top:0;bottom:0;width:8px;cursor:ew-resize}",
      "#code-preview-panel .cp-down{position:absolute;top:45px;left:0;right:0;bottom:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px;background:#fff;color:#6b7280;font:400 13px/1.5 system-ui,-apple-system,\"Segoe UI\",Roboto,Helvetica,Arial,sans-serif}",
      "html.dark #code-preview-panel .cp-down{background:#171717;color:#9ca3af}",
      "#code-preview-panel .cp-down b{display:block;margin-bottom:6px;font-weight:600;font-size:14px;color:#111}",
      "html.dark #code-preview-panel .cp-down b{color:#fff}",
      "#code-preview-panel .cp-down span{display:block;max-width:320px}",
      "#code-preview-panel .cp-down[hidden]{display:none}",
      "#code-preview-panel .cp-sub{font-weight:500;font-size:11px;color:#9ca3af;transition:color .3s}",
      "#code-preview-panel .cp-sub.on{color:#4f8cff}",
      ".cp-tabs{flex:1;display:flex;align-items:center;gap:4px}",
      ".cp-tab{appearance:none;border:0;background:transparent;cursor:pointer;display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:8px;font:600 13px/1 system-ui,-apple-system,\"Segoe UI\",Roboto,Helvetica,Arial,sans-serif;color:#6b7280}",
      ".cp-tab.on{background:#f3f4f6;color:#111}",
      "html.dark .cp-tab{color:#9ca3af}",
      "html.dark .cp-tab.on{background:rgba(255,255,255,0.08);color:#fff}",
      ".cp-tab[hidden],.cp-frame[hidden],.cp-btn[hidden]{display:none}",
      ".cp-fixdot{width:6px;height:6px;border-radius:50%;background:#f59e0b}",
      ".cp-featstar{color:#d97706;font-size:12px}html.dark .cp-featstar{color:#f5a524}",
      "a.code-feature-chip{display:inline-flex;align-items:center;gap:6px;font:600 12px/1 system-ui,-apple-system,\"Segoe UI\",Roboto,Helvetica,Arial,sans-serif;color:#374151;background:#fffbeb;border:1px solid #fde68a;padding:5px 10px;border-radius:999px;text-decoration:none!important;cursor:pointer}",
      "html.dark a.code-feature-chip{color:#f3d9a4;background:rgba(245,165,36,0.07);border-color:rgba(245,165,36,0.25)}",
      "a.code-feature-chip::before{content:'\\2726';color:#d97706}html.dark a.code-feature-chip::before{color:#f5a524}",
      "a.code-feature-chip.ready{color:#065f46;background:#ecfdf5;border-color:#a7f3d0}",
      "html.dark a.code-feature-chip.ready{color:#3ecf8e;background:rgba(62,207,142,0.07);border-color:rgba(62,207,142,0.25)}",
      ".cp-fixdot.busy{animation:cpPulse 1.4s ease-in-out infinite}",
      ".cp-fixdot.ok{background:#22c55e}",
      "@keyframes cpPulse{0%,100%{opacity:1}50%{opacity:.25}}",
      "a.code-fixer-chip{display:inline-flex;align-items:center;gap:6px;font:600 12px/1 system-ui,-apple-system,\"Segoe UI\",Roboto,Helvetica,Arial,sans-serif;color:#374151;background:#fff7ed;border:1px solid #fed7aa;padding:5px 10px;border-radius:999px;text-decoration:none!important;cursor:pointer}",
      "html.dark a.code-fixer-chip{color:#fcd34d;background:rgba(245,158,11,0.08);border-color:rgba(245,158,11,0.25)}",
      "a.code-fixer-chip::before{content:'';width:6px;height:6px;border-radius:50%;background:#f59e0b}",
      "html.code-preview-open #chat-container{padding-right:var(--code-preview-width,0px);transition:padding-right .18s ease}",
      // the Checklist tab and its button at the right edge (the builder's CHECKLIST.md)
      "#code-preview-panel .cp-ck{flex:1;overflow-y:auto;padding:16px 16px 20px;font:400 13px/1.5 system-ui,-apple-system,\"Segoe UI\",Roboto,Helvetica,Arial,sans-serif;color:#1f2937}",
      "html.dark #code-preview-panel .cp-ck{color:#e5e7eb}",
      ".cp-ck-count{font:500 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:#2563eb}html.dark .cp-ck-count{color:#a9c8ff}",
      ".ck-title{font-weight:600;font-size:14px}",
      ".ck-sub,.ck-meta{color:#6b7280;font-size:12px}html.dark .ck-sub,html.dark .ck-meta{color:#a1a1aa}",
      ".ck-meta{display:flex;justify-content:space-between;gap:12px;margin-top:8px}",
      ".ck-bar{position:relative;height:6px;border-radius:999px;background:#e5e7eb;margin-top:12px;overflow:hidden}html.dark .ck-bar{background:#26262d}",
      ".ck-bar i{position:absolute;left:0;top:0;bottom:0;background:#16a34a;border-radius:999px;transition:width .6s ease}html.dark .ck-bar i{background:#3ecf8e}",
      ".ck-bar b{position:absolute;top:0;bottom:0;overflow:hidden;background:rgba(79,140,255,.35)}",
      ".ck-bar b::after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,rgba(79,140,255,0),#4f8cff,rgba(79,140,255,0));animation:ckSweep 1.8s ease-in-out infinite}",
      ".ck-sec{margin-top:18px}",
      ".ck-h{display:flex;align-items:center;justify-content:space-between;font:600 11px/1 system-ui,-apple-system,sans-serif;letter-spacing:.08em;color:#6b7280;margin-bottom:8px}html.dark .ck-h{color:#8a8a94}",
      "button.ck-h{appearance:none;border:0;background:none;width:100%;min-height:32px;padding:0;cursor:pointer}",
      ".ck-now{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border-radius:10px;background:#eef4ff}html.dark .ck-now{background:#161b26}",
      ".ck-now.live{background-image:linear-gradient(100deg,rgba(79,140,255,0) 35%,rgba(79,140,255,.18) 50%,rgba(79,140,255,0) 65%);background-size:240% 100%;animation:ckShimmer 2.6s linear infinite}",
      ".ck-dot{position:relative;flex:none;width:9px;height:9px;margin-top:5px;border-radius:50%;background:#4f8cff}",
      ".ck-now.live .ck-dot::after,#code-ck-rail .ck-live::after{content:'';position:absolute;inset:0;border-radius:50%;background:inherit;animation:ckHalo 1.6s ease-out infinite}",
      ".ck-row{display:flex;align-items:center;gap:10px;padding:5px 2px;min-width:0}",
      ".ck-id{flex:none;width:34px;font:500 12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:#6b7280}html.dark .ck-id{color:#8a8a94}",
      ".ck-now .ck-id{color:#2563eb;margin-right:6px}html.dark .ck-now .ck-id{color:#a9c8ff}",
      ".ck-label{flex:1;min-width:0}",
      ".ck-row.done .ck-label{color:#6b7280;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}html.dark .ck-row.done .ck-label{color:#a1a1aa}",
      ".ck-ev{flex:none;max-width:38%;font:400 11px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:#9ca3af;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}html.dark .ck-ev{color:#7c7c86}",
      ".ck-detail{color:#6b7280;font-size:12px;margin-top:3px}html.dark .ck-detail{color:#a1a1aa}",
      ".ck-label code,.ck-detail code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:#b45309}html.dark .ck-label code,html.dark .ck-detail code{color:#f0d9a8}",
      ".ck-ic{flex:none;width:14px;height:14px}",
      ".ck-pop{animation:ckPop .6s ease-out both}",
      ".ck-foot{margin-top:18px;color:#9ca3af;font-size:12px}html.dark .ck-foot{color:#7c7c86}",
      ".ck-empty{color:#6b7280;padding:24px 4px}html.dark .ck-empty{color:#a1a1aa}",
      "#code-ck-rail{position:fixed;top:96px;right:10px;z-index:59;display:flex;flex-direction:column;align-items:center;gap:3px;width:48px;padding:7px 0 6px;border-radius:12px;border:1px solid #e5e7eb;background:#fff;color:#111;box-shadow:0 4px 16px rgba(0,0,0,.08);cursor:pointer;transition:right .18s ease,background .15s}",
      "html.dark #code-ck-rail{background:#141418;border-color:#26262d;color:#e7e7ea;box-shadow:0 6px 20px rgba(0,0,0,.4)}",
      "#code-ck-rail:hover{background:#f3f4f6}html.dark #code-ck-rail:hover{background:#1c1c22}",
      "#code-ck-rail.on{background:#eef4ff;border-color:#bfd3ff}html.dark #code-ck-rail.on{background:#1a2333;border-color:#2b3a57}",
      "#code-ck-rail[hidden],#code-ck-rail .ck-live[hidden]{display:none}",
      "html.code-preview-open #code-ck-rail{right:calc(var(--code-preview-width,0px) + 10px)}",
      "html.code-narrow #code-ck-rail{top:var(--code-row2-top,50px);flex-direction:row;gap:5px;width:auto;height:30px;padding:0 10px 0 4px;border-radius:999px}",
      "html.code-narrow #code-ck-rail svg{width:22px;height:22px}html.code-narrow #code-ck-rail .ck-live{top:1px;right:1px;width:6px;height:6px}",
      "#code-ck-rail .ck-n{font:500 10px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:#6b7280}html.dark #code-ck-rail .ck-n{color:#a1a1aa}",
      "#code-ck-rail .ck-live{position:absolute;top:5px;right:5px;width:7px;height:7px;border-radius:50%;background:#4f8cff}",
      "#code-ck-rail .ck-arc-now{animation:cpPulse 1.6s ease-in-out infinite}",
      ".code-task-strip{box-sizing:border-box;margin:0 0 6px;padding:8px 14px 9px;border-radius:14px;border:1px solid #1f1f23;background:#0a0a0b;color:#e4e4e7;cursor:pointer;font:13px/1.45 Inter,system-ui,-apple-system,\"Segoe UI\",Roboto,sans-serif;transition:background .6s,border-color .6s}",
      ".code-task-strip:hover{border-color:#2b3a57}.code-task-strip:focus-visible{outline:2px solid #4f8cff;outline-offset:1px}",
      ".code-task-strip .cts-top{display:flex;align-items:baseline;gap:8px;min-width:0}",
      ".code-task-strip .cts-l{flex:none;font:600 10px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.12em;text-transform:uppercase;color:#6ea8ff}",
      ".code-task-strip .cts-n{flex:none;font:500 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:#85858f}",
      ".code-task-strip .cts-t{flex:1;min-width:0;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      ".code-task-strip .cts-d{margin-top:3px;color:#b4b4bb;font-size:12.5px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}",
      ".code-task-strip code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:#c8d3ff}",
      ".code-task-strip.cts-new{animation:ctsNew 1.6s ease-out}",
      "@keyframes ctsNew{0%{background:#1a2333;border-color:#4f8cff}100%{background:#0a0a0b;border-color:#1f1f23}}",
      "@keyframes ckShimmer{0%{background-position:160% 0}100%{background-position:-60% 0}}",
      "@keyframes ckSweep{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}",
      "@keyframes ckHalo{0%{transform:scale(.6);opacity:.7}100%{transform:scale(2.4);opacity:0}}",
      "@keyframes ckPop{0%{transform:scale(.4);opacity:0}60%{transform:scale(1.2);opacity:1}100%{transform:scale(1);opacity:1}}",
      "@media (prefers-reduced-motion:reduce){.ck-now.live,.ck-bar b::after,.ck-dot::after,#code-ck-rail .ck-live::after,.ck-pop,#code-ck-rail .ck-arc-now,.code-task-strip.cts-new{animation:none}}",
    ].join("");
    var s = document.createElement("style");
    s.id = "code-preview-style";
    s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
  }

  function previewPanel() {
    var panel = document.getElementById("code-preview-panel");
    if (panel) return panel;
    injectPreviewStyle();
    panel = document.createElement("div");
    panel.id = "code-preview-panel";
    var width = parseInt(safeGet("codeToggle:previewWidth") || "", 10);
    panel.style.width = Math.max(360, Math.min(isNaN(width) ? Math.round(window.innerWidth * 0.42) : width,
                                               window.innerWidth - 200)) + "px";
    panel.innerHTML =
      '<div class="cp-grip" title="Drag to resize"></div>' +
      '<div class="cp-head"><div class="cp-tabs">' +
      '<button class="cp-tab on" data-tab="preview" hidden><span class="cp-live"></span>Preview' +
      '<span class="cp-sub" title="Reloads by itself when the project changes">live</span></button>' +
      '<button class="cp-tab" data-tab="fixer" hidden><span class="cp-fixdot"></span>Fixer</button>' +
      '<button class="cp-tab" data-tab="features" hidden><span class="cp-featstar">\u2726</span>Features</button>' +
      '<button class="cp-tab" data-tab="checklist" hidden>Checklist<span class="cp-ck-count"></span></button>' +
      '<button class="cp-tab" data-tab="desktop" hidden>Desktop</button></div>' +
      '<button class="cp-btn cp-home" title="Back to the project">&#x2302;</button>' +
      '<button class="cp-btn cp-reload" title="Reload">&#x21bb;</button>' +
      '<button class="cp-btn cp-open" title="Open in a new tab">&#x2197;</button>' +
      '<button class="cp-btn cp-close" title="Close (Esc)">&#x2715;</button></div>' +
      '<div class="cp-down" hidden><b>The preview is not answering</b>' +
      '<span>The preview server on the coding machine gave no answer. Trying again every few seconds.</span></div>' +
      '<iframe class="cp-frame cp-frame-preview" title="Preview" sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-modals allow-downloads"></iframe>' +
      '<iframe class="cp-frame cp-frame-fixer" title="Fixer" sandbox="allow-scripts allow-same-origin allow-popups" hidden></iframe>' +
      '<iframe class="cp-frame cp-frame-features" title="Features" sandbox="allow-scripts allow-same-origin allow-popups allow-forms" hidden></iframe>' +
      '<div class="cp-frame cp-frame-checklist cp-ck" role="region" aria-label="Checklist" hidden></div>' +
      '<iframe class="cp-frame cp-frame-desktop" title="Desktop" sandbox="allow-scripts allow-same-origin allow-popups allow-forms" allow="clipboard-read; clipboard-write" hidden></iframe>';
    panel.querySelector(".cp-ck").addEventListener("click", function (e) {
      if (!e.target.closest || !e.target.closest(".ck-fold")) return;
      checklist.doneOpen = !checklist.doneOpen;
      renderChecklist(panel.querySelector(".cp-ck"));
    });
    Array.prototype.forEach.call(panel.querySelectorAll(".cp-tab"), function (tab) {
      tab.addEventListener("click", function () { setTab(tab.getAttribute("data-tab")); });
    });
    panel.querySelector(".cp-reload").addEventListener("click", function () {
      if (activeTab === "preview") return reloadPreview();
      if (activeTab === "checklist") { checklist.sig = ""; return refreshChecklist(); }
      var f = panel.querySelector(".cp-frame-" + activeTab);
      f.setAttribute("src", f.getAttribute("src"));
    });
    panel.querySelector(".cp-home").addEventListener("click", function () { showPreviewRoot(); });
    panel.querySelector(".cp-open").addEventListener("click", function () {
      var url = activeTab === "fixer" ? fixer.url : activeTab === "features" ? DEPLOY_ORIGIN + "/features/"
              : activeTab === "desktop" ? desk.view : preview.base;
      if (url) window.open(url, "_blank", "noopener");
    });
    panel.querySelector(".cp-close").addEventListener("click", function () { closePreview(true); });
    var grip = panel.querySelector(".cp-grip");
    grip.addEventListener("mousedown", function (e) {
      e.preventDefault();
      var frames = panel.querySelectorAll(".cp-frame");
      Array.prototype.forEach.call(frames, function (f) { f.style.pointerEvents = "none"; }); // keep the drag
      function move(ev) {
        panel.style.width = Math.max(320, Math.min(window.innerWidth - ev.clientX, window.innerWidth - 200)) + "px";
        squeezeChat(true);
      }
      function up() {
        Array.prototype.forEach.call(frames, function (f) { f.style.pointerEvents = ""; });
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        safeSet("codeToggle:previewWidth", String(parseInt(panel.style.width, 10)));
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
    document.body.appendChild(panel);
    return panel;
  }

  // The frame is never pointed at a preview server that does not answer: the browser would show its own error page
  // in it, and the "Try Again" button on Firefox's error page reloads the whole tab, chat included (2026-10-13, after
  // an agent's kill had taken the preview server down). So the server is asked first; while nothing answers, the
  // panel says so in its own words and keeps asking.
  var PREVIEW_PROBE_MS = 4000;
  var PREVIEW_RETRY_MS = 3000;

  function previewAnswers(base) {
    var ctl = typeof AbortController === "function" ? new AbortController() : null;
    var timer = ctl ? setTimeout(function () { ctl.abort(); }, PREVIEW_PROBE_MS) : 0;
    // no-cors: any answer at all counts, the server's own error pages included (they carry no CORS header)
    return _fetch(base + "__version", { mode: "no-cors", cache: "no-store", signal: ctl ? ctl.signal : undefined })
      .then(function () { return true; }, function () { return false; })
      .then(function (ok) { clearTimeout(timer); return ok; });
  }

  function previewDown(on) {
    var panel = document.getElementById("code-preview-panel");
    var cover = panel && panel.querySelector(".cp-down");
    preview.down = !!on;
    if (cover) cover.hidden = !on || activeTab !== "preview";
  }

  // Show `url` (the project, or one page of it) in the frame as soon as the preview server answers.
  function loadPreviewFrame(url) {
    var frame = previewPanel().querySelector(".cp-frame-preview");
    var base = preview.base;
    var seq = ++preview.seq;
    clearTimeout(preview.retry);
    previewAnswers(base).then(function (ok) {
      if (seq !== preview.seq || base !== preview.base) return;  // a newer load, or another chat, took over
      if (ok) {
        previewDown(false);
        frame.setAttribute("src", url);
      } else {
        previewDown(true);
        preview.retry = setTimeout(function () {
          if (seq === preview.seq && base === preview.base) loadPreviewFrame(url);
        }, PREVIEW_RETRY_MS);
      }
    });
  }

  // `page` is set when the link clicked points at one page of the project (the "Ideas board" link, .../ideas/):
  // the panel shows that page. The plain Preview link then goes back to the project itself.
  function openPreview(base, page) {
    if (!base) return;
    var panel = previewPanel();
    var frame = panel.querySelector(".cp-frame-preview");
    var other = preview.base !== base;
    preview.base = base;
    if (page && page !== base && page.indexOf(base) === 0) {
      preview.page = page;
      loadPreviewFrame(page);
    } else if (other || !frame.getAttribute("src") || preview.page || preview.down) {
      preview.page = null;
      loadPreviewFrame(base);
    }
    panel.querySelector('.cp-tab[data-tab="preview"]').hidden = false;
    preview.dismissed = false;
    setTab("preview");
    requestAnimationFrame(function () {
      panel.classList.add("open");
      squeezeChat(true);
    });
  }

  // While the panel is open the chat column gives up its width instead of being covered.
  function squeezeChat(on) {
    var root = document.documentElement;
    var panel = document.getElementById("code-preview-panel");
    if (on && panel) {
      root.style.setProperty("--code-preview-width", panel.getBoundingClientRect().width + "px");
      root.classList.add("code-preview-open");
    } else {
      root.classList.remove("code-preview-open");
    }
    scheduleReconcile(); // re-centre the pill in the narrower bar...
    setTimeout(scheduleReconcile, 260); // ...again once the padding transition (.18s) has finished
  }

  function showPreviewRoot() {
    preview.page = null;
    if (preview.base && document.getElementById("code-preview-panel")) loadPreviewFrame(preview.base + "?_r=" + Date.now());
  }

  function closePreview(byUser) {
    var panel = document.getElementById("code-preview-panel");
    if (panel) panel.classList.remove("open");
    var rail = document.getElementById("code-ck-rail");
    if (rail) rail.classList.remove("on");
    squeezeChat(false);
    if (byUser) preview.dismissed = true;  // until the next Code turn
  }

  function reloadPreview() {
    var panel = document.getElementById("code-preview-panel");
    var frame = panel && panel.querySelector(".cp-frame-preview");
    if (!frame || !preview.base) return;
    var base = preview.base;
    previewAnswers(base).then(function (ok) {
      if (base !== preview.base) return;
      if (!ok || preview.down || !frame.getAttribute("src")) return loadPreviewFrame(preview.page || base);
      try {
        frame.contentWindow.location.reload();  // keeps the page the user navigated to (Firefox allows it)
      } catch (e) {
        showPreviewRoot();  // cross-origin reload blocked (Chrome): reload the project root instead
      }
    });
  }

  function decoratePreviewLinks() {
    var links = previewLinks();
    for (var i = 0; i < links.length; i++) {
      var a = links[i];
      if (a.classList.contains("code-preview-chip")) continue;
      a.classList.add("code-preview-chip");
      a.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        var href = this.getAttribute("href");
        openPreview(previewBaseOf(href), href);
      }, true);
    }
    if (links.length) {
      var base = previewBaseOf(links[links.length - 1].getAttribute("href"));
      if (links.length > preview.links) preview.dismissed = false;  // a new Code turn started
      preview.links = links.length;
      if (base && base !== preview.base) {
        preview.base = base;
        preview.version = null;
      }
    }
  }

  // A reply can also name the chat's app by the address the user's browser can open:
  // http://<this Open WebUI's host>/previews/<chat id>/... (the Code pipe writes it where the agent wrote its own
  // localhost address). Clicked in its chat it opens in the panel, at that page; in a new tab, or from anywhere
  // else, the browser follows it to the forwarding service on the Open WebUI machine.
  var FRIENDLY_RE = /^https?:\/\/[^\/]+\/previews\/([A-Za-z0-9_-]{8,64})(?:\/(.*))?$/;

  function decorateFriendlyLinks() {
    var links = document.querySelectorAll('a[href*="/previews/"]:not([data-code-preview])');
    for (var i = 0; i < links.length; i++) {
      var a = links[i];
      if (!FRIENDLY_RE.test(a.getAttribute("href") || "")) continue;
      a.setAttribute("data-code-preview", "1");
      a.addEventListener("click", function (e) {
        var m = FRIENDLY_RE.exec(this.getAttribute("href") || "");
        if (!m || m[1] !== currentChatId || !preview.base) return;
        if (e.button || e.ctrlKey || e.metaKey || e.shiftKey) return;  // a new tab or window was asked for
        e.preventDefault();
        e.stopPropagation();
        openPreview(preview.base, preview.base + (m[2] || ""));
      }, true);
    }
  }

  async function pollPreview() {
    try {
      if (!preview.base || document.visibilityState !== "visible") return;
      var panel = document.getElementById("code-preview-panel");
      var open = panel && panel.classList.contains("open");
      if (!open && state.mode !== "code" && !wt.on) return;
      var r, base = preview.base;
      try {
        r = await _fetch(base + "__version", { cache: "no-store" });
      } catch (e) {
        // Nothing readable came back. If the server is gone, cover the frame before the browser's own error page
        // can show in it; the project loads again when the server is back.
        if (open && !preview.down && !(await previewAnswers(base)) && base === preview.base) {
          previewDown(true);
          loadPreviewFrame(preview.page || base);
        }
        return;
      }
      if (!r.ok) return;
      var v = await r.json();
      var live = panel && panel.querySelectorAll(".cp-live, .cp-sub");
      if (preview.version !== null && v.version !== preview.version) {
        if (live) {
          live.forEach(function (el) { el.classList.add("on"); });
          setTimeout(function () { live.forEach(function (el) { el.classList.remove("on"); }); }, 1200);
        }
        if (wt.on) wtPreviewChanged();
        if (open) reloadPreview();  // in the background too, if the Fixer tab is in front
        else if (!wt.on && !preview.dismissed && (v.entry || v.files > 0)) openPreview(preview.base);
      }
      preview.version = v.version;
    } catch (e) {
      warnOnce("preview-poll", e);
    }
  }

  function resetPreviewForChat() {
    closePreview(false);
    var panel = document.getElementById("code-preview-panel");
    if (panel) {
      panel.querySelector(".cp-frame-preview").removeAttribute("src");
      panel.querySelector(".cp-frame-fixer").removeAttribute("src");
      panel.querySelector('.cp-tab[data-tab="fixer"]').hidden = true;
      panel.querySelector('.cp-tab[data-tab="features"]').hidden = true; // the frame stays loaded: it is global
      panel.querySelector('.cp-tab[data-tab="preview"]').hidden = true;
      setTab("preview");
    }
    fixer.url = null;
    fixer.done = false;
    fixer.links = 0;
    fixer.since = Date.now();
    preview.base = null;
    preview.page = null;
    preview.links = 0;
    preview.version = null;
    preview.dismissed = false;
    preview.seq++;  // drops a load that is still waiting for the server
    clearTimeout(preview.retry);
    previewDown(false);
  }

  // -------------------------------------------------------------- checklist
  // The builder keeps a CHECKLIST.md (a feature chat's in its feature folder, a project chat's in the project's
  // .code-mode/): code-features parses it, code-deploy hands it to admins, and this page draws it in a Checklist tab
  // and a small button at the right edge. "Doing now" is the first box not ticked; it shimmers, and the button's dot
  // pulses, only while the chat's turn is running (Open WebUI's own task list says so).
  var CHECKLIST_POLL_MS = 5000;
  var CK_RING = 72.257;  // the button ring's circumference (r = 11.5)
  var checklist = { chat: null, data: null, working: false, at: 0, sig: "", ticked: null, doneOpen: true, busy: false };

  function ckEsc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function ckText(s) {  // escaped, with the builder's `code` spans and **bold** kept
    return ckEsc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  }

  function ckAgo(sec) {
    sec = Math.max(0, Math.round(sec));
    return sec < 60 ? sec + " s" : sec < 3600 ? Math.round(sec / 60) + " min" : Math.round(sec / 3600) + " h";
  }

  async function refreshChecklist() {
    if (checklist.busy || document.visibilityState === "hidden") return;
    var chat = currentChatId;
    var token = safeGet("token");
    if (!chat || !token) return setChecklist(chat, null, false);
    checklist.busy = true;
    try {
      var auth = { headers: { Authorization: "Bearer " + token }, cache: "no-store" };
      var r = await _fetch(DEPLOY_ORIGIN + "/api/checklist/" + encodeURIComponent(chat), auth);
      var data = r && r.ok ? await r.json() : null;
      var working = false;
      if (data && data.exists) {
        try {
          var t = await _fetch("/api/tasks/chat/" + encodeURIComponent(chat), auth);
          var tj = t && t.ok ? await t.json() : null;
          working = !!(tj && tj.task_ids && tj.task_ids.length);
        } catch (e) {
          working = false;
        }
      }
      if (chat === currentChatId) setChecklist(chat, data && data.exists ? data : null, working);
    } catch (e) {
      // code-deploy not answering: keep what is shown and ask again at the next poll
    } finally {
      checklist.busy = false;
      if (chat !== currentChatId) setTimeout(refreshChecklist, 0);  // the user moved on while it asked
    }
  }

  function setChecklist(chat, data, working) {
    if (chat !== checklist.chat) {
      checklist.ticked = null;
      checklist.sig = "";
    }
    checklist.chat = chat;
    checklist.data = data;
    checklist.working = !!working;
    checklist.at = Date.now();
    drawChecklist();
    try { if (wt.on) wtRender(); } catch (e) { warnOnce("watch-checklist", e); }
  }

  function checklistChatChanged() {
    setChecklist(currentChatId, null, false);
    refreshChecklist();
  }

  function checklistRail() {
    var b = document.getElementById("code-ck-rail");
    if (b) return b;
    injectPreviewStyle();
    b = document.createElement("button");
    b.id = "code-ck-rail";
    b.type = "button";
    b.hidden = true;
    b.innerHTML =
      '<svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true">' +
      '<circle cx="14" cy="14" r="11.5" fill="none" stroke="currentColor" stroke-opacity=".15" stroke-width="2.5"></circle>' +
      '<circle class="ck-arc" cx="14" cy="14" r="11.5" fill="none" stroke="#3ecf8e" stroke-width="2.5" stroke-linecap="round" transform="rotate(-90 14 14)"></circle>' +
      '<circle class="ck-arc-now" cx="14" cy="14" r="11.5" fill="none" stroke="#4f8cff" stroke-width="2.5" stroke-linecap="round" transform="rotate(-90 14 14)"></circle>' +
      '<path d="M9.5 11h9M9.5 14h9M9.5 17h6" fill="none" stroke="currentColor" stroke-opacity=".75" stroke-width="1.5" stroke-linecap="round"></path></svg>' +
      '<span class="ck-n"></span><span class="ck-live" hidden></span>';
    b.addEventListener("click", function () {
      var panel = document.getElementById("code-preview-panel");
      if (panel && panel.classList.contains("open") && activeTab === "checklist") closePreview(true);
      else openChecklist();
    });
    document.body.appendChild(b);
    return b;
  }

  function drawChecklist() {
    var d = checklist.data;
    var rail = checklistRail();
    var panel = document.getElementById("code-preview-panel");
    var tab = panel && panel.querySelector('.cp-tab[data-tab="checklist"]');
    var box = panel && panel.querySelector(".cp-ck");
    var shown = !!(panel && panel.classList.contains("open") && activeTab === "checklist");
    rail.classList.toggle("on", shown);
    if (!d) {
      rail.hidden = true;
      if (tab) tab.hidden = true;
      if (box && activeTab === "checklist") {
        checklist.sig = "";
        box.innerHTML = '<div class="ck-empty">No checklist in this chat yet. The builder writes one when it starts a ' +
          "bigger piece of work.</div>";
      }
      return;
    }
    var total = d.total || 0;
    var done = d.done || 0;
    var frac = total ? done / total : 0;
    var label = done + " of " + total + " done" + (checklist.working ? ", working" : "");
    rail.hidden = false;
    rail.setAttribute("aria-label", "Checklist: " + label);
    rail.title = "Checklist: " + label;
    rail.querySelector(".ck-n").textContent = done + "/" + total;
    rail.querySelector(".ck-arc").setAttribute("stroke-dasharray", (frac * CK_RING).toFixed(2) + " " + CK_RING);
    var nowArc = rail.querySelector(".ck-arc-now");
    var live = !!(checklist.working && d.now != null && total);
    nowArc.setAttribute("stroke-dasharray", live ? Math.max(1.5, CK_RING / total - 1.2).toFixed(2) + " " + CK_RING : "0 " + CK_RING);
    nowArc.setAttribute("stroke-dashoffset", live ? (-(frac * CK_RING + 0.6)).toFixed(2) : "0");
    rail.querySelector(".ck-live").hidden = !checklist.working;
    if (tab) {
      tab.hidden = false;
      tab.querySelector(".cp-ck-count").textContent = done + "/" + total;
    }
    if (box && activeTab === "checklist") renderChecklist(box);
    placeTaskStrip();
  }

  // ------------------------------------------------------------------- the task strip
  // Above the running live window: which checklist item the agent is on and what it is doing for it, from the two
  // "## Now" lines it keeps at the top of its CHECKLIST.md (code-features parses them into d.current). Without them,
  // the first open box, labelled as such. Only while a turn runs (the window says so: data-code-live, live.html).
  function placeTaskStrip() {
    var frame = runningLiveFrame();
    var d = checklist.data && checklist.chat === currentChatId ? checklist.data : null;
    var want = frame && d && d.total ? frame : null;
    Array.prototype.forEach.call(document.querySelectorAll(".code-task-strip"), function (x) {
      if (!want || x.nextElementSibling !== want) x.remove();
    });
    if (!want) return;
    var strip = want.previousElementSibling;
    if (!strip || !strip.classList || !strip.classList.contains("code-task-strip")) {
      strip = document.createElement("div");
      strip.className = "code-task-strip";
      strip.setAttribute("role", "button");
      strip.tabIndex = 0;
      strip.title = "From the agent's checklist (.code-mode/CHECKLIST.md): click to open it";
      strip.addEventListener("click", function () { openChecklist(); });
      strip.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openChecklist(); } });
      want.parentNode.insertBefore(strip, want);
    }
    var cur = d.current && d.current.task ? d.current : null;
    var first = d.now != null && d.items ? d.items[d.now] : null;
    var label = cur ? "Task" : first ? "First open item" : "Checklist";
    var task = cur ? cur.task : first ? (first.id ? first.id + " " : "") + first.label : "every item is done";
    var doing = cur && cur.doing ? cur.doing : "";
    var sig = JSON.stringify([d.done, d.total, label, task, doing]);
    if (strip.getAttribute("data-sig") === sig) return;
    var moved = strip.getAttribute("data-task") !== null && strip.getAttribute("data-task") !== task;
    strip.setAttribute("data-sig", sig);
    strip.setAttribute("data-task", task);
    strip.innerHTML = '<div class="cts-top"><span class="cts-l">' + ckEsc(label) + '</span><span class="cts-n">' +
      ckEsc(d.done + "/" + d.total) + '</span><span class="cts-t">' + ckText(task) + "</span></div>" +
      (doing ? '<div class="cts-d">' + ckText(doing) + "</div>" : "");
    if (moved) {  // a new task: a short flash so the change is noticed
      strip.classList.remove("cts-new");
      void strip.offsetWidth;
      strip.classList.add("cts-new");
    }
  }

  function renderChecklist(box) {
    var d = checklist.data;
    if (!box || !d) return;
    var items = d.items || [];
    var age = ckAgo((d.server_time || 0) - (d.updated || 0) + (Date.now() - checklist.at) / 1000);
    var sig = JSON.stringify([d.title, d.kind, d.feature, items, d.open, d.now, d.current, checklist.working, checklist.doneOpen]);
    if (sig === checklist.sig) {  // nothing new: only the age moves (drawing again would restart the animations)
      var a = box.querySelector(".ck-age");
      if (a) a.textContent = "updated " + age + " ago";
      return;
    }
    var key = function (it) { return (it.id || "") + "|" + it.label; };
    var before = checklist.ticked;
    var ticked = {};
    items.forEach(function (it) { if (it.done) ticked[key(it)] = 1; });
    checklist.ticked = ticked;
    checklist.sig = sig;
    var total = d.total || 0;
    var done = d.done || 0;
    var pct = total ? 100 * done / total : 0;
    var now = d.now != null ? items[d.now] : null;
    var todo = items.filter(function (it, i) { return !it.done && (i !== d.now || (d.current && d.current.task)); });
    var dones = items.filter(function (it) { return it.done; });
    var check = function (cls) {
      return '<svg class="ck-ic' + (cls || "") + '" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" ' +
        'stroke="#3ecf8e" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path></svg>';
    };
    var ring = '<svg class="ck-ic" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.5" fill="none" ' +
      'stroke="currentColor" stroke-opacity=".45" stroke-width="1.6"></circle></svg>';
    var html = '<div class="ck-title">' + ckText(d.title || "Checklist") + "</div>" +
      '<div class="ck-sub">' + (d.kind === "feature" ? "Feature #" + ckEsc(d.feature) : "This project") + "</div>" +
      '<div class="ck-bar"><i style="width:' + pct.toFixed(1) + '%"></i>' +
      (checklist.working && now && total ? '<b style="left:' + pct.toFixed(1) + "%;width:" + (100 / total).toFixed(1) + '%"></b>' : "") +
      "</div>" +
      '<div class="ck-meta"><span>' + done + " of " + total + " done" + (checklist.working ? " · working" : "") +
      '</span><span class="ck-age">updated ' + age + " ago</span></div>";
    var cur = d.current && d.current.task ? d.current : null;  // the builder's own "## Now" lines
    if (cur) {
      html += '<div class="ck-sec"><div class="ck-h">' + (checklist.working ? "DOING NOW" : "LAST ON") + "</div>" +
        '<div class="ck-now' + (checklist.working ? " live" : "") + '"><span class="ck-dot"></span><div class="ck-label">' +
        ckText(cur.task) + (cur.doing ? '<div class="ck-detail">' + ckText(cur.doing) + "</div>" : "") + "</div></div></div>";
    } else if (now) {
      html += '<div class="ck-sec"><div class="ck-h">' + (checklist.working ? "DOING NOW" : "NEXT UP") + "</div>" +
        '<div class="ck-now' + (checklist.working ? " live" : "") + '"><span class="ck-dot"></span><div class="ck-label">' +
        (now.id ? '<span class="ck-id">' + ckEsc(now.id) + "</span>" : "") + ckText(now.label) +
        (now.detail ? '<div class="ck-detail">' + ckText(now.detail) + "</div>" : "") + "</div></div></div>";
    } else if (total) {
      html += '<div class="ck-sec"><div class="ck-now">' + check() + '<div class="ck-label">All ' + total + " done</div></div></div>";
    }
    if (todo.length) {
      html += '<div class="ck-sec"><div class="ck-h">TO DO · ' + todo.length + "</div>" + todo.map(function (it) {
        return '<div class="ck-row">' + ring + '<span class="ck-id">' + ckEsc(it.id) + '</span><span class="ck-label">' +
          ckText(it.label) + "</span></div>";
      }).join("") + "</div>";
    }
    if (dones.length) {
      html += '<div class="ck-sec"><button type="button" class="ck-h ck-fold" aria-expanded="' + checklist.doneOpen + '">' +
        "<span>DONE · " + dones.length + '</span><span aria-hidden="true">' + (checklist.doneOpen ? "▾" : "▸") +
        "</span></button>" + (checklist.doneOpen ? dones.map(function (it) {
          var fresh = before && !before[key(it)];  // ticked since the last look: its check pops in
          var ev = it.sha || (it.detail || "").replace(/`|\*\*/g, "");
          return '<div class="ck-row done" title="' + ckEsc(it.detail || it.label) + '">' + check(fresh ? " ck-pop" : "") +
            '<span class="ck-id">' + ckEsc(it.id) + '</span><span class="ck-label">' + ckText(it.label) + "</span>" +
            (ev ? '<span class="ck-ev">' + ckEsc(ev) + "</span>" : "") + "</div>";
        }).join("") : "") + "</div>";
    }
    html += '<div class="ck-sec"><div class="ck-h">OPEN</div>' + ((d.open || []).length ? d.open.map(function (o) {
      return '<div class="ck-row"><span class="ck-label">' + ckText(o) + "</span></div>";
    }).join("") : '<div class="ck-row"><span class="ck-label ck-sub">None</span></div>') + "</div>" +
      '<div class="ck-foot">From CHECKLIST.md ' + (d.kind === "feature" ? "in the feature folder" : "in the project's .code-mode/") +
      "</div>";
    var top = box.scrollTop;
    box.innerHTML = html;
    box.scrollTop = top;
  }

  function openChecklist() {
    var panel = previewPanel();
    panel.querySelector('.cp-tab[data-tab="preview"]').hidden = !preview.base;
    setTab("checklist");
    requestAnimationFrame(function () {
      panel.classList.add("open");
      squeezeChat(true);
      drawChecklist();
    });
    refreshChecklist();
    return "ok";
  }

  // ----------------------------------------------------------------- fixer
  // A bug report opens the Fixer tab: the live view of Ornith investigating and fixing it. The Chat-mode tool
  // calls window.__codeToggle.openFixer(url); in Code mode the pipe puts a [Fixer](url) link in the reply.
  var FIXER_ORIGIN = window.__codeFixerOrigin || "http://ai-sandbox:4098";
  var fixer = { url: null, links: 0, since: Date.now() }; // since: when this chat's page started rendering
  var activeTab = "preview";

  function setTab(name) {
    var panel = document.getElementById("code-preview-panel");
    if (!panel) return;
    activeTab = name;
    Array.prototype.forEach.call(panel.querySelectorAll(".cp-tab"), function (t) {
      t.classList.toggle("on", t.getAttribute("data-tab") === name);
    });
    panel.querySelector(".cp-frame-preview").hidden = name !== "preview";
    panel.querySelector(".cp-frame-fixer").hidden = name !== "fixer";
    panel.querySelector(".cp-frame-features").hidden = name !== "features";
    panel.querySelector(".cp-frame-checklist").hidden = name !== "checklist";
    panel.querySelector(".cp-frame-desktop").hidden = name !== "desktop";
    panel.querySelector(".cp-home").hidden = name !== "preview";
    panel.querySelector(".cp-open").hidden = name === "checklist";  // it has no page of its own to open
    panel.querySelector(".cp-down").hidden = !preview.down || name !== "preview";
    drawChecklist();
  }

  function openFixer(url) {
    if (!url || url.indexOf(FIXER_ORIGIN + "/f/") !== 0) return "bad url";
    var panel = previewPanel();
    var tab = panel.querySelector('.cp-tab[data-tab="fixer"]');
    var frame = panel.querySelector(".cp-frame-fixer");
    tab.hidden = false;
    var dot = tab.querySelector(".cp-fixdot");
    if (fixer.url !== url) {
      frame.setAttribute("src", url);
      dot.classList.remove("ok");
      dot.classList.add("busy");
      fixer.done = false;
      fixer.verdict = "";
    }
    fixer.url = url;
    fixer.chat = currentChatId;   // Watch's stage shows it only in this chat
    updatePill();
    setTab("fixer");
    requestAnimationFrame(function () {
      panel.classList.add("open");
      squeezeChat(true);
    });
    return "ok";
  }

  function decorateFixerLinks() {
    var links = document.querySelectorAll('a[href^="' + FIXER_ORIGIN + '/f/"]');
    for (var i = 0; i < links.length; i++) {
      var a = links[i];
      if (a.classList.contains("code-fixer-chip")) continue;
      a.classList.add("code-fixer-chip");
      a.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        openFixer(this.getAttribute("href"));
      }, true);
    }
    // While a chat is still rendering, its old reports appear: only count them. Later ones are new: open them.
    if (links.length > fixer.links && Date.now() - fixer.since > 2500) openFixer(links[links.length - 1].getAttribute("href"));
    fixer.links = links.length;
  }

  // The viewer tells us when the fix is finished: the tab's dot stops pulsing. After a fix the preview reloads
  // with it applied and, once the verdict has been on screen a moment, the panel goes back to the Preview tab
  // while the chat carries on. Only the first "done" per report counts (the viewer replays it on every reload).
  window.addEventListener("message", function (e) {
    try {
      if (e.origin !== FIXER_ORIGIN || !e.data || e.data.type !== "fixer:status") return;
      var dot = document.querySelector('#code-preview-panel .cp-tab[data-tab="fixer"] .cp-fixdot');
      if (e.data.done && !fixer.done) {
        fixer.done = true;
        fixer.verdict = String(e.data.verdict || "");
        updatePill();
      }
      if (dot && e.data.done) {
        dot.classList.remove("busy");
        if (e.data.verdict === "fixed" || e.data.verdict === "not_a_bug") dot.classList.add("ok");
      }
      if (e.data.done && e.data.verdict === "fixed" && fixer.doneFor !== fixer.url) {
        fixer.doneFor = fixer.url;
        if (preview.base) {
          reloadPreview();
          setTimeout(function () { if (activeTab === "fixer") setTab("preview"); }, 4000);
        }
      }
    } catch (err) {
      warnOnce("fixer-message", err);
    }
  });

  window.__codeToggle.openFixer = openFixer;

  // ------------------------------------------------------------ Fixer button
  // The wrench after ✦ Feature: the user opens the fixer on this chat by hand, to look into something the models
  // didn't report. A small box asks what is wrong (it may stay empty: the fixer then reads the chat to find out) and
  // says which model the fixer will run on; Investigate files the report through the Code pipe (code_fixer ops,
  // which hold the fixer's token) and opens the Fixer tab. While a report for this chat runs the wrench pulses and
  // a click just shows it.
  var fixAsk = { open: false, seq: 0, busy: false, active: "", wired: false };

  function wrenchIcon(cls) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    if (cls) svg.setAttribute("class", cls);
    var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", WRENCH);
    svg.appendChild(path);
    return svg;
  }

  // ------------------------------------------------- Desktop and Upload buttons (Feature #31, pipe 0.30.0)
  // Desktop: the chat's own desktop (the sandbox viewer the live window shows during a turn) in the side panel, any
  // time - also after the turn that opened the browser for the user has ended and its live window has folded.
  // Upload: files or whole folders (folders kept) straight into the chat's project folder on the coding machine,
  // under uploads/ (code-live's PUT /u/..., kept out of git), so the agent reads them with its own tools instead of
  // Open WebUI's retrieval seeing pieces of them. A note for the model goes into the message box when they are there.
  var UP_MAX_TOTAL = 2 * 1024 * 1024 * 1024;   // one upload at a time, at most this much (the VM's disk is small)
  var UP_PARALLEL = 3;
  var desk = { chat: null, view: "", upload: "", folder: "uploads/", at: 0 };
  var up = { open: false, busy: false, seq: 0, wired: false, chat: null, files: 0, done: 0, total: 0, sent: 0,
             errors: [], result: "" };

  var DESK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>';
  var UP_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true"><path d="M12 15V4M7 9l5-5 5 5"/><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg>';

  /** One call to the pipe's desktop op: its {ok, ...} dict, or {ok: false, error}. code_github rides along so a pipe
   *  older than 0.30.0 answers "unknown op" instead of starting a coding turn. */
  function desktopCall(args) {
    var token = safeGet("token");
    return _fetch("/api/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + (token || "") },
      body: JSON.stringify({ model: CODE_MODEL, stream: false, messages: [{ role: "user", content: "desktop" }],
                             code_desktop: args, code_github: { op: "desktop" } })
    }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (d) {
      return d && typeof d === "object" && typeof d.ok === "boolean" ? d : { ok: false, error: "The Code pipe did not answer (is it 0.30.0 or newer?)." };
    }).catch(function () {
      return { ok: false, error: "The Code pipe did not answer." };
    });
  }

  /** The chat's desktop and upload links (asked once per chat, kept 10 minutes). */
  function deskLinks(chatId) {
    if (desk.chat === chatId && Date.now() - desk.at < 10 * 60 * 1000) return Promise.resolve(desk);
    return desktopCall({ op: "links", chat_id: chatId }).then(function (d) {
      if (!d.ok) return { error: d.error || "The desktop links failed." };
      desk = { chat: chatId, view: d.view || "", upload: d.upload || "", folder: d.folder || "uploads/", at: Date.now() };
      return desk;
    });
  }

  function deskNote(text) {
    var n = document.getElementById("code-desk-note");
    if (!n) {
      n = document.createElement("div");
      n.id = "code-desk-note";
      n.setAttribute("role", "status");
      document.body.appendChild(n);
    }
    n.textContent = text;
    var btn = document.querySelector("#" + PILL_ID + " .ct-desk") || document.getElementById(PILL_ID);
    if (btn) {
      var r = btn.getBoundingClientRect();
      n.style.top = Math.round(r.bottom + 8) + "px";
      n.style.left = Math.round(Math.max(16, Math.min(r.left, window.innerWidth - 376))) + "px";
    }
    n.hidden = false;
    clearTimeout(deskNote.timer);
    deskNote.timer = setTimeout(function () { n.hidden = true; }, 7000);
  }

  function openDesktop() {
    var chatId = currentChatId;
    if (!chatId) return;
    deskLinks(chatId).then(function (l) {
      if (chatId !== currentChatId) return;
      if (l.error) return deskNote(l.error);
      if (!l.view) return deskNote("The desktop isn't set up here (the Code pipe's VIEW_TOKEN / DESKTOP_URL valves).");
      var panel = previewPanel();
      var tab = panel.querySelector('.cp-tab[data-tab="desktop"]');
      var frame = panel.querySelector(".cp-frame-desktop");
      tab.hidden = false;
      if (frame.getAttribute("src") !== l.view) frame.setAttribute("src", l.view);
      setTab("desktop");
      requestAnimationFrame(function () {
        panel.classList.add("open");
        squeezeChat(true);
      });
    });
  }

  /** Another chat: its desktop is not this one's. */
  function deskChatChanged() {
    upClose();
    var panel = document.getElementById("code-preview-panel");
    if (!panel) return;
    var tab = panel.querySelector('.cp-tab[data-tab="desktop"]');
    var frame = panel.querySelector(".cp-frame-desktop");
    if (!tab || !frame || desk.chat === currentChatId) return;
    tab.hidden = true;
    frame.removeAttribute("src");
    if (activeTab === "desktop") closePreview(false);
  }

  // ---- Upload
  function upFmt(n) {
    return n >= 1073741824 ? (n / 1073741824).toFixed(1) + " GB" : n >= 1048576 ? (n / 1048576).toFixed(1) + " MB"
         : n >= 1024 ? Math.round(n / 1024) + " KB" : n + " B";
  }

  function upBox() {
    var box = document.getElementById("code-up-pop");
    if (box) return box;
    box = document.createElement("div");
    box.id = "code-up-pop";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", "Upload files to the coding machine");
    box.hidden = true;
    box.innerHTML =
      '<div class="up-title">' + UP_ICON + 'Upload to the coding machine</div>' +
      '<div class="up-where"></div>' +
      '<div class="up-btns"><button type="button" class="up-files">Files…</button>' +
      '<button type="button" class="up-folder">Folder…</button></div>' +
      '<div class="up-drop">or drop files and folders here</div>' +
      '<div class="up-prog" hidden><div class="up-bar"><i></i></div><div class="up-status"></div></div>' +
      '<div class="up-done" hidden></div>' +
      '<div class="up-err" hidden></div>' +
      '<input type="file" class="up-in-files" multiple hidden>' +
      '<input type="file" class="up-in-folder" multiple webkitdirectory directory hidden>';
    var inFiles = box.querySelector(".up-in-files");
    var inFolder = box.querySelector(".up-in-folder");
    box.querySelector(".up-files").addEventListener("click", function () { if (!up.busy) inFiles.click(); });
    box.querySelector(".up-folder").addEventListener("click", function () { if (!up.busy) inFolder.click(); });
    [inFiles, inFolder].forEach(function (input) {
      input.addEventListener("change", function () {
        var list = Array.prototype.map.call(input.files || [], function (f) {
          return { file: f, rel: f.webkitRelativePath || f.name };
        });
        input.value = "";
        upStart(list);
      });
    });
    var drop = box.querySelector(".up-drop");
    drop.addEventListener("dragover", function (e) { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", function () { drop.classList.remove("over"); });
    drop.addEventListener("drop", function (e) {
      e.preventDefault();
      drop.classList.remove("over");
      if (up.busy) return;
      upFromDrop(e.dataTransfer).then(upStart, function () { upDraw({ error: "That drop could not be read." }); });
    });
    document.body.appendChild(box);
    if (!up.wired) {
      up.wired = true;
      document.addEventListener("mousedown", function (e) {
        if (!up.open) return;
        var b = document.getElementById("code-up-pop");
        var btn = document.querySelector("#" + PILL_ID + " .ct-up");
        if ((b && b.contains(e.target)) || (btn && btn.contains(e.target))) return;
        upClose();
      }, true);
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && up.open && !e.codeHandled) { e.codeHandled = true; upClose(); }
      });
      window.addEventListener("resize", function () { if (up.open) upPlace(); });
    }
    return box;
  }

  function upPlace() {
    var box = document.getElementById("code-up-pop");
    var btn = document.querySelector("#" + PILL_ID + " .ct-up");
    if (!box || !btn) return;
    var r = btn.getBoundingClientRect();
    var width = Math.min(360, window.innerWidth - 32);
    box.style.width = width + "px";
    box.style.top = Math.round(r.bottom + 8) + "px";
    box.style.left = Math.round(Math.max(16, Math.min(r.left + r.width / 2 - width / 2, window.innerWidth - width - 16))) + "px";
  }

  /** busy / result / error drawn from `up`; s.error replaces the error line, s.where the destination line. */
  function upDraw(s) {
    var box = upBox();
    s = s || {};
    if (s.where !== undefined) box.querySelector(".up-where").innerHTML = s.where;
    box.querySelector(".up-files").disabled = up.busy;
    box.querySelector(".up-folder").disabled = up.busy;
    box.querySelector(".up-drop").hidden = up.busy;
    var prog = box.querySelector(".up-prog");
    prog.hidden = !up.busy;
    if (up.busy) {
      box.querySelector(".up-bar i").style.width = (up.total ? Math.round(100 * up.sent / up.total) : 0) + "%";
      box.querySelector(".up-status").textContent = "Uploading " + Math.min(up.done + 1, up.files) + " of " + up.files +
        " · " + upFmt(up.sent) + " of " + upFmt(up.total);
    }
    var done = box.querySelector(".up-done");
    done.hidden = up.busy || !up.result;
    done.textContent = up.result;
    var err = box.querySelector(".up-err");
    var text = s.error || (up.errors.length ? up.errors.slice(0, 8).join("\n") +
      (up.errors.length > 8 ? "\n…and " + (up.errors.length - 8) + " more" : "") : "");
    err.hidden = !text;
    err.textContent = text;
  }

  function upButton() {
    if (up.open) return upClose();
    upOpen();
  }

  function upOpen() {
    var chatId = currentChatId;
    if (!chatId) return;
    upBox();
    up.open = true;
    var seq = ++up.seq;
    if (up.chat !== chatId && !up.busy) { up.result = ""; up.errors = []; }
    upDraw({ where: "Finding this chat's folder…", error: "" });
    document.getElementById("code-up-pop").hidden = false;
    upPlace();
    updatePill();
    deskLinks(chatId).then(function (l) {
      if (seq !== up.seq || !up.open) return;
      if (l.error || !l.upload) {
        return upDraw({ where: "", error: l.error || "Uploads aren't set up here (the Code pipe's PREVIEW_SECRET / LIVE_URL valves)." });
      }
      upDraw({ where: "Into <code>" + l.folder + "</code> in this chat's project folder, where the agent can read them. " +
                      "Kept out of git. Folders keep their structure." });
    });
  }

  function upClose() {
    up.seq++;
    var box = document.getElementById("code-up-pop");
    if (box) box.hidden = true;   // an upload under way goes on; the button pulses until it is done
    if (up.open) {
      up.open = false;
      updatePill();
    }
  }

  /** Files and folders dropped on the box: every file with its path, folders walked (Chrome, Firefox, Safari). */
  function upFromDrop(dt) {
    var items = dt && dt.items ? Array.prototype.slice.call(dt.items) : [];
    var entries = items.map(function (it) { return it.webkitGetAsEntry ? it.webkitGetAsEntry() : null; })
                       .filter(function (e) { return !!e; });
    if (!entries.length) {
      return Promise.resolve(Array.prototype.map.call((dt && dt.files) || [], function (f) { return { file: f, rel: f.name }; }));
    }
    var out = [];
    function walk(entry, prefix) {
      if (entry.isFile) {
        return new Promise(function (resolve) {
          entry.file(function (f) { out.push({ file: f, rel: prefix + f.name }); resolve(); }, function () { resolve(); });
        });
      }
      if (!entry.isDirectory) return Promise.resolve();
      var reader = entry.createReader();
      var kids = [];
      return new Promise(function (resolve) {
        (function more() {   // readEntries hands out a folder in batches: read until an empty one
          reader.readEntries(function (batch) {
            if (!batch.length) return resolve();
            kids = kids.concat(Array.prototype.slice.call(batch));
            more();
          }, function () { resolve(); });
        })();
      }).then(function () {
        return kids.reduce(function (p, kid) {
          return p.then(function () { return walk(kid, prefix + entry.name + "/"); });
        }, Promise.resolve());
      });
    }
    return entries.reduce(function (p, e) { return p.then(function () { return walk(e, ""); }); }, Promise.resolve())
      .then(function () { return out; });
  }

  function upPut(url, item, onProgress) {
    return new Promise(function (resolve) {
      var xhr = new XMLHttpRequest();
      xhr.open("PUT", url + "?path=" + encodeURIComponent(item.rel));
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      if (xhr.upload) xhr.upload.onprogress = function (e) { if (e.lengthComputable) onProgress(e.loaded); };
      xhr.onload = function () {
        var d = null;
        try { d = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
        if (xhr.status === 201) return resolve({ ok: true });
        resolve({ ok: false, error: item.rel + ": " + ((d && d.error) || "the coding machine said " + xhr.status) });
      };
      xhr.onerror = function () { resolve({ ok: false, error: item.rel + ": the coding machine could not be reached" }); };
      xhr.send(item.file);
    });
  }

  /** "report.pdf, data/ (37 files)": what was uploaded, by its top-level names. */
  function upSummary(list) {
    var tops = {}, order = [];
    list.forEach(function (it) {
      var cut = it.rel.indexOf("/");
      var top = cut >= 0 ? it.rel.slice(0, cut + 1) : it.rel;
      if (!tops[top]) { tops[top] = 0; order.push(top); }
      tops[top]++;
    });
    var names = order.slice(0, 6).map(function (t) {
      return "`" + t + "`" + (t.slice(-1) === "/" ? " (" + tops[t] + " file" + (tops[t] === 1 ? "" : "s") + ")" : "");
    });
    return names.join(", ") + (order.length > 6 ? " and " + (order.length - 6) + " more" : "");
  }

  function upStart(list) {
    var chatId = currentChatId;
    if (up.busy || !chatId || !list || !list.length) return;
    list = list.filter(function (it) { return it && it.file && it.rel; });
    var total = list.reduce(function (n, it) { return n + (it.file.size || 0); }, 0);
    if (total > UP_MAX_TOTAL) return upDraw({ error: "That is " + upFmt(total) + "; one upload can be at most " + upFmt(UP_MAX_TOTAL) + "." });
    up.busy = true;
    up.chat = chatId;
    up.files = list.length;
    up.done = 0;
    up.total = total;
    up.sent = 0;
    up.errors = [];
    up.result = "";
    upDraw({ error: "" });
    updatePill();
    deskLinks(chatId).then(function (l) {
      if (l.error || !l.upload) {
        up.busy = false;
        updatePill();
        return upDraw({ error: l.error || "Uploads aren't set up here." });
      }
      var next = 0, per = list.map(function () { return 0; });
      var ok = [];
      function one() {
        if (next >= list.length) return Promise.resolve();
        var i = next++;
        var item = list[i];
        return upPut(l.upload, item, function (loaded) {
          up.sent += loaded - per[i];
          per[i] = loaded;
          upDraw();
        }).then(function (res) {
          up.sent += (item.file.size || 0) - per[i];
          per[i] = item.file.size || 0;
          up.done++;
          if (res.ok) ok.push(item); else up.errors.push(res.error);
          upDraw();
          return one();
        });
      }
      var lanes = [];
      for (var k = 0; k < Math.min(UP_PARALLEL, list.length); k++) lanes.push(one());
      return Promise.all(lanes).then(function () {
        up.busy = false;
        var size = ok.reduce(function (n, it) { return n + (it.file.size || 0); }, 0);
        up.result = ok.length ? "Uploaded " + ok.length + " file" + (ok.length === 1 ? "" : "s") + " (" + upFmt(size) +
          ") into " + l.folder + (up.errors.length ? "; " + up.errors.length + " failed" : "") + "." : "";
        upDraw();
        updatePill();
        if (ok.length && chatId === currentChatId) upTellModel(ok, size, l.folder);
      });
    });
  }

  /** The model learns about the files from the message: a line in the box, the user's text kept. */
  function upTellModel(ok, size, folder) {
    var box = document.getElementById("chat-input");
    if (!box) return;
    var editable = editorOf(box);
    var line = "I uploaded " + ok.length + " file" + (ok.length === 1 ? "" : "s") + " (" + upFmt(size) + ") to `" + folder +
      "` in your project folder: " + upSummary(ok) + ". Read them from there.";
    try {
      typeInto(editable, (boxText(editable).trim() ? " " : "") + line);
    } catch (e) {
      warnOnce("upload-note", e);
    }
  }

  function fixerRunning() {
    return !!(fixer && fixer.url && !fixer.done);
  }

  /** One call to the pipe's Fixer ops: its {ok, ...} dict, or {ok: false, error} when the request itself failed.
   *  code_github rides along so a pipe older than 0.25.0 answers "unknown op" instead of starting a coding turn. */
  function fixerCall(args) {
    var token = safeGet("token");
    return _fetch("/api/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + (token || "") },
      body: JSON.stringify({ model: CODE_MODEL, stream: false, messages: [{ role: "user", content: "fixer" }],
                             code_fixer: args, code_github: { op: "fixer" } })
    }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (d) {
      return d && typeof d === "object" && typeof d.ok === "boolean" ? d : { ok: false, error: "The Fixer request failed." };
    }).catch(function () {
      return { ok: false, error: "The Fixer request failed." };
    });
  }

  function fixerButton() {
    if (fixAsk.open) return fixAskClose();
    if (fixerRunning()) {  // this page already shows a report that is still going: bring it up
      openFixer(fixer.url);
      return;
    }
    fixAskOpen();
  }

  function fixAskBox() {
    var box = document.getElementById("fixer-ask");
    if (box) return box;
    box = document.createElement("div");
    box.id = "fixer-ask";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", "Fixer");
    box.hidden = true;
    var title = document.createElement("div");
    title.className = "fx-title";
    title.appendChild(wrenchIcon(""));
    title.appendChild(document.createTextNode("Fixer"));
    var running = document.createElement("div");
    running.className = "fx-running";
    running.textContent = "The fixer is already looking into this chat.";
    var text = document.createElement("textarea");
    text.className = "fx-text";
    text.rows = 4;
    text.placeholder = "What went wrong? Leave it empty and it reads the chat to find out.";
    var note = document.createElement("div");
    note.className = "fx-dim fx-note";
    note.textContent = "It reads this chat, its coding steps and the service logs, then fixes what it can reach: " +
      "this project, the desktop tool and the preview.";
    var model = document.createElement("div");
    model.className = "fx-dim fx-model";
    var error = document.createElement("div");
    error.className = "fx-error";
    error.hidden = true;
    var foot = document.createElement("div");
    foot.className = "fx-foot";
    var last = document.createElement("button");
    last.type = "button";
    last.className = "fx-link fx-last";
    last.textContent = "Last report";
    last.addEventListener("click", function () {
      var url = fixer.url;
      fixAskClose();
      if (url) openFixer(url);
    });
    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "fx-link fx-cancel";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", function () { fixAskClose(); });
    var go = document.createElement("button");
    go.type = "button";
    go.className = "fx-go";
    go.textContent = "Investigate";
    go.addEventListener("click", function () { fixAskGo(); });
    foot.appendChild(last);
    foot.appendChild(cancel);
    foot.appendChild(go);
    [title, running, text, note, model, error, foot].forEach(function (n) { box.appendChild(n); });
    box.addEventListener("keydown", function (e) {
      e.stopPropagation();  // Open WebUI's own shortcuts stay out of this box
      if (e.key === "Escape") fixAskClose();
      else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); fixAskGo(); }
    });
    document.body.appendChild(box);
    if (!fixAsk.wired) {
      fixAsk.wired = true;
      document.addEventListener("mousedown", function (e) {
        if (!fixAsk.open) return;
        var b = document.getElementById("fixer-ask");
        var btn = document.querySelector("#" + PILL_ID + " .ct-fix");
        if ((b && b.contains(e.target)) || (btn && btn.contains(e.target))) return;
        fixAskClose();
      }, true);
      window.addEventListener("resize", function () { if (fixAsk.open) fixAskPlace(); });
    }
    return box;
  }

  /** Hangs under the wrench, inside the window. */
  function fixAskPlace() {
    var box = document.getElementById("fixer-ask");
    var btn = document.querySelector("#" + PILL_ID + " .ct-fix");
    if (!box || !btn) return;
    var r = btn.getBoundingClientRect();
    var width = Math.min(380, window.innerWidth - 32);
    box.style.width = width + "px";
    box.style.top = Math.round(r.bottom + 8) + "px";
    box.style.left = Math.round(Math.max(16, Math.min(r.left + r.width / 2 - width / 2, window.innerWidth - width - 16))) + "px";
  }

  /** active: a report for this chat is still going (the box then only offers to show it). */
  function fixAskDraw(state) {
    var box = fixAskBox();
    var active = !!fixAsk.active;
    box.querySelector(".fx-running").hidden = !active;
    box.querySelector(".fx-text").hidden = active;
    box.querySelector(".fx-note").hidden = active;
    box.querySelector(".fx-last").hidden = active || !fixer.url;
    var go = box.querySelector(".fx-go");
    go.textContent = active ? "Show it" : fixAsk.busy ? "Starting…" : "Investigate";
    go.disabled = fixAsk.busy;
    var model = box.querySelector(".fx-model");
    if (state && "model" in state) {
      model.hidden = active || !state.model;
      model.className = "fx-model " + (state.loaded === false ? "fx-warn" : "fx-dim");
      model.textContent = state.loaded === false
        ? "Nothing it can use is loaded, so this loads " + state.model + ": the model in your picker goes out."
        : state.loaded ? "Runs on " + state.model + ", already loaded." : "Runs on " + state.model + ".";
    }
    var err = box.querySelector(".fx-error");
    err.textContent = (state && state.error) || "";
    err.hidden = !(state && state.error);
  }

  function fixAskOpen() {
    var chatId = currentChatId;
    if (!chatId) return;
    var box = fixAskBox();
    fixAsk.open = true;
    fixAsk.active = "";
    fixAsk.busy = false;
    var seq = ++fixAsk.seq;
    fixAskDraw({ model: "", error: "" });
    var model = box.querySelector(".fx-model");
    model.hidden = false;
    model.className = "fx-model fx-dim";
    model.textContent = "Checking which model it runs on…";
    box.hidden = false;
    fixAskPlace();
    updatePill();
    var text = box.querySelector(".fx-text");
    try { text.focus(); } catch (e) { /* not focusable yet */ }
    fixerCall({ op: "status", chat_id: chatId }).then(function (d) {
      if (seq !== fixAsk.seq || !fixAsk.open) return;
      if (!d.ok) return fixAskDraw({ model: "", error: d.error });
      fixAsk.active = d.active || "";
      fixAskDraw({ model: d.model || "", loaded: d.loaded, error: "" });
    });
  }

  function fixAskClose() {
    if (!fixAsk) return;
    fixAsk.seq++;
    fixAsk.busy = false;
    var box = document.getElementById("fixer-ask");
    if (box) box.hidden = true;  // what was typed stays for the next open, until a report goes out
    if (fixAsk.open) {
      fixAsk.open = false;
      updatePill();
    }
  }

  function fixAskGo() {
    var chatId = currentChatId;
    if (!fixAsk.open || fixAsk.busy || !chatId) return;
    if (fixAsk.active) {
      var url = fixAsk.active;
      fixAskClose();
      openFixer(url);
      return;
    }
    var box = fixAskBox();
    var text = box.querySelector(".fx-text");
    fixAsk.busy = true;
    var seq = fixAsk.seq;
    fixAskDraw({ error: "" });
    fixerCall({ op: "report", chat_id: chatId, details: text.value }).then(function (d) {
      if (seq !== fixAsk.seq || !fixAsk.open) return;
      fixAsk.busy = false;
      if (!d.ok) return fixAskDraw({ error: d.error });
      if (openFixer(d.view) !== "ok") {
        return fixAskDraw({ error: "The fixer started, but this page can't open its address: " + d.view });
      }
      text.value = "";
      fixAskClose();
    });
  }

  // ------------------------------------------------------- approval / question box
  // Approvals ("the agent wants to run rm -rf build") and the agent's questions wait in this box, not in Open WebUI's
  // pop-up: that one went only to the tab that sent the message and was gone for good after a reload or a click to
  // another chat, while the turn waited for it forever (2026-10-26). The Code pipe lists what waits in this user's
  // Code chats (code_ask {op: list}); every tab and browser asks every few seconds, shows the one for the open chat
  // as a dialog and the others as a notice with a link to their chat. Later (or Esc) folds the dialog into a pill
  // above the message box; a reload or another browser shows it again. Answers go back the same way (op: answer).
  var QB_ID = "code-qb", QB_PILL = "code-qb-pill", QB_NOTE = "code-qb-note";
  var QB_FAST_MS = 2500, QB_SLOW_MS = 10000, QB_HIDDEN_MS = 30000, QB_OFF_MS = 60000;
  var qb = { items: [], busy: false, again: false, timer: 0, hidden: {}, sent: {}, shown: "", gone: {}, off: false,
             started: false };
  var QB_SHIELD = '<svg class="qb-ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 4.5 6v5.5c0 4.6 3.1 8.3 7.5 9.5 4.4-1.2 7.5-4.9 7.5-9.5V6z"></path><path d="M12 8v5"></path><path d="M12 16.2v.1"></path></svg>';
  var QB_ASK = '<svg class="qb-ic" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M9.6 9.3a2.5 2.5 0 1 1 3.3 2.4c-.6.2-.9.8-.9 1.4v.4"></path><path d="M12 16.6v.1"></path></svg>';

  /** One call to the pipe's code_ask ops: its {ok, ...} dict, or {ok: false, error} when the request failed.
   *  code_github rides along so a pipe older than 0.28.0 answers "unknown op" instead of starting a coding turn. */
  function qbCall(args) {
    var token = safeGet("token");
    return _fetch("/api/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + (token || "") },
      body: JSON.stringify({ model: CODE_MODEL, stream: false, messages: [{ role: "user", content: "ask" }],
                             code_ask: args, code_github: { op: "ask" } })
    }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (d) {
      return d && typeof d === "object" && typeof d.ok === "boolean" ? d : { ok: false, error: "The request failed." };
    }).catch(function () {
      return { ok: false, error: "The request failed." };
    });
  }

  function qbStart() {
    if (qb.started) return;
    qb.started = true;
    document.addEventListener("visibilitychange", function () { if (!document.hidden) qbPoll(); });
    window.addEventListener("focus", function () { qbPoll(); });
    window.addEventListener("keydown", qbKey, true);   // window capture: before Watch's and the overlays' Esc
    qbPoll();
  }

  function qbSchedule() {
    clearTimeout(qb.timer);
    var ms = document.hidden ? QB_HIDDEN_MS : qb.off ? QB_OFF_MS
      : qb.items.length || runningLiveFrame() ? QB_FAST_MS : QB_SLOW_MS;
    qb.timer = setTimeout(qbPoll, ms);
  }

  function qbPoll() {
    clearTimeout(qb.timer);
    if (qb.busy) { qb.again = true; return; }   // asked again mid-call (focus, an answer): once more right after
    if (!safeGet("token")) { qbSchedule(); return; }
    qb.busy = true;
    qb.again = false;
    var done = function () {
      qb.busy = false;
      if (qb.again) { qb.again = false; qbPoll(); return true; }
      return false;
    };
    qbCall({ op: "list" }).then(function (r) {
      if (done()) return;
      if (!r.ok) {
        qb.off = /unknown/i.test(r.error || "");   // an older pipe: ask rarely until it is updated
        qbSchedule();
        return;
      }
      qb.off = false;
      var now = Date.now();
      Object.keys(qb.sent).forEach(function (k) { if (now - qb.sent[k] > 60000) delete qb.sent[k]; });
      qb.items = (r.asks || []).filter(function (a) { return a && a.id && !qb.sent[a.id]; });
      try { qbRender(); } catch (e) { warnOnce("ask-box", e); }
      qbSchedule();
    }, function () {
      if (!done()) qbSchedule();
    });
  }

  function qbMine() {
    return qb.items.filter(function (a) { return a.chat_id === currentChatId; });
  }

  function qbRender() {
    var mine = qbMine(), cur = null;
    for (var i = 0; i < mine.length; i++) if (!qb.hidden[mine[i].id]) { cur = mine[i]; break; }
    if (cur) qbShow(cur, mine.indexOf(cur) + 1, mine.length);
    else qbClose();
    qbRenderPill(cur ? [] : mine);
    qbRenderNote(qb.items.filter(function (a) { return a.chat_id !== currentChatId && !qb.gone[a.id]; }));
  }

  function qbInjectStyle() {
    cmpInjectStyle();
    rvInjectStyle();
    if (document.getElementById("code-qb-style")) return;
    var css = [
      "#code-qb{z-index:85}",
      "#code-qb .cmp-card{max-width:620px;gap:14px;border-color:#3a3226}",
      "#code-qb[data-kind=question] .cmp-card{border-color:#263247}",
      ".qb-ic{width:24px;height:24px;flex:none;fill:none;stroke:#e5b567;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}",
      "#code-qb[data-kind=question] .qb-ic{stroke:#6ea8ff}",
      ".qb-n{font:500 12px ui-monospace,monospace;color:#8a8a95}",
      ".qb-what{display:flex;align-items:center;gap:8px;font-size:13px;color:#a6a6b0}",
      ".qb-act{padding:2px 8px;border-radius:6px;font:600 11px ui-monospace,monospace;letter-spacing:.04em;background:rgba(229,181,103,.14);color:#f0c987}",
      ".qb-res{margin:0;max-height:38vh;overflow:auto;padding:12px 14px;border-radius:12px;background:#0b0b0d;border:1px solid #23232a;font:12.5px/1.55 ui-monospace,\"JetBrains Mono\",Menlo,monospace;color:#e8e8ee;white-space:pre-wrap;word-break:break-word}",
      ".qb-res div{padding-left:14px;text-indent:-14px}",
      ".qb-res div::before{content:'$ ';color:#6e6e78}",
      ".qb-q{white-space:pre-wrap;font-size:14px;line-height:1.5;color:#ececf1}",
      ".qb-opts{display:flex;flex-direction:column;gap:8px}",
      ".qb-opt{display:flex;align-items:baseline;gap:10px;width:100%;text-align:left;padding:10px 12px;border-radius:12px;border:1px solid #2a2a30;background:#16161a;color:#ececf1;font:500 13.5px Inter,system-ui,sans-serif;cursor:pointer}",
      ".qb-opt:hover{background:#1e1e24;border-color:#3b4a66}",
      ".qb-opt i{font:500 11px ui-monospace,monospace;color:#8a8a95;font-style:normal;flex:none}",
      ".qb-opt small{display:block;margin-top:2px;font-weight:400;font-size:12px;color:#8a8a95}",
      ".qb-opt em{font-style:normal;margin-left:6px;padding:1px 6px;border-radius:5px;font:600 10px ui-monospace,monospace;background:rgba(110,168,255,.14);color:#8fbaff}",
      ".qb-own{display:flex;gap:8px}",
      ".qb-in,.qb-txt{flex:1;min-width:0;box-sizing:border-box;padding:9px 12px;border-radius:10px;border:1px solid #2a2a30;background:#0b0b0d;color:#ececf1;font:13.5px Inter,system-ui,sans-serif;outline:none}",
      ".qb-in:focus,.qb-txt:focus{border-color:#4a5d80}",
      ".qb-txt{width:100%;resize:vertical;min-height:64px}",
      ".qb-field{display:flex;flex-direction:column;gap:6px}",
      ".qb-field b{font-size:13px;font-weight:600}",
      ".qb-chips{display:flex;flex-wrap:wrap;gap:6px}",
      ".qb-chips button{padding:4px 10px;border-radius:99px;border:1px solid #2a2a30;background:#16161a;color:#c9c9d1;font:12px Inter,system-ui,sans-serif;cursor:pointer}",
      ".qb-deny{display:flex;flex-direction:column;gap:6px}.qb-deny[hidden]{display:none}",
      ".qb-lbl{font-size:12.5px;color:#c9c9d1}.qb-hint{font-size:11.5px;color:#8a8a95}",
      ".qb-err{font-size:12.5px;color:#f4a3a3}.qb-err[hidden]{display:none}",
      ".qb-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}",
      ".qb-sp{flex:1}",
      ".qb-b{height:36px;padding:0 16px;border-radius:10px;border:1px solid #2a2a30;background:#18181c;color:#ececf1;font:600 13px Inter,system-ui,sans-serif;cursor:pointer}",
      ".qb-b:hover{background:#222228}",
      ".qb-b:disabled{opacity:.5;cursor:default}",
      ".qb-yes{background:#e5b567;border-color:#e5b567;color:#141414}.qb-yes:hover{background:#f0c987}",
      ".qb-send{background:#6ea8ff;border-color:#6ea8ff;color:#0d0d0f}.qb-send:hover{background:#8fbaff}",
      ".qb-stop{border-color:#5a2a2a;color:#f4a3a3}.qb-stop:hover{background:#2a1717}",
      ".qb-pill i{width:8px;height:8px;border-radius:50%;background:#e5b567;flex:none;animation:qb-blink 1.4s ease-in-out infinite}",
      "@keyframes qb-blink{50%{opacity:.25}}",
      "#code-qb-note{position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:86;display:flex;flex-direction:column;gap:6px;max-width:calc(100vw - 32px)}",
      ".qb-note{display:flex;align-items:center;gap:10px;padding:8px 10px 8px 14px;border-radius:99px;background:#16130d;border:1px solid #4a3c22;color:#f0dcb0;font:500 13px Inter,system-ui,sans-serif;box-shadow:0 10px 30px rgba(0,0,0,.45);animation:cmp-pop .35s ease both}",
      ".qb-note i{width:8px;height:8px;border-radius:50%;background:#e5b567;flex:none;animation:qb-blink 1.4s ease-in-out infinite}",
      ".qb-note span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}",
      ".qb-note a{flex:none;padding:4px 12px;border-radius:99px;background:#e5b567;color:#141414;font-weight:600;text-decoration:none}",
      ".qb-note button{flex:none;width:26px;height:26px;border-radius:50%;border:0;background:transparent;color:#a6a6b0;font-size:16px;line-height:1;cursor:pointer}",
      "@media (max-width:560px){#code-qb .cmp-card{padding:16px}.qb-own{flex-direction:column}}",
    ].join("\n");
    var s = document.createElement("style");
    s.id = "code-qb-style";
    s.textContent = css;
    document.head.appendChild(s);
  }

  function qbShow(a, n, total) {
    qbInjectStyle();
    var ov = document.getElementById(QB_ID);
    if (!ov || qb.shown !== a.id) {
      if (ov) ov.remove();
      ov = qbBuild(a);
      qb.shown = a.id;
      var act = document.activeElement;   // take the keys only when the user is not typing somewhere
      if (!act || act === document.body || !/^(INPUT|TEXTAREA)$/.test(act.tagName) && !act.isContentEditable) {
        try { ov.querySelector(".cmp-card").focus(); } catch (e) { /* fine */ }
      }
    }
    setText(ov.querySelector(".qb-n"), total > 1 ? n + " of " + total : "");
  }

  function qbClose() {
    var ov = document.getElementById(QB_ID);
    if (ov) ov.remove();
    qb.shown = "";
  }

  function qbBuild(a) {
    var ov = cmpEl("div", "cmp-ov");
    ov.id = QB_ID;
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-modal", "true");
    ov.setAttribute("data-kind", a.kind === "permission" ? "permission" : "question");
    var d = a.data || {};
    var where = a.title ? "in “" + a.title + "”" : "";
    if (a.kind === "permission") {
      ov.setAttribute("aria-label", "Approval needed");
      ov.innerHTML =   // fixed markup only: the agent's commands and the chat's title go in with textContent
        '<div class="cmp-back"></div><div class="cmp-card" tabindex="-1">' +
        '<div class="cmp-hd">' + QB_SHIELD + '<div class="cmp-tt"><div class="cmp-title">Approval needed</div><div class="cmp-sub"></div></div>' +
        '<span class="qb-n"></span><button type="button" class="cmp-hide qb-later" title="Answer later: a pill above the message box brings it back, and so does reloading or opening the chat anywhere else">Later</button></div>' +
        '<div class="qb-what"><span class="qb-act"></span><span>The agent wants to run this. It waits until you answer.</span></div>' +
        '<div class="qb-res"></div>' +
        '<div class="qb-deny" hidden><label class="qb-lbl">Tell the agent why, or what to do instead (optional)</label>' +
        '<textarea class="qb-txt" rows="3" placeholder="e.g. keep the build folder, just delete build/cache"></textarea>' +
        '<div class="qb-hint">With a note the agent reads it and carries on. Empty = the agent stops here.</div></div>' +
        '<div class="qb-err" hidden></div>' +
        '<div class="qb-row"><button type="button" class="qb-b qb-no">Deny…</button><span class="qb-sp"></span>' +
        '<button type="button" class="qb-b qb-yes">Allow once</button></div></div>';
      setText(ov.querySelector(".cmp-sub"), "Code mode " + where);
      setText(ov.querySelector(".qb-act"), String(d.action || "action"));
      var res = ov.querySelector(".qb-res");
      (d.resources && d.resources.length ? d.resources : ["(no details)"]).forEach(function (r) {
        res.appendChild(cmpEl("div", "", String(r)));
      });
      var deny = ov.querySelector(".qb-deny"), txt = ov.querySelector(".qb-txt"), no = ov.querySelector(".qb-no"),
          yes = ov.querySelector(".qb-yes");
      var denyLabel = function () { setText(yes, txt.value.trim() ? "Deny with this note" : "Deny and stop"); };
      no.addEventListener("click", function () {
        if (deny.hidden) {
          deny.hidden = false;
          setText(no, "Back");
          yes.classList.remove("qb-yes");
          yes.classList.add("qb-stop");
          denyLabel();
          try { txt.focus(); } catch (e) { /* fine */ }
        } else {
          deny.hidden = true;
          setText(no, "Deny…");
          yes.classList.add("qb-yes");
          yes.classList.remove("qb-stop");
          setText(yes, "Allow once");
        }
      });
      txt.addEventListener("input", denyLabel);
      yes.addEventListener("click", function () {
        qbAnswer(a, deny.hidden ? { allow: true } : { allow: false, message: txt.value.trim() });
      });
    } else if (d.fields) {   // a question no turn waits on any more: all of its questions at once
      ov.setAttribute("aria-label", "The agent has questions");
      ov.innerHTML =
        '<div class="cmp-back"></div><div class="cmp-card" tabindex="-1">' +
        '<div class="cmp-hd">' + QB_ASK + '<div class="cmp-tt"><div class="cmp-title">The agent has questions</div><div class="cmp-sub"></div></div>' +
        '<span class="qb-n"></span><button type="button" class="cmp-hide qb-later" title="Answer later: a pill above the message box brings it back">Later</button></div>' +
        '<div class="qb-fields" style="display:flex;flex-direction:column;gap:14px"></div><div class="qb-err" hidden></div>' +
        '<div class="qb-row"><button type="button" class="qb-b qb-skip" title="The questions go into the reply; your next message can answer them">Skip</button>' +
        '<span class="qb-sp"></span><button type="button" class="qb-b qb-send">Send answers</button></div></div>';
      setText(ov.querySelector(".cmp-sub"), "Code mode " + where + " · empty = the first choice");
      var box = ov.querySelector(".qb-fields"), inputs = {};
      (d.fields || []).forEach(function (f) {
        var row = cmpEl("div", "qb-field");
        row.appendChild(cmpEl("b", "", String(f.message || f.title || "?")));
        var opts = f.options || [];
        if (opts.length) {
          var chips = cmpEl("div", "qb-chips");
          opts.forEach(function (o) {
            var b = cmpEl("button", "", String(o));
            b.type = "button";
            b.addEventListener("click", function () { inp.value = String(o); });
            chips.appendChild(b);
          });
          row.appendChild(chips);
        }
        var inp = cmpEl("input", "qb-in");
        inp.placeholder = opts.length ? String(opts[0]) : "Your answer";
        row.appendChild(inp);
        inputs[f.key] = inp;
        box.appendChild(row);
      });
      ov.querySelector(".qb-send").addEventListener("click", function () {
        var out = {};
        Object.keys(inputs).forEach(function (k) { out[k] = inputs[k].value; });
        qbAnswer(a, { answers: out });
      });
      ov.querySelector(".qb-skip").addEventListener("click", function () { qbAnswer(a, false); });
    } else {   // one question, as the turn asks them
      ov.setAttribute("aria-label", "The agent has a question");
      ov.innerHTML =
        '<div class="cmp-back"></div><div class="cmp-card" tabindex="-1">' +
        '<div class="cmp-hd">' + QB_ASK + '<div class="cmp-tt"><div class="cmp-title"></div><div class="cmp-sub"></div></div>' +
        '<span class="qb-n"></span><button type="button" class="cmp-hide qb-later" title="Answer later: a pill above the message box brings it back, and so does reloading or opening the chat anywhere else">Later</button></div>' +
        '<div class="qb-q"></div><div class="qb-opts"></div>' +
        '<div class="qb-own"><input class="qb-in" placeholder="Or type your own answer"><button type="button" class="qb-b qb-send">Send</button></div>' +
        '<div class="qb-err" hidden></div>' +
        '<div class="qb-row"><button type="button" class="qb-b qb-skip" title="The question goes into the reply; your next message can answer it">Skip</button>' +
        '<span class="qb-hint">Waits until you answer.</span></div></div>';
      setText(ov.querySelector(".cmp-title"), String(d.title || "The agent has a question"));
      setText(ov.querySelector(".cmp-sub"), "Code mode " + where);
      setText(ov.querySelector(".qb-q"), String(d.question || d.message || ""));
      var list = ov.querySelector(".qb-opts"), descs = d.descriptions || [];
      (d.options || []).forEach(function (o, i) {
        var b = cmpEl("button", "qb-opt");
        b.type = "button";
        b.appendChild(cmpEl("i", "", String(i + 1)));
        var t = cmpEl("span", "", String(o));
        if (i === 0) t.appendChild(cmpEl("em", "", "SUGGESTED"));
        if (descs[i]) t.appendChild(cmpEl("small", "", String(descs[i])));
        b.appendChild(t);
        b.addEventListener("click", function () { qbAnswer(a, String(i + 1)); });
        list.appendChild(b);
      });
      var own = ov.querySelector(".qb-in");
      if (d.placeholder) own.placeholder = "Or type your own answer (empty = " + String(d.placeholder) + ")";
      var send = function () { qbAnswer(a, own.value); };
      ov.querySelector(".qb-send").addEventListener("click", send);
      own.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); send(); } });
      ov.querySelector(".qb-skip").addEventListener("click", function () { qbAnswer(a, false); });
    }
    ov.querySelector(".qb-later").addEventListener("click", function () { qbLater(a.id); });
    ov.querySelector(".cmp-back").addEventListener("click", function () { qbLater(a.id); });
    document.body.appendChild(ov);
    return ov;
  }

  function qbLater(id) {
    qb.hidden[id] = true;
    qbRender();
  }

  function qbKey(e) {
    if (e.key !== "Escape" || !qb.shown || !document.getElementById(QB_ID)) return;
    e.codeHandled = true;
    e.stopPropagation();   // Watch, the overlays and Open WebUI below keep theirs for the next Esc
    qbLater(qb.shown);
  }

  function qbAnswer(a, value) {
    var ov = document.getElementById(QB_ID);
    Array.prototype.forEach.call(ov ? ov.querySelectorAll("button,input,textarea") : [], function (b) { b.disabled = true; });
    var args = { op: "answer", id: a.id, value: value };
    if (a.direct) args.sid = a.sid;
    qbCall(args).then(function (r) {
      if (r.ok || r.gone) {
        qb.sent[a.id] = Date.now();
        qb.items = qb.items.filter(function (x) { return x.id !== a.id; });
        qbClose();
        qbRender();
        qbPoll();   // the next one, if the agent asks again straight away
        return;
      }
      var box = document.getElementById(QB_ID);
      if (!box) return;
      Array.prototype.forEach.call(box.querySelectorAll("button,input,textarea"), function (b) { b.disabled = false; });
      var err = box.querySelector(".qb-err");
      err.hidden = false;
      setText(err, (r.error || "That did not go through.") + " Try again.");
    });
  }

  function qbRenderPill(mine) {
    var p = document.getElementById(QB_PILL);
    if (!mine.length) {
      if (p) p.remove();
      return;
    }
    qbInjectStyle();
    p = rvPillHost(QB_PILL, "cmp-pill qb-pill");
    if (!p) return;
    if (!p.querySelector(".cmp-pill-b")) {
      p.innerHTML = '<i></i><span class="cmp-pill-t"></span><button type="button" class="cmp-pill-b">Show</button>';
      p.querySelector(".cmp-pill-b").addEventListener("click", function () {
        qbMine().forEach(function (a) { delete qb.hidden[a.id]; });
        qbRender();
      });
    }
    var a = mine[0], d = a.data || {};
    var what = a.kind === "permission" ? "Approval waiting · " + String(d.action || "") + ": " + String((d.resources || [])[0] || "")
      : "Question waiting · " + String(d.question || d.message || (d.fields && d.fields[0] && d.fields[0].message) || "");
    setText(p.querySelector(".cmp-pill-t"), (what.length > 90 ? what.slice(0, 89) + "…" : what) + (mine.length > 1 ? " (+" + (mine.length - 1) + ")" : ""));
  }

  function qbRenderNote(others) {
    var box = document.getElementById(QB_NOTE);
    var byChat = {}, chats = [];
    others.forEach(function (a) {
      if (!byChat[a.chat_id]) { byChat[a.chat_id] = []; chats.push(a.chat_id); }
      byChat[a.chat_id].push(a);
    });
    if (!chats.length) {
      if (box) box.remove();
      return;
    }
    qbInjectStyle();
    if (!box) {
      box = cmpEl("div");
      box.id = QB_NOTE;
      box.setAttribute("role", "status");
      document.body.appendChild(box);
    }
    var sig = chats.map(function (c) { return c + ":" + byChat[c].length; }).join("|");
    if (box.getAttribute("data-sig") === sig) return;
    box.setAttribute("data-sig", sig);
    box.textContent = "";
    chats.slice(0, 3).forEach(function (c) {
      var list = byChat[c], a = list[0];
      var row = cmpEl("div", "qb-note");
      row.appendChild(cmpEl("i"));
      var kind = list.every(function (x) { return x.kind === "permission"; }) ? "Approval" : list.every(function (x) { return x.kind !== "permission"; }) ? "Question" : "Approval";
      row.appendChild(cmpEl("span", "", (list.length > 1 ? list.length + " waiting" : kind + " waiting") + " in “" + String(a.title || "a Code chat") + "”"));
      var go = cmpEl("a", "", "Open");
      go.href = "/c/" + encodeURIComponent(c);   // Open WebUI's router takes it from here, no reload
      row.appendChild(go);
      var x = cmpEl("button", "", "×");
      x.type = "button";
      x.title = "Hide this notice (it comes back with the next approval or question)";
      x.setAttribute("aria-label", "Hide");
      x.addEventListener("click", function () {
        list.forEach(function (y) { qb.gone[y.id] = true; });
        qbRender();
      });
      row.appendChild(x);
      box.appendChild(row);
    });
  }

  // ------------------------------------------------- Watch: the Fixer takes the stage
  // While the bug fixer looks into this chat (a report the agent filed with report-bug, or the user with the Fixer
  // button: the chat is paused either way), Watch gives the stage to the fixer's own viewer, like a review's dialog,
  // and makes it impossible to miss (10-26, the user: "catch the eye, holy crap something happened and it's
  // investigating itself"): an alarm card the first time a report shows, then hazard tape and a pulsing red frame
  // around the viewer while it runs, and the verdict stamped across it at the end before the stage goes back to the
  // builder. "Keep minimized" (held across chats, like the review's) and Hide turn it into a blinking pill on the
  // stage. The report comes from openFixer (this page filed it or saw its link) or from the pipe's code_fixer
  // {op: active} every WF_POLL_MS while Watch is open (after a reload, or a report filed from another tab).
  var WF_ID = "code-wf", WF_PILL = "code-wf-pill", WF_KEEP = "code-mode.fixer-minimized", WF_POLL_MS = 8000;
  var WF_INTRO_MS = 2600, WF_FOLD_MS = 6000;
  var WF_VERDICTS = { fixed: "FIXED", not_a_bug: "NOT A BUG", needs_human: "NEEDS YOU", could_not_fix: "COULDN'T FIX",
                      cancelled: "CANCELLED" };
  var WF_STALL_MS = 25000;   // the viewer says it is alive every 5 s; this long without it, it is reloaded
  var wf = { url: "", id: "", title: "", since: 0, model: "", done: false, verdict: "", hidden: false, peek: false,
             pollAt: 0, busy: false, introFor: "", introTimer: 0, foldTimer: 0, toastTimer: 0, missing: 0, ended: {},
             phase: "fixer", claudeFor: "", aliveAt: 0, frameAt: 0, reloadAt: 0, doneAt: 0, armTimer: 0 };

  function wfIdOf(url) {
    var m = /\/f\/([A-Za-z0-9_-]{1,64})\//.exec(String(url || ""));
    return m ? m[1] : "";
  }

  function wfKeep() {
    try { return window.localStorage.getItem(WF_KEEP) === "1"; } catch (e) { return false; }
  }
  function wfSetKeep(on) {
    try {
      if (on) window.localStorage.setItem(WF_KEEP, "1");
      else window.localStorage.removeItem(WF_KEEP);
    } catch (e) { /* private mode: it holds for this page only */ }
  }
  function wfMinimized() { return (wfKeep() && !wf.peek) || wf.hidden; }
  function wfActive() { return !!(wt.on && wf.url && !wf.done); }
  function wfShowing() { return !!(wt.on && wf.url && document.getElementById(WF_ID) && !wfMinimized()); }
  function wfGoodUrl(u) { return typeof u === "string" && u.indexOf(FIXER_ORIGIN + "/f/") === 0; }

  function wfInjectStyle() {
    rvInjectStyle();
    if (document.getElementById("code-wf-style")) return;
    var tape = "repeating-linear-gradient(-45deg,#ffb020 0 14px,#16110a 14px 28px)";
    var css = [
      "#code-wf{position:absolute;inset:0;z-index:4;display:flex;flex-direction:column;background:#0b0b0d;overflow:hidden}",
      "#code-wf[hidden]{display:none}",
      ".wf-glow{position:absolute;inset:0;pointer-events:none;z-index:3;border-radius:16px;animation:wf-glow 1.6s ease-in-out infinite}",
      "@keyframes wf-glow{0%,100%{box-shadow:inset 0 0 0 2px rgba(255,77,77,.9),inset 0 0 40px rgba(255,60,40,.28)}50%{box-shadow:inset 0 0 0 2px rgba(255,176,32,.55),inset 0 0 90px rgba(255,60,40,.08)}}",
      "#code-wf[data-state=done] .wf-glow{animation:none;box-shadow:inset 0 0 0 2px var(--wf-end,#7bd88f),inset 0 0 60px rgba(0,0,0,0)}",
      ".wf-tape{flex:none;height:8px;background:" + tape + ";background-size:40px 40px;animation:wf-tape 1s linear infinite}",
      "@keyframes wf-tape{to{background-position:40px 0}}",
      "#code-wf[data-state=done] .wf-tape{animation:none;background:var(--wf-end,#7bd88f)}",
      ".wf-bar{flex:none;display:flex;align-items:center;gap:12px;flex-wrap:wrap;row-gap:8px;padding:10px 16px;background:linear-gradient(180deg,#1c0f0c,#120b0a);border-bottom:1px solid #3a1d17}",
      "#code-wf[data-state=done] .wf-bar{background:#0f1512;border-bottom-color:#1f3326}",
      ".wf-siren{position:relative;width:14px;height:14px;flex:none;border-radius:50%;background:#ff4d4d;animation:wf-siren .9s ease-in-out infinite}",
      "@keyframes wf-siren{0%,100%{box-shadow:0 0 0 0 rgba(255,77,77,.75),0 0 12px #ff4d4d}50%{background:#ffb020;box-shadow:0 0 0 9px rgba(255,77,77,0),0 0 18px #ffb020}}",
      "#code-wf[data-state=done] .wf-siren{animation:none;background:var(--wf-end,#7bd88f);box-shadow:none}",
      ".wf-chip{flex:none;padding:4px 9px;border-radius:6px;font:700 11px ui-monospace,\"JetBrains Mono\",Menlo,monospace;letter-spacing:.12em;background:#ff4d4d;color:#160606}",
      "#code-wf[data-state=done] .wf-chip{background:var(--wf-end,#7bd88f);color:#08130c}",
      ".wf-title{flex:1 1 220px;min-width:0;font-size:15px;font-weight:600;color:#ffe9e2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      ".wf-paused{flex:none;padding:3px 8px;border-radius:6px;font:600 10px ui-monospace,monospace;letter-spacing:.08em;background:rgba(255,176,32,.14);color:#ffc860}",
      ".wf-clock{font:600 14px ui-monospace,\"JetBrains Mono\",Menlo,monospace;color:#ffd0c4}",
      ".wf-bar .rv-switch[aria-checked=true] .rv-track{background:#ff6b4a}",
      ".wt-stage #code-wf .wf-frame{position:relative;inset:auto;flex:1;min-height:0;width:100%;height:auto;border:0;background:#0b0b0d}",
      ".wf-intro{position:absolute;inset:0;z-index:4;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;text-align:center;padding:24px;background:radial-gradient(ellipse at center,rgba(120,14,10,.92),rgba(10,4,4,.97) 70%);animation:wf-introout .5s ease-in " + (WF_INTRO_MS - 500) + "ms both}",
      "@keyframes wf-introout{to{opacity:0;transform:scale(1.04);visibility:hidden}}",
      ".wf-intro::before,.wf-intro::after{content:'';position:absolute;left:0;right:0;height:16px;background:" + tape + ";background-size:40px 40px;animation:wf-tape .6s linear infinite}",
      ".wf-intro::before{top:0}.wf-intro::after{bottom:0}",
      ".wf-rings{position:relative;width:96px;height:96px}",
      ".wf-rings i{position:absolute;inset:0;border-radius:50%;border:3px solid #ff4d4d;animation:wf-ring 1.2s ease-out infinite}",
      ".wf-rings i:nth-child(2){animation-delay:.4s}.wf-rings i:nth-child(3){animation-delay:.8s}",
      ".wf-rings b{position:absolute;inset:30px;border-radius:50%;background:#ff4d4d;box-shadow:0 0 30px #ff4d4d;animation:wf-siren .45s ease-in-out infinite}",
      "@keyframes wf-ring{from{transform:scale(.35);opacity:1}to{transform:scale(1.6);opacity:0}}",
      ".wf-big{font:800 clamp(28px,5.4vw,64px)/1 Inter,ui-sans-serif,system-ui,sans-serif;letter-spacing:.06em;color:#fff;text-shadow:0 0 24px rgba(255,77,77,.9),0 0 2px #ff4d4d;animation:wf-shake .5s ease-in-out 3}",
      "@keyframes wf-shake{0%,100%{transform:translateX(0)}20%{transform:translateX(-6px) skewX(-4deg)}40%{transform:translateX(5px)}60%{transform:translateX(-3px) skewX(3deg)}80%{transform:translateX(2px)}}",
      ".wf-small{font:600 clamp(14px,1.8vw,20px) Inter,system-ui,sans-serif;color:#ffd0c4;letter-spacing:.02em}",
      ".wf-what{max-width:640px;font-size:14px;color:#ffb8a8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      ".wf-scan{position:absolute;left:0;right:0;height:120px;top:-120px;background:linear-gradient(180deg,transparent,rgba(255,77,77,.22),transparent);animation:wf-scan 1.3s linear infinite;pointer-events:none}",
      "@keyframes wf-scan{to{top:100%}}",
      ".wf-stamp{position:absolute;left:50%;top:50%;z-index:5;transform:translate(-50%,-50%) rotate(-8deg);display:none;flex-direction:column;align-items:center;gap:6px;padding:18px 34px;border:5px solid var(--wf-end,#7bd88f);border-radius:14px;background:rgba(8,10,9,.88);color:var(--wf-end,#7bd88f);box-shadow:0 0 50px rgba(0,0,0,.6);text-align:center;pointer-events:none}",
      "#code-wf[data-state=done] .wf-stamp{display:flex;animation:wf-stamp .45s cubic-bezier(.2,.9,.3,1.3) both}",
      "@keyframes wf-stamp{from{opacity:0;transform:translate(-50%,-50%) rotate(-8deg) scale(2.2)}}",
      ".wf-stamp b{font:800 clamp(26px,4.5vw,52px)/1 Inter,system-ui,sans-serif;letter-spacing:.08em}",
      ".wf-stamp span{font-size:13px;color:#d6f5dd;max-width:420px}",
      "#code-wf.is-folding{transition:opacity .7s ease-in,transform .7s ease-in;opacity:0;transform:scale(.92)}",
      "#code-wf-pill{left:14px;bottom:14px;z-index:6;background:#1c0f0c;border-color:#5a241a;color:#ffe2d9}",
      "#code-wf-pill .wf-siren{width:10px;height:10px}",
      "#code-wf-pill.is-done{background:#13201a;border-color:#2f5a3c;color:#d6f5dd}",
      "#code-wf-pill.is-done .wf-siren{animation:none;background:#7bd88f;box-shadow:none}",
      "#code-wf[data-phase=claude] .wf-glow{animation:wf-glowc 1.8s ease-in-out infinite}",
      "@keyframes wf-glowc{0%,100%{box-shadow:inset 0 0 0 2px rgba(180,139,255,.95),inset 0 0 50px rgba(120,80,255,.25)}50%{box-shadow:inset 0 0 0 2px rgba(79,209,197,.7),inset 0 0 90px rgba(79,209,197,.08)}}",
      "#code-wf[data-phase=claude] .wf-tape{background:repeating-linear-gradient(-45deg,#b48bff 0 14px,#120e1c 14px 28px);background-size:40px 40px}",
      "#code-wf[data-phase=claude] .wf-bar{background:linear-gradient(180deg,#160f24,#0f0b18);border-bottom-color:#2e2645}",
      "#code-wf[data-phase=claude] .wf-siren{background:#b48bff;animation:wf-sirenc 1.1s ease-in-out infinite}",
      "@keyframes wf-sirenc{0%,100%{box-shadow:0 0 0 0 rgba(180,139,255,.75),0 0 12px #b48bff}50%{background:#4fd1c5;box-shadow:0 0 0 9px rgba(180,139,255,0),0 0 18px #4fd1c5}}",
      "#code-wf[data-phase=claude] .wf-chip{background:#b48bff;color:#140a26}",
      "#code-wf[data-phase=claude] .wf-title{color:#ece6ff}",
      "#code-wf[data-phase=claude] .wf-paused{background:rgba(180,139,255,.14);color:#d4c2ff}",
      ".wf-flash{position:absolute;inset:0;z-index:4;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;text-align:center;padding:24px;background:radial-gradient(ellipse at center,rgba(70,30,140,.9),rgba(8,5,16,.96) 70%);animation:wf-introout .5s ease-in 1900ms both;pointer-events:none}",
      ".wf-flash .wf-rings i{border-color:#b48bff}.wf-flash .wf-rings b{background:#b48bff;box-shadow:0 0 30px #b48bff}",
      ".wf-flash .wf-big{text-shadow:0 0 24px rgba(180,139,255,.95),0 0 2px #b48bff}",
      ".wf-flash .wf-small{color:#d4c2ff}",
      ".wf-cancel.is-armed{border-color:#7a2c2c;background:#2a1414;color:#ffb4b4}",
      "#code-wf[data-rest=\"1\"] .wf-stamp{left:auto;right:22px;top:auto;bottom:22px;transform:rotate(-6deg) scale(.55);transform-origin:bottom right;transition:all .6s ease}",
      "#code-watch[data-mode=fixer] .wt-dot{background:#ff4d4d;animation:wf-siren .9s ease-in-out infinite}",
      "#code-watch[data-mode=fixer] .wt-chip{background:#ff4d4d;color:#160606}",
      "#code-watch[data-mode=fixer] .wt-stage{border-color:#5a241a}",
      "@media (prefers-reduced-motion:reduce){#code-wf *,#code-wf-pill *{animation:none!important}.wf-intro{display:none}}",
    ].join("\n");
    var s = document.createElement("style");
    s.id = "code-wf-style";
    s.textContent = css;
    document.head.appendChild(s);
  }

  /** A report for this chat runs (or was just seen): show it. Only the first sight of a report sounds the alarm. */
  function wfStart(url, info) {
    if (!wfGoodUrl(url) || wf.ended[url]) return;   // a report shown to its verdict never comes back
    info = info || {};
    if (wf.url !== url) {
      wfClear();
      wf.url = url;
      wf.done = false; wf.verdict = ""; wf.hidden = false; wf.peek = false; wf.missing = 0;
    }
    wf.id = wfIdOf(url);
    if (info.title) wf.title = String(info.title);
    if (info.since) wf.since = Number(info.since) * 1000;
    if (info.model) wf.model = String(info.model);
    if (info.phase === "claude") wf.phase = "claude";   // never back to the fixer for the same report
    if (!wf.since) wf.since = Date.now();
    if (wt.on) wfRender();
  }

  function wfClear() {
    clearTimeout(wf.introTimer);
    clearTimeout(wf.foldTimer);
    var ov = document.getElementById(WF_ID);
    if (ov) ov.remove();
    var p = document.getElementById(WF_PILL);
    if (p && !p.classList.contains("is-done")) p.remove();
    wf.url = ""; wf.id = ""; wf.title = ""; wf.since = 0; wf.model = ""; wf.done = false; wf.verdict = "";
    wf.phase = "fixer"; wf.aliveAt = 0; wf.frameAt = 0; wf.doneAt = 0;
  }

  function wfBuild() {
    wfInjectStyle();
    var ov = cmpEl("div");
    ov.id = WF_ID;
    ov.setAttribute("role", "region");
    ov.setAttribute("aria-label", "The Fixer is investigating");
    ov.innerHTML =   // fixed markup only: the report's title goes in with textContent
      '<div class="wf-glow"></div><div class="wf-tape"></div>' +
      '<header class="wf-bar"><span class="wf-siren"></span><span class="wf-chip">FIXER · INVESTIGATING</span>' +
      '<span class="wf-title"></span><span class="wf-paused">CHAT PAUSED</span><span class="wf-clock">0:00</span>' +
      '<button type="button" class="cmp-hide wf-cancel" title="Stop the fixer (or Claude) now; the chat carries on without it">Cancel</button>' +
      '<button type="button" class="rv-switch wf-keep" role="switch" aria-checked="false" title="Show the Fixer as a pill on the stage, in every chat">' +
      '<span class="rv-track"><span class="rv-knob"></span></span>Keep minimized</button>' +
      '<button type="button" class="cmp-hide wf-hide" title="Hide it; the pill on the stage brings it back">Hide</button></header>' +
      '<iframe class="wf-frame" title="The Fixer investigating this chat"></iframe>' +
      '<div class="wf-stamp"><b></b><span></span></div>';
    ov.querySelector(".wf-hide").addEventListener("click", function () { wfHide(true); });
    ov.querySelector(".wf-cancel").addEventListener("click", function () {   // two clicks: arm, then cancel
      var b = this;
      if (!b.classList.contains("is-armed")) {
        b.classList.add("is-armed");
        setText(b, "Cancel it?");
        clearTimeout(wf.armTimer);
        wf.armTimer = setTimeout(function () { b.classList.remove("is-armed"); setText(b, "Cancel"); }, 4000);
        return;
      }
      clearTimeout(wf.armTimer);
      b.disabled = true;
      setText(b, "Cancelling…");
      wfCancel(wf.id);
    });
    ov.querySelector(".wf-keep").addEventListener("click", function () {
      var on = !wfKeep();
      wfSetKeep(on);
      if (on) wf.peek = false;
      wfRender();
    });
    wt.stage.appendChild(ov);
    return ov;
  }

  function wfIntro(ov) {   // the alarm: rings, shaking type, a scan line, hazard tape top and bottom
    if (wf.introFor === wf.url) return;
    wf.introFor = wf.url;
    var intro = cmpEl("div", "wf-intro");
    intro.innerHTML = '<div class="wf-scan"></div><div class="wf-rings"><i></i><i></i><i></i><b></b></div>' +
      '<div class="wf-big">SOMETHING BROKE</div><div class="wf-small">The agent is investigating itself</div><div class="wf-what"></div>';
    setText(intro.querySelector(".wf-what"), wf.title);
    ov.appendChild(intro);
    clearTimeout(wf.introTimer);
    wf.introTimer = setTimeout(function () { if (intro.isConnected) intro.remove(); }, WF_INTRO_MS + 100);
  }

  function wfClaudeFlash(ov) {   // the hand-off: once per report, when Claude takes over
    if (wf.claudeFor === wf.url) return;
    wf.claudeFor = wf.url;
    var f = cmpEl("div", "wf-flash");
    f.innerHTML = '<div class="wf-rings"><i></i><i></i><i></i><b></b></div><div class="wf-big">CALLING CLAUDE</div>' +
      '<div class="wf-small">The fixer couldn\'t fix it — Claude takes over</div>';
    ov.appendChild(f);
    setTimeout(function () { if (f.isConnected) f.remove(); }, 2500);
  }

  function wfCancel(id) {
    if (!id) return;
    fixerCall({ op: "cancel", chat_id: currentChatId, id: id }).then(function (r) {
      if (!r.ok) {
        var b = document.querySelector("#code-wf .wf-cancel");
        if (b) { b.disabled = false; b.classList.remove("is-armed"); setText(b, "Cancel"); b.title = r.error || ""; }
      }
    });
  }

  function wfHide(on) {
    wf.hidden = !!on;
    if (!on) wf.peek = true;
    wfRender();
  }

  function wfRender() {
    if (!wt.on || !wt.stage || !wf.url) return;
    var ov = document.getElementById(WF_ID), min = wfMinimized();
    if (!ov) ov = wfBuild();
    if (ov.parentNode !== wt.stage) wt.stage.appendChild(ov);
    var frame = ov.querySelector(".wf-frame");
    if (frame.getAttribute("src") !== wf.url) {
      frame.setAttribute("src", wf.url);
      wf.frameAt = Date.now();
    }
    ov.hidden = min;
    if (!min && !wf.done) wfIntro(ov);
    if (!min && !wf.done && wf.phase === "claude") wfClaudeFlash(ov);
    setAttr(ov, "data-state", wf.done ? "done" : "run");
    setAttr(ov, "data-phase", wf.phase);
    setText(ov.querySelector(".wf-title"), wf.title || "Looking into this chat");
    setAttr(ov.querySelector(".wf-keep"), "aria-checked", wfKeep() ? "true" : "false");
    var who = wf.phase === "claude" ? "CLAUDE" : "FIXER";
    setText(ov.querySelector(".wf-chip"), wf.done ? who + " · " + (WF_VERDICTS[wf.verdict] || "DONE")
      : wf.phase === "claude" ? "CLAUDE · ON IT" : "FIXER · INVESTIGATING");
    ov.querySelector(".wf-paused").hidden = wf.done;
    ov.querySelector(".wf-cancel").hidden = wf.done;
    var end = wf.verdict === "fixed" || wf.verdict === "not_a_bug" ? "#7bd88f" : wf.verdict ? "#f08a8a" : "#a6a6b0";
    ov.style.setProperty("--wf-end", end);
    setText(ov.querySelector(".wf-stamp b"), WF_VERDICTS[wf.verdict] || "DONE");
    setText(ov.querySelector(".wf-stamp span"), wf.verdict === "fixed" ? "The agent picks up again with the fix"
      : wf.verdict === "not_a_bug" ? "Nothing to fix; the agent carries on"
      : wf.verdict === "cancelled" ? "Cancelled; the agent carries on without it"
      : "The agent carries on; the notes are in the Fixer tab");
    wfRenderPill(min);
    wfClock();
  }

  function wfClock() {
    var ov = document.getElementById(WF_ID);
    if (ov && wf.since) setText(ov.querySelector(".wf-clock"), wtClock(Date.now() - wf.since));
    var p = document.getElementById(WF_PILL);
    if (p && !p.classList.contains("is-done") && wf.since) {
      setText(p.querySelector(".cmp-pill-t"), "Fixer investigating · " + (wf.title || "this chat").slice(0, 70) + " · " + wtClock(Date.now() - wf.since));
    }
  }

  function wfRenderPill(min) {
    var p = document.getElementById(WF_PILL);
    if (!min || wf.done || !wt.on) {
      if (p && !p.classList.contains("is-done")) p.remove();
      return;
    }
    if (!p) {
      p = cmpEl("div", "cmp-pill");
      p.id = WF_PILL;
      p.innerHTML = '<span class="wf-siren"></span><span class="cmp-pill-t"></span><button type="button" class="cmp-pill-b">Show</button>';
      p.querySelector(".cmp-pill-b").addEventListener("click", function () { wfHide(false); });
    }
    if (p.parentNode !== wt.stage) wt.stage.appendChild(p);
  }

  function wfFinish(verdict) {
    if (!wf.url || wf.done) return;
    wf.done = true;
    wf.ended[wf.url] = true;
    wf.verdict = String(verdict || "");
    wf.doneAt = Date.now();
    if (!wt.on) return;
    wfRender();
    clearTimeout(wf.foldTimer);
    if (wfMinimized()) wf.foldTimer = setTimeout(wfFold, 0);
    // otherwise the result stays on the stage until the chat's work picks up again (wfTick), its stamp moving
    // aside after a moment (10-26, the user: "follow, keep the last result", never an empty window)
  }

  function wfFold() {   // the stage goes back to the builder; a short note says what the Fixer found
    var ov = document.getElementById(WF_ID), verdict = wf.verdict;
    if (ov) ov.classList.add("is-folding");
    setTimeout(function () {
      var url = wf.url;
      wfClear();
      wf.introFor = url;   // the same report is not announced again
      if (!wt.on || !wt.stage) return;
      var p = cmpEl("div", "cmp-pill is-done");
      p.id = WF_PILL;
      p.innerHTML = '<span class="wf-siren"></span><span class="cmp-pill-t"></span>';
      setText(p.querySelector(".cmp-pill-t"), "Fixer: " + (WF_VERDICTS[verdict] || "done").toLowerCase() + " · the agent carries on");
      wt.stage.appendChild(p);
      clearTimeout(wf.toastTimer);
      wf.toastTimer = setTimeout(function () { var q = document.getElementById(WF_PILL); if (q && q.classList.contains("is-done")) q.remove(); }, 6000);
    }, ov ? 750 : 0);
  }

  function wfTick() {   // every second while Watch is open
    if (fixer.url && wfGoodUrl(fixer.url) && fixer.url !== wf.url && !fixer.done && fixer.chat === currentChatId) wfStart(fixer.url);
    if (wf.url && fixer.url === wf.url && fixer.done && !wf.done) wfFinish(fixer.verdict || "");
    wfClock();
    var now = Date.now(), ov = document.getElementById(WF_ID);
    if (wf.done && ov && !ov.classList.contains("is-folding")) {
      if (runningLiveFrame() && now - wf.doneAt > 2500) wfFold();   // the builder picked up: the stage follows it
      else if (now - wf.doneAt > WF_FOLD_MS) setAttr(ov, "data-rest", "1");
    }
    if (wfShowing() && !wf.done && !document.hidden && now - Math.max(wf.aliveAt, wf.frameAt) > WF_STALL_MS &&
        now - wf.reloadAt > 30000) {   // the viewer stopped saying it is alive: reload it
      wf.reloadAt = now;
      wf.frameAt = now;
      var frame = ov.querySelector(".wf-frame");
      frame.setAttribute("src", "about:blank");
      setTimeout(function () { if (frame.isConnected) frame.setAttribute("src", wf.url); }, 80);
    }
    if (now - wf.pollAt >= WF_POLL_MS) wfPoll();
  }

  function wfPoll() {
    if (wf.busy || !currentChatId) return;
    wf.busy = true;
    wf.pollAt = Date.now();
    var chat = currentChatId;
    fixerCall({ op: "active", chat_id: chat }).then(function (r) {
      wf.busy = false;
      if (!wt.on || chat !== currentChatId || !r.ok) return;
      if (r.active) {
        wf.missing = 0;
        wfStart(r.active, r);
      } else if (wf.url && !wf.done && ++wf.missing >= 2) {
        wfFinish(wf.verdict);   // it ended while this page heard nothing from its viewer
      }
    });
  }

  function wfOnStatus(e) {   // the stage's viewer (the side panel's has its own listener)
    var ov = document.getElementById(WF_ID), frame = ov && ov.querySelector(".wf-frame");
    if (!frame || e.source !== frame.contentWindow) return;
    wf.aliveAt = Date.now();
    if (e.data.phase === "claude" && wf.phase !== "claude" && !wf.done) {
      wf.phase = "claude";
      wfRender();
    }
    if (e.data.done) wfFinish(e.data.verdict);
  }

  // The fixer's own viewer (in the side panel or on Watch's stage) has Cancel and Ask Claude buttons. It cannot
  // reach the pipe, so it asks this page, which checks the message came from one of those frames, for its own
  // report, and passes it on (code_fixer op cancel / escalate; the pipe holds the fixer's token).
  function fixerFrameUrl(source) {
    var frames = document.querySelectorAll("#code-preview-panel .cp-frame-fixer, #code-wf .wf-frame");
    for (var i = 0; i < frames.length; i++) {
      if (frames[i].contentWindow === source) return frames[i].getAttribute("src") || "";
    }
    return "";
  }

  window.addEventListener("message", function (e) {
    try {
      if (e.origin !== FIXER_ORIGIN || !e.data) return;
      var d = e.data;
      if (d.type === "fixer:status") { wfOnStatus(e); return; }
      if (d.type !== "fixer:cancel" && d.type !== "fixer:escalate") return;
      var url = fixerFrameUrl(e.source);
      if (!url || wfIdOf(url) !== String(d.id || "") || !currentChatId) return;
      fixerCall({ op: d.type === "fixer:cancel" ? "cancel" : "escalate", chat_id: currentChatId, id: wfIdOf(url) })
        .then(function (r) {
          if (r.ok && d.type === "fixer:escalate") {
            wf.ended[url] = false;   // the report goes on with Claude: Watch shows it again
            delete wf.ended[url];
            wf.pollAt = 0;
            if (wf.url === url) { wf.done = false; wf.phase = "claude"; wfRender(); }
          }
        });
    } catch (err) {
      warnOnce("watch-fixer", err);
    }
  });

  // ------------------------------------------------ reload when Code mode is updated
  // A deploy writes a new /static/code-toggle.version (code-deploy). Every tab looks once a minute and, when this
  // page runs an older script, reloads itself at the first quiet moment: nothing typed in the message box or in a
  // box of ours, no menu of ours open. Watch comes back after the reload (WT_REOPEN_KEY). 10-26, the user: "reload
  // on new version".
  var VER = { timer: 0, pending: false, tries: 0 };

  function verPoll() {
    if (!MY_VERSION) return;   // loaded some other way (tests, a bookmarklet): nothing to compare
    _fetch("/static/code-toggle.version", { cache: "no-store" }).then(function (r) {
      return r.ok ? r.text() : "";
    }).then(function (v) {
      v = String(v || "").trim();
      if (v && /^[A-Za-z0-9._-]{4,64}$/.test(v) && v !== MY_VERSION && !VER.pending) {
        VER.pending = true;
        verTryReload();
      }
    }, function () { /* offline: next minute */ });
  }

  function verQuiet() {
    var box = document.getElementById("chat-input");
    if (box && String(box.textContent || "").trim()) return false;   // a message being written
    var mine = document.querySelectorAll("#code-qb input, #code-qb textarea, #fixer-ask textarea, #fixer-ask input");
    for (var i = 0; i < mine.length; i++) if (String(mine[i].value || "").trim()) return false;
    if (document.querySelector("#fixer-ask:not([hidden]) .fx-go:disabled")) return false;   // filing a report
    return true;
  }

  function verTryReload() {
    if (!verQuiet()) {
      VER.tries++;
      setTimeout(verTryReload, 5000);
      return;
    }
    wtNoteReopen();
    try {
      var t = cmpEl("div", "qb-note");
      t.style.cssText = "position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:90";
      t.appendChild(cmpEl("i"));
      t.appendChild(cmpEl("span", "", "Code mode was updated — reloading"));
      qbInjectStyle();
      document.body.appendChild(t);
    } catch (e) { /* the reload matters, not the note */ }
    setTimeout(function () { try { location.reload(); } catch (e) { /* fine */ } }, 1200);
  }

  function verStart() {
    if (!MY_VERSION || VER.timer) return;
    VER.timer = setInterval(verPoll, 60000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) verPoll(); });
  }

  function wtReopenAfterReload() {   // this tab had Watch open on this chat before it reloaded
    var note = null;
    try { note = JSON.parse(sessionStorage.getItem(WT_REOPEN_KEY) || "null"); } catch (e) { note = null; }
    if (!note || note.chat !== currentChatId || Date.now() - (note.at || 0) > 10 * 60000) return;
    var tries = 0;
    var go = function () {
      if (wt.on || note.chat !== currentChatId) return;
      if (document.querySelector(LIVE_FRAMES) || ++tries > 10) {
        try { wtOpen(true); } catch (e) { warnOnce("watch-reopen", e); }
        return;
      }
      setTimeout(go, 500);   // give the chat a moment to draw its live windows
    };
    setTimeout(go, 800);
  }

  // ---------------------------------------------------------------- features
  // Feature chats: ✦ Feature starts one (the next new chat's first message flags it for the Code pipe, which asks
  // code-features for the feature's workspace). code-deploy lists them (/api/chats, Open WebUI admins only), which
  // marks them in the sidebar on every device, and its Features page in the side panel deploys and rolls back.
  var features = { enabled: false, chats: {}, refreshing: false };
  var STATUS_WORD = { draft: "In progress", ready: "Ready", deployed: "Deployed", rolled_back: "Rolled back",
                      discarded: "Discarded" };
  var FEATURE_LINK_RE = /:8095\/features\/#f(\d+)(-ready)?$/;
  var pendingFocus = null;
  var featureLinks = { count: 0, since: Date.now() };

  try {
    var cached = JSON.parse(safeGet(KEY_FEATURE_CHATS) || "null");
    if (cached && cached.chats) { features.chats = cached.chats; features.enabled = !!cached.enabled; }
  } catch (e) {}
  (function () {
    var t = parseInt(safeGet(KEY_NEW_FEATURE) || "", 10);
    state.pendingFeature = !isNaN(t) && Date.now() - t < 30 * 60 * 1000;
  })();

  function setAttr(el, name, value) {
    if (el && el.getAttribute(name) !== value) el.setAttribute(name, value);  // no-op writes would re-trigger the observer
  }

  function setText(el, value) {
    if (el && el.textContent !== value) el.textContent = value;
  }

  function featureOf(chatId) {
    if (!chatId) return null;
    var f = features.chats[chatId];
    if (f) return f.status === "discarded" ? null : f;
    return safeGet(KEY_FEATURE_PREFIX + chatId) ? { n: null, status: "draft" } : null;
  }

  function userTurns(messages) {
    return Array.isArray(messages) ? messages.filter(function (m) { return m && m.role === "user"; }).length : 0;
  }

  function adoptFeature(chatId) {
    safeSet(KEY_FEATURE_PREFIX + chatId, "1");
    safeSet(modeKey(chatId), "code");
    cancelPendingFeature();
  }

  function cancelPendingFeature() {
    if (!state.pendingFeature) return;
    state.pendingFeature = false;
    try { localStorage.removeItem(KEY_NEW_FEATURE); } catch (e) {}
    scheduleReconcile();
  }

  function startFeatureChat() {
    state.pendingFeature = true;
    safeSet(KEY_NEW_FEATURE, String(Date.now()));
    if (currentChatId) {  // to a new chat, the way Open WebUI's own New Chat does it (no page reload)
      var hidden = document.getElementById("sidebar-new-chat-button");
      var link = document.querySelector('a[href="/"][aria-label="New Chat"]') || document.querySelector('#sidebar a[href="/"]');
      if (link) link.click();
      else if (hidden) hidden.click();
      else location.assign("/");
    }
    scheduleReconcile();
    setTimeout(function () {
      var box = document.getElementById("chat-input");
      if (box) try { editorOf(box).focus(); } catch (e) {}
    }, 300);
  }

  function openFeatures(n) {
    var panel = previewPanel();
    var tab = panel.querySelector('.cp-tab[data-tab="features"]');
    var frame = panel.querySelector(".cp-frame-features");
    tab.hidden = false;
    panel.querySelector('.cp-tab[data-tab="preview"]').hidden = !preview.base;
    if (!frame.getAttribute("src")) {
      frame.setAttribute("src", DEPLOY_ORIGIN + "/features/" + (n ? "#f" + n : ""));
      pendingFocus = n || null;  // also sent once the page says hello
    } else if (n && frame.contentWindow) {
      frame.contentWindow.postMessage({ type: "features:focus", n: n }, DEPLOY_ORIGIN_ONLY);
    }
    setTab("features");
    requestAnimationFrame(function () {
      panel.classList.add("open");
      squeezeChat(true);
    });
    return "ok";
  }

  async function refreshFeatureChats() {
    if (features.refreshing || document.visibilityState === "hidden") return;
    var token = safeGet("token");
    if (!token) return;
    features.refreshing = true;
    try {
      var r = await _fetch(DEPLOY_ORIGIN + "/api/chats", { headers: { Authorization: "Bearer " + token }, cache: "no-store" });
      if (r.status === 401 || r.status === 403) {
        features.enabled = false;  // not an admin (or signed out): no ✦ Feature, no Features entry
        features.chats = {};
      } else if (r.ok) {
        var data = await r.json();
        features.enabled = true;
        features.chats = data.chats || {};
        Object.keys(features.chats).forEach(function (id) { safeSet(KEY_FEATURE_PREFIX + id, "1"); });
      } else {
        return;
      }
      safeSet(KEY_FEATURE_CHATS, JSON.stringify({ enabled: features.enabled, chats: features.chats }));
      scheduleReconcile();
    } catch (e) {
      warnOnce("feature-chats", e);  // code-deploy down: keep the cached list
    } finally {
      features.refreshing = false;
    }
  }

  // Sidebar: a small ✦ before every feature chat's title (green once deployed, grey when rolled back), and a
  // "Features" entry under Workspace with the number of features waiting to be deployed.
  function decorateSidebar() {
    var items = document.querySelectorAll('a[id="sidebar-chat-item"][href^="/c/"]');
    for (var i = 0; i < items.length; i++) {
      var a = items[i];
      var id = (a.getAttribute("href") || "").slice(3);
      var f = featureOf(id);
      var mark = a.querySelector(".ct-feat-mark");
      if (!f) {
        if (mark) mark.parentNode.removeChild(mark);
        continue;
      }
      var titleBox = a.querySelector("div.flex-1") || a.firstElementChild;
      if (!titleBox) continue;
      if (!mark) {
        mark = document.createElement("span");
        mark.textContent = "\u2726";
        titleBox.insertBefore(mark, titleBox.firstChild);
      }
      setAttr(mark, "class", "ct-feat-mark " + (f.status || "draft"));
      setAttr(mark, "title", "Feature" + (f.n ? " #" + f.n : "") + " · " + (f.update_available ? "Update waiting"
                                                                                  : STATUS_WORD[f.status] || "In progress"));
    }
    var ws = document.getElementById("sidebar-workspace-button");
    var nav = document.getElementById("sidebar-features-button");
    if (!features.enabled || !ws || !ws.parentElement) {
      if (nav && nav.parentElement) nav.parentElement.parentNode.removeChild(nav.parentElement);
      return;
    }
    if (!nav) {
      var wrap = ws.parentElement.cloneNode(false);
      nav = ws.cloneNode(false);
      nav.id = "sidebar-features-button";
      nav.setAttribute("href", "/");
      nav.setAttribute("aria-label", "Features");
      nav.innerHTML = '<div class="self-center"><svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" ' +
        'stroke-width="2" stroke="currentColor" class="size-4.5"><path stroke-linecap="round" stroke-linejoin="round" ' +
        'd="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 ' +
        '5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259' +
        '-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 ' +
        '3.375 0 0 0 2.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 0 0-2.456 2.456Z"/></svg></div> ' +
        '<div class="flex self-center translate-y-[0.5px]"><div class="self-center text-sm font-primary">Features</div></div>' +
        '<span class="ct-nav-count"></span>';
      nav.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        openFeatures(null);
      }, true);
      wrap.appendChild(nav);
      ws.parentElement.parentNode.insertBefore(wrap, ws.parentElement.nextSibling);
    }
    var waiting = Object.keys(features.chats).filter(function (k) {
      var f = features.chats[k];
      return f.status === "ready" || f.update_available;
    }).length;
    var count = nav.querySelector(".ct-nav-count");
    setText(count, waiting ? String(waiting) : "");
    setAttr(count, "title", waiting ? waiting + " ready to deploy" : "");
  }

  // On a new chat after ✦ Feature: a quiet line under the pill, so it is clear what this chat is for.
  function placeFeatureHint() {
    var hint = document.getElementById("ct-feature-hint");
    var pill = getPill();
    var show = !currentChatId && state.pendingFeature && pill && pill.isConnected;
    if (!show) {
      if (hint) hint.parentNode.removeChild(hint);
      return;
    }
    if (!hint) {
      hint = document.createElement("div");
      hint.id = "ct-feature-hint";
      hint.innerHTML = "<b>\u2726 Feature chat</b> Ornith builds it on a branch of Code mode; " +
        "nothing is live until you deploy it from Features";
      document.body.appendChild(hint);
    }
    var r = pill.getBoundingClientRect();
    var half = hint.getBoundingClientRect().width / 2;
    var x = Math.max(half + 8, Math.min(r.left + r.width / 2, window.innerWidth - half - 8));
    setAttr(hint, "style", "left:" + Math.round(x) + "px;top:" + Math.round(r.bottom + 10) + "px");
  }

  // Replies in feature chats start with a [Feature #n] link; [Ready to deploy] appears when Ornith marks it ready.
  function decorateFeatureLinks() {
    var links = Array.prototype.filter.call(document.querySelectorAll('a[href*=":8095/features/#f"]'),
                                            function (a) { return FEATURE_LINK_RE.test(a.getAttribute("href")); });
    var ready = null;
    for (var i = 0; i < links.length; i++) {
      var a = links[i];
      var m = FEATURE_LINK_RE.exec(a.getAttribute("href"));
      if (m[2]) ready = a;
      if (a.classList.contains("code-feature-chip")) continue;
      a.classList.add("code-feature-chip");
      if (m[2]) a.classList.add("ready");
      a.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        openFeatures(+FEATURE_LINK_RE.exec(this.getAttribute("href"))[1]);
      }, true);
    }
    // A "Ready to deploy" link that appears while the chat is open (not one rendered with the chat) opens Features.
    if (links.length > featureLinks.count && ready && ready === links[links.length - 1] && Date.now() - featureLinks.since > 2500) {
      openFeatures(+FEATURE_LINK_RE.exec(ready.getAttribute("href"))[1]);
      refreshFeatureChats();
    }
    featureLinks.count = links.length;
  }

  // The Features page asks for the Open WebUI token (it checks the user is an admin with it) and can ask us to open
  // a chat or reload the page after the page script was updated.
  window.addEventListener("message", function (e) {
    try {
      if (e.origin !== DEPLOY_ORIGIN_ONLY || !e.data) return;
      var frame = document.querySelector("#code-preview-panel .cp-frame-features");
      if (!frame || e.source !== frame.contentWindow) return;
      if (e.data.type === "features:hello") {
        e.source.postMessage({ type: "features:token", token: safeGet("token") || "" }, DEPLOY_ORIGIN_ONLY);
        if (pendingFocus) e.source.postMessage({ type: "features:focus", n: pendingFocus }, DEPLOY_ORIGIN_ONLY);
        pendingFocus = null;
      } else if (e.data.type === "features:open-chat" && /^[A-Za-z0-9_-]{1,64}$/.test(e.data.chat_id || "")) {
        var link = document.querySelector('a[href="/c/' + e.data.chat_id + '"]');
        if (link) link.click();
        else location.assign("/c/" + e.data.chat_id);
      } else if (e.data.type === "features:reload-page") {
        location.reload();
      } else if (e.data.type === "features:changed") {
        refreshFeatureChats();
      }
    } catch (err) {
      warnOnce("features-message", err);
    }
  });

  window.__codeToggle.openFeatures = openFeatures;
  window.__codeToggle.openChecklist = openChecklist;
  window.__codeToggle.startFeatureChat = startFeatureChat;

  // ------------------------------------------------------------ start coding
  // The "Desktop and Code" tool's start_coding runs window.__codeToggle.startCoding(task) in this page: switch the
  // chat to Code, then send the task as the next message once the current reply has finished, so the coding
  // agent's work shows up as a normal Code turn (streaming, preview, diff).
  var pendingTask = null; // {chatId, task, at}

  var BUILD_IT = "Let's build it.";

  function startCoding(task) {
    setPillMode("code");
    task = String(task || "").trim();
    pendingTask = { chatId: currentChatId, task: task, at: Date.now() };
    if (currentChatId && task) safeSet("codeToggle:task:" + currentChatId, task);
    return "ok";
  }

  // The visible message is just BUILD_IT; the full task the chat model wrote goes to the coding agent only.
  function withStashedTask(parsed, chatId) {
    var msgs = parsed.messages;
    var last = Array.isArray(msgs) && msgs.length ? msgs[msgs.length - 1] : null;
    if (!chatId || !last || last.role !== "user" || typeof last.content !== "string") return;
    if (last.content.trim().indexOf(BUILD_IT) !== 0) return;
    var task = safeGet("codeToggle:task:" + chatId);
    if (!task) return;
    last.content = "Let's build it: " + task;
    try { localStorage.removeItem("codeToggle:task:" + chatId); } catch (e) {}
  }

  // The latest reply is finished once Open WebUI renders its toolbar (it holds the hidden regenerate button).
  // Not "the send button is back": with an empty box Open WebUI shows the voice button instead of send.
  // Only message-<uuid>: other ids start with "message-" too (message-input-container comes after the messages).
  var MESSAGE_ID_RE = /^message-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  function replyFinished() {
    var msgs = Array.prototype.filter.call(document.querySelectorAll('[id^="message-"]'),
                                           function (el) { return MESSAGE_ID_RE.test(el.id); });
    var last = msgs.length ? msgs[msgs.length - 1] : null;
    return !!(last && last.querySelector(".regenerate-response-button"));
  }

  function editorOf(box) {
    return box.matches("[contenteditable]") ? box : box.querySelector('[contenteditable="true"]') || box;
  }

  // The typing mechanics the compose path has proven in real Open WebUI (caretToEnd, insertPiece, boxText —
  // defined with the compose section below; function declarations hoist) are shared by every path that types a
  // message in from outside. What does NOT work (bug report 2026-10-19, Ask the agent): one insertText carrying
  // the whole multi-line text with the selection covering the whole box — the editor takes nothing, and the old
  // code then pasted the whole brief at once, which Open WebUI turns into a pasted-text FILE entry (its
  // PASTED_TEXT_CHARACTER_LIMIT is 1000): the user saw a blank entry and no message.
  function probeOf(text) {
    var lines = String(text).split("\n");
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].trim()) return lines[i].trim().slice(0, 40); // the first line, to check the text landed
    }
    return "";
  }

  // Paste the text in line-bounded chunks under Open WebUI's 1000-character limit, so each is taken as text.
  function pasteText(editable, text) {
    var chunk = "";
    var paste = function (piece) {
      var dt = new DataTransfer();
      dt.setData("text/plain", piece);
      editable.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
    };
    var flush = function () { if (chunk) { paste(chunk); chunk = ""; } };
    var lines = String(text).split("\n");
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i] + (i + 1 < lines.length ? "\n" : "");
      while (line.length > 900) { flush(); paste(line.slice(0, 900)); line = line.slice(900); }
      if ((chunk + line).length > 900) flush();
      chunk += line;
    }
    flush();
  }

  function typeInto(editable, text) {
    var canPaste = typeof DataTransfer === "function" && typeof ClipboardEvent === "function";
    if (canPaste && String(text).indexOf("\n") >= 0 && editable.classList && editable.classList.contains("ProseMirror")) {
      /* Open WebUI's editor (Tiptap/ProseMirror) and a multi-line text: paste it, and let finishTypedMessage judge
         on a later poll whether it landed. Typed in line by line in one go (insertParagraph + insertText), the text
         IS in the box for an instant - so a check made at once said "landed" and skipped the paste - and then
         ProseMirror puts its own state back, leaving the empty paragraphs the user saw as a blank entry (measured
         headless on the live page 2026-10-19: gone at 0 ms; a paste of the same text stays and enables Send). */
      caretToEnd(editable);
      pasteText(editable, text);
      return;
    }
    if (document.execCommand) {
      caretToEnd(editable); // a collapsed caret at the end, the way a real caret sits there
      insertPiece(editable, text); // line by line: insertParagraph between, insertText per line
    }
    var probe = probeOf(text);
    var landed = function () { return boxText(editable).indexOf(probe) >= 0; };
    if (!landed() && canPaste) pasteText(editable, text); // Open WebUI's editor also takes a paste
    if (!document.execCommand && !landed()) {
      /* A page with no execCommand and nothing listening for the paste (a test page): put the text in the box
         directly. A real browser has execCommand, so its editor is never written into from outside. */
      if (editable.tagName === "TEXTAREA") editable.value = (boxText(editable) ? boxText(editable) + "\n" : "") + text;
      else editable.textContent = (editable.textContent ? editable.textContent + "\n" : "") + text;
      editable.dispatchEvent(new window.Event("input", { bubbles: true }));
    }
  }

  // The half start_coding and Ask the agent share once the text has been typed once: Open WebUI may take the
  // message only after it is VERIFIED in the box. Retype a bounded number of times if it never landed, and give
  // up quietly: Enter in an editor that does not hold the text would only split it, leaving the user a blank
  // line where a message should have been. Returns true when this pending message is finished with.
  function finishTypedMessage(p, editable, text) {
    var txt = boxText(editable);
    if (txt.indexOf(probeOf(text)) < 0) {
      if (txt.trim()) {
        console.log("Code mode: the chat's message box no longer holds the message; nothing was sent.");
        return true; // the user typed in it: leave the box to them
      }
      if ((p.tries || 1) < 3) { p.tries = (p.tries || 1) + 1; typeInto(editable, text); return false; }
      console.log("Code mode: the message never reached the chat's message box; nothing was sent.");
      return true;
    }
    var send = document.getElementById("send-message-button"); // Open WebUI enables it once the box has text
    if (send && !send.disabled) { send.click(); return true; }
    if (Date.now() - p.typedAt > 5000) { // no send button: Enter submits in Open WebUI's editor — with the text verified in the box
      editable.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true, cancelable: true }));
      return true;
    }
    return false;
  }

  function sendPendingTask() {
    var p = pendingTask;
    if (!p) return;
    if (Date.now() - p.at > 15 * 60 * 1000 || p.chatId !== currentChatId) {
      pendingTask = null; // gave up, or the user went to another chat
      return;
    }
    var box = document.getElementById("chat-input");
    if (!box) return;
    var editable = editorOf(box);
    if (!p.typedAt) {
      if (Date.now() - p.at < 1500 || !replyFinished()) return;
      if (boxText(editable).trim()) {
        pendingTask = null; // the user is typing something: leave the box to them
        return;
      }
      if (!p.task) {
        pendingTask = null;
        return;
      }
      typeInto(editable, BUILD_IT);
      p.typedAt = Date.now();
      p.tries = 1;
      return;
    }
    if (finishTypedMessage(p, editable, BUILD_IT)) pendingTask = null;
  }

  // ------------------------------------------------------------- ask the agent
  // Diagnose's "Ask the agent": the pipe wrote the brief (the findings, the facts, and what may be done about
  // them); the panel sends it as the chat's next message, so the coding agent investigates and fixes in a normal
  // Code turn the user can watch. It waits for the same things start_coding waits for: a finished reply and an
  // empty box. Pushing and pulling themselves stay in the panel: the brief tells the agent so.
  var pendingAsk = null; // {chatId, text, at}

  function ghAskAgent() {
    var d = gh.diag;
    var brief = d && d.data && d.data.brief;
    if (!brief || !currentChatId || d.chatId !== currentChatId || pendingTask || pendingAsk) return;
    pendingAsk = { chatId: currentChatId, text: String(brief), at: Date.now() };
    ghClose(); // the panel steps aside: the user watches the brief land in the chat
  }

  function sendPendingAsk() {
    var p = pendingAsk;
    if (!p) return;
    if (Date.now() - p.at > 15 * 60 * 1000 || p.chatId !== currentChatId) {
      pendingAsk = null; // gave up, or the user went to another chat
      return;
    }
    var box = document.getElementById("chat-input");
    if (!box) return;
    var editable = editorOf(box);
    if (!p.typedAt) {
      if (Date.now() - p.at < 1500 || !replyFinished()) return;
      if (boxText(editable).trim()) {
        pendingAsk = null; // the user is typing something: leave the box to them
        return;
      }
      typeInto(editable, p.text);
      p.typedAt = Date.now();
      p.tries = 1;
      return;
    }
    if (finishTypedMessage(p, editable, p.text)) pendingAsk = null;
  }

  window.__codeToggle.setMode = setPillMode;
  window.__codeToggle.startCoding = startCoding;

  // ---------------------------------------------------------------- compose
  // A message typed in from outside: an admin (Claude driving a build) queues it for a chat at code-deploy
  // (POST /api/compose), and the page open on that chat types it into the message box, so the user watches it
  // land, then sends it -- the way start_coding sends "Let's build it." (2026-10-09, user: "use that to type in
  // chat"). It waits for the reply to finish and for an empty box; if the user is typing, the message goes back
  // to the queue.
  var TAB_ID = Math.random().toString(36).slice(2, 12);
  var COMPOSE_TYPE_MS = 3000; // typing takes about this long, whatever the length

  function sleep(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }

  function boxText(el) {
    return el.tagName === "TEXTAREA" ? el.value : el.textContent || "";
  }

  function composeReady() {
    if (!currentChatId || pendingTask) return false;
    var box = document.getElementById("chat-input");
    if (!box || boxText(editorOf(box)).trim()) return false;
    var any = Array.prototype.some.call(document.querySelectorAll('[id^="message-"]'),
                                        function (el) { return MESSAGE_ID_RE.test(el.id); });
    return !any || replyFinished();
  }

  function caretToEnd(el) {
    el.focus();
    if (el.tagName === "TEXTAREA") {
      el.selectionStart = el.selectionEnd = el.value.length;
      return;
    }
    var range = document.createRange();
    range.selectNodeContents(el);
    range.collapse(false);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  function insertPiece(el, text) {
    if (el.tagName === "TEXTAREA") {
      document.execCommand("insertText", false, text);
      return;
    }
    text.split("\n").forEach(function (part, i) {
      if (i) document.execCommand("insertParagraph");
      if (part) document.execCommand("insertText", false, part);
    });
  }

  // A little at a time, by the clock, so a throttled background tab still finishes in a few ticks.
  async function typeMessage(el, text) {
    var before = boxText(el).length;
    var t0 = Date.now();
    var done = 0;
    while (done < text.length) {
      var upto = Math.max(done + 1, Math.ceil(text.length * Math.min(1, (Date.now() - t0) / COMPOSE_TYPE_MS)));
      caretToEnd(el);
      insertPiece(el, text.slice(done, upto));
      if (!done && boxText(el).length <= before) {
        // execCommand did nothing (the window has no focus): Open WebUI's editor also takes a paste
        var dt = new DataTransfer();
        dt.setData("text/plain", text);
        el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
        return;
      }
      done = upto;
      await sleep(30);
    }
  }

  // Send it the way a person would; Open WebUI empties the box once it has taken the message.
  async function sendTyped(el) {
    await sleep(500);
    var send = document.getElementById("send-message-button");
    if (send && !send.disabled) send.click();
    else el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true, cancelable: true }));
    for (var i = 0; i < 25; i++) {
      await sleep(400);
      var box = document.getElementById("chat-input");
      if (!box || !boxText(editorOf(box)).trim()) return true;
    }
    return false;
  }

  function composeReport(token, item, status, note) {
    return _fetch(DEPLOY_ORIGIN + "/api/compose/" + encodeURIComponent(item.id), {
      method: "POST",
      headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      body: JSON.stringify({ status: status, note: note || "" }),
      cache: "no-store"
    }).catch(function () {});
  }

  async function composeLoop() {
    for (;;) {
      var token = safeGet("token");
      if (!token || !composeReady()) {
        await sleep(1500);
        continue;
      }
      var item = null;
      try {
        var r = await _fetch(DEPLOY_ORIGIN + "/api/compose?chat=" + encodeURIComponent(currentChatId) + "&wait=25&tab=" + TAB_ID,
                             { headers: { Authorization: "Bearer " + token }, cache: "no-store" });
        if (r.status === 401 || r.status === 403) { // not an admin: there is never anything for this page
          await sleep(300000);
          continue;
        }
        if (!r.ok) throw new Error("compose " + r.status);
        item = (await r.json()).item;
      } catch (e) {
        await sleep(10000);
        continue;
      }
      if (!item) continue;
      try {
        var box = document.getElementById("chat-input");
        var el = box && editorOf(box);
        if (item.chat_id !== currentChatId || !el || !composeReady()) {
          composeReport(token, item, "requeue"); // another chat now, or the user started typing
          await sleep(3000);
          continue;
        }
        if (item.mode) setPillMode(item.mode);
        await typeMessage(el, item.text.replace(/^\s+/, ""));
        if (!item.send) composeReport(token, item, "typed");
        else if (await sendTyped(el)) composeReport(token, item, "sent");
        else composeReport(token, item, "failed", "typed, but Open WebUI did not take it (the text is still in the box)");
      } catch (e) {
        warnOnce("compose", e);
        composeReport(token, item, "failed", String((e && e.message) || e));
      }
    }
  }

  // ---------------------------------------------------------- queue on cryoai
  // Messages typed while a reply runs. Open WebUI queues them in this tab's memory and sends them when this tab sees
  // the reply finish, so a tab that slept, crashed or reloaded first lost them for good (chat 0c682232, 2026-10-29: a
  // four-hour Code turn, the laptop asleep, then a reboot). Now each one moves to code-deploy on cryoai the moment
  // Open WebUI queues it, and leaves Open WebUI's queue so it can't go out twice. It goes out once the reply has
  // finished: a tab open on the chat types it in and sends it the normal way, and when no tab does (asleep, closed),
  // code-wake on cryoai sends a Code chat's queue itself. Stop holds the queue, as Open WebUI's own Stop did; Send now,
  // Edit and Delete work as before. A message with files stays in Open WebUI's own queue (its files can't be read
  // from here), and so does everything while cryoai's queue does not answer.
  var CQ_ID = "code-queue";
  var CQ_OUTBOX = "codeQueue:outbox";    // moved out of Open WebUI's queue, not yet confirmed by cryoai
  var CQ_RELOADED = "codeQueue:reloaded"; // sessionStorage: chat -> when this tab last reloaded for the queue
  var CQ_FAST_MS = 3000, CQ_RUN_MS = 6000, CQ_SLOW_MS = 15000, CQ_HIDDEN_MS = 30000, CQ_RETRY_MS = 5000;
  var CQ_STALE_MS = 20000;   // Stop still showing this long while Open WebUI runs no task: this tab missed the end
  var STOP_ICON = "M2.25 12c0-5.385"; // Open WebUI's Stop button, shown instead of Send while a reply runs
  var cq = { chat: null, seen: null, items: [], at: 0, ok: false, down: 0, busy: false, timer: 0, flushing: false,
             readySince: 0, claiming: false, sending: null, typeBackoff: 0, runningSince: 0, staleChecks: 0,
             loadedAt: Date.now(), banner: null, sig: "", started: false, again: false, wasRunning: null };
  var CQ_ICONS = {
    cloud: "M2.25 15a4.5 4.5 0 0 0 4.5 4.5H18a3.75 3.75 0 0 0 1.332-7.257 3 3 0 0 0-3.758-3.848 5.25 5.25 0 0 0-10.233 2.33A4.502 4.502 0 0 0 2.25 15Z",
    wait: "M13.5 4.5 21 12m0 0-7.5 7.5M21 12H3",
    now: "M4.5 10.5 12 3m0 0 7.5 7.5M12 3v18",
    edit: "m16.862 4.487 1.687-1.688a1.875 1.875 0 1 1 2.652 2.652L10.582 16.07a4.5 4.5 0 0 1-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 0 1 1.13-1.897l8.932-8.931Zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0 1 15.75 21H5.25A2.25 2.25 0 0 1 3 18.75V8.25A2.25 2.25 0 0 1 5.25 6H10",
    del: "m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0"
  };
  window.__codeToggle.reload = function () { location.reload(); }; // one place, so the page tests can watch it

  function cqStyle() {
    if (document.getElementById("code-queue-style")) return;
    var st = document.createElement("style");
    st.id = "code-queue-style";
    st.textContent = [
      "#code-queue{margin:0 8px 4px;padding:2px 6px;border-radius:16px;background:#fff;border:1px solid #f3f4f6;max-height:25vh;overflow-x:hidden;overflow-y:auto}",
      "html.dark #code-queue{background:rgba(17,24,39,.6);border-color:rgba(31,41,55,.5)}",
      "#code-queue .cq-row{display:flex;align-items:center;gap:8px;padding:6px 8px}",
      "#code-queue .cq-ic{flex:none;width:14px;height:14px;color:#8b5cf6}html.dark #code-queue .cq-ic{color:#a78bfa}",
      "#code-queue .cq-row[data-local=\"1\"] .cq-ic{color:#9ca3af}",
      "#code-queue .cq-main{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;gap:1px}",
      "#code-queue .cq-text{margin:0;font-size:14px;line-height:1.35;color:#4b5563;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      "html.dark #code-queue .cq-text{color:#d1d5db}",
      "#code-queue .cq-st{font-size:11.5px;line-height:1.3;color:#9ca3af}html.dark #code-queue .cq-st{color:#6b7280}",
      "#code-queue .cq-row[data-tone=\"warn\"] .cq-st{color:#d97706}html.dark #code-queue .cq-row[data-tone=\"warn\"] .cq-st{color:#f5a524}",
      "#code-queue .cq-acts{display:flex;align-items:center;gap:2px;flex:none}",
      "#code-queue .cq-b{appearance:none;-webkit-appearance:none;border:0;background:transparent;padding:4px;border-radius:6px;color:#9ca3af;cursor:pointer;display:inline-flex}",
      "#code-queue .cq-b:hover{color:#374151}html.dark #code-queue .cq-b:hover{color:#d1d5db}",
      "#code-queue .cq-b:focus-visible,#code-queue .cq-reload:focus-visible{outline:2px solid #4f8cff;outline-offset:1px}",
      "#code-queue .cq-b svg{width:14px;height:14px}",
      "#code-queue .cq-reload{appearance:none;border:1px solid #c4b5fd;background:transparent;color:#7c3aed;border-radius:999px;padding:3px 10px;font:600 12px/1.2 system-ui,-apple-system,\"Segoe UI\",Roboto,sans-serif;cursor:pointer;flex:none}",
      "html.dark #code-queue .cq-reload{border-color:#5b4a8a;color:#c4b5fd}"
    ].join("\n");
    (document.head || document.documentElement).appendChild(st);
  }

  function cqApi(method, path, body) {
    var token = safeGet("token");
    if (!token) return Promise.resolve({ ok: false, status: 401, data: null });
    var init = { method: method, headers: { Authorization: "Bearer " + token }, cache: "no-store" };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    return _fetch(DEPLOY_ORIGIN + path, init).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (d) { return { ok: r.ok, status: r.status, data: d }; });
    }, function () {
      return { ok: false, status: 0, data: null };
    });
  }

  /** true / false: Open WebUI runs / runs no task for the chat; null: it could not say. */
  function owuiTasks(chat) {
    var token = safeGet("token");
    return _fetch("/api/tasks/chat/" + encodeURIComponent(chat), { headers: { Authorization: "Bearer " + (token || "") },
                                                                   cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { return d && Array.isArray(d.task_ids) ? d.task_ids.length > 0 : null; },
            function () { return null; });
  }

  function owuiStopButton() {
    var mic = document.getElementById("message-input-container");
    if (!mic) return null;
    var paths = mic.querySelectorAll("button path");
    for (var i = 0; i < paths.length; i++) {
      if ((paths[i].getAttribute("d") || "").indexOf(STOP_ICON) === 0) return paths[i].closest("button");
    }
    return null;
  }

  function owuiRunning() {
    return !!owuiStopButton();
  }

  function cqReplyDone() {
    var any = Array.prototype.some.call(document.querySelectorAll('[id^="message-"]'),
                                        function (el) { return MESSAGE_ID_RE.test(el.id); });
    return !any || replyFinished();
  }

  function cqMode(chatId) {
    if (featureOf(chatId)) return "code";
    var saved = safeGet(modeKey(chatId));
    return isMode(saved) ? saved : state.mode;
  }

  // What a Code turn sends besides the text (handleRequest adds the same), for when cryoai sends it with no tab.
  function cqExtras(chatId) {
    var x = {};
    var pick = safeGet(KEY_PICK_PREFIX + chatId) || "";
    if (pick) x.code_model = pick;
    var effort = null;
    try { effort = pick ? effortFor(pick) : null; } catch (e) { effort = null; }
    if (effort) x.code_effort = effort;
    var repo = safeGet(KEY_REPO_PREFIX + chatId);
    if (repo) x.code_repo = repo;
    if (featureOf(chatId)) x.code_feature = true;
    return x;
  }

  // ---- this browser's outbox: moved out of Open WebUI's queue, not yet on cryoai (kept across reloads)
  function cqOutbox() {
    try {
      var a = JSON.parse(safeGet(CQ_OUTBOX) || "[]");
      return Array.isArray(a) ? a : [];
    } catch (e) {
      return [];
    }
  }

  function cqOutboxSet(a) {
    safeSet(CQ_OUTBOX, JSON.stringify(a));
  }

  function cqOutboxDrop(lid) {
    cqOutboxSet(cqOutbox().filter(function (x) { return x.lid !== lid; }));
  }

  function cqFlush() {
    if (cq.flushing) return;
    var it = cqOutbox().filter(function (x) { return !x.err; })[0];
    if (!it) return;
    cq.flushing = true;
    cqApi("POST", "/api/queue", { chat: it.chat, text: it.text, mode: it.mode, extras: it.extras, lid: it.lid,
                                  held: !!it.held })
      .then(function (r) {
        if (r.ok && r.data && r.data.item) {
          var still = cqOutbox().some(function (x) { return x.lid === it.lid; });
          cqOutboxDrop(it.lid);
          if (!still) cqApi("POST", "/api/queue/" + encodeURIComponent(r.data.item.id), { op: "cancel" }); // deleted meanwhile
          cq.down = 0;
          return true;
        }
        if (r.status === 400 || r.status === 403 || r.status === 404) {   // cryoai said no: tell the user, keep it here
          var a = cqOutbox();
          a.forEach(function (x) { if (x.lid === it.lid) x.err = (r.data && r.data.error) || "cryoai said " + r.status; });
          cqOutboxSet(a);
          return true;
        }
        if (!cq.down) cq.down = Date.now();                              // not answering: try again shortly
        return false;
      })
      .then(function (go) {
        cq.flushing = false;
        cqRender();
        if (go) {
          cqFlush();
          cqPollSoon();
        } else {
          setTimeout(cqFlush, CQ_RETRY_MS);
        }
      });
  }

  // ---- Open WebUI's own queue: move what it queues to cryoai
  function cqOwuiRows() {   // its queued-messages box sits right before the message box (ours aside)
    var mic = document.getElementById("message-input-container");
    var n = mic && mic.previousElementSibling;
    if (n && n.id === CQ_ID) n = n.previousElementSibling;
    if (!n) return [];
    return Array.prototype.filter.call(n.children, function (r) {
      var acts = r.lastElementChild;
      return r.children.length >= 3 && acts && acts.querySelectorAll("button").length === 3;
    });
  }

  function cqCapture() {
    var chat = currentChatId;
    if (!chat || !cq.ok || cq.chat !== chat) return; // cryoai's queue is not answering: Open WebUI keeps its own
    var moved = 0;
    cqOwuiRows().forEach(function (row) {
      if (row.getAttribute("data-cq")) return;
      var content = row.children[1];
      var p = content && content.querySelector("p");
      var text = p ? p.textContent || "" : "";
      var files = !content || content.children.length !== 1 || !!content.querySelector("img");
      var del = row.lastElementChild.querySelectorAll("button")[2];
      if (!p || files || p.classList.contains("italic") || !text.trim() || !del) {
        row.setAttribute("data-cq", "kept"); // files can't be read from here: Open WebUI keeps this one
        return;
      }
      row.setAttribute("data-cq", "moved");
      var a = cqOutbox();
      a.push({ lid: Math.random().toString(36).slice(2, 12) + Date.now().toString(36), chat: chat, text: text,
               mode: cqMode(chat), extras: cqExtras(chat), at: Date.now() });
      cqOutboxSet(a);
      del.click(); // out of Open WebUI's queue in the same step, so it can't go out twice
      moved++;
    });
    if (moved) {
      cqRender();
      cqFlush();
    }
  }

  // ---- the list for the open chat
  function cqPollSoon() {
    clearTimeout(cq.timer);
    cq.timer = setTimeout(cqPoll, 300);
  }

  function cqSchedule() {
    clearTimeout(cq.timer);
    var mine = cq.items.some(function (it) { return it.status === "queued" || it.status === "claimed"; }) ||
      cqOutbox().some(function (x) { return x.chat === currentChatId; });
    var ms = document.hidden ? CQ_HIDDEN_MS : mine ? CQ_FAST_MS : owuiRunning() ? CQ_RUN_MS : CQ_SLOW_MS;
    cq.timer = setTimeout(cqPoll, ms);
  }

  function cqPoll() {
    clearTimeout(cq.timer);
    var chat = currentChatId;
    if (cq.busy) { // asked again mid-call (another chat, a change): once more right after
      cq.again = true;
      return;
    }
    if (!chat || !safeGet("token")) {
      cqSchedule();
      return;
    }
    cq.busy = true;
    cqApi("GET", "/api/queue?chat=" + encodeURIComponent(chat) + "&visible=" + (document.hidden ? 0 : 1))
      .then(function (r) {
        if (chat !== currentChatId) return;
        if (r.ok && r.data && Array.isArray(r.data.items)) {
          cq.chat = chat;
          cq.items = r.data.items;
          cq.at = Date.now();
          cq.ok = true;
          cq.down = 0;
          cqAfterPoll(chat);
        } else {
          cq.ok = false; // no queue on cryoai right now: Open WebUI's own queue is left alone
          if (r.status === 0 || r.status >= 500) cq.down = cq.down || Date.now();
        }
      })
      .catch(function (e) { warnOnce("queue-poll", e); })
      .then(function () {
        cq.busy = false;
        cqRender();
        cqCapture();
        cqFlush();
        if (cq.again) {
          cq.again = false;
          cqPollSoon();
        } else {
          cqSchedule();
        }
      });
  }

  function cqAfterPoll(chat) {
    // Sent by cryoai after this page loaded, and not on the page: this tab was away (asleep, or stuck on a reply
    // that had ended). Load the chat again so it shows the new turn; a later save from a stale page would drop it.
    var rendered = Array.prototype.some.call(document.querySelectorAll('[id^="message-"]'),
                                             function (el) { return MESSAGE_ID_RE.test(el.id); });
    var missed = rendered && cq.items.some(function (it) {
      return it.status === "sent" && it.sent_by === "code-wake" && it.message_id &&
        (it.finished_at || 0) * 1000 > cq.loadedAt && !document.getElementById("message-" + it.message_id);
    });
    if (missed) {
      cqReload(chat);
      return;
    }
    // Stop still showing while Open WebUI runs nothing for the chat: this tab never heard that the reply ended
    // (it slept through it), so it would never send the queue. Load the chat again.
    var due = cq.items.some(function (it) { return it.status === "queued"; });
    if (document.hidden || !due || !owuiRunning()) {
      cq.runningSince = 0;
      cq.staleChecks = 0;
      return;
    }
    if (!cq.runningSince) cq.runningSince = Date.now();
    if (Date.now() - cq.runningSince < CQ_STALE_MS) return;
    owuiTasks(chat).then(function (busy) {
      if (chat !== currentChatId) return;
      cq.staleChecks = busy === false ? cq.staleChecks + 1 : 0;
      if (cq.staleChecks >= 2) cqReload(chat);
    });
  }

  function cqReload(chat) {
    var box = document.getElementById("chat-input");
    var typing = !!(box && boxText(editorOf(box)).trim());
    var marks = {};
    try { marks = JSON.parse(sessionStorage.getItem(CQ_RELOADED) || "{}") || {}; } catch (e) { marks = {}; }
    if (typing || cq.sending || Date.now() - (marks[chat] || 0) < 5 * 60000) {
      cq.banner = chat; // the user is typing (or this tab reloaded for it already): ask instead
      cqRender();
      return;
    }
    marks[chat] = Date.now();
    try { sessionStorage.setItem(CQ_RELOADED, JSON.stringify(marks)); } catch (e) { /* the reload matters */ }
    window.__codeToggle.reload();
  }

  // ---- sending from this tab, once the reply has finished
  function cqTick() {
    if (cq.seen !== currentChatId) { // another chat: its own list
      cq.seen = currentChatId;
      cq.items = [];
      cq.ok = false;
      cq.chat = null;
      cq.banner = null;
      cq.runningSince = 0;
      cq.staleChecks = 0;
      cq.readySince = 0;
      cqRender();
      cqPollSoon();
    }
    var running = owuiRunning();
    if (running !== cq.wasRunning) { // a reply started or ended here: look at the queue now, not at the next poll
      cq.wasRunning = running;
      cqRender();
      cqPollSoon();
    }
    if (cq.sending) {
      cqSendStep();
      return;
    }
    cqDeliverCheck();
  }

  function cqDeliverCheck() {
    var chat = currentChatId;
    var due = chat && cq.ok && cq.chat === chat && Date.now() - cq.at < 20000 &&
      cq.items.some(function (it) { return it.status === "queued"; });
    var box = document.getElementById("chat-input");
    var el = box && editorOf(box);
    if (!due || document.hidden || !el || boxText(el).trim() || pendingTask || pendingAsk || owuiRunning() ||
        !cqReplyDone() || Date.now() < cq.typeBackoff) {
      cq.readySince = 0;
      return;
    }
    if (!cq.readySince) {
      cq.readySince = Date.now();
      return;
    }
    if (Date.now() - cq.readySince < 1500 || cq.claiming) return;
    cq.claiming = true;
    owuiTasks(chat).then(function (busy) {
      if (busy !== false || chat !== currentChatId) return null; // a turn runs, or Open WebUI can't say: not now
      return cqApi("POST", "/api/queue/claim", { chat: chat, by: "tab:" + TAB_ID });
    }).then(function (r) {
      var c = r && r.ok && r.data && r.data.claim ? r.data : null;
      if (!c) return;
      var box2 = document.getElementById("chat-input");
      var el2 = box2 && editorOf(box2);
      if (chat !== currentChatId || !el2 || boxText(el2).trim() || owuiRunning()) {
        cqFinish(c.claim, "requeue", "");
        return;
      }
      if (c.mode && !featureOf(chat)) setPillMode(c.mode); // the mode it was queued in
      typeInto(el2, c.text);
      cq.sending = { chat: chat, claim: c.claim, text: c.text, typedAt: Date.now(), tries: 1, clicked: 0 };
      cq.items.forEach(function (it) { if (it.status === "queued") it.status = "claimed"; });
      cqRender();
    }).catch(function (e) {
      warnOnce("queue-claim", e);
    }).then(function () {
      cq.claiming = false;
      cq.readySince = 0;
    });
  }

  function cqFinish(claim, status, note) {
    return cqApi("POST", "/api/queue/claim/" + encodeURIComponent(claim), { status: status, note: note || "" })
      .then(function () { cqPollSoon(); });
  }

  function cqSendStep() {
    var s = cq.sending;
    var box = document.getElementById("chat-input");
    var el = box && editorOf(box);
    if (s.chat !== currentChatId || !el) {
      // Gone to another chat mid-send. If Send was clicked we can't tell from here whether it went: the claim runs
      // out on cryoai, which then looks in the chat for the text (sent) or queues it again (not sent).
      if (!s.clicked) cqFinish(s.claim, "requeue", "");
      cq.sending = null;
      return;
    }
    var txt = boxText(el);
    var landed = txt.indexOf(probeOf(s.text)) >= 0;
    if (!s.clicked) {
      if (!landed) {
        if (txt.trim()) { // the user typed over it
          cqFinish(s.claim, "requeue", "");
          cq.sending = null;
        } else if (s.tries < 3 && Date.now() - s.typedAt > 700) {
          s.tries++;
          s.typedAt = Date.now();
          typeInto(el, s.text);
        } else if (s.tries >= 3 && Date.now() - s.typedAt > 2000) {
          cqFinish(s.claim, "requeue", "This tab could not type it into the message box; it tries again in a minute.");
          cq.typeBackoff = Date.now() + 60000;
          cq.sending = null;
        }
        return;
      }
      var send = document.getElementById("send-message-button");
      if (send && !send.disabled) {
        send.click();
        s.clicked = Date.now();
      } else if (Date.now() - s.typedAt > 5000) { // no Send button: Enter sends, with the text verified in the box
        el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true, cancelable: true }));
        s.clicked = Date.now();
      }
      return;
    }
    if (!landed) { // Open WebUI took it and emptied the box
      cqFinish(s.claim, "sent", "");
      cq.sending = null;
      return;
    }
    if (Date.now() - s.clicked > 10000) {
      // Not taken. The text is still in the box, where the user can send it, so it must not go out from here again.
      cqFinish(s.claim, "failed", "Typed into the message box, but Open WebUI did not send it. It is still in the box.");
      cq.sending = null;
    }
  }

  // ---- Stop holds the queue (Open WebUI's Stop never sent its queue either)
  function cqOnStop(e) {
    try {
      var chat = currentChatId;
      if (!chat) return;
      var hit = false;
      if (e.type === "click") {
        var stop = owuiStopButton();
        hit = !!(stop && e.target && (e.target === stop || stop.contains(e.target)));
      } else if (e.key === "Escape") { // Esc in the message box stops the reply too
        var box = document.getElementById("chat-input");
        hit = !!(box && e.target && box.contains(e.target) && owuiRunning());
      }
      if (hit) cqHold(chat);
    } catch (err) {
      warnOnce("queue-stop", err);
    }
  }

  function cqHold(chat) {
    var a = cqOutbox();
    var changed = false;
    a.forEach(function (x) { if (x.chat === chat && !x.held) { x.held = true; changed = true; } });
    if (changed) cqOutboxSet(a);
    if (cq.chat === chat) cq.items.forEach(function (it) { if (it.status === "queued" && !it.send_now) it.status = "held"; });
    cqRender();
    cqApi("POST", "/api/queue/hold", { chat: chat }).then(function () { cqPollSoon(); });
  }

  // ---- Send now / Edit / Delete
  function cqRowsFor(chat) {
    var rows = [];
    if (chat && cq.chat === chat) {
      cq.items.forEach(function (it) {
        if (it.status === "queued" || it.status === "held" || it.status === "claimed" || it.status === "failed") rows.push(it);
      });
    }
    cqOutbox().forEach(function (x) {
      if (x.chat === chat) rows.push({ local: true, lid: x.lid, text: x.text, err: x.err, held: x.held, mode: x.mode });
    });
    return rows;
  }

  function cqFind(id, lid) {
    return cqRowsFor(currentChatId).filter(function (r) { return lid ? r.lid === lid : r.id === id; })[0] || null;
  }

  function cqDrop(r, then) {
    if (r.local) {
      cqOutboxDrop(r.lid);
      cqRender();
      if (then) then();
      return;
    }
    cqApi("POST", "/api/queue/" + encodeURIComponent(r.id), { op: "cancel" }).then(function (res) {
      if (!res.ok) {
        r.note = (res.data && res.data.error) || "cryoai is not answering";
        r.flash = true;
        cqRender();
        return;
      }
      cq.items = cq.items.filter(function (it) { return it.id !== r.id; });
      cqRender();
      if (then) then();
    });
  }

  function cqPutBack(text) { // Edit: the text goes back into the message box, after anything already there
    var box = document.getElementById("chat-input");
    var el = box && editorOf(box);
    if (el) typeInto(el, (boxText(el).trim() ? "\n\n" : "") + text);
  }

  function cqSendNow(r) {
    var chat = currentChatId;
    cqApi("POST", "/api/queue/" + encodeURIComponent(r.id), { op: "send_now" }).then(function (res) {
      if (!res.ok) {
        r.note = (res.data && res.data.error) || "cryoai is not answering";
        r.flash = true;
        cqRender();
        return;
      }
      r.status = "queued";
      r.send_now = true;
      var stop = owuiStopButton();
      if (stop && chat === currentChatId) stop.click(); // stop the reply (which holds the others); this one goes next
      cqRender();
      cqPollSoon();
    });
  }

  function cqClick(e) {
    var b = e.target && e.target.closest ? e.target.closest("[data-cq-act]") : null;
    if (!b) return;
    e.preventDefault();
    e.stopPropagation();
    var act = b.getAttribute("data-cq-act");
    if (act === "reload") {
      window.__codeToggle.reload();
      return;
    }
    var row = b.closest(".cq-row");
    var r = row && cqFind(row.getAttribute("data-cq-id"), row.getAttribute("data-cq-lid"));
    if (!r) return;
    if (act === "now") cqSendNow(r);
    else if (act === "edit") cqDrop(r, function () { cqPutBack(r.text); });
    else if (act === "del") cqDrop(r);
  }

  function cqStatus(r, running) {
    if (r.flash) return { text: r.note, warn: true };
    if (r.local) {
      if (r.err) return { text: "Not saved on cryoai: " + r.err, warn: true };
      if (cq.down && Date.now() - cq.down > 4000) {
        return { text: "Not saved on cryoai yet (it isn't answering). Kept in this browser, trying again.", warn: true };
      }
      return { text: "Saving to cryoai…" };
    }
    if (r.status === "claimed") return { text: "Sending…" };
    if (r.status === "held") return { text: "Held because you stopped the reply" };
    if (r.status === "failed") return { text: "Not sent: " + (r.note || "unknown error"), warn: true };
    if (r.note) return { text: r.note };
    return { text: running ? "Sends when this reply finishes" : "Sending in a moment" };
  }

  function cqSvg(d) {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">' +
      '<path stroke-linecap="round" stroke-linejoin="round" d="' + d + '"></path></svg>';
  }

  function cqButton(act, label, icon) {
    var b = document.createElement("button");
    b.type = "button"; // the box sits inside Open WebUI's form: a plain button must not submit it
    b.className = "cq-b";
    b.setAttribute("data-cq-act", act);
    b.setAttribute("aria-label", label);
    b.title = label;
    b.innerHTML = cqSvg(icon);
    return b;
  }

  function cqRender() {
    try {
      var chat = currentChatId;
      var mic = document.getElementById("message-input-container");
      var rows = chat ? cqRowsFor(chat) : [];
      var banner = !!chat && cq.banner === chat;
      var box = document.getElementById(CQ_ID);
      if (!mic || !mic.parentNode || (!rows.length && !banner)) {
        if (box && box.parentNode) box.parentNode.removeChild(box);
        cq.sig = "";
        return;
      }
      cqStyle();
      var running = owuiRunning();
      var shown = rows.map(function (r) {
        return { r: r, st: cqStatus(r, running) };
      });
      var sig = JSON.stringify([chat, banner, shown.map(function (x) {
        return [x.r.id || x.r.lid, x.r.status, x.r.text, x.st.text, !!x.st.warn];
      })]);
      if (!box) {
        box = document.createElement("div");
        box.id = CQ_ID;
        box.setAttribute("aria-label", "Queued messages");
        box.addEventListener("click", cqClick);
      }
      if (box.parentNode !== mic.parentNode || box.nextElementSibling !== mic) mic.parentNode.insertBefore(box, mic);
      if (sig === cq.sig) return;
      cq.sig = sig;
      while (box.firstChild) box.removeChild(box.firstChild);
      if (banner) {
        var bn = document.createElement("div");
        bn.className = "cq-row cq-banner";
        var bt = document.createElement("div");
        bt.className = "cq-main";
        var bs = document.createElement("span");
        bs.className = "cq-text";
        bs.textContent = "This chat moved on while this tab was away. Reload to see it before you send anything.";
        bt.appendChild(bs);
        var rb = document.createElement("button");
        rb.type = "button";
        rb.className = "cq-reload";
        rb.setAttribute("data-cq-act", "reload");
        rb.textContent = "Reload";
        bn.appendChild(bt);
        bn.appendChild(rb);
        box.appendChild(bn);
      }
      shown.forEach(function (x) {
        var r = x.r;
        var row = document.createElement("div");
        row.className = "cq-row";
        row.setAttribute("data-cq-id", r.id || "");
        row.setAttribute("data-cq-lid", r.lid || "");
        row.setAttribute("data-local", r.local ? "1" : "0");
        if (x.st.warn) row.setAttribute("data-tone", "warn");
        var ic = document.createElement("span");
        ic.className = "cq-ic";
        ic.innerHTML = cqSvg(r.local ? CQ_ICONS.wait : CQ_ICONS.cloud);
        ic.title = r.local ? "Not on cryoai yet" : r.mode === "chat"
          ? "Kept on cryoai. It goes out when the reply finishes and this chat is open."
          : "Kept on cryoai. It goes out when the reply finishes, even if you close this tab.";
        var main = document.createElement("div");
        main.className = "cq-main";
        var t = document.createElement("p");
        t.className = "cq-text";
        t.textContent = r.text;
        t.title = r.text;
        var st = document.createElement("span");
        st.className = "cq-st";
        st.textContent = x.st.text;
        main.appendChild(t);
        main.appendChild(st);
        var acts = document.createElement("div");
        acts.className = "cq-acts";
        if (!r.local && (r.status === "queued" || r.status === "held")) acts.appendChild(cqButton("now", "Send now", CQ_ICONS.now));
        if (r.status !== "claimed") {
          acts.appendChild(cqButton("edit", "Edit", CQ_ICONS.edit));
          acts.appendChild(cqButton("del", "Delete", CQ_ICONS.del));
        }
        row.appendChild(ic);
        row.appendChild(main);
        row.appendChild(acts);
        box.appendChild(row);
      });
    } catch (e) {
      warnOnce("queue-render", e);
    }
  }

  function cqStart() {
    if (cq.started) return;
    cq.started = true;
    document.addEventListener("click", cqOnStop, true);
    document.addEventListener("keydown", cqOnStop, true);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) cqPollSoon(); });
    cq.seen = currentChatId;
    cqPoll();
    cqFlush();
  }

  // --------------------------------------------------------------------- boot
  function boot() {
    try {
      injectStyle();
      currentChatId = chatIdFromUrl();
      loadCurrentMode();
      startObserver();
      scheduleReconcile();
      window.addEventListener("resize", function () {
        scheduleReconcile();
      });
      window.addEventListener("message", onLiveFrameMessage);
      document.addEventListener("click", onScrollDownArrow, true);
      patchHistory("pushState", history.pushState);
      patchHistory("replaceState", history.replaceState);
      window.addEventListener("popstate", function () {
        scheduleUrlChange();
      });
      setInterval(pollPreview, PREVIEW_POLL_MS);
      refreshFeatureChats();
      setInterval(refreshFeatureChats, 30000);
      refreshChecklist();
      setInterval(refreshChecklist, CHECKLIST_POLL_MS);
      setInterval(loadThinkingCaps, 300000); // new models, changed presets
      setInterval(function () {
        try { sendPendingTask(); } catch (e) { warnOnce("start-coding", e); }
        try { sendPendingAsk(); } catch (e) { warnOnce("ask-agent", e); }
        try { cqTick(); } catch (e) { warnOnce("queue-tick", e); }
      }, 700);
      composeLoop();
      cqStart();
      qbStart();
      verStart();
      wtReopenAfterReload();
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
          var panel = document.getElementById("code-preview-panel");
          if (panel && panel.classList.contains("open")) closePreview(true);
        }
      });
    } catch (e) {
      warnOnce("boot", e);
    }
  }

  if (document.body) boot();
  else document.addEventListener("DOMContentLoaded", boot);
})();
