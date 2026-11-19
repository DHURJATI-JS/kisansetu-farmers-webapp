/* Kisan Mitra AI widget + assistant page. Replies are rendered as TEXT only; actions need the user's GO. */
(function () {
  "use strict";
  var KS = window.KS; if (!KS) return;
  var t = KS.t, el = KS.el, icon = KS.icon;
  var NAV_LABEL = { "/jobs": "js.nav_jobs", "/shop": "js.nav_shop", "/schemes": "js.nav_schemes", "/account": "js.nav_account", "/survey": "js.nav_survey",
    "/report": "js.nav_report", "/orders": "js.nav_orders", "/dashboard": "js.nav_dashboard", "/farms/new": "js.nav_farm_new",
    "/notifications": "js.nav_notifications", "/policies": "js.nav_policies", "/community": "js.nav_community", "/assistant": "js.nav_assistant" };

  function init(panel) {
    var log = panel.querySelector("[data-ai-log]"), input = panel.querySelector("[data-ai-input]"), form = panel.querySelector("[data-ai-form]");
    var autoSpeak = false, pending = {}, sessionId = sessionStorage.getItem("ks_ai_sid") || KS.uuid();
    try { sessionStorage.setItem("ks_ai_sid", sessionId); } catch (e) {}
    if (!log || !form) return;

    function scroll() { log.scrollTop = log.scrollHeight; }
    function add(node) { log.appendChild(node); KS.icons(); scroll(); return node; }
    function userMsg(text) { add(el("div", { class: "ks-msg user", text: text })); }
    function typing() { return add(el("div", { class: "ks-msg bot ks-typing", "aria-label": t("js.ai_typing") }, el("span"), el("span"), el("span"))); }

    function actionCard(card) {
      if (card.error) return el("div", { class: "ks-ai-card" }, el("strong", { text: t("js.ai_cannot") }), el("div", { class: "ks-small", text: card.error }));
      var list = el("ul", { class: "ks-list ks-small" });
      (card.summary || []).forEach(function (s) { list.appendChild(el("li", { text: s })); });
      (card.warnings || []).forEach(function (w) { list.appendChild(el("li", { class: "ks-badge warn", text: w })); });
      var go = el("button", { class: "ks-btn ks-btn-go ks-btn-sm", type: "button" }, icon("sparkles"), card.go_label || t("js.go"));
      go.addEventListener("click", function () { KS.showPrepared(card, { reload: false, onDone: function () { go.disabled = true; } }); });
      return el("div", { class: "ks-ai-card" }, el("div", { class: "ks-small ks-muted", text: t("js.ai_proposes") }), list, go);
    }

    function botMsg(res) {
      var box = el("div", { class: "ks-msg bot" });
      if (res.error) { box.appendChild(el("span", { text: res.error })); return add(box); }
      box.appendChild(el("span", { text: res.reply || "" }));
      if (res.low_confidence) box.appendChild(el("span", { class: "conf", text: t("js.ai_low_conf") }));
      (res.actions || []).forEach(function (c) { box.appendChild(actionCard(c)); });
      if ((res.navigate || []).length) {
        var row = el("div", { class: "ks-row" });
        res.navigate.forEach(function (route) {
          if (!NAV_LABEL[route]) return;  // only fixed internal routes — never URLs from the model
          row.appendChild(el("a", { class: "ks-chip", href: route }, icon("arrow-right"), t(NAV_LABEL[route])));
        });
        box.appendChild(row);
      }
      if ((res.suggestions || []).length) {
        var sug = el("div", { class: "ks-row" });
        res.suggestions.forEach(function (s) { sug.appendChild(el("button", { class: "ks-chip", type: "button", text: s, onclick: function () { send(s); } })); });
        box.appendChild(sug);
      }
      var talk = function () { if (window.KSGuide) window.KSGuide.say(res.reply, res.speech); else KS.speak(res.reply); };
      var spk = el("button", { class: "ks-icon-btn", type: "button", "aria-label": t("js.listen"), onclick: talk }, icon("volume-2"));
      var row = el("div", { class: "ks-row" }, spk);
      // the assistant may point at a part of THIS screen (validated on the server; shown only if it is really here)
      if (res.highlight && window.KSGuide) {
        row.appendChild(el("button", { class: "ks-chip", type: "button", onclick: function () { window.KSGuide.point(res.highlight); } },
          icon("scan-eye"), t("js.ai_show_me")));
      }
      box.appendChild(row);
      if (autoSpeak) talk();
      return add(box);
    }

    async function send(text) {
      text = (text || "").trim(); if (!text) return;
      userMsg(text); input.value = "";
      var tip = typing();
      var finish = function (res) { tip.remove(); botMsg(res); };
      if (KS.socket && KS.socket.connected) {
        var id = KS.uuid(); pending[id] = finish;
        KS.socket.emit("ai:ask", { id: id, message: text, page_keys: window.KSGuide ? window.KSGuide.keys() : [] });
        // local models can be slow — wait for the socket reply instead of sending a duplicate request
        setTimeout(function () { if (pending[id]) { delete pending[id]; finish({ error: t("js.ai_timeout") }); } }, 240000);
      } else httpSend(text, finish);
    }
    async function httpSend(text, finish) {
      try { finish(await KS.api("/api/ai/chat", { method: "POST", json: { message: text, session: sessionId.slice(0, 36),
                                                                         page_keys: window.KSGuide ? window.KSGuide.keys() : [] } })); }
      catch (e) { finish({ error: e.message }); }
    }
    if (KS.socket) KS.socket.on("ai:reply", function (res) { var f = pending[res.id]; if (f) { delete pending[res.id]; f(res); } });

    form.addEventListener("submit", function (e) { e.preventDefault(); send(input.value); });
    input.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); } });
    panel.querySelectorAll("[data-ai-prompt]").forEach(function (b) { b.addEventListener("click", function () { send(b.dataset.aiPrompt || b.textContent); }); });
    var mic = panel.querySelector("[data-ai-mic]");
    if (mic) mic.addEventListener("click", function () { KS.listen(function (txt) { input.value = txt; send(txt); }); });
    var spk = panel.querySelector("[data-ai-speak]");
    if (spk) spk.addEventListener("click", function () { autoSpeak = !autoSpeak; spk.classList.toggle("ks-btn-gold", autoSpeak); KS.toast(autoSpeak ? t("js.ai_voice_on") : t("js.ai_voice_off")); });
    var photo = panel.querySelector("[data-ai-photo]");
    if (photo) photo.addEventListener("change", async function () {
      var f = photo.files && photo.files[0]; if (!f) return;
      userMsg(t("js.ai_photo_sent"));
      var tip = typing();
      var fd = new FormData(); fd.append("file", f); if (input.value.trim()) fd.append("question", input.value.trim());
      try {
        var r = await KS.api("/api/ai/plant", { method: "POST", form: fd });
        tip.remove();
        var card = el("div", { class: "ks-msg bot" },
          el("img", { src: r.image, alt: t("js.ai_plant_photo"), class: "rounded-lg mb-2 max-h-48" }),
          el("strong", { text: (r.plant || t("js.unknown")) + " · " + t("js.ai_confidence", { n: Math.round((r.confidence || 0) * 100) }) }),
          el("div", { class: "ks-small", text: t("js.ai_health") + ": " + (r.health || "") }), el("div", { text: r.reply || "" }));
        (r.issues || []).forEach(function (i) { card.appendChild(el("div", { class: "ks-small" }, el("strong", { text: "• " + i.name + ": " }), i.signs || "")); });
        [["js.ai_organic", r.organic], ["js.ai_chemical", r.chemical], ["js.ai_prevention", r.prevention]].forEach(function (sec) {
          if (!sec[1] || !sec[1].length) return;
          var ul = el("ul", { class: "ks-list ks-small" }); sec[1].forEach(function (x) { ul.appendChild(el("li", { text: x })); });
          card.appendChild(el("div", {}, el("strong", { text: t(sec[0]) }), ul));
        });
        card.appendChild(el("div", { class: "ks-small ks-muted", text: t("js.ai_kvk", { phone: r.helpline || "" }) }));
        add(card); if (autoSpeak) KS.speak(r.reply);
      } catch (e) { tip.remove(); botMsg({ error: e.message }); }
      photo.value = "";
    });
  }

  var fab = document.querySelector("[data-ai-fab]"), floating = document.querySelector("[data-ai-panel]");
  if (fab && floating) {
    fab.addEventListener("click", function () {
      var open = floating.classList.toggle("ks-hidden") === false;
      fab.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) { var i = floating.querySelector("[data-ai-input]"); if (i) i.focus(); }
    });
    floating.querySelectorAll("[data-ai-close]").forEach(function (b) {
      b.addEventListener("click", function () { floating.classList.add("ks-hidden"); fab.setAttribute("aria-expanded", "false"); fab.focus(); });
    });
    init(floating);
    if (floating.dataset.autoOpen === "1") {
      // open beside the page only where it cannot cover the form; otherwise draw attention to the helper button
      if (window.innerWidth >= 1480) floating.classList.remove("ks-hidden"); else fab.classList.add("ks-attention");
    }
  }
  document.querySelectorAll("[data-ai-embedded]").forEach(init);
})();
