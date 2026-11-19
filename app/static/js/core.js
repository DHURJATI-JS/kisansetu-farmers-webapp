/* KisanSetu core (CSP-safe: no inline handlers, no eval, DOM built with textContent only). */
(function () {
  "use strict";
  var doc = document, root = doc.documentElement;

  /* Trusted Types: only two vetted sinks are allowed — our service-worker URL and Leaflet's own static control markup.
     Everything else must use textContent / DOM APIs (ready for an enforced `require-trusted-types-for 'script'`). */
  var TT = null;
  try {
    if (window.trustedTypes && window.trustedTypes.createPolicy) {
      TT = window.trustedTypes.createPolicy("ks", {
        createScriptURL: function (u) { if (u === "/sw.js") return u; throw new TypeError("blocked script URL"); }
      });
      // default policy (used only by third-party map controls): static markup made of a/span/svg/path, nothing executable
      var safeStatic = function (s) {
        if (/<\s*script|javascript:|\son[a-z]+\s*=|<\s*(iframe|object|embed|style|img|link|meta|form|input|base|template)/i.test(s)) return false;
        return (s.match(/<\/?\s*([a-z0-9]+)/gi) || []).every(function (tg) { return /^(a|span|svg|path)$/i.test(tg.replace(/[<\/\s]/g, "")); });
      };
      window.trustedTypes.createPolicy("default", {
        createHTML: function (s) { s = String(s); if (safeStatic(s)) return s; throw new TypeError("blocked HTML sink"); }
      });
    }
  } catch (e) { TT = null; }
  var $ = function (s, el) { return (el || doc).querySelector(s); };
  var $$ = function (s, el) { return Array.prototype.slice.call((el || doc).querySelectorAll(s)); };
  var I18N = {};
  try { I18N = JSON.parse(($("#ks-i18n") || {}).textContent || "{}"); } catch (e) { I18N = {}; }
  var CSRF = ($('meta[name="csrf-token"]') || {}).content || "";
  var LANG = root.dataset.lang || "en", SPEECH = root.dataset.speech || "en-IN";

  function t(key, params) {
    var s = Object.prototype.hasOwnProperty.call(I18N, key) ? I18N[key] : key;
    if (params) Object.keys(params).forEach(function (k) { s = s.split("{" + k + "}").join(String(params[k])); });
    return s;
  }

  /** Safe DOM builder: el("div", {class: "x", text: "hi", onclick: fn}, child, ...) */
  function el(tag, attrs) {
    var node = doc.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "text") node.textContent = v;
      else if (k === "class") node.className = v;
      else if (k.indexOf("on") === 0 && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (k === "href" || k === "src") {
        var u = String(v);
        if (/^\s*(javascript|data|vbscript):/i.test(u) && !/^data:image\//i.test(u)) return; // never allow script URLs
        node.setAttribute(k, u);
      } else node.setAttribute(k, v === true ? "" : v);
    });
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c === null || c === undefined || c === false) continue;
      if (Array.isArray(c)) c.forEach(function (x) { if (x) node.appendChild(typeof x === "string" ? doc.createTextNode(x) : x); });
      else node.appendChild(typeof c === "string" || typeof c === "number" ? doc.createTextNode(String(c)) : c);
    }
    return node;
  }
  function icon(name, cls) { return el("i", { "data-lucide": name, class: cls || "" }); }
  function icons() { try { if (window.lucide) window.lucide.createIcons({ attrs: { "aria-hidden": "true" } }); } catch (e) {} }

  async function api(url, opts) {
    opts = opts || {};
    var method = opts.method || "GET", headers = { Accept: "application/json" }, body;
    if (method !== "GET") headers["X-CSRF-Token"] = CSRF;
    if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(opts.json); }
    else if (opts.form) body = opts.form;
    var res;
    try { res = await fetch(url, { method: method, headers: headers, body: body, credentials: "same-origin", signal: opts.signal }); }
    catch (e) { throw Object.assign(new Error(t("js.offline")), { status: 0 }); }
    var data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (res.status === 401 && data && data.error === "session_revoked") signedOut(data.redirect);
    if (!res.ok) throw Object.assign(new Error((data && data.message) || t("js.error_generic")), { status: res.status, data: data });
    return data;
  }

  /* The server signed this device out (signed in elsewhere, password changed, ...): nothing was changed — go to the
     sign-in page, which explains why. Only same-site /login links are followed. */
  var leaving = false;
  function signedOut(url) {
    if (leaving) return;
    leaving = true;
    try { if (socket) socket.disconnect(); } catch (e) {}
    location.href = (typeof url === "string" && /^\/login\?[\w=&%.-]*$/.test(url)) ? url : "/login?reason=signed_out_all";
  }

  /* ---------- toasts ---------- */
  function toast(msg, type, ms) {
    var box = $("#ks-toasts"); if (!box || !msg) return;
    var n = el("div", { class: "ks-toast " + (type || "info"), role: "alert" },
      icon(type === "success" ? "check-circle-2" : type === "danger" ? "alert-octagon" : type === "warning" ? "alert-triangle" : "info"),
      el("div", { text: String(msg) }));
    box.appendChild(n); icons();
    setTimeout(function () { n.remove(); }, ms || 5500);
  }

  /* ---------- modal ---------- */
  var modalCleanup = null;      // e.g. the live camera: switched off however its dialog goes away
  function modal(title, content, buttons, onClose) {
    var rootEl = $("#ks-modal-root");
    if (modalCleanup) { var prev = modalCleanup; modalCleanup = null; try { prev(); } catch (e) {} }
    rootEl.textContent = "";
    modalCleanup = onClose || null;
    var close = function () {
      rootEl.textContent = ""; doc.removeEventListener("keydown", onKey);
      if (modalCleanup) { var c = modalCleanup; modalCleanup = null; try { c(); } catch (e) {} }
    };
    var onKey = function (e) { if (e.key === "Escape") close(); };
    var actions = el("div", { class: "ks-row", style: null });
    (buttons || []).forEach(function (b) { actions.appendChild(b); });
    var box = el("div", { class: "ks-panel ornate ks-modal", role: "dialog", "aria-modal": "true", "aria-label": title },
      el("div", { class: "ks-between" }, el("h2", { class: "ks-title ks-h3", text: title }),
        el("button", { class: "ks-icon-btn", type: "button", "aria-label": t("js.close"), onclick: close }, icon("x"))),
      content, el("div", { class: "ks-divider" }), actions);
    var back = el("div", { class: "ks-modal-backdrop", onclick: function (e) { if (e.target === back) close(); } }, box);
    rootEl.appendChild(back); doc.addEventListener("keydown", onKey); icons();
    var f = box.querySelector("input,textarea,select,button.ks-btn-go"); if (f) f.focus();
    return { close: close, box: box };
  }

  function prompt(label, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var input = opts.multiline ? el("textarea", { class: "ks-textarea", maxlength: opts.max || 2000 }) :
        el("input", { class: "ks-input", type: opts.type || "text", maxlength: opts.max || 300, step: opts.step || null });
      var m = modal(label, el("div", { class: "ks-field" }, el("label", { class: "ks-label", text: label }), input), [
        el("button", { class: "ks-btn ks-btn-ghost", type: "button", text: t("js.cancel"), onclick: function () { m.close(); resolve(null); } }),
        el("button", { class: "ks-btn", type: "button", text: t("js.ok"), onclick: function () { var v = input.value.trim(); if (!v && !opts.optional) { input.focus(); return; } m.close(); resolve(v); } })
      ]);
    });
  }

  /* ---------- the universal "summary of changes → GO" flow ---------- */
  async function action(type, payload, opts) {
    opts = opts || {};
    var prep;
    try { prep = await api("/api/actions/prepare", { method: "POST", json: { type: type, payload: payload || {} } }); }
    catch (e) { toast(e.message, "danger"); return null; }
    return showPrepared(prep, opts);
  }

  function showPrepared(prep, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var list = el("ul", { class: "ks-summary" });
      (prep.summary || []).forEach(function (line) { list.appendChild(el("li", {}, el("span", { text: line }))); });
      var warn = el("div");
      (prep.warnings || []).forEach(function (w) { warn.appendChild(el("div", { class: "ks-alert warn" }, icon("alert-triangle"), el("div", { text: w }))); });
      if (prep.consensus) warn.appendChild(el("div", { class: "ks-alert info" }, icon("users"), el("div", { text: t("js.needs_consensus", { n: prep.voters }) })));
      var speakBtn = el("button", { class: "ks-btn ks-btn-ghost ks-btn-sm", type: "button", onclick: function () { speak((prep.summary || []).join(". ")); } }, icon("volume-2"), t("js.listen"));
      var go = el("button", { class: "ks-btn ks-btn-go", type: "button" }, icon("sparkles"), prep.go_label || t("js.go"));
      var cancel = el("button", { class: "ks-btn ks-btn-ghost", type: "button", text: t("js.cancel") });
      var m = modal(t("js.review_changes"), el("div", {}, el("p", { class: "ks-muted ks-small", text: t("js.review_hint") }), list, warn, speakBtn), [cancel, go]);
      cancel.addEventListener("click", function () { api("/api/actions/" + prep.action_id + "/cancel", { method: "POST", json: {} }).catch(function () {}); m.close(); resolve(null); });
      go.addEventListener("click", async function () {
        go.classList.add("is-loading");
        try {
          var res = await api("/api/actions/" + prep.action_id + "/go", { method: "POST", json: {} });
          m.close(); toast(res.message || t("js.done"), res.status === "proposed" ? "warning" : "success");
          if (opts.onDone) opts.onDone(res);
          if (res.clear_cart_shop) cartClearShop(res.clear_cart_shop);
          if (res.redirect) { setTimeout(function () { location.href = res.redirect; }, 600); }
          else if (opts.reload !== false) setTimeout(function () { location.reload(); }, 700);
          resolve(res);
        } catch (e) { go.classList.remove("is-loading"); toast(e.message, "danger"); }
      });
    });
  }

  async function upload(kind, file) {
    var fd = new FormData(); fd.append("kind", kind); fd.append("file", file);
    return api("/api/uploads", { method: "POST", form: fd });
  }

  var geoCache = null;
  /* quiet: no toast on failure — the caller offers the typed address instead */
  function geo(force, quiet) {
    return new Promise(function (resolve, reject) {
      if (geoCache && !force) return resolve(geoCache);
      if (!navigator.geolocation) { if (!quiet) toast(t("js.no_geolocation"), "danger"); return reject(new Error("no geo")); }
      navigator.geolocation.getCurrentPosition(function (p) {
        geoCache = { lat: +p.coords.latitude.toFixed(6), lon: +p.coords.longitude.toFixed(6) }; resolve(geoCache);
      }, function () { if (!quiet) toast(t("js.location_denied"), "danger"); reject(new Error("denied")); },
      { enableHighAccuracy: true, timeout: 20000, maximumAge: 120000 });
    });
  }

  /* "Near me": the live position only if location is ALREADY allowed (never a prompt), else the saved location from the
     profile (GPS or typed, base.html meta ks-home), else ask for GPS (signed out, nothing saved). */
  var HOME = (function () {
    var m = $('meta[name="ks-home"]'); if (!m) return null;
    var p = String(m.content).split(","), la = Number(p[0]), lo = Number(p[1]);
    return isFinite(la) && isFinite(lo) ? { lat: la, lon: lo, saved: true } : null;
  })();
  async function position() {
    try {
      if (navigator.geolocation && navigator.permissions && navigator.permissions.query) {
        var st = await navigator.permissions.query({ name: "geolocation" });
        if (st.state === "granted") return await geo(true, true);
      }
    } catch (e) { /* not allowed, timed out or no permissions API: use the saved location */ }
    if (HOME) return HOME;
    return geo(true);
  }

  /* ---------- typed location (no GPS, or a wrong fix): partials/_macros.html typed_location() ---------- */
  function showTyped(sel) {
    var box = typeof sel === "string" ? $(sel) : sel; if (!box) return null;
    box.classList.remove("ks-hidden");
    var first = $("[data-loc=state]", box); if (first && !first.value) first.focus();
    return box;
  }
  function typedAddress(box) {
    function v(k) { var n = $("[data-loc=" + k + "]", box); return n ? n.value.replace(/\s+/g, " ").trim() : ""; }
    var a = { state: v("state"), district: v("district"), place: v("place"), pincode: v("pincode") || null };
    if (!a.state || a.district.length < 2 || a.place.length < 2) { toast(t("js.loc_fill"), "warning"); speak(t("js.loc_fill")); return null; }
    if (a.pincode && !/^[1-9][0-9]{5}$/.test(a.pincode)) { toast(t("js.loc_pin_invalid"), "warning"); speak(t("js.loc_pin_invalid")); return null; }
    return a;
  }
  function fillTyped(box, a) {
    if (!box || !a) return;
    ["state", "district", "place", "pincode"].forEach(function (k) { var n = $("[data-loc=" + k + "]", box); if (n && a[k]) n.value = a[k]; });
  }
  doc.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-loc-typed-toggle]");
    if (b) showTyped(b.dataset.locTypedToggle);
  });

  /* ---------- live camera in the page (laptops too): a [data-camera] button fills the nearby file input ----------
     The photo goes through the input's own upload code (scanned, re-encoded and face-checked on the server as before).
     Needs a secure context (https or localhost); elsewhere the buttons stay hidden and the file input still works. */
  var camOk = (function () {
    try { return !!(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && new DataTransfer()); }
    catch (e) { return false; }
  })();
  if (camOk) root.classList.add("ks-cam");
  function cameraTarget(btn) {
    if (btn.dataset.camera) return $(btn.dataset.camera);
    var scope = btn.closest("[data-camera-scope], .ks-field");
    return scope ? scope.querySelector('input[type="file"]') : null;
  }
  function liveCamera(input, facing) {
    var mode = facing === "user" ? "user" : "environment", stream = null, m = null;
    var video = el("video", { class: "ks-camera-view" + (mode === "user" ? " mirror" : ""), "aria-label": t("js.camera_title") });
    video.muted = true; video.autoplay = true; video.playsInline = true; video.setAttribute("playsinline", "");
    var shot = el("canvas", { class: "ks-camera-view ks-hidden" + (mode === "user" ? " mirror" : "") });
    var status = el("p", { class: "ks-small ks-muted mt-2", role: "status", "aria-live": "polite", text: t("js.camera_starting") });
    var flip = el("button", { class: "ks-btn ks-btn-ghost ks-hidden", type: "button" }, icon("switch-camera"), t("js.camera_switch"));
    var retake = el("button", { class: "ks-btn ks-btn-ghost ks-hidden", type: "button" }, icon("rotate-ccw"), t("js.camera_retake"));
    var snap = el("button", { class: "ks-btn ks-btn-gold", type: "button", disabled: true }, icon("aperture"), t("js.camera_capture"));
    var use = el("button", { class: "ks-btn ks-btn-go ks-hidden", type: "button" }, icon("check"), t("js.camera_use"));
    function off() { if (stream) stream.getTracks().forEach(function (tr) { tr.stop(); }); stream = null; video.srcObject = null; }
    function say(key) { status.textContent = t(key); speak(status.textContent); }
    async function start() {
      off(); snap.disabled = true; status.textContent = t("js.camera_starting");
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: false,
          video: { facingMode: { ideal: mode }, width: { ideal: 1280 }, height: { ideal: 960 } } });
        if (!m) { off(); return; }                       // dialog closed while the browser was asking
        video.srcObject = stream;
        try { await video.play(); } catch (e) {}
        snap.disabled = false;
        say(mode === "user" ? "js.camera_hint_face" : "js.camera_hint");
        try {
          var cams = (await navigator.mediaDevices.enumerateDevices()).filter(function (d) { return d.kind === "videoinput"; });
          flip.classList.toggle("ks-hidden", cams.length < 2);
        } catch (e) {}
      } catch (e) {
        off();
        say(e && (e.name === "NotAllowedError" || e.name === "SecurityError") ? "js.camera_denied" : "js.camera_unavailable");
      }
    }
    snap.addEventListener("click", function () {
      var w = video.videoWidth, h = video.videoHeight; if (!w || !h) return;
      var k = Math.min(1, 1600 / Math.max(w, h));      // ≤1600 px: sharp enough, small upload
      shot.width = Math.round(w * k); shot.height = Math.round(h * k);
      shot.getContext("2d").drawImage(video, 0, 0, shot.width, shot.height);
      off();                                             // camera light off while the photo is checked
      video.classList.add("ks-hidden"); shot.classList.remove("ks-hidden");
      [snap, flip].forEach(function (b) { b.classList.add("ks-hidden"); });
      [retake, use].forEach(function (b) { b.classList.remove("ks-hidden"); });
      say("js.camera_check"); use.focus();
    });
    retake.addEventListener("click", function () {
      shot.classList.add("ks-hidden"); video.classList.remove("ks-hidden");
      [retake, use].forEach(function (b) { b.classList.add("ks-hidden"); });
      snap.classList.remove("ks-hidden"); start();
    });
    flip.addEventListener("click", function () {
      mode = mode === "user" ? "environment" : "user";
      video.classList.toggle("mirror", mode === "user"); shot.classList.toggle("mirror", mode === "user"); start();
    });
    use.addEventListener("click", function () {
      use.disabled = true;
      shot.toBlob(function (blob) {
        if (!blob) { use.disabled = false; toast(t("js.camera_failed"), "danger"); return; }
        var file = new File([blob], "camera-" + Date.now() + ".jpg", { type: "image/jpeg", lastModified: Date.now() });
        var dt = new DataTransfer();
        if (input.multiple && input.files) Array.prototype.forEach.call(input.files, function (f) { dt.items.add(f); });
        dt.items.add(file);
        input.files = dt.files;
        m.close();
        input.dispatchEvent(new Event("change", { bubbles: true }));   // the page's own upload code takes it from here
      }, "image/jpeg", 0.9);
    });
    m = modal(t("js.camera_title"), el("div", {}, el("div", { class: "ks-camera" }, video, shot), status),
      [flip, retake, snap, use], function () { m = null; off(); });
    start();
  }
  doc.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-camera]");
    if (!b || !camOk) return;
    e.preventDefault();
    var input = cameraTarget(b);
    if (input && !input.disabled) liveCamera(input, b.dataset.facing);
  });
  function uuid() { return (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/[^A-Za-z0-9-]/g, ""); }

  /* ---------- voice (helps low-literacy users) ---------- */
  function speak(text) {
    try {
      if (!window.speechSynthesis || !text) return;
      window.speechSynthesis.cancel();
      var u = new SpeechSynthesisUtterance(String(text).slice(0, 1200)); u.lang = SPEECH; u.rate = .95;
      var voices = window.speechSynthesis.getVoices(), base = SPEECH.split("-")[0];
      var v = voices.find(function (x) { return x.lang === SPEECH; }) || voices.find(function (x) { return x.lang && x.lang.indexOf(base) === 0; });
      if (v) u.voice = v;
      window.speechSynthesis.speak(u);
    } catch (e) {}
  }
  function listen(onText) {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { toast(t("js.no_speech"), "warning"); return null; }
    var r = new SR(); r.lang = SPEECH; r.interimResults = false; r.maxAlternatives = 1;
    r.onresult = function (e) { onText(e.results[0][0].transcript); };
    r.onerror = function () { toast(t("js.speech_failed"), "warning"); };
    r.start(); toast(t("js.listening"), "info", 2500); return r;
  }

  /* ---------- cart (per-viewer convenience in localStorage) ---------- */
  function cartGet() { try { return JSON.parse(localStorage.getItem("ks_cart") || "{}"); } catch (e) { return {}; } }
  function cartSet(c) { try { localStorage.setItem("ks_cart", JSON.stringify(c)); } catch (e) {} cartBadge(); }
  function cartAdd(item, qty) { var c = cartGet(); var cur = c[item.id] || { id: item.id, qty: 0 }; cur.qty = +(Number(cur.qty) + Number(qty || 1)).toFixed(2); c[item.id] = cur; cartSet(c); toast(t("js.added_to_cart", { name: item.name || "" }), "success"); }
  function cartClearShop(shopId) { var c = cartGet(); Object.keys(c).forEach(function (k) { if (c[k].shop_id === shopId || !c[k].shop_id) delete c[k]; }); cartSet(c); }
  function cartBadge() { var n = Object.keys(cartGet()).length; $$("[data-cart-count]").forEach(function (b) { b.textContent = n ? String(n) : ""; b.classList.toggle("ks-hidden", !n); }); }

  /* ---------- dynamic content translation ---------- */
  async function translateDynamic(scope) {
    if (LANG === "en") return;
    var nodes = $$("[data-translate]", scope || doc).filter(function (n) { return !n.dataset.translated && n.textContent.trim(); });
    for (var i = 0; i < nodes.length; i += 30) {
      var batch = nodes.slice(i, i + 30);
      try {
        var res = await api("/api/i18n/translate", { method: "POST", json: { texts: batch.map(function (n) { return n.textContent.trim().slice(0, 2000); }) } });
        batch.forEach(function (n, j) {
          var tr = res.texts[j]; if (!tr || tr === n.textContent.trim()) return;
          var orig = n.textContent; n.dataset.translated = "1"; n.textContent = tr; n.title = t("js.original") + ": " + orig.slice(0, 300);
        });
      } catch (e) { return; }
    }
  }

  /* ---------- declarative helpers ---------- */
  function parsePayload(node) { try { return JSON.parse(node.dataset.payload || "{}"); } catch (e) { return {}; } }
  function setDeep(obj, path, value) {
    var parts = path.split("."), cur = obj;
    for (var i = 0; i < parts.length - 1; i++) { cur[parts[i]] = cur[parts[i]] || {}; cur = cur[parts[i]]; }
    cur[parts[parts.length - 1]] = value;
  }
  function formPayload(form) {
    var out = {};
    $$("[name]", form).forEach(function (f) {
      if (f.disabled || f.dataset.skip !== undefined) return;
      if (f.multiple && f.selectedOptions) { setDeep(out, f.name, Array.prototype.map.call(f.selectedOptions, function (o) { return o.value; })); return; }
      var type = f.dataset.type || "", v;
      if (f.type === "checkbox" && type !== "list-check") v = f.checked;
      else if (f.type === "radio") { if (!f.checked) return; v = f.value; }
      else v = f.value;
      if (type === "list-check") { var arr = (out[f.name] = out[f.name] || []); if (f.checked) arr.push(f.value); return; }
      if (v === "" && f.dataset.optional !== undefined) return;
      if (v === "" && type) return;
      if (type === "number") v = v === "" ? null : Number(v);
      else if (type === "int") v = parseInt(v, 10);
      else if (type === "bool") v = v === true || v === "true";
      else if (type === "json") { try { v = JSON.parse(v); } catch (e) { v = null; } }
      else if (type === "intlist") v = $$('[name="' + f.name + '"]:checked', form).map(function (x) { return parseInt(x.value, 10); });
      setDeep(out, f.name, v);
    });
    var extra = form.dataset.payload; if (extra) { try { Object.assign(out, JSON.parse(extra)); } catch (e) {} }
    return out;
  }

  doc.addEventListener("click", async function (e) {
    var a = e.target.closest("[data-action]");
    if (a && !a.closest("form[data-action-form]")) {
      e.preventDefault();
      var payload = parsePayload(a);
      if (a.dataset.prompt) {
        var val = await prompt(t(a.dataset.prompt), { multiline: a.dataset.promptMultiline !== undefined, type: a.dataset.promptType,
          step: a.dataset.promptStep || (a.dataset.promptType === "number" ? "0.01" : null), optional: a.dataset.promptOptional !== undefined });
        if (val === null) return;
        if (val !== "") payload[a.dataset.promptField || "note"] = a.dataset.promptType === "number" ? Number(val) : val;
      }
      action(a.dataset.action, payload, { reload: a.dataset.noReload === undefined });
      return;
    }
    var lo = e.target.closest("[data-logout]");
    if (lo) { e.preventDefault(); try { var r = await api("/api/auth/logout", { method: "POST", json: {} }); location.href = r.redirect || "/"; } catch (x) { location.href = "/"; } return; }
    var sp = e.target.closest("[data-speak]");
    if (sp) { e.preventDefault(); var target = sp.dataset.speak ? $(sp.dataset.speak) : null; speak(target ? target.textContent : sp.dataset.text || ""); return; }
    var mic = e.target.closest("[data-mic]");
    if (mic) { e.preventDefault(); var inp = $(mic.dataset.mic); if (inp) listen(function (txt) { inp.value = (inp.value ? inp.value + " " : "") + txt; inp.dispatchEvent(new Event("input")); }); return; }
    var add = e.target.closest("[data-cart-add]");
    if (add) { e.preventDefault(); var qtyIn = add.dataset.qtyInput ? $(add.dataset.qtyInput) : null; cartAdd(parsePayload(add), qtyIn ? Number(qtyIn.value || 1) : Number(add.dataset.qty || 1)); return; }
  });

  doc.addEventListener("submit", function (e) {
    var f = e.target.closest("form[data-action-form]");
    if (!f) return;
    e.preventDefault();
    if (!f.reportValidity()) return;
    action(f.dataset.actionForm, formPayload(f), { reload: f.dataset.noReload === undefined });
  });

  /* ---------- theme & language ---------- */
  function setTheme(mode) {
    var dark = mode === "dark" || (mode === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    root.classList.toggle("dark", dark);
    doc.cookie = "ks_theme=" + mode + "; Path=/; Max-Age=31536000; SameSite=Lax";
  }
  $$("[data-theme-toggle]").forEach(function (b) {
    b.addEventListener("click", function () { setTheme(root.classList.contains("dark") ? "light" : "dark"); });
  });
  $$("[data-lang-select]").forEach(function (s) {
    s.addEventListener("change", async function () {
      try { await api("/api/i18n/lang", { method: "POST", json: { lang: s.value } }); } catch (e) {}
      location.reload();
    });
  });

  /* ---------- notifications (Socket.IO) ---------- */
  var socket = null, transports = (root.dataset.sio || "websocket,polling").split(",");
  if (window.io && root.dataset.auth === "1") {
    try {
      socket = window.io({ transports: transports, withCredentials: true });
      socket.on("session:revoked", function (d) { signedOut(d && d.redirect); });
      socket.on("notify", function (n) {
        toast((n.title || "") + (n.body ? " — " + n.body : ""), n.severity === "danger" ? "danger" : n.severity === "warning" ? "warning" : n.severity === "success" ? "success" : "info", 8000);
        $$("[data-unread]").forEach(function (b) { var v = (parseInt(b.textContent || "0", 10) || 0) + 1; b.textContent = String(v); b.classList.remove("ks-hidden"); });
      });
    } catch (e) { socket = null; }
  } else if (window.io) {
    try { socket = window.io({ transports: transports }); } catch (e) { socket = null; }
  }

  /* ---------- tab bars: on phones the bar scrolls sideways - bring the current tab into view ---------- */
  function centerTabs() {
    document.querySelectorAll(".ks-tabs").forEach(function (bar) {
      var a = bar.querySelector("a.active");
      if (a && bar.scrollWidth > bar.clientWidth) {
        bar.scrollLeft += a.getBoundingClientRect().left - bar.getBoundingClientRect().left - (bar.clientWidth - a.offsetWidth) / 2;
      }
    });
  }
  centerTabs();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(centerTabs);  // web fonts widen the tabs
  window.addEventListener("load", centerTabs);

  /* ---------- reveal animations ---------- */
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) { entries.forEach(function (en) { if (en.isIntersecting) { en.target.classList.add("is-visible"); io.unobserve(en.target); } }); }, { threshold: .01 });
    $$(".ks-reveal").forEach(function (n) { io.observe(n); });
  } else $$(".ks-reveal").forEach(function (n) { n.classList.add("is-visible"); });
  // never leave content hidden on slow or unusual devices
  setTimeout(function () { $$(".ks-reveal").forEach(function (n) { n.classList.add("is-visible"); }); }, 1800);

  /* ---------- progress bars (CSSOM width — CSP safe) ---------- */
  $$("[data-progress]").forEach(function (p) { var s = p.querySelector("span"); if (s) s.style.width = Math.max(0, Math.min(100, Number(p.dataset.progress))) + "%"; });

  /* ---------- particles: leaves by day, fireflies by night ---------- */
  (function particles() {
    var c = $("#ks-particles"); if (!c || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    var ctx = c.getContext("2d"), W, H, items = [], N = window.innerWidth < 700 ? 14 : 28;
    function size() { W = c.width = window.innerWidth; H = c.height = window.innerHeight; }
    size(); window.addEventListener("resize", size);
    for (var i = 0; i < N; i++) items.push({ x: Math.random() * W, y: Math.random() * H, r: 2 + Math.random() * 5, s: .2 + Math.random() * .6, a: Math.random() * 6.28, w: .5 + Math.random() });
    function frame() {
      if (doc.hidden) { requestAnimationFrame(frame); return; }
      ctx.clearRect(0, 0, W, H);
      var dark = root.classList.contains("dark");
      items.forEach(function (p) {
        p.a += .01 * p.w; p.y += dark ? Math.sin(p.a) * .3 : p.s; p.x += Math.cos(p.a) * (dark ? .4 : .6);
        if (p.y > H + 10) { p.y = -10; p.x = Math.random() * W; } if (p.x > W + 10) p.x = -10; if (p.x < -10) p.x = W + 10;
        if (dark) {
          var g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r * 3);
          g.addColorStop(0, "rgba(220,255,160," + (.5 + .5 * Math.sin(p.a * 3)) + ")"); g.addColorStop(1, "rgba(220,255,160,0)");
          ctx.fillStyle = g; ctx.beginPath(); ctx.arc(p.x, p.y, p.r * 3, 0, 6.28); ctx.fill();
        } else {
          ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.a); ctx.fillStyle = "rgba(46,160,96,.35)";
          ctx.beginPath(); ctx.ellipse(0, 0, p.r * 1.8, p.r, 0, 0, 6.28); ctx.fill(); ctx.restore();
        }
      });
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  })();

  /* ---------- service worker (offline support for rural networks) ---------- */
  if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) {
    try { navigator.serviceWorker.register(TT ? TT.createScriptURL("/sw.js") : "/sw.js").catch(function () {}); } catch (e) {}
  }

  window.KS = { t: t, el: el, icon: icon, icons: icons, $: $, $$: $$, api: api, toast: toast, modal: modal, prompt: prompt, action: action,
    showPrepared: showPrepared, upload: upload, geo: geo, position: position, home: HOME,
    showTyped: showTyped, typedAddress: typedAddress, fillTyped: fillTyped,
    uuid: uuid, speak: speak, listen: listen, socket: socket, lang: LANG,
    cartGet: cartGet, cartSet: cartSet, cartAdd: cartAdd, cartBadge: cartBadge, translateDynamic: translateDynamic, formPayload: formPayload };

  /* header menu: icons only when the words do not fit (any language, role or zoom) — never overlapping the logo */
  (function () {
    var hdr = $(".ks-header"), nav = hdr && $(".ks-nav", hdr);
    if (!nav) return;
    function over() { return nav.scrollWidth > nav.clientWidth + 1; }
    function fit() {        // words + icons → words only → icons only (with tooltips): words help people who read slowly
      hdr.classList.remove("ks-nav-tight", "ks-nav-compact");
      if (!over()) return;
      hdr.classList.add("ks-nav-tight");
      if (!over()) return;
      hdr.classList.remove("ks-nav-tight");
      hdr.classList.add("ks-nav-compact");
    }
    if (window.ResizeObserver) new ResizeObserver(function () { window.requestAnimationFrame(fit); }).observe(hdr);
    else window.addEventListener("resize", fit);
    if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(fit);
    window.requestAnimationFrame(fit);
  })();

  doc.addEventListener("DOMContentLoaded", function () { icons(); cartBadge(); translateDynamic(); });
  if (doc.readyState !== "loading") { icons(); cartBadge(); translateDynamic(); }
})();
