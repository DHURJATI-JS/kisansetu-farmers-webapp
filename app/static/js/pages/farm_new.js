/* Farm creation: GSTIN OCR scan → location → AI crop advice → crops → insurance uploads → terms (AI legality) → GO. */
(function () {
  "use strict";
  var KS = window.KS, $ = KS.$, $$ = KS.$$, t = KS.t, el = KS.el;
  var form = $("#farm-new"); if (!form) return;

  function addRow(tplId, containerId) {
    var node = $(tplId).content.firstElementChild.cloneNode(true);
    $(containerId).appendChild(node); KS.icons(); return node;
  }
  document.addEventListener("click", function (e) { var r = e.target.closest("[data-remove-row]"); if (r) r.closest("[data-crop-row],[data-ins-row]").remove(); });
  $("#crop-add").addEventListener("click", function () { addRow("#tpl-crop", "#crop-rows"); });
  $("#ins-add").addEventListener("click", function () { addRow("#tpl-ins", "#ins-rows"); });
  addRow("#tpl-crop", "#crop-rows");
  var a = addRow("#tpl-ins", "#ins-rows"); a.querySelector('[data-k="policy_type"]').value = "salary"; a.querySelector('[data-k="covers_salaries"]').checked = true;
  var b = addRow("#tpl-ins", "#ins-rows"); b.querySelector('[data-k="policy_type"]').value = "health";

  /* GSTIN scan */
  $("#gst-scan").addEventListener("click", async function () {
    var f = $("#gst-file").files[0], out = $("#gst-result");
    if (!f) { KS.toast(t("js.choose_file"), "warning"); return; }
    var fd = new FormData(); fd.append("file", f);
    var manual = $("#gst-manual"); if (manual && manual.value.trim()) fd.append("manual_gstin", manual.value.trim());
    out.textContent = t("js.scanning");
    try {
      var r = await KS.api("/api/gstin/scan", { method: "POST", form: fd });
      out.textContent = "";
      var engines = Object.keys(r.engines || {}).map(function (k) { return k + ": " + (r.engines[k] || "—"); }).join(" · ");
      out.appendChild(el("div", { class: "ks-alert ok" }, KS.icon("scan-text"), el("div", {},
        el("strong", { text: r.gstin }), el("div", { class: "ks-small", text: engines }),
        r.manual ? el("div", { class: "ks-small", text: t("js.manual_test") }) : null)));
      KS.icons();
      KS.action("gstin.verify", { verification_id: r.verification_id });
    } catch (e) { out.textContent = ""; KS.toast(e.message, "danger"); }
  });

  /* AI recommendation (needs the branch location) */
  $("#ai-recommend").addEventListener("click", async function () {
    var lat = Number($("#b-lat").value), lon = Number($("#b-lon").value), out = $("#ai-recommend-out");
    if (!lat || !lon) { KS.toast(t("js.pick_location_first"), "warning"); return; }
    out.textContent = t("js.thinking");
    try {
      var r = await KS.api("/api/farms/recommend", { method: "POST", json: { lat: lat, lon: lon } });
      out.textContent = "";
      out.appendChild(el("div", { class: "ks-alert info" }, KS.icon("sparkles"), el("div", {},
        el("strong", { text: t("js.recommended_type", { type: r.farming_type_label }) }),
        el("div", { class: "ks-small", text: t("js.avg_temp", { t: r.avg_temp == null ? "?" : r.avg_temp }) }))));
      var chips = el("div", { class: "ks-row" });
      (r.recommendations || []).forEach(function (c) {
        chips.appendChild(el("button", { type: "button", class: "ks-chip", title: (c.farming_label || []).join(", "), onclick: function () {
          var row = addRow("#tpl-crop", "#crop-rows"); row.querySelector('[data-k="name"]').value = c.name;
        } }, KS.icon("plus"), c.name + (c.hi ? " (" + c.hi + ")" : "")));
      });
      out.appendChild(chips);
      var sel = $("#f-farming-type"); if (sel && r.farming_type) sel.value = r.farming_type;
      KS.icons();
    } catch (e) { out.textContent = ""; KS.toast(e.message, "danger"); }
  });

  /* Terms */
  $$('input[name="terms_mode"]').forEach(function (r) { r.addEventListener("change", function () { $("#terms-custom").classList.toggle("ks-hidden", form.terms_mode.value !== "custom"); }); });
  $("#terms-check").addEventListener("click", async function () {
    var out = $("#terms-result"); out.textContent = t("js.ai_checking_terms");
    try {
      var r = await KS.api("/api/terms/check", { method: "POST", json: { text: $("#terms-text").value } });
      out.textContent = "";
      if (r.ok) out.appendChild(el("div", { class: "ks-alert ok" }, KS.icon("check-circle-2"), el("div", { text: t("js.terms_ok") })));
      else {
        var list = el("ul", { class: "ks-list" });
        (r.issues || []).forEach(function (i) { list.appendChild(el("li", {}, el("strong", { text: i.clause || i.code || "" }), el("div", { class: "ks-small", text: (i.problem || "") + (i.law ? " — " + i.law : "") }))); });
        out.appendChild(el("div", { class: "ks-alert danger" }, KS.icon("scale"), el("div", {}, el("strong", { text: t("js.terms_bad") }), list)));
      }
      KS.icons();
    } catch (e) { out.textContent = ""; KS.toast(e.message, "danger"); }
  });

  /* Submit → summary → GO */
  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    var gid = (form.querySelector('input[name="gstin_verification_id"]:checked') || {}).value;
    if (!gid) { KS.toast(t("js.gstin_needed"), "warning"); return; }
    if (!$("#b-lat").value) { KS.toast(t("js.pick_location_first"), "warning"); return; }
    var p = KS.formPayload(form);
    p.gstin_verification_id = gid;
    p.branch.crops = $$("[data-crop-row]").map(function (r) {
      var o = {}; $$("[data-k]", r).forEach(function (i) { if (i.value) o[i.dataset.k] = i.type === "number" ? Number(i.value) : i.value; }); return o;
    }).filter(function (c) { return c.name; });
    if (!p.branch.crops.length) { KS.toast(t("js.crop_needed"), "warning"); return; }
    var btn = form.querySelector("button[type=submit]"); btn.classList.add("is-loading");
    try {
      p.insurance = [];
      for (var row of $$("[data-ins-row]")) {
        var o = {}; $$("[data-k]", row).forEach(function (i) { if (i.type === "checkbox") o[i.dataset.k] = i.checked; else if (i.type !== "file" && i.value) o[i.dataset.k] = i.type === "number" ? Number(i.value) : i.value; });
        var file = row.querySelector('[data-k="file"]').files[0];
        if (!file) { KS.toast(t("js.policy_file_needed"), "warning"); btn.classList.remove("is-loading"); return; }
        o.upload_id = (await KS.upload("insurance", file)).upload_id;
        p.insurance.push(o);
      }
    } catch (err) { KS.toast(err.message, "danger"); btn.classList.remove("is-loading"); return; }
    if (p.terms_mode !== "custom") delete p.terms_text;
    btn.classList.remove("is-loading");
    if (p.accept_platform_terms !== true) { KS.toast(t("js.accept_terms"), "warning"); return; }
    KS.action("farm.create", p);
  });
})();
