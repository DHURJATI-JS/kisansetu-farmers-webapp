/* Login, forgot-password and the low-literacy friendly sign-up wizard. Passwords are never stored client-side. */
(function () {
  "use strict";
  var KS = window.KS, $ = KS.$, $$ = KS.$$, t = KS.t;

  /* Where to go after signing in: only a page of THIS site. "//host", "/\host", tabs or newlines inside the slashes and
     "/.//host" all resolve to another site in browsers, so the target is resolved first, its origin compared, and the
     absolute same-site URL used (never the bare path, which could start with "//" after normalisation). */
  function safeNext(next) {
    if (!next || !/^\/(?![\/\\])/.test(next)) return null;
    try {
      var u = new URL(next, location.origin);
      return u.origin === location.origin ? u.href : null;
    } catch (e) { return null; }
  }

  /* ---------- login ---------- */
  var lf = $("#login-form");
  if (lf) lf.addEventListener("submit", async function (e) {
    e.preventDefault();
    var btn = lf.querySelector("button[type=submit]"); btn.classList.add("is-loading");
    try {
      var r = await KS.api("/api/auth/login", { method: "POST", json: { phone: lf.phone.value, password: lf.password.value, totp: lf.totp.value || null } });
      if (r.totp_required) { $("#totp-wrap").classList.remove("ks-hidden"); lf.totp.focus(); KS.toast(t("js.totp_needed"), "warning"); return; }
      location.href = safeNext(new URLSearchParams(location.search).get("next")) || r.redirect;
    } catch (err) { KS.toast(err.message, "danger"); }
    finally { btn.classList.remove("is-loading"); }
  });

  /* ---------- forgot ---------- */
  var ff = $("#forgot-form");
  if (ff) {
    $("#forgot-send").addEventListener("click", async function () {
      try {
        var r = await KS.api("/api/auth/otp", { method: "POST", json: { phone: ff.phone.value, purpose: "reset" } });
        KS.toast(r.message, "success"); $("#forgot-step2").classList.remove("ks-hidden");
        if (r.test_code) { var b = $("#forgot-test"); b.textContent = t("js.test_code", { code: r.test_code }); b.classList.remove("ks-hidden"); }
      } catch (err) { KS.toast(err.message, "danger"); }
    });
    ff.addEventListener("submit", async function (e) {
      e.preventDefault();
      try {
        var r = await KS.api("/api/auth/password/reset", { method: "POST", json: { phone: ff.phone.value, otp: ff.otp.value, new_password: ff.new_password.value } });
        KS.toast(r.message, "success"); setTimeout(function () { location.href = r.redirect; }, 900);
      } catch (err) { KS.toast(err.message, "danger"); }
    });
  }

  /* ---------- sign-up wizard ---------- */
  var box = $("[data-signup]"); if (!box) return;
  var LAST = 6, state = {};
  try { state = JSON.parse(sessionStorage.getItem("ks_signup") || "{}"); } catch (e) { state = {}; }
  var step = Number(state.step || 0);
  function save() { try { sessionStorage.setItem("ks_signup", JSON.stringify({ step: step, role: state.role, phone: state.phone, name: state.name, dob: state.dob, email: state.email, lat: state.lat, lon: state.lon, address: state.address, otp_ok: state.otp_ok })); } catch (e) {} }
  function show(n) {
    step = Math.max(0, Math.min(LAST, n));
    $$("[data-step]", box).forEach(function (s) { s.classList.toggle("ks-hidden", Number(s.dataset.step) !== step); });
    $$("[data-step-bar]", box).forEach(function (b) { b.classList.toggle("done", Number(b.dataset.stepBar) <= step); });
    $("[data-step-label]", box).textContent = t("js.step_of", { n: step + 1, total: LAST + 1 });
    $("[data-prev]", box).classList.toggle("ks-hidden", step === 0);
    $("[data-next]", box).classList.toggle("ks-hidden", step === LAST);
    $("[data-submit]", box).classList.toggle("ks-hidden", step !== LAST);
    var owner = state.role === "owner";
    $("[data-email-wrap]", box).classList.toggle("ks-hidden", !owner);
    $("[data-dob-wrap]", box).classList.toggle("ks-hidden", state.role === "buyer");
    $("[data-pin-wrap]", box).classList.toggle("ks-hidden", owner);
    $("[data-pass-wrap]", box).classList.toggle("ks-hidden", !owner);
    $("[data-voice-pin]", box).classList.toggle("ks-hidden", owner);
    $("[data-voice-pass]", box).classList.toggle("ks-hidden", !owner);
    $$("[data-role-pick]", box).forEach(function (b) { b.classList.toggle("selected", b.dataset.rolePick === state.role); });
    save(); KS.icons();
    var cur = $('[data-step="' + step + '"]', box); var f = cur && cur.querySelector("input:not([type=checkbox])"); if (f && step > 1) f.focus();
  }
  // restore fields
  if (state.phone) $("#su-phone").value = state.phone;
  if (state.name) $("#su-name").value = state.name;
  if (state.dob) $("#su-dob").value = state.dob;
  if (state.email) $("#su-email").value = state.email;
  if (state.otp_ok) $("[data-otp-wrap]", box).classList.remove("ks-hidden");

  $$("[data-lang-pick]", box).forEach(function (b) {
    b.addEventListener("click", async function () {
      step = 1; save();
      try { await KS.api("/api/i18n/lang", { method: "POST", json: { lang: b.dataset.langPick } }); } catch (e) {}
      location.reload();
    });
  });
  $$("[data-role-pick]", box).forEach(function (b) { b.addEventListener("click", function () { state.role = b.dataset.rolePick; show(2); }); });

  $("[data-send-otp]", box).addEventListener("click", async function () {
    state.phone = $("#su-phone").value.trim(); save();
    try {
      var r = await KS.api("/api/auth/otp", { method: "POST", json: { phone: state.phone, purpose: "signup" } });
      KS.toast(r.message, "success"); $("[data-otp-wrap]", box).classList.remove("ks-hidden"); $("#su-otp").focus();
      if (r.test_code) { var tc = $("[data-test-code]", box); tc.textContent = t("js.test_code", { code: r.test_code }); tc.classList.remove("ks-hidden"); }
    } catch (err) { KS.toast(err.message, "danger"); }
  });

  var pin = $("#su-pin");
  $$("[data-key]", box).forEach(function (k) {
    k.addEventListener("click", function () {
      var v = k.dataset.key;
      if (v === "⌫") pin.value = pin.value.slice(0, -1);
      else if (v === "✓") show(step + 1);
      else if (pin.value.length < 8) pin.value += v;
    });
  });

  function locResult(ok, text, silent) {
    var out = $("[data-location-result]", box);
    out.classList.remove("ks-hidden");
    out.classList.toggle("danger", !ok); out.classList.toggle("ok", ok);
    out.textContent = text; if (!silent) KS.speak(text);
  }
  var typedBox = $("#su-typed", box);
  if (state.address) {          // back on this step after a reload: the typed place is still chosen
    KS.fillTyped(typedBox, state.address); typedBox.classList.remove("ks-hidden");
    locResult(true, t("js.loc_found", { place: [state.address.place, state.address.district, state.address.state].join(", ") }), true);
  }

  $("[data-get-location]", box).addEventListener("click", async function () {
    var g;
    try { g = await KS.geo(true, true); }
    catch (err) { KS.showTyped(typedBox); locResult(false, t("js.loc_gps_failed")); return; }   // no GPS → type it
    try {
      var r = await KS.api("/api/auth/location", { method: "POST", json: g });
      locResult(r.ok, r.ok ? t("js.location_ok", { place: [r.city, r.district, r.state].filter(Boolean).join(", ") }) : r.message);
      if (r.ok) { state.lat = g.lat; state.lon = g.lon; state.address = null; save(); }
    } catch (err) { /* toast shown by KS.api */ }
  });

  var findBtn = $("[data-loc-find]", typedBox);
  findBtn.addEventListener("click", async function () {
    var a = KS.typedAddress(typedBox); if (!a) return;
    findBtn.classList.add("is-loading"); findBtn.disabled = true;
    try {
      var r = await KS.api("/api/auth/location/typed", { method: "POST", json: a });
      locResult(r.ok, r.ok ? t(r.approx ? "js.loc_found_approx" : "js.loc_found", { place: r.found }) : r.message);
      if (r.ok) { state.address = a; state.lat = null; state.lon = null; save(); }
    } catch (err) { /* toast shown by KS.api */ }
    finally { findBtn.classList.remove("is-loading"); findBtn.disabled = false; }
  });
  // editing the typed fields after a successful search: the next "Next" needs a fresh search
  $$("[data-loc]", typedBox).forEach(function (n) {
    n.addEventListener("input", function () {
      if (state.address) { state.address = null; save(); $("[data-location-result]", box).classList.add("ks-hidden"); }
    });
  });

  function validate(n) {
    if (n === 1 && !state.role) return t("js.pick_role");
    if (n === 2) { if (!$("#su-otp").value || !/^\d{6}$/.test($("#su-otp").value)) return t("js.enter_otp"); }
    if (n === 3) {
      state.name = $("#su-name").value.trim(); state.dob = $("#su-dob").value; state.email = $("#su-email").value.trim(); save();
      if (state.name.length < 2) return t("js.enter_name");
      if (state.role !== "buyer" && !state.dob) return t("js.enter_dob");
      if (state.role === "owner" && !state.email) return t("js.enter_email");
    }
    if (n === 4) {
      if (state.role === "owner") { var p = $("#su-pass").value; if (p.length < 10) return t("js.password_short"); if (p !== $("#su-pass2").value) return t("js.password_mismatch"); }
      else if (!/^\d{6,8}$/.test(pin.value)) return t("js.pin_short");
    }
    if (n === 5 && !(state.lat && state.lon) && !state.address) return t("js.location_needed");
    return null;
  }
  $("[data-next]", box).addEventListener("click", function () { var err = validate(step); if (err) { KS.toast(err, "warning"); KS.speak(err); return; } show(step + 1); });
  $("[data-prev]", box).addEventListener("click", function () { show(step - 1); });
  $("[data-submit]", box).addEventListener("click", async function () {
    for (var i = 1; i < LAST; i++) { var err = validate(i); if (err) { KS.toast(err, "warning"); show(i); return; } }
    if (!$("#su-terms").checked || !$("#su-privacy").checked) { KS.toast(t("js.accept_terms"), "warning"); return; }
    var btn = $("[data-submit]", box); btn.classList.add("is-loading");
    try {
      var body = {
        role: state.role, full_name: state.name, phone: state.phone, otp: $("#su-otp").value, language: KS.lang,
        password: state.role === "owner" ? $("#su-pass").value : pin.value, email: state.email || null, dob: state.dob || null,
        accept_terms: true, accept_privacy: true };
      if (state.address) body.address = state.address; else { body.lat = state.lat; body.lon = state.lon; }   // typed or GPS
      var r = await KS.api("/api/auth/signup", { method: "POST", json: body });
      try { sessionStorage.removeItem("ks_signup"); } catch (e) {}
      location.href = r.redirect;
    } catch (err) { KS.toast(err.message, "danger"); KS.speak(err.message); btn.classList.remove("is-loading"); }
  });
  show(step);
})();
