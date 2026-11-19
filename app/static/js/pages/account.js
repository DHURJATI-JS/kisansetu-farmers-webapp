/* Onboarding + account page: face-verified photo, location, ID proofs (reveal to self only), sessions. */
(function () {
  "use strict";
  var KS = window.KS, $ = KS.$, t = KS.t;

  var photo = $("#ob-photo");
  if (photo) photo.addEventListener("change", async function () {
    var f = photo.files && photo.files[0]; if (!f) return;
    var out = $("#ob-photo-result"); out.textContent = t("js.checking_photo");
    var fd = new FormData(); fd.append("file", f);
    try {
      var r = await KS.api("/api/account/photo", { method: "POST", form: fd });
      var img = $("#ob-preview"); img.src = r.url; img.classList.remove("ks-hidden");
      out.className = "ks-alert ok mt-2"; out.textContent = r.message; KS.speak(r.message);
    } catch (e) { out.className = "ks-alert danger mt-2"; out.textContent = e.message; KS.speak(e.message); }
    photo.value = "";
  });

  var loc = $("#ob-location");
  if (loc) loc.addEventListener("click", async function () {
    var g;
    try { g = await KS.geo(true, true); }
    catch (e) { KS.showTyped("#ob-typed"); KS.toast(t("js.loc_gps_failed"), "warning"); KS.speak(t("js.loc_gps_failed")); return; }
    KS.action("location.update", g);
  });
  var typed = $("#ob-typed");
  if (typed) $("[data-loc-find]", typed).addEventListener("click", function () {
    var a = KS.typedAddress(typed); if (a) KS.action("location.update", { address: a });   // the confirm step shows what was found
  });

  KS.$$("[data-reveal-id]").forEach(function (b) {
    b.addEventListener("click", async function () {
      var target = $("#" + b.dataset.target);
      try {
        var r = await KS.api("/api/account/id-docs/" + b.dataset.revealId + "/reveal");
        target.textContent = r.number;
        setTimeout(function () { target.textContent = "••••••••"; }, 30000);
      } catch (e) { KS.toast(e.message, "danger"); }
    });
  });

  var idForm = $("#iddoc-form");
  if (idForm) idForm.addEventListener("submit", async function (e) {
    e.preventDefault();
    var payload = { doc_type: idForm.doc_type.value, number: idForm.number.value };
    var file = idForm.file.files && idForm.file.files[0];
    try { if (file) payload.upload_id = (await KS.upload("idproof", file)).upload_id; } catch (err) { KS.toast(err.message, "danger"); return; }
    KS.action("iddoc.add", payload);
  });

  var sess = $("#sessions-list");
  if (sess) KS.api("/api/account/sessions").then(function (r) {
    sess.textContent = "";
    r.sessions.forEach(function (s) {
      sess.appendChild(KS.el("li", { class: "ks-between" },
        KS.el("span", { text: (s.ua || "?").slice(0, 70) + " · " + (s.ip || "") }),
        KS.el("span", { class: "ks-badge " + (s.current ? "" : "info"), text: s.current ? t("js.this_device") : new Date((s.seen || 0) * 1000).toLocaleString() })));
    });
  }).catch(function () {});
})();
