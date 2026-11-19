/* Marketplace search, product Q&A (AI → owner fallback), cart & checkout (COD/pickup, idempotent). */
(function () {
  "use strict";
  var KS = window.KS, $ = KS.$, $$ = KS.$$, t = KS.t, el = KS.el;

  /* ---------- marketplace ---------- */
  var form = $("#shop-search");
  if (form) {
    var page = 1, loc = null;
    var render = function (items, append) {
      var box = $("#ss-results"); if (!append) box.textContent = "";
      if (!items.length && !append) box.appendChild(el("div", { class: "ks-empty", text: t("js.no_products") }));
      items.forEach(function (p) {
        box.appendChild(el("article", { class: "ks-card hover" },
          el("a", { href: "/shop/p/" + p.id, class: "no-underline text-inherit" },
            p.image ? el("img", { src: p.image, alt: p.name, class: "rounded-lg w-full h-40 object-cover mb-2", loading: "lazy" }) : null,
            el("strong", { text: p.name, "data-translate": "" }),
            el("div", { class: "ks-small ks-muted", text: p.farm + " · " + [p.city, p.state].filter(Boolean).join(", ") })),
          el("div", { class: "ks-row mt-1" }, el("span", { class: "ks-badge gold", text: p.price_fmt + " / " + p.unit }),
            p.rating ? el("span", { class: "ks-badge info", text: "★ " + p.rating + " (" + p.reviews + ")" }) : null,
            p.distance_km != null ? el("span", { class: "ks-badge", text: t("js.km", { n: p.distance_km }) }) : null),
          el("button", { class: "ks-btn ks-btn-sm mt-2", type: "button", "data-cart-add": "", "data-qty": p.min_qty,
            "data-payload": JSON.stringify({ id: p.id, name: p.name, shop_id: p.shop_id, branch_id: p.branch_id }) }, KS.icon("shopping-basket"), t("js.add_to_cart"))));
      });
      KS.icons();
      KS.translateDynamic(box);
    };
    var search = async function (append) {
      var q = new URLSearchParams();
      ["q", "category", "city", "sort"].forEach(function (k) { var v = form[k].value.trim(); if (v) q.set(k, v); });
      if (form.dataset.shop) q.set("shop", form.dataset.shop);
      if (loc) { q.set("lat", loc.lat); q.set("lon", loc.lon); }
      q.set("page", page);
      try { var r = await KS.api("/api/shop/products?" + q.toString()); render(r.items, append); $("#ss-more").classList.toggle("ks-hidden", !r.has_more); }
      catch (e) { KS.toast(e.message, "danger"); }
    };
    form.addEventListener("submit", function (e) { e.preventDefault(); page = 1; search(false); });
    $("#ss-more").addEventListener("click", function () { page += 1; search(true); });
    $("#ss-near").addEventListener("click", async function () { try { loc = await KS.position(); form.sort.value = "distance"; page = 1; search(false); } catch (e) {} });
    search(false);
  }

  /* ---------- ask AI about a product ---------- */
  var ask = $("#ask-form");
  if (ask) ask.addEventListener("submit", async function (e) {
    e.preventDefault();
    var out = $("#ask-out"); out.textContent = t("js.thinking");
    try {
      var r = await KS.api("/api/shop/products/" + ask.dataset.product + "/ask", { method: "POST", json: { question: $("#ask-q").value } });
      out.textContent = "";
      if (r.answer) out.appendChild(el("div", { class: "ks-alert ok" }, KS.icon("sparkles"), el("div", {}, el("div", { text: r.answer }), el("div", { class: "ks-small ks-muted", text: t("js.ai_confidence", { n: Math.round(r.confidence * 100) }) }))));
      else out.appendChild(el("div", { class: "ks-alert info" }, KS.icon("send"), el("div", {}, el("div", { text: r.message }),
        r.ai_guess ? el("div", { class: "ks-small ks-muted", text: t("js.ai_guess") + ": " + r.ai_guess }) : null)));
      KS.icons();
    } catch (err) { out.textContent = ""; KS.toast(err.message, "danger"); }
  });

  /* ---------- cart (grouped per pickup branch → one order each) ---------- */
  var groupsBox = $("#cart-groups");
  if (groupsBox) (async function () {
    var cart = KS.cartGet(), ids = Object.keys(cart);
    if (!ids.length) { groupsBox.appendChild(el("div", { class: "ks-empty", text: t("js.cart_empty") })); return; }
    var info;
    try { info = await KS.api("/api/shop/cart?ids=" + ids.join(",")); } catch (e) { KS.toast(e.message, "danger"); return; }
    var groups = {};
    info.items.forEach(function (p) { (groups[p.branch_id] = groups[p.branch_id] || { shop_id: p.shop_id, shop: p.shop, branch: p.branch, city: p.city, items: [] }).items.push(p); });
    Object.keys(groups).forEach(function (bid) {
      var g = groups[bid], idemKey = "ks_idem_" + bid;
      var list = el("ul", { class: "ks-list" });
      g.items.forEach(function (p) {
        var qty = el("input", { class: "ks-input w-28", type: "number", min: p.min_qty, step: "0.5", value: cart[p.id] ? cart[p.id].qty : p.min_qty, "aria-label": t("js.qty") });
        qty.addEventListener("change", function () { var c = KS.cartGet(); if (c[p.id]) { c[p.id].qty = Number(qty.value); KS.cartSet(c); } sessionStorage.removeItem(idemKey); });
        var rm = el("button", { class: "ks-icon-btn", type: "button", "aria-label": t("js.remove") }, KS.icon("trash-2"));
        rm.addEventListener("click", function () { var c = KS.cartGet(); delete c[p.id]; KS.cartSet(c); location.reload(); });
        list.appendChild(el("li", { class: "ks-between" }, el("span", {}, el("strong", { text: p.name }), el("span", { class: "ks-small ks-muted", text: " · " + p.price_fmt + " / " + p.unit + (p.active ? "" : " · " + t("js.unavailable")) })),
          el("span", { class: "flex gap-2 items-center" }, qty, rm)));
        qty.dataset.pid = p.id;
      });
      var delivery = el("input", { type: "radio", name: "ful-" + bid, value: "delivery", checked: true });
      var pickup = el("input", { type: "radio", name: "ful-" + bid, value: "pickup" });
      var addr = el("input", { class: "ks-input", maxlength: "300", placeholder: t("js.address") });
      var locBtn = el("button", { class: "ks-btn ks-btn-ghost ks-btn-sm", type: "button" }, KS.icon("locate-fixed"), t("js.use_location"));
      var locState = { lat: null, lon: null };
      locBtn.addEventListener("click", async function () { try { var gg = await KS.geo(true); locState = gg; locBtn.textContent = t("js.location_set"); } catch (e) {} });
      var farmSel = el("select", { class: "ks-select" });
      var tpl = $("#tpl-buyer-farms"); if (tpl) farmSel.appendChild(tpl.content.cloneNode(true));
      var place = el("button", { class: "ks-btn ks-btn-go", type: "button" }, KS.icon("sparkles"), t("js.place_order"));
      place.addEventListener("click", function () {
        if (groupsBox.dataset.loggedIn !== "1") { location.href = "/login?next=/cart"; return; }
        var key = sessionStorage.getItem(idemKey) || KS.uuid(); sessionStorage.setItem(idemKey, key);
        var items = $$("input[data-pid]", list).map(function (q) { return { product_id: q.dataset.pid, qty: Number(q.value) }; });
        var payload = { shop_id: g.shop_id, items: items, fulfilment: delivery.checked ? "delivery" : "pickup", idempotency_key: key, client_ts: new Date().toISOString() };
        if (delivery.checked) { payload.address = addr.value.trim(); payload.lat = locState.lat; payload.lon = locState.lon; }
        if (farmSel.value) payload.buyer_farm_id = farmSel.value;
        KS.action("order.place", payload, { onDone: function () { sessionStorage.removeItem(idemKey); } });
      });
      groupsBox.appendChild(el("section", { class: "ks-panel ornate" },
        el("h2", { class: "ks-title ks-h3", text: g.shop + " · " + g.branch + (g.city ? " (" + g.city + ")" : "") }), list,
        el("div", { class: "ks-row mt-3" }, el("label", { class: "ks-check" }, delivery, el("span", { text: t("js.delivery_cod") })), el("label", { class: "ks-check" }, pickup, el("span", { text: t("js.self_pickup") }))),
        el("div", { class: "ks-grid cols-2 mt-2" }, addr, locBtn),
        tpl && tpl.content.children.length > 1 ? el("div", { class: "ks-field mt-2" }, el("label", { class: "ks-label", text: t("js.buy_for_farm") }), farmSel) : null,
        el("p", { class: "ks-small ks-muted", text: t("js.courier_note") }), place));
    });
    KS.icons();
  })();
})();
