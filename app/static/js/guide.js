/* Kisan Didi — talking guide for people who cannot read well: a live face (lip-sync, blinking, eyebrows), page tours
   that light up one part of the screen at a time, tap-to-explain hard words, and terms explained in plain words with
   warnings. CSP-safe: DOM APIs and SVG attributes only — no inline handlers, no eval, no HTML strings. */
(function () {
  "use strict";
  var KS = window.KS; if (!KS) return;
  var doc = document, root = doc.documentElement, t = KS.t, el = KS.el, icon = KS.icon;
  var LANG = root.dataset.lang || "en", SPEECH = root.dataset.speech || "en-IN";
  var REDUCED = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  var CAT = null, catLoading = null;

  function catalog() {
    if (CAT) return Promise.resolve(CAT);
    if (!catLoading) catLoading = KS.api("/api/guide/catalog?lang=" + encodeURIComponent(LANG))
      .then(function (c) { CAT = c || { guide: {}, gloss: [] }; return CAT; })
      .catch(function () { CAT = { guide: {}, gloss: [] }; return CAT; });
    return catLoading;
  }

  /* ------------------------------------------------------------------ the face */
  var Face = (function () {
    var svg = doc.querySelector("[data-av-root]"); if (!svg) return null;
    var q = function (k) { return svg.querySelector('[data-av="' + k + '"]'); };
    var head = q("head"), mouth = q("mouth"), smile = q("smile"), browL = q("brow-l"), browR = q("brow-r");
    var eyeL = q("eye-l"), eyeR = q("eye-r"), irisL = q("iris-l"), irisR = q("iris-r");
    // visemes: [rx, ry] of the open mouth
    var SHAPE = { rest: [7, .9], A: [7.6, 5.6], E: [9.2, 2.7], O: [4.6, 4.9], M: [6.6, .45], F: [7.2, 1.7], C: [6.2, 3.1] };
    var cur = [7, .9], goal = [7, .9], talking = false, live = false, raf = 0, t0 = performance.now(), wordTimer = 0;
    var gaze = [0, 0], gazeGoal = [0, 0];

    function frame(now) {
      cur[0] += (goal[0] - cur[0]) * .38; cur[1] += (goal[1] - cur[1]) * .38;
      mouth.setAttribute("rx", cur[0].toFixed(2)); mouth.setAttribute("ry", cur[1].toFixed(2));
      smile.setAttribute("opacity", talking ? "0" : "1");
      gaze[0] += (gazeGoal[0] - gaze[0]) * .2; gaze[1] += (gazeGoal[1] - gaze[1]) * .2;
      var g = "translate(" + gaze[0].toFixed(2) + " " + gaze[1].toFixed(2) + ")";
      irisL.setAttribute("transform", g); irisR.setAttribute("transform", g);
      if (!REDUCED) {
        var s = (now - t0) / 1000, tilt = Math.sin(s * .75) * 1.6 + (talking ? Math.sin(s * 5.3) * .7 : 0);
        head.setAttribute("transform", "rotate(" + tilt.toFixed(2) + " 80 122) translate(0 " + (Math.sin(s * 1.5) * .6).toFixed(2) + ")");
      }
      raf = live ? requestAnimationFrame(frame) : 0;
    }
    function wake() { live = true; if (!raf) raf = requestAnimationFrame(frame); }
    function sleep() { live = false; }
    function setEyes(sy) {
      [[eyeL, 68], [eyeR, 92]].forEach(function (e) { e[0].setAttribute("transform", sy === 1 ? "" : "translate(" + e[1] + " 66) scale(1 " + sy + ") translate(-" + e[1] + " -66)"); });
    }
    function blink() { setEyes(.12); setTimeout(function () { setEyes(1); }, 130); }
    (function blinkLoop() { setTimeout(function () { if (live) { blink(); if (Math.random() < .2) setTimeout(blink, 260); } blinkLoop(); }, 2300 + Math.random() * 3700); })();
    (function lookLoop() { setTimeout(function () { gazeGoal = live && !REDUCED && Math.random() < .5 ? [(Math.random() - .5) * 2.2, (Math.random() - .5) * 1.2] : [0, 0]; lookLoop(); }, 1500 + Math.random() * 2500); })();
    function brows(up) { var tr = up ? "translate(0 -2.6)" : ""; browL.setAttribute("transform", tr); browR.setAttribute("transform", tr); }
    function lift(ms) { brows(true); setTimeout(function () { brows(false); }, ms || 650); }

    // letters → mouth shapes (Latin, Devanagari vowels/matras; other scripts alternate open/half-open per syllable)
    var V_A = /[aāअआा]/i, V_E = /[eiyīइईएऐिीेै]/i, V_O = /[ouwūउऊओऔुूोौ]/i, V_M = /[mbpमबपभफ]/i, V_F = /[fvफ़व]/i;
    function visemes(word) {
      var out = [], letters = Array.from(word || "");
      letters.forEach(function (c, i) {
        if (V_M.test(c)) out.push("M"); else if (V_F.test(c)) out.push("F"); else if (V_O.test(c)) out.push("O");
        else if (V_E.test(c)) out.push("E"); else if (V_A.test(c)) out.push("A");
        else if (/\p{L}/u.test(c)) out.push(i % 2 ? "C" : "A");
      });
      return out.length ? out : ["C"];
    }
    function mouthWord(word, ms) {
      var shapes = visemes(word), step = Math.max(55, ms / shapes.length), i = 0;
      clearTimeout(wordTimer);
      (function next() {
        if (i >= shapes.length) { goal = [SHAPE.C[0], SHAPE.C[1] * .35]; return; }
        goal = SHAPE[shapes[i++]].slice(); wordTimer = setTimeout(next, step);
      })();
    }
    function shut() { clearTimeout(wordTimer); talking = false; goal = SHAPE.rest.slice(); brows(false); }

    function voiceFor(lang) {
      var vs = window.speechSynthesis ? window.speechSynthesis.getVoices() : [], base = lang.split("-")[0];
      return vs.find(function (v) { return v.lang === lang; }) || vs.find(function (v) { return v.lang && v.lang.replace("_", "-").indexOf(base) === 0; }) || null;
    }
    function voicesReady() {
      return new Promise(function (res) {
        if (!window.speechSynthesis) return res([]);
        var v = window.speechSynthesis.getVoices(); if (v.length) return res(v);
        var done = false, fin = function () { if (!done) { done = true; res(window.speechSynthesis.getVoices()); } };
        window.speechSynthesis.addEventListener("voiceschanged", fin, { once: true }); setTimeout(fin, 1200);
      });
    }
    var current = null;
    async function say(text, lang) {
      text = String(text || "").slice(0, 1500); lang = lang || SPEECH;
      stop(); wake();
      if (!text || !window.speechSynthesis) return;
      await voicesReady();
      var voice = voiceFor(lang);
      if (!voice) { KS.toast(t("js.guide_no_voice"), "info", 3500); return; }
      return new Promise(function (resolve) {
        var u = new SpeechSynthesisUtterance(text), charMs = 62 / .95, sawBoundary = false, fallback = 0;
        u.lang = lang; u.voice = voice; u.rate = .95; current = u;
        talking = true; lift(700);
        u.onboundary = function (e) {
          if (e.name && e.name !== "word") return;
          sawBoundary = true; clearTimeout(fallback);
          var rest = text.slice(e.charIndex), len = e.charLength || (rest.search(/[\s,.;:!?।]/) + 1 || rest.length);
          var word = rest.slice(0, len);
          mouthWord(word, word.length * charMs);
          if (/[?!]/.test(text.charAt(e.charIndex + len)) || /^[A-Zऀ-ॿ]/.test(word) && Math.random() < .15) lift(500);
        };
        // engines without word events (many Android / network voices): time the words ourselves
        fallback = setTimeout(function play() {
          if (sawBoundary || current !== u) return;
          var words = text.split(/\s+/), i = 0;
          (function next() {
            if (current !== u || sawBoundary || i >= words.length) return;
            var w = words[i++]; mouthWord(w, w.length * charMs); if (/[?!]$/.test(w)) lift(450);
            fallback = setTimeout(next, w.length * charMs + 70);
          })();
        }, 380);
        u.onend = u.onerror = function () { clearTimeout(fallback); if (current === u) { current = null; shut(); } resolve(); };
        window.speechSynthesis.speak(u);
      });
    }
    function stop() { current = null; try { if (window.speechSynthesis) window.speechSynthesis.cancel(); } catch (e) {} shut(); }
    return { say: say, stop: stop, wake: wake, sleep: sleep, blink: blink, lift: lift };
  })();

  /* ------------------------------------------------------------------ tours */
  var dock = doc.querySelector("[data-guide-dock]"), spot = doc.querySelector("[data-guide-spot]"), fab = doc.querySelector("[data-guide-start]");
  var title = dock && dock.querySelector(".ks-guide-title"), text = dock && dock.querySelector(".ks-guide-text"), count = dock && dock.querySelector("[data-guide-count]");
  // page → screen parts (existing ids/selectors) in reading order; parts marked data-guide="key" in templates join in DOM order
  var TOURS = [
    [/^\/$/, [["lang", "#ks-lang"], ["signup", 'a[href="/signup"]'], ["login", 'a[href="/login"]']]],
    [/^\/dashboard\/?$/, [["my-job", '[data-guide="my-job"]'], ["pay-stats", '[data-guide="pay-stats"]'], ["contacts", '[data-guide="contacts"]'],
      ["report", 'a[href="/report"]'], ["statement", 'a[href="/account/statement.csv"]'], ["resign", '[data-action="member.resign"]']]],
    [/^\/jobs\/?$/, [["job-search", "#job-search"], ["jobs-near", "#js-near"], ["job-list", "#js-internal"]]],
    [/^\/jobs\/[^/]+$/, [["job-rights", "#rights"], ["terms", "#terms"], ["terms-explain", "[data-terms-explain]"], ["terms-accept", "#apply-terms"], ["job-apply", "#apply-btn"]]],
    [/^\/f\/[^/]+$/, [["terms", "#terms"], ["terms-explain", "[data-terms-explain]"]]],
    [/^\/schemes\/?$/, [["scheme-form", "#scheme-form"], ["scheme-results", "#scheme-results"]]],
    [/^\/report\/?$/, [["report-what", 'form[data-action-form="grievance.report"] fieldset'], ["report-voice", "#rp-desc"], ["report-send", 'form[data-action-form="grievance.report"] [type="submit"]']]],
    [/^\/signup/, [["signup-role", '[name="role"]'], ["signup-phone", '[name="phone"]'], ["signup-otp", '[name="otp"]'], ["signup-pin", '[name="password"]']]],
    [/^\/login/, [["login-phone", '[name="phone"]'], ["login-pin", '[name="password"]']]],
    [/^\/account/, [["account-lang", "#ks-lang"]]],
    [/^\/shop/, [["shop-search", 'form[role="search"]'], ["shop-cart", "[data-cart-count]"]]],
  ];
  var COMMON = [["nav", ".ks-bottomnav"], ["ai-help", "[data-ai-fab]"]];

  function visible(n) {
    if (!n || !n.getClientRects().length) return false;
    var r = n.getBoundingClientRect(); return r.width > 4 && r.height > 4 && getComputedStyle(n).visibility !== "hidden";
  }
  function anchor(n) { return n && n.matches && n.matches("input,select,textarea") ? (n.closest(".ks-field,fieldset,label") || n) : n; }
  function parts() {
    var path = location.pathname, list = [], seen = {};
    function add(key, node) { node = anchor(node); if (!seen[key] && visible(node) && CAT.guide[key]) { seen[key] = 1; list.push({ key: key, node: node }); } }
    TOURS.forEach(function (tr) { if (tr[0].test(path)) tr[1].forEach(function (p) { add(p[0], doc.querySelector(p[1])); }); });
    doc.querySelectorAll("[data-guide]").forEach(function (n) { add(n.dataset.guide, n); });
    COMMON.forEach(function (p) { add(p[0], doc.querySelector(p[1])); });
    return list;
  }
  function keysOnScreen() {
    if (!CAT) return [];
    return parts().map(function (p) { return p.key; }).slice(0, 30);
  }

  var steps = [], at = -1, follow = null;
  function place() {
    var s = steps[at]; if (!s || !spot) return;
    var r = s.node.getBoundingClientRect(), pad = 8;
    spot.style.top = (r.top - pad) + "px"; spot.style.left = (r.left - pad) + "px";
    spot.style.width = (r.width + pad * 2) + "px"; spot.style.height = (r.height + pad * 2) + "px";
  }
  function show(i) {
    if (i < 0 || i >= steps.length) return;
    at = i; var s = steps[i], g = CAT.guide[s.key];
    s.node.scrollIntoView({ block: "center", behavior: REDUCED ? "auto" : "smooth" });
    spot.classList.remove("ks-hidden");
    setTimeout(place, REDUCED ? 0 : 380); place();
    title.textContent = g.title; text.textContent = g.text;
    count.textContent = steps.length > 1 ? t("js.guide_step", { n: i + 1, total: steps.length }) : "";
    dock.querySelector("[data-guide-prev]").disabled = i === 0;
    var nx = dock.querySelector("[data-guide-next]");
    nx.replaceChildren(doc.createTextNode(i === steps.length - 1 ? t("js.guide_done") : t("js.guide_next")), icon(i === steps.length - 1 ? "check" : "chevron-right"));
    KS.icons();
    Face && Face.say(g.title + ". " + g.text, CAT.speech);
  }
  async function open(list) {
    await catalog();
    steps = list || parts();
    if (!steps.length) { KS.toast(t("js.guide_nothing"), "info"); return; }
    dock.classList.remove("ks-hidden"); fab && fab.classList.add("ks-hidden");
    if (Face) Face.wake();
    if (!follow) { follow = function () { place(); }; window.addEventListener("scroll", follow, { passive: true }); window.addEventListener("resize", follow); }
    show(0);
    dock.querySelector("[data-guide-next]").focus({ preventScroll: true });
    try { localStorage.setItem("ks_guided:" + location.pathname.split("/")[1], "1"); } catch (e) {}
  }
  function close() {
    if (Face) { Face.stop(); Face.sleep(); }
    dock.classList.add("ks-hidden"); spot.classList.add("ks-hidden"); fab && fab.classList.remove("ks-hidden");
    if (follow) { window.removeEventListener("scroll", follow); window.removeEventListener("resize", follow); follow = null; }
    steps = []; at = -1;
  }
  async function point(key) {               // the assistant asked to show one screen part (validated server-side)
    await catalog();
    var p = parts().filter(function (x) { return x.key === key; });
    if (p.length) open(p);
  }
  async function say(textToSay, lang) {     // answers read aloud by the face (AI replies, glossary, terms)
    if (!Face) { KS.speak(textToSay); return; }
    await catalog();
    if (dock.classList.contains("ks-hidden")) {
      dock.classList.remove("ks-hidden"); spot.classList.add("ks-hidden"); fab && fab.classList.add("ks-hidden");
      title.textContent = t("js.guide_name"); count.textContent = ""; steps = []; at = -1;
      dock.querySelector("[data-guide-prev]").disabled = true;
    }
    text.textContent = textToSay;
    Face.wake(); await Face.say(textToSay, lang);
  }

  if (dock && fab) {
    fab.addEventListener("click", function () { open(); });
    dock.querySelector("[data-guide-close]").addEventListener("click", close);
    dock.querySelector("[data-guide-prev]").addEventListener("click", function () { show(at - 1); });
    dock.querySelector("[data-guide-next]").addEventListener("click", function () { if (at >= steps.length - 1) close(); else show(at + 1); });
    dock.querySelector("[data-guide-repeat]").addEventListener("click", function () {
      if (steps[at]) show(at); else if (Face) Face.say(text.textContent, CAT && CAT.speech);
    });
    doc.addEventListener("keydown", function (e) {
      if (dock.classList.contains("ks-hidden")) return;
      if (e.key === "Escape") close();
      else if (steps.length && e.key === "ArrowRight" && !/INPUT|TEXTAREA|SELECT/.test((e.target.tagName || ""))) { if (at < steps.length - 1) show(at + 1); }
      else if (steps.length && e.key === "ArrowLeft" && !/INPUT|TEXTAREA|SELECT/.test((e.target.tagName || ""))) show(at - 1);
    });
    // first visit to this kind of page: the face waves (pulses) — speech needs a tap, never plays by itself
    var seenKey = "ks_guided:" + location.pathname.split("/")[1];
    try { if (!localStorage.getItem(seenKey)) fab.classList.add("ks-attention"); } catch (e) {}
  }

  /* ------------------------------------------------------------------ glossary: tap a hard word */
  var pop = null;
  function closePop() { if (pop) { pop.remove(); pop = null; } }
  function esc(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
  async function markWords() {
    var main = doc.querySelector("#main"); if (!main || !dock) return;   // the guide (and its glossary) is for people, not the admin console
    await catalog();
    var entries = (CAT.gloss || []), byTerm = {};
    var terms = [];
    entries.forEach(function (g) { g.terms.forEach(function (w) { byTerm[w.toLocaleLowerCase()] = g; terms.push(w); }); });
    if (!terms.length) return;
    terms.sort(function (a, b) { return b.length - a.length; });
    var rx;
    try { rx = new RegExp("(?<![\\p{L}\\p{M}\\p{N}])(" + terms.map(esc).join("|") + ")(?![\\p{L}\\p{M}\\p{N}])", "iu"); } catch (e) { return; }
    var used = {}, marked = 0, nodes = [];
    var walker = doc.createTreeWalker(main, NodeFilter.SHOW_TEXT, { acceptNode: function (n) {
      if (!n.nodeValue || n.nodeValue.trim().length < 2) return NodeFilter.FILTER_REJECT;
      var p = n.parentElement;
      if (!p || p.closest("script,style,textarea,input,select,option,button,a,label,code,pre,h1,h2,.ks-term,[data-no-gloss],[contenteditable],.ks-modal,.ks-badge,svg")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT; } });
    while (walker.nextNode() && nodes.length < 600) nodes.push(walker.currentNode);
    nodes.forEach(function (node) {
      if (marked >= 25) return;
      var m = rx.exec(node.nodeValue); if (!m) return;
      var g = byTerm[m[1].toLocaleLowerCase()]; if (!g || used[g.id]) return;
      used[g.id] = 1; marked++;
      var mid = node.splitText(m.index); mid.splitText(m[1].length);
      var b = el("button", { type: "button", class: "ks-term", "data-term": g.id, "aria-haspopup": "dialog", text: mid.nodeValue });
      mid.parentNode.replaceChild(b, mid);
    });
  }
  doc.addEventListener("click", async function (e) {
    var b = e.target.closest(".ks-term");
    if (!b) { if (pop && !e.target.closest(".ks-pop-term")) closePop(); return; }
    e.preventDefault(); closePop(); await catalog();
    var g = (CAT.gloss || []).find(function (x) { return x.id === b.dataset.term; }); if (!g) return;
    var listen = el("button", { type: "button", class: "ks-btn ks-btn-sm" }, icon("volume-2"), t("js.listen"));
    var word = b.textContent.trim() || g.word;
    listen.addEventListener("click", function () { say(word + ". " + g.simple, CAT.speech); });
    var x = el("button", { type: "button", class: "ks-icon-btn", "aria-label": t("js.close") }, icon("x"));
    x.addEventListener("click", closePop);
    pop = el("div", { class: "ks-pop-term", role: "dialog", "aria-label": word },
      el("div", { class: "ks-between" }, el("strong", { text: word }), x), el("p", { class: "m-0", text: g.simple }), listen);
    doc.body.appendChild(pop); KS.icons();
    var r = b.getBoundingClientRect(), w = Math.min(320, window.innerWidth - 24);
    pop.style.width = w + "px";
    pop.style.left = Math.max(12, Math.min(window.innerWidth - w - 12, r.left)) + "px";
    pop.style.top = (r.bottom + 8 + window.scrollY) + "px";
    listen.focus({ preventScroll: true });
  });
  doc.addEventListener("keydown", function (e) { if (e.key === "Escape") closePop(); });

  /* ------------------------------------------------------------------ terms in plain words + risk warnings */
  function markQuote(container, quote, cls) {
    // the quote may span line breaks in the shown text: match its words with any whitespace between them
    var words = String(quote || "").trim().split(/\s+/).filter(Boolean);
    if (!container || words.join(" ").length < 6) return false;
    var rq; try { rq = new RegExp(words.map(esc).join("\\s+"), "iu"); } catch (e) { return false; }
    var walker = doc.createTreeWalker(container, NodeFilter.SHOW_TEXT), n;
    while ((n = walker.nextNode())) {
      var m = rq.exec(n.nodeValue);
      if (!m || n.parentElement.closest("mark")) continue;
      var mid = n.splitText(m.index); mid.splitText(m[0].length);
      var mk = el("mark", { class: cls, text: mid.nodeValue }); mid.parentNode.replaceChild(mk, mid);
      return true;
    }
    return false;
  }
  var LEVEL_ICON = { high: "octagon-alert", medium: "triangle-alert", low: "info" };
  doc.addEventListener("click", async function (e) {
    var b = e.target.closest("[data-terms-explain]"); if (!b) return;
    e.preventDefault();
    var out = doc.querySelector(b.dataset.target), src = doc.querySelector(b.dataset.source);
    if (!out) return;
    b.disabled = true; out.replaceChildren(el("div", { class: "ks-msg bot ks-typing", "aria-label": t("js.ai_typing") }, el("span"), el("span"), el("span")));
    try {
      var r = await KS.api("/api/terms/explain", { method: "POST", json: { farm_id: b.dataset.farm } });
      var box = el("div", { class: "ks-terms-x", role: "region", "aria-label": t("js.terms_simple") });
      var spoken = [];
      if (r.risks && r.risks.length) {
        box.appendChild(el("h3", { class: "ks-h3", text: t("js.terms_watch") }));
        var ul = el("ul", { class: "ks-list" });
        r.risks.forEach(function (k) {
          ul.appendChild(el("li", { class: "ks-risk-item ks-risk-" + k.level },
            el("span", { class: "ks-badge " + (k.level === "high" ? "danger" : k.level === "medium" ? "warn" : "info") }, icon(LEVEL_ICON[k.level] || "info"), k.label),
            el("strong", { text: " " + k.title }), el("div", { class: "ks-small", text: k.why }),
            k.quote ? el("q", { class: "ks-small ks-muted", text: k.quote }) : null));
          markQuote(src, k.quote, "ks-risk-mark ks-risk-" + k.level);
          spoken.push(k.label + ": " + k.title + ". " + k.why);
        });
        box.appendChild(ul);
      } else if (r.custom) {
        box.appendChild(el("div", { class: "ks-alert info" }, icon("shield-check"), el("div", { text: t("js.terms_no_risks") })));
      }
      if (r.points && r.points.length) {
        box.appendChild(el("h3", { class: "ks-h3", text: t("js.terms_simple") }));
        var ol = el("ol", { class: "ks-list" });
        r.points.forEach(function (p) { ol.appendChild(el("li", {}, el("span", { text: p.text }), p.quote ? el("q", { class: "ks-small ks-muted block", text: p.quote }) : null)); spoken.push(p.text); });
        box.appendChild(ol);
      } else if (r.custom) {
        box.appendChild(el("p", { class: "ks-small ks-muted", text: t(r.pending ? "js.terms_pending" : "js.terms_ai_off") }));
      }
      var listen = el("button", { type: "button", class: "ks-btn ks-btn-sm" }, icon("volume-2"), t("js.listen"));
      listen.addEventListener("click", function () { say(spoken.join(" "), r.speech); });
      box.appendChild(el("div", { class: "ks-row mt-2" }, listen));
      box.appendChild(el("p", { class: "ks-small ks-muted mt-2", text: t("js.terms_not_legal_advice") }));
      out.replaceChildren(box); KS.icons();
    } catch (err) { out.replaceChildren(el("div", { class: "ks-alert warn" }, icon("triangle-alert"), el("div", { text: err.message }))); KS.icons(); }
    b.disabled = false;
  });

  window.KSGuide = { open: open, point: point, say: say, keys: keysOnScreen, ready: catalog };
  var start = function () { catalog().then(markWords); };
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", start); else start();
})();
