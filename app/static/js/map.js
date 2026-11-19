/* Leaflet location picker (OpenStreetMap tiles). */
(function () {
  "use strict";
  var KS = window.KS;
  function picker(node) {
    if (!window.L || node.dataset.ready) return null;
    node.dataset.ready = "1";
    var lat = KS.$(node.dataset.lat), lon = KS.$(node.dataset.lon);
    var start = [Number(node.dataset.startLat || 22.5), Number(node.dataset.startLon || 79)];
    var map = window.L.map(node, { scrollWheelZoom: false }).setView(start, node.dataset.startLat ? 11 : 5);
    window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap" }).addTo(map);
    var marker = null;
    function set(ll) {
      if (marker) marker.setLatLng(ll); else marker = window.L.marker(ll, { draggable: true }).addTo(map).on("dragend", function (e) { set(e.target.getLatLng()); });
      lat.value = ll.lat.toFixed(6); lon.value = ll.lng.toFixed(6);
      lat.dispatchEvent(new Event("change"));
    }
    map.on("click", function (e) { set(e.latlng); });
    node._ks = { map: map, set: set };
    setTimeout(function () { map.invalidateSize(); }, 300);
    return node._ks;
  }
  KS.$$("[data-map-picker]").forEach(picker);
  document.addEventListener("click", async function (e) {
    var b = e.target.closest("[data-map-locate]"); if (!b) return;
    var node = KS.$(b.dataset.mapLocate); var p = node && (node._ks || picker(node)); if (!p) return;
    try { var g = await KS.geo(true); p.map.setView([g.lat, g.lon], 15); p.set({ lat: g.lat, lng: g.lon }); } catch (err) {}
  });
  window.KSMap = { picker: picker };
})();
