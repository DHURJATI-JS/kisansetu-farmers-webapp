/* Notifications, ban appeal, daily wellbeing survey. */
(function () {
  "use strict";
  var KS = window.KS, $ = KS.$, $$ = KS.$$, t = KS.t;

  var mark = $("#mark-all");
  if (mark) mark.addEventListener("click", async function () {
    try { await KS.api("/api/notifications/read", { method: "POST", json: { all: true } }); location.reload(); } catch (e) { KS.toast(e.message, "danger"); }
  });

  var appeal = $("#appeal-form");
  if (appeal) appeal.addEventListener("submit", async function (e) {
    e.preventDefault();
    try { var r = await KS.api("/api/account/unban-request", { method: "POST", json: { message: $("#appeal-msg").value } }); KS.toast(r.message, "success"); setTimeout(function () { location.reload(); }, 900); }
    catch (err) { KS.toast(err.message, "danger"); }
  });

  var sv = $("#survey");
  if (sv) {
    var rating = null, answers = {};
    $$("[data-rating]", sv).forEach(function (b) {
      b.addEventListener("click", function () {
        rating = b.dataset.rating;
        $$("[data-rating]", sv).forEach(function (x) { x.classList.toggle("selected", x === b); });
        $("#sv-more").classList.remove("ks-hidden");
        KS.speak(t(rating === "bad" ? "js.survey_sorry" : "js.survey_glad"));
      });
    });
    $$("[data-question]", sv).forEach(function (q) {
      $$("[data-answer]", q).forEach(function (b) {
        b.addEventListener("click", function () {
          answers[q.dataset.question] = b.dataset.answer === "true";
          $$("[data-answer]", q).forEach(function (x) { x.classList.toggle("ks-btn-gold", x === b); });
        });
      });
    });
    $("#sv-submit").addEventListener("click", async function () {
      if (!rating) { KS.toast(t("js.survey_pick"), "warning"); return; }
      try {
        var r = await KS.api("/api/welfare/surveys/" + sv.dataset.id, { method: "POST", json: { rating: rating, answers: answers, comment: $("#sv-comment").value || null } });
        KS.toast(r.message, "success"); KS.speak(r.message); setTimeout(function () { location.href = "/dashboard"; }, 1500);
      } catch (e) { KS.toast(e.message, "danger"); }
    });
  }
})();
