/* Production-test charts (Chart.js 4, self-hosted). Data comes from a JSON <script> block; every label is set as text.
   Colours are CSS tokens (validated palette, light + dark); charts are rebuilt when the theme changes. */
(function () {
  "use strict";
  var KS = window.KS, node = document.getElementById("lt-data"), root = document.querySelector(".viz-root");
  if (!KS || !node || !root || !window.Chart) return;
  var t = KS.t, D = JSON.parse(node.textContent);
  var LOCALE = document.documentElement.lang || "en";
  var nf = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 1 });
  var nfc = new Intl.NumberFormat(LOCALE, { notation: "compact", maximumFractionDigits: 1 });
  var charts = [];

  function css(name) { return getComputedStyle(root).getPropertyValue(name).trim(); }
  function font() { return getComputedStyle(document.body).fontFamily || "system-ui, sans-serif"; }
  function alpha(hex, a) {
    var h = hex.replace("#", ""); if (h.length === 3) h = h.replace(/(.)/g, "$1$1");
    var n = parseInt(h, 16); return "rgba(" + (n >> 16 & 255) + "," + (n >> 8 & 255) + "," + (n & 255) + "," + a + ")";
  }
  function mmss(s) { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }
  // one style per axis: compact (10K) only when the axis itself reaches 10,000
  function tick(v, _i, ticks) {
    var top = Math.max.apply(null, (ticks || []).map(function (x) { return Math.abs(x.value); }).concat([Math.abs(v)]));
    return top >= 10000 ? nfc.format(v) : nf.format(v);
  }
  function narrow(el) { return el.parentNode.clientWidth < 520; }
  function shorten(s, n) { return s.length > n ? s.slice(0, n - 1) + "\u2026" : s; }
  function fmt(v, unit) { return v === null || v === undefined ? "—" : nf.format(v) + (unit ? " " + unit : ""); }
  function series(rows, key) { return rows.map(function (p) { return { x: p.t, y: p[key] === undefined ? null : p[key] }; }); }

  /* vertical hairline that snaps to the hovered time (line charts) */
  var crosshair = {
    id: "ksCrosshair",
    afterDatasetsDraw: function (chart) {
      var act = chart.tooltip && chart.tooltip.getActiveElements ? chart.tooltip.getActiveElements() : [];
      if (!act.length || chart.config.type !== "line") return;
      var x = Math.round(act[0].element.x) + 0.5, a = chart.chartArea, c = chart.ctx;
      c.save(); c.strokeStyle = css("--viz-axis"); c.lineWidth = 1;
      c.beginPath(); c.moveTo(x, a.top); c.lineTo(x, a.bottom); c.stroke(); c.restore();
    }
  };
  /* direct labels at line ends — text ink, never the series colour; a label that would collide is left to the legend */
  var endLabels = {
    id: "ksEndLabels",
    afterDatasetsDraw: function (chart, _args, opts) {
      if (!opts || !opts.enabled) return;
      var c = chart.ctx, placed = [];
      c.save(); c.font = "600 11px " + font(); c.fillStyle = css("--viz-text-2"); c.textBaseline = "middle";
      chart.data.datasets.forEach(function (ds, i) {
        var meta = chart.getDatasetMeta(i), pts = meta.data, last = null;
        if (meta.hidden) return;
        for (var k = pts.length - 1; k >= 0; k--) { var v = ds.data[k] && ds.data[k].y; if (v !== null && v !== undefined && pts[k]) { last = pts[k]; break; } }
        if (!last || placed.some(function (y) { return Math.abs(y - last.y) < 13; })) return;
        placed.push(last.y);
        c.fillText(ds.label, last.x + 6, last.y);
      });
      c.restore();
    }
  };
  /* value at the tip of each bar — outside the bar, so it is never clipped */
  var barValues = {
    id: "ksBarValues",
    afterDatasetsDraw: function (chart, _args, opts) {
      if (!opts || !opts.enabled) return;
      var c = chart.ctx; c.save(); c.font = "600 11px " + font(); c.fillStyle = css("--viz-text"); c.textBaseline = "middle";
      chart.getDatasetMeta(0).data.forEach(function (bar, i) {
        var v = chart.data.datasets[0].data[i]; if (v === null || v === undefined) return;
        c.fillText(nf.format(v) + " ms", bar.x + 6, bar.y);
      });
      c.restore();
    }
  };
  Chart.register(crosshair, endLabels, barValues);

  function lineChart(id, sets, o) {
    var el = document.getElementById(id); if (!el) return;
    var surface = css("--viz-surface");
    var cfg = {
      type: "line",
      data: { datasets: sets.map(function (s) {
        return { label: s.label, data: s.data, borderColor: s.color, backgroundColor: alpha(s.color, 0.1), fill: o.area ? "origin" : false,
                 borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, pointHitRadius: 12, pointHoverBorderWidth: 2,
                 pointHoverBorderColor: surface, pointHoverBackgroundColor: s.color, stepped: !!o.stepped, tension: 0, spanGaps: true,
                 borderCapStyle: "round", borderJoinStyle: "round", pointStyle: "line" };
      }) },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false, normalized: true,
        interaction: { mode: "index", intersect: false },
        layout: { padding: { right: o.endLabels ? 46 : 8, top: 4 } },
        plugins: {
          legend: { display: sets.length > 1, align: "start", labels: { usePointStyle: true, pointStyle: "line", boxWidth: 18, color: css("--viz-text-2"), font: { family: font(), size: 12 } } },
          tooltip: {
            backgroundColor: css("--viz-tip"), titleColor: css("--viz-text-2"), bodyColor: css("--viz-text"), borderColor: css("--viz-axis"), borderWidth: 1,
            padding: 10, usePointStyle: true, boxPadding: 4, titleFont: { family: font(), weight: "400" }, bodyFont: { family: font(), weight: "600" },
            callbacks: {
              title: function (items) { return items.length ? t("js.lt_at", { time: mmss(items[0].parsed.x) }) : ""; },
              label: function (c) { return " " + fmt(c.parsed.y, o.unit) + "  ·  " + c.dataset.label; }   // value leads, series follows
            }
          },
          ksEndLabels: { enabled: !!o.endLabels }, ksBarValues: { enabled: false }
        },
        scales: {
          x: { type: "linear", min: 0, max: Math.max(60, Math.ceil(D.duration_s / 60) * 60),   // whole minutes → ticks on 0:00, 1:00…
               ticks: { stepSize: narrow(el) ? 120 : 60, includeBounds: false, maxRotation: 0, callback: mmss, color: css("--viz-muted"),
                        font: { family: font(), size: 11 } },
               grid: { color: css("--viz-grid"), lineWidth: 1, drawTicks: false }, border: { color: css("--viz-axis") } },
          y: { type: o.log ? "logarithmic" : "linear", beginAtZero: !o.log, suggestedMax: o.max, min: o.log ? undefined : 0,
               ticks: { callback: tick, color: css("--viz-muted"), font: { family: font(), size: 11 }, maxTicksLimit: 6 },
               title: { display: !!o.yTitle, text: o.yTitle, color: css("--viz-muted"), font: { family: font(), size: 11 } },
               grid: { color: css("--viz-grid"), lineWidth: 1, drawTicks: false }, border: { color: css("--viz-axis") } }
        }
      }
    };
    charts.push(new Chart(el, cfg));
  }

  function slowestEndpoints() {
    var el = document.getElementById("c-slow"); if (!el) return;
    var rows = (D.endpoints || []).filter(function (e) { return e.requests >= 10 && e.p95 !== null && !/^(chaos|switch):/.test(e.name); })
      .sort(function (a, b) { return b.p95 - a.p95; }).slice(0, 12);
    var color = css("--viz-s1");
    charts.push(new Chart(el, {
      type: "bar",
      data: { labels: rows.map(function (e) { return e.method + " " + e.name; }),
              datasets: [{ label: "p95", data: rows.map(function (e) { return e.p95; }), backgroundColor: color, hoverBackgroundColor: alpha(color, 0.8),
                           borderRadius: 4, borderSkipped: "start", maxBarThickness: 24, categoryPercentage: 0.8, barPercentage: 0.9 }] },
      options: {
        indexAxis: "y", responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: "index", axis: "y", intersect: false },
        layout: { padding: { right: narrow(el) ? 58 : 70 } },
        plugins: {
          legend: { display: false }, ksBarValues: { enabled: true }, ksEndLabels: { enabled: false },
          tooltip: {
            backgroundColor: css("--viz-tip"), titleColor: css("--viz-text-2"), bodyColor: css("--viz-text"), borderColor: css("--viz-axis"), borderWidth: 1, padding: 10,
            bodyFont: { family: font(), weight: "600" }, titleFont: { family: font(), weight: "400" },
            callbacks: { label: function (c) { var e = rows[c.dataIndex];
              return [" p95 " + fmt(e.p95, "ms") + "  ·  p50 " + fmt(e.p50, "ms") + "  ·  p99 " + fmt(e.p99, "ms"),
                      " " + t("js.lt_requests", { n: nf.format(e.requests) }) + "  ·  " + t("js.lt_failures", { n: nf.format(e.failures) })]; } }
          }
        },
        scales: {
          x: { beginAtZero: true, ticks: { callback: tick, maxTicksLimit: narrow(el) ? 4 : 9, maxRotation: 0, color: css("--viz-muted"), font: { family: font(), size: 11 } },
               title: { display: true, text: "ms", color: css("--viz-muted") }, grid: { color: css("--viz-grid"), drawTicks: false }, border: { color: css("--viz-axis") } },
          y: { ticks: { color: css("--viz-text-2"), font: { family: font(), size: 11 }, autoSkip: false,
                        callback: function (v) { var l = this.getLabelForValue(v); return narrow(el) ? shorten(l, 22) : l; } },
               grid: { display: false }, border: { color: css("--viz-axis") } }
        }
      }
    }));
  }

  function build() {
    charts.forEach(function (c) { c.destroy(); }); charts = [];
    Chart.defaults.font.family = font();
    var tl = D.timeline || [], sv = D.server || [];
    var s1 = css("--viz-s1"), s2 = css("--viz-s2"), s3 = css("--viz-s3"), crit = css("--viz-crit");
    lineChart("c-users", [{ label: t("js.lt_users"), data: series(tl, "users"), color: s1 }], { area: true, stepped: true });
    lineChart("c-rps", [{ label: t("js.lt_rps"), data: series(tl, "rps"), color: s1 }], { area: true, unit: t("js.lt_unit_rps") });
    lineChart("c-fail", [{ label: t("js.lt_fail"), data: series(tl, "fail_s"), color: crit }], { area: true, max: 1, unit: t("js.lt_unit_rps") });
    lineChart("c-lat", [{ label: "p50", data: series(tl, "p50"), color: s1 }, { label: "p95", data: series(tl, "p95"), color: s2 },
                        { label: "p99", data: series(tl, "p99"), color: s3 }], { log: true, endLabels: true, unit: "ms", yTitle: t("js.lt_ms_log") });
    lineChart("c-cpu", [{ label: "CPU", data: series(sv, "cpu"), color: s1 }], { area: true, unit: "%", max: 100 });
    lineChart("c-mem", [{ label: t("js.lt_memory"), data: series(sv, "rss_mb"), color: s1 }], { area: true, unit: "MB" });
    lineChart("c-lag", [{ label: t("js.lt_lag"), data: series(sv, "loop_lag_max_ms"), color: s1 }], { area: true, unit: "ms" });
    lineChart("c-press", [{ label: t("js.lt_db"), data: series(sv, "db_out"), color: s1 }, { label: t("js.lt_ai"), data: series(sv, "ai_queued"), color: s2 }],
              { max: 5 });   // long series names: the legend carries identity (end labels would not fit)
    slowestEndpoints();
  }

  build();
  new MutationObserver(function (m) { if (m.some(function (x) { return x.attributeName === "class"; })) build(); })
    .observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
})();
