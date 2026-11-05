// PMS Annaba – interactions côté navigateur (aucune dépendance réseau).
(function () {
  "use strict";

  var csrfToken = function () {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  };

  var formatDA = function (value) {
    return Math.round(value).toLocaleString("fr-FR").replace(/ /g, " ") + " DA";
  };

  // ------------------------------------------------------------ internationalisation
  // Libellés traduits {texte source français: traduction} fournis par layout/base.html dans un bloc JSON
  // (la CSP n'autorise aucun script en ligne). Repli : le texte source français.
  var readI18n = function () {
    var node = document.getElementById("i18n-data");
    if (!node) return {};
    try { return JSON.parse(node.textContent || "{}") || {}; } catch (e) { return {}; }
  };
  window.I18N = Object.assign({}, readI18n(), window.I18N || {});
  var t = function (text) {
    var value = window.I18N[text];
    return typeof value === "string" && value ? value : text;
  };

  // ------------------------------------------------------------ formulaires
  // Confirmation (data-confirm, traduit via window.I18N) et protection contre la double soumission.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute("data-confirm");
    if (message !== null && !window.confirm(message ? t(message) : t("Confirmer cette action ?"))) {
      event.preventDefault();
      return;
    }
    if (form.dataset.submitted === "1") {
      event.preventDefault();
      return;
    }
    form.dataset.submitted = "1";
    window.setTimeout(function () { delete form.dataset.submitted; }, 5000);
  }, true);

  // ------------------------------------------------------------ douchette / scanner USB
  // Un lecteur de codes-barres / QR se comporte comme un clavier très rapide terminé par « Entrée ».
  // Hors d'un champ de saisie, la séquence est envoyée à la recherche universelle.
  var buffer = "";
  var lastKey = 0;
  var MAX_GAP_MS = 45;

  var isTyping = function (target) {
    if (!target) return false;
    var tag = target.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
  };

  document.addEventListener("keydown", function (event) {
    if (event.key === "F2") {
      var search = document.getElementById("global-search");
      if (search) { event.preventDefault(); search.focus(); search.select(); }
      return;
    }
    if (isTyping(event.target)) { buffer = ""; return; }
    var now = performance.now();
    if (now - lastKey > MAX_GAP_MS) buffer = "";
    lastKey = now;
    if (event.key === "Enter") {
      if (buffer.length >= 4) {
        event.preventDefault();
        window.location.href = "/search?q=" + encodeURIComponent(buffer);
      }
      buffer = "";
      return;
    }
    if (event.key.length === 1) buffer += event.key;
  });

  // ------------------------------------------------------------ graphiques (Chart.js local)
  var palette = ["#0d9488", "#38bdf8", "#f59e0b", "#6366f1", "#f43f5e", "#84cc16"];
  var builders = {
    revenue: function (d) {
      var datasets = [
        { label: t("Hébergement"), data: d.room, backgroundColor: "#0d9488", stack: "ca", borderRadius: 4 },
        { label: t("Services & POS"), data: d.services, backgroundColor: "#38bdf8", stack: "ca", borderRadius: 4 }
      ];
      if (d.tax) datasets.push({ label: t("Taxe de séjour"), data: d.tax, backgroundColor: "#f59e0b", stack: "ca", borderRadius: 4 });
      return {
        type: "bar",
        data: { labels: d.labels, datasets: datasets },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { position: "bottom" }, tooltip: { callbacks: { label: function (c) { return c.dataset.label + " : " + formatDA(c.parsed.y); } } } },
          scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, ticks: { callback: function (v) { return formatDA(v); } } } }
        }
      };
    },
    occupancy: function (d) {
      return {
        type: "line",
        data: {
          labels: d.labels,
          datasets: [
            { label: t("Taux d'occupation (%)"), data: d.occupancy, borderColor: "#0d9488", backgroundColor: "rgba(13,148,136,.15)", fill: true, tension: 0.3, yAxisID: "y" },
            { label: t("ADR (DA)"), data: d.adr, borderColor: "#6366f1", tension: 0.3, yAxisID: "y1" },
            { label: t("RevPAR (DA)"), data: d.revpar, borderColor: "#f59e0b", tension: 0.3, yAxisID: "y1" }
          ]
        },
        options: {
          responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
          plugins: { legend: { position: "bottom" } },
          scales: {
            y: { min: 0, max: 100, title: { display: true, text: t("TO %") } },
            y1: { position: "right", grid: { drawOnChartArea: false }, ticks: { callback: function (v) { return formatDA(v); } } }
          }
        }
      };
    },
    payments: function (d) {
      return {
        type: "doughnut",
        data: { labels: d.labels, datasets: [{ data: d.values, backgroundColor: palette, borderWidth: 0 }] },
        options: { responsive: true, maintainAspectRatio: false, cutout: "62%", plugins: { legend: { position: "bottom" } } }
      };
    }
  };

  var charts = [];
  var renderCharts = function () {
    if (!window.Chart) return;
    var dark = document.documentElement.classList.contains("dark");
    window.Chart.defaults.color = dark ? "#cbd5e1" : "#475569";
    window.Chart.defaults.borderColor = dark ? "rgba(148,163,184,.15)" : "rgba(148,163,184,.3)";
    charts.forEach(function (chart) { chart.destroy(); });
    charts = [];
    document.querySelectorAll("canvas[data-chart]").forEach(function (canvas) {
      var source = document.getElementById(canvas.getAttribute("data-source"));
      var builder = builders[canvas.getAttribute("data-chart")];
      if (!source || !builder) return;
      var data = JSON.parse(source.textContent);
      var key = canvas.getAttribute("data-key");
      charts.push(new window.Chart(canvas, builder(key ? data[key] : data)));
    });
  };
  document.addEventListener("DOMContentLoaded", renderCharts);
  document.addEventListener("theme-changed", renderCharts);

  // ------------------------------------------------------------ impression directe
  document.addEventListener("DOMContentLoaded", function () {
    var frame = document.getElementById("print-frame");
    if (!frame) return;
    frame.addEventListener("load", function () {
      window.setTimeout(function () {
        try { frame.contentWindow.focus(); frame.contentWindow.print(); } catch (e) { /* visionneuse PDF externe */ }
      }, 500);
    });
    var again = document.getElementById("print-again");
    if (again) again.addEventListener("click", function () {
      try { frame.contentWindow.focus(); frame.contentWindow.print(); } catch (e) { window.open(frame.src, "_blank"); }
    });
  });

  // ------------------------------------------------------------ composants Alpine.js
  document.addEventListener("alpine:init", function () {
    var Alpine = window.Alpine;

    Alpine.data("layout", function () {
      return {
        sidebar: false,
        dark: document.documentElement.classList.contains("dark"),
        toggleTheme: function () {
          this.dark = !this.dark;
          document.documentElement.classList.toggle("dark", this.dark);
          try { localStorage.setItem("theme", this.dark ? "dark" : "light"); } catch (e) { /* ignoré */ }
          document.dispatchEvent(new CustomEvent("theme-changed"));
        }
      };
    });

    Alpine.data("guestPicker", function (initial) {
      return {
        q: "", results: [], selected: initial || null, loading: false,
        search: async function () {
          if (this.q.trim().length < 2) { this.results = []; return; }
          this.loading = true;
          try {
            var response = await fetch("/api/search?q=" + encodeURIComponent(this.q), { headers: { Accept: "application/json" } });
            var data = await response.json();
            this.results = data.guests || [];
          } catch (e) {
            this.results = [];
          } finally {
            this.loading = false;
          }
        },
        choose: function (guest) { this.selected = guest; this.results = []; this.q = ""; },
        clear: function () { this.selected = null; }
      };
    });

    Alpine.data("identityCheck", function (excludeId) {
      return {
        duplicate: null,
        check: async function (idType, idNumber) {
          if (!idNumber || idNumber.trim().length < 4) { this.duplicate = null; return; }
          var url = "/api/guests/check-identity?id_type=" + encodeURIComponent(idType) +
            "&id_number=" + encodeURIComponent(idNumber) + (excludeId ? "&exclude=" + excludeId : "");
          try {
            var response = await fetch(url, { headers: { Accept: "application/json", "X-CSRF-Token": csrfToken() } });
            var data = await response.json();
            this.duplicate = data.duplicate;
          } catch (e) {
            this.duplicate = null;
          }
        }
      };
    });

    Alpine.data("posMenu", function () {
      return { category: "all", filter: "" };
    });
  });
})();
