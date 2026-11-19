(function () {
  "use strict";
  var KS = window.KS, $ = KS.$, t = KS.t, el = KS.el;
  var form = $("#scheme-form"); if (!form) return;
  var CLS = { eligible: "", check_portal: "info", need_info: "warn", not_eligible: "danger" };
  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    var box = $("#scheme-results"); box.textContent = t("js.checking");
    try {
      var r = await KS.api("/api/schemes/check", { method: "POST", json: KS.formPayload(form) });
      box.textContent = "";
      r.results.forEach(function (s) {
        var name = el("strong", { text: s.name }), desc = el("p", { class: "ks-small ks-muted m-0", text: s.desc });
        if (s.translate) { name.setAttribute("data-translate", ""); desc.setAttribute("data-translate", ""); }
        var tags = el("span", { class: "ks-row" });
        (s.badges || []).forEach(function (b) { tags.appendChild(el("span", { class: "ks-badge " + (b === "new" ? "gold" : "info"), text: t("js.scheme_badge_" + b) })); });
        tags.appendChild(el("span", { class: "ks-badge " + CLS[s.status], text: s.status_label }));
        box.appendChild(el("article", { class: "ks-card" },
          el("div", { class: "ks-between" }, name, tags),
          desc,
          el("p", { class: "ks-small m-0 mt-1", text: t("js.scheme_who") + ": " + (s.rules || "") }),
          s.reasons.length ? el("div", { class: "ks-small text-red-600", text: s.reasons.join(" · ") }) : null,
          s.missing.length ? el("div", { class: "ks-small", text: t("js.missing_info") + ": " + s.missing.join(", ") }) : null,
          s.status === "eligible" || s.status === "check_portal" ? el("a", { class: "ks-btn ks-btn-sm mt-2", href: s.go }, KS.icon("arrow-up-right"), t("js.apply_official")) : null));
      });
      var eligible = r.results.filter(function (s) { return s.status === "eligible"; }).map(function (s) { return s.name; });
      if (eligible.length) KS.speak(t("js.eligible_for", { list: eligible.join(", ") }));
      KS.icons();
      KS.translateDynamic(box);
    } catch (err) { box.textContent = ""; KS.toast(err.message, "danger"); }
  });
})();
