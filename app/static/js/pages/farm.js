/* Farm management flows that need uploads or extra input before the summary → GO step. */
(function () {
  "use strict";
  var KS = window.KS, $ = KS.$, $$ = KS.$$, t = KS.t, el = KS.el;

  /* ---- pay salary: the amount is read from the screenshot by OCR + Gemma — never typed ---- */
  $$("[data-pay-member]").forEach(function (b) {
    b.addEventListener("click", function () {
      var file = el("input", { type: "file", accept: "image/*", class: "ks-input", capture: "environment" });
      var method = el("select", { class: "ks-select" }, el("option", { value: "upi", text: t("js.pay_upi") }), el("option", { value: "bank", text: t("js.pay_bank") }), el("option", { value: "cash", text: t("js.pay_cash") }));
      var go = el("button", { class: "ks-btn ks-btn-gold", type: "button" }, KS.icon("scan-text"), t("js.read_screenshot"));
      var m = KS.modal(t("js.pay_title", { name: b.dataset.name }), el("div", {},
        el("p", { class: "ks-small", text: t("js.pay_due", { due: b.dataset.due }) }),
        el("div", { class: "ks-alert info" }, KS.icon("info"), el("div", { class: "ks-small", text: t("js.pay_rule") })),
        el("div", { class: "ks-field" }, el("label", { class: "ks-label", text: t("js.pay_method") }), method),
        el("div", { class: "ks-field" }, el("label", { class: "ks-label", text: t("js.pay_screenshot") }), file)), [go]);
      go.addEventListener("click", async function () {
        var f = file.files[0]; if (!f) { KS.toast(t("js.choose_file"), "warning"); return; }
        go.classList.add("is-loading");
        try {
          var up = await KS.upload("payment", f);
          m.close();
          KS.toast(t("js.ocr_running"), "info", 3000);
          KS.action("payroll.pay", { member_id: b.dataset.payMember, upload_id: up.upload_id, method: method.value });
        } catch (e) { go.classList.remove("is-loading"); KS.toast(e.message, "danger"); }
      });
    });
  });

  /* ---- change salary (owner) ---- */
  $$("[data-salary-member]").forEach(function (b) {
    b.addEventListener("click", function () {
      var type = el("select", { class: "ks-select" }, el("option", { value: "daily", text: t("js.salary_daily") }), el("option", { value: "monthly", text: t("js.salary_monthly") }));
      type.value = b.dataset.type;
      var amt = el("input", { class: "ks-input", type: "number", min: "1", step: "0.01", value: b.dataset.amount });
      var ok = el("button", { class: "ks-btn", type: "button", text: t("js.continue") });
      var m = KS.modal(t("js.change_salary"), el("div", {}, type, el("div", { class: "mt-2" }, amt)), [ok]);
      ok.addEventListener("click", function () { m.close(); KS.action("member.update_salary", { member_id: b.dataset.salaryMember, salary_type: type.value, salary_amount: Number(amt.value) }); });
    });
  });

  /* ---- inventory adjustment (versioned + timestamped) ---- */
  $$("[data-stock-form]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      KS.action("inventory.adjust", { product_id: f.dataset.product, delta: Number(f.delta.value), reason: f.reason.value,
        expected_version: Number(f.dataset.version), client_ts: new Date().toISOString() });
    });
  });

  /* ---- new product with photos ---- */
  var pf = $("#product-form");
  if (pf) pf.addEventListener("submit", async function (e) {
    e.preventDefault(); if (!pf.reportValidity()) return;
    var p = KS.formPayload(pf); p.shop_id = pf.dataset.shop; p.image_upload_ids = [];
    try { for (var f of Array.prototype.slice.call($("#product-images").files, 0, 5)) p.image_upload_ids.push((await KS.upload("product", f)).upload_id); }
    catch (err) { KS.toast(err.message, "danger"); return; }
    KS.action("product.create", p);
  });

  /* ---- insurance policy with document ---- */
  var inf = $("#insurance-form");
  if (inf) inf.addEventListener("submit", async function (e) {
    e.preventDefault(); if (!inf.reportValidity()) return;
    var file = $("#ins-file").files[0]; if (!file) { KS.toast(t("js.choose_file"), "warning"); return; }
    var policy = KS.formPayload(inf);
    try { policy.upload_id = (await KS.upload("insurance", file)).upload_id; } catch (err) { KS.toast(err.message, "danger"); return; }
    KS.action("insurance.add", { farm_id: inf.dataset.farm, policy: policy });
  });

  /* ---- ledger entry with optional receipt ---- */
  var lf = $("#ledger-form");
  if (lf) lf.addEventListener("submit", async function (e) {
    e.preventDefault(); if (!lf.reportValidity()) return;
    var p = KS.formPayload(lf); p.farm_id = lf.dataset.farm;
    var f = $("#ledger-file").files[0];
    try { if (f) p.upload_id = (await KS.upload("expense", f)).upload_id; } catch (err) { KS.toast(err.message, "danger"); return; }
    KS.action("ledger.add", p);
  });

  /* ---- new branch ---- */
  var bf = $("#branch-new");
  if (bf) bf.addEventListener("submit", function (e) {
    e.preventDefault(); if (!bf.reportValidity()) return;
    if (!$("#nb-lat").value) { KS.toast(t("js.pick_location_first"), "warning"); return; }
    var p = KS.formPayload(bf);
    p.branch.crops = [{ name: bf.querySelector('[name="first_crop"]').value }];
    KS.action("branch.create", { farm_id: bf.dataset.farm, branch: p.branch });
  });

  /* ---- co-owner votes ---- */
  $$("[data-vote]").forEach(function (b) {
    b.addEventListener("click", async function () {
      try { var r = await KS.api("/api/proposals/" + b.dataset.vote + "/vote", { method: "POST", json: { vote: b.dataset.choice } }); KS.toast(t("js.vote_" + r.status, { n: r.remaining || 0 }), "success"); setTimeout(function () { location.reload(); }, 800); }
      catch (e) { KS.toast(e.message, "danger"); }
    });
  });

  /* ---- AI terms legality check ---- */
  $$("[data-terms-check]").forEach(function (b) {
    b.addEventListener("click", async function () {
      var out = $(b.dataset.out); out.textContent = t("js.ai_checking_terms");
      try {
        var r = await KS.api("/api/terms/check", { method: "POST", json: { text: $(b.dataset.termsCheck).value } });
        out.textContent = "";
        if (r.ok) out.appendChild(el("div", { class: "ks-alert ok", text: t("js.terms_ok") }));
        else { var ul = el("ul", { class: "ks-list" }); (r.issues || []).forEach(function (i) { ul.appendChild(el("li", { text: (i.clause || "") + " — " + (i.problem || "") + (i.law ? " (" + i.law + ")" : "") })); }); out.appendChild(el("div", { class: "ks-alert danger" }, el("div", {}, el("strong", { text: t("js.terms_bad") }), ul))); }
      } catch (e) { out.textContent = ""; KS.toast(e.message, "danger"); }
    });
  });
})();
