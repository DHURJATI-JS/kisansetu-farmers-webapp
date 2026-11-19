/* Job search (internal ≤30 km first, external authentic sources as fallback) + apply with live location. */
(function () {
  "use strict";
  var KS = window.KS, $ = KS.$, t = KS.t, el = KS.el, loc = null;

  var applyBtn = $("#apply-btn");
  if (applyBtn) applyBtn.addEventListener("click", async function () {
    if (!$("#apply-terms").checked) { KS.toast(t("js.accept_terms"), "warning"); KS.speak(t("js.accept_terms")); return; }
    var payload = { job_id: applyBtn.dataset.job, accept_terms: true };   // distance from the saved location (GPS or typed)
    var note = $("#apply-note").value.trim(); if (note) payload.note = note;
    KS.action("job.apply", payload);
  });

  var form = $("#job-search"); if (!form) return;
  function card(j) {
    return el("a", { class: "ks-card hover no-underline text-inherit ks-reveal is-visible", href: "/jobs/" + j.id },
      el("div", { class: "ks-between" }, el("strong", { text: j.title, "data-translate": "" }), j.distance_km != null ? el("span", { class: "ks-badge", text: t("js.km", { n: j.distance_km }) }) : null),
      el("div", { class: "ks-small ks-muted", text: j.farm + " · " + j.branch + " · " + [j.city, j.state].filter(Boolean).join(", ") }),
      el("div", { class: "ks-row mt-2" }, el("span", { class: "ks-badge gold", text: j.salary + " / " + j.unit }), el("span", { class: "ks-badge info", text: j.hours }),
        el("span", { class: "ks-badge", text: j.role }), j.custom_terms ? el("span", { class: "ks-badge warn", text: t("js.custom_terms") }) : null),
      j.crops && j.crops.length ? el("div", { class: "ks-small mt-1", text: j.crops.join(", "), "data-translate": "" }) : null);
  }
  function extCard(j) {
    return el("div", { class: "ks-card" },
      el("div", { class: "ks-between" }, el("strong", { text: j.title, "data-translate": "" }), el("span", { class: "ks-badge warn", text: t("js.external_badge", { site: j.site }) })),
      el("div", { class: "ks-small ks-muted", text: [j.employer, j.city, j.state].filter(Boolean).join(" · ") }),
      j.salary ? el("div", { class: "ks-badge gold mt-1", text: j.salary }) : null,
      j.snippet ? el("p", { class: "ks-small", text: j.snippet, "data-translate": "" }) : null,
      el("div", { class: "ks-between mt-2" }, el("span", { class: "ks-small ks-muted", text: j.distance_km != null ? t("js.km", { n: j.distance_km }) : "" }),
        el("a", { class: "ks-btn ks-btn-ghost ks-btn-sm", href: j.url, target: "_blank", rel: "noopener noreferrer nofollow" }, KS.icon("external-link"), t("js.open_source"))));
  }
  async function search() {
    var p = new URLSearchParams();
    ["q", "city", "min_salary", "sort", "source"].forEach(function (k) { var v = form[k].value.trim(); if (v) p.set(k, v); });
    if (loc) { p.set("lat", loc.lat); p.set("lon", loc.lon); }
    var box = $("#js-internal"); box.textContent = t("js.searching");
    try {
      var r = await KS.api("/api/jobs/search?" + p.toString());
      box.textContent = "";
      if (!r.internal.length) box.appendChild(el("div", { class: "ks-empty", text: loc ? t("js.no_jobs_near", { km: r.radius_km }) : t("js.no_jobs") }));
      r.internal.forEach(function (j) { box.appendChild(card(j)); });
      var ext = $("#js-external"), wrap = $("#js-external-wrap"); ext.textContent = "";
      wrap.classList.toggle("ks-hidden", !(r.external.length || (r.portals || []).length));
      r.external.forEach(function (j) { ext.appendChild(extCard(j)); });
      var portals = $("#js-portals"); portals.textContent = "";
      if ((r.portals || []).length) {
        portals.appendChild(el("h3", { class: "ks-h3", text: t("js.official_portals") }));
        var row = el("div", { class: "ks-row" });
        r.portals.forEach(function (pt) { row.appendChild(el("a", { class: "ks-chip", href: pt.url, target: "_blank", rel: "noopener noreferrer" }, KS.icon("landmark"), pt.name)); });
        portals.appendChild(row);
      }
      KS.icons();
      KS.translateDynamic($("#js-internal").parentNode);
    } catch (e) { box.textContent = ""; KS.toast(e.message, "danger"); }
  }
  form.addEventListener("submit", function (e) { e.preventDefault(); search(); });
  $("#js-near").addEventListener("click", async function () {
    try {
      loc = await KS.position();
      $("#js-loc").textContent = t(loc.saved ? "js.using_saved_location" : "js.using_location");
      form.sort.value = "distance"; search();
    } catch (e) {}
  });
  search();
})();
