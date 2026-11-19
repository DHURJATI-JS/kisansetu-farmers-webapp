(function () {
  "use strict";
  var KS = window.KS, $ = KS.$, $$ = KS.$$, t = KS.t;

  $$("[data-setting-form]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      var raw = f.value.value, value = raw;
      if (f.dataset.json !== undefined) { try { value = JSON.parse(raw); } catch (err) { KS.toast(t("js.bad_json"), "danger"); return; } }
      else if (f.dataset.list !== undefined) value = raw.split(/[\n,]+/).map(function (s) { return s.trim(); }).filter(Boolean);
      else if (/^-?\d+(\.\d+)?$/.test(raw.trim())) value = Number(raw);
      KS.action("admin.settings.update", { key: f.dataset.key, value: value });
    });
  });

  var setup = $("#mfa-setup");
  if (setup) setup.addEventListener("click", async function () {
    try { var r = await KS.api("/api/auth/totp/setup", { method: "POST", json: {} }); var img = $("#mfa-qr"); img.src = r.qr; img.classList.remove("ks-hidden"); $("#mfa-secret").textContent = r.secret; }
    catch (e) { KS.toast(e.message, "danger"); }
  });
  var mf = $("#mfa-form");
  if (mf) mf.addEventListener("submit", async function (e) {
    e.preventDefault();
    try { var r = await KS.api("/api/auth/totp/verify", { method: "POST", json: { code: mf.code.value } }); location.href = r.redirect; }
    catch (err) { KS.toast(err.message, "danger"); }
  });
})();
