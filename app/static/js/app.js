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
  // Couleurs de la charte lues dans les variables CSS (feuille dynamique theme-custom.css) à chaque rendu :
  // primaire et secondaire de l'établissement, repli sur la charte d'origine si la variable est absente.
  var cssVar = function (name, fallback) {
    var value = window.getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  };
  var rgba = function (hex, alpha) {
    var m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
    if (!m) return hex;
    return "rgba(" + parseInt(m[1], 16) + "," + parseInt(m[2], 16) + "," + parseInt(m[3], 16) + "," + alpha + ")";
  };
  var chartColors = function () {
    var primary = cssVar("--color-primary", "#0d9488"), secondary = cssVar("--color-secondary", "#0284c7");
    return { primary: primary, secondary: secondary,
             palette: [primary, secondary, "#f59e0b", "#6366f1", "#f43f5e", "#84cc16"] };
  };
  var builders = {
    revenue: function (d) {
      var c = chartColors();
      var datasets = [
        { label: t("Hébergement"), data: d.room, backgroundColor: c.primary, stack: "ca", borderRadius: 4 },
        { label: t("Services & POS"), data: d.services, backgroundColor: c.secondary, stack: "ca", borderRadius: 4 }
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
      var c = chartColors();
      return {
        type: "line",
        data: {
          labels: d.labels,
          datasets: [
            { label: t("Taux d'occupation (%)"), data: d.occupancy, borderColor: c.primary, backgroundColor: rgba(c.primary, 0.15), fill: true, tension: 0.3, yAxisID: "y" },
            { label: t("ADR (DA)"), data: d.adr, borderColor: c.secondary, tension: 0.3, yAxisID: "y1" },
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
        data: { labels: d.labels, datasets: [{ data: d.values, backgroundColor: chartColors().palette, borderWidth: 0 }] },
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

  // ------------------------------------------------------------ couleurs de la charte (aperçu du thème)
  // Portage fidèle de theme_manager.py : mix(), palette(), relative_luminance(), contrast_text().
  var themeColor = (function () {
    var HEX = /^#[0-9a-f]{6}$/;
    // Arrondi « au pair le plus proche » comme round() de Python : mêmes nuances au bit près.
    var roundHalfEven = function (x) {
      var floor = Math.floor(x), diff = x - floor;
      if (diff > 0.5) return floor + 1;
      if (diff < 0.5) return floor;
      return floor % 2 === 0 ? floor : floor + 1;
    };
    var normalize = function (value) {
      var text = String(value || "").trim().toLowerCase();
      return HEX.test(text) ? text : "";
    };
    var rgb = function (hex) {
      return [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)];
    };
    var toHex = function (channels) {
      return "#" + channels.map(function (c) {
        var v = Math.max(0, Math.min(255, roundHalfEven(c)));
        return (v < 16 ? "0" : "") + v.toString(16);
      }).join("");
    };
    var mix = function (color, other, weight) {
      var a = rgb(normalize(color) || "#000000"), b = rgb(normalize(other) || "#000000");
      return toHex(a.map(function (ca, i) { return ca + (b[i] - ca) * weight; }));
    };
    var palette = function (value, tokens) {
      var base = normalize(value) || "#0d9488";
      var known = (tokens.known_palettes || {})[base];
      var result = {};
      if (known) {
        Object.keys(known).forEach(function (shade) { result[shade] = known[shade]; });
        return result;
      }
      var mixes = tokens.shade_mix || {};
      Object.keys(mixes).forEach(function (shade) {
        var towards = mixes[shade][0], weight = mixes[shade][1];
        result[shade] = mix(base, towards === "white" ? "#ffffff" : "#000000", weight);
      });
      return result;
    };
    var luminance = function (hex) {
      var channel = function (c) {
        var s = c / 255;
        return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
      };
      var c = rgb(hex);
      return 0.2126 * channel(c[0]) + 0.7152 * channel(c[1]) + 0.0722 * channel(c[2]);
    };
    var contrast = function (first, second) {
      var la = luminance(first), lb = luminance(second);
      return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
    };
    var contrastText = function (value, tokens) {
      var light = tokens.light_text || "#ffffff", dark = tokens.dark_text || "#0f172a";
      var hex = normalize(value) || "#000000";
      var white = contrast(hex, light);
      if (white >= (tokens.min_white_contrast || 3)) return light;
      return white >= contrast(hex, dark) ? light : dark;
    };
    var triplet = function (hex) { return rgb(hex).join(" "); };
    // Portage de readable_on() : assombrit (fond clair) ou éclaircit (fond foncé) jusqu'au contraste visé.
    var readableOn = function (color, background, minimum) {
      var base = normalize(color) || "#000000", bg = normalize(background) || "#ffffff";
      var target = luminance(bg) >= 0.18 ? "#000000" : "#ffffff";
      var candidate = base;
      for (var step = 1; step <= 20; step++) {
        if (contrast(candidate, bg) >= minimum) return candidate;
        candidate = mix(base, target, step / 20);
      }
      return candidate;
    };
    return { normalize: normalize, mix: mix, palette: palette, luminance: luminance, contrast: contrast,
             contrastText: contrastText, triplet: triplet, readableOn: readableOn };
  })();
  window.themeColor = themeColor;

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

    // Aperçu en direct de la charte (Administration > Thème et marque) : applique les valeurs saisies à un
    // panneau d'aperçu avant tout enregistrement. Les nuances 50…950 et le choix du texte lisible
    // reproduisent exactement palette() / contrast_text() de app/services/theme_manager.py, à partir des
    // jetons fournis par le serveur (bloc JSON #theme-editor-data, la CSP interdisant les scripts en ligne).
    Alpine.data("themeEditor", function () {
      var node = document.getElementById("theme-editor-data");
      var data = {};
      try { data = JSON.parse((node && node.textContent) || "{}") || {}; } catch (e) { data = {}; }
      var tokens = data.tokens || {};
      var fields = data.fields || [];
      var colors = {}, defaults = {}, saved = {}, legal = {};
      fields.forEach(function (f) { colors[f.name] = f.value; saved[f.name] = f.value; defaults[f.name] = f["default"]; });
      (data.legal || []).forEach(function (f) { legal[f.name] = f.value || ""; });
      return {
        colors: colors, defaults: defaults, saved: saved, legal: legal,
        legalFields: data.legal || [],
        font: data.font || "inter",
        density: data.density || "comfortable",
        current: data.current || {},
        pending: { logo: "", "logo-dark": "", favicon: "" },
        pendingTypes: { logo: "", "logo-dark": "", favicon: "" },
        errors: { logo: "", "logo-dark": "", favicon: "" },
        identity: {
          hotel_name: data.hotel_name || "", hotel_name_ar: data.hotel_name_ar || "",
          slogan: data.slogan || "", slogan_ar: data.slogan_ar || ""
        },
        docs: { header: data.document_header || "", footer: data.document_footer || "", logo: !!data.doc_logo },
        pvDark: false,
        pvRtl: !!data.rtl,
        pvTab: "ui",

        init: function () {
          var self = this;
          window.addEventListener("beforeunload", function () {
            Object.keys(self.pending).forEach(function (kind) { if (self.pending[kind]) URL.revokeObjectURL(self.pending[kind]); });
          });
        },

        // ---------------------------------------------------------------- couleurs
        setHex: function (name, value) {
          var hex = themeColor.normalize(value);
          if (hex) this.colors[name] = hex;
        },
        revertColors: function () {
          var self = this;
          Object.keys(this.saved).forEach(function (name) { self.colors[name] = self.saved[name]; });
          document.querySelectorAll("[data-hex-for]").forEach(function (input) {
            input.value = self.colors[input.getAttribute("data-hex-for")];
          });
        },
        get colorsChanged() {
          var self = this;
          return Object.keys(this.saved).some(function (name) { return self.colors[name] !== self.saved[name]; });
        },
        get cardTooDark() {
          var card = themeColor.normalize(this.colors.card_color);
          return !!card && themeColor.luminance(card) < (tokens.min_card_luminance || 0.6);
        },
        get shades() {
          return {
            brand: themeColor.palette(this.colors.primary_color, tokens),
            accent: themeColor.palette(this.colors.secondary_color, tokens)
          };
        },
        get primaryTooLight() {
          return themeColor.contrast(this.shades.brand[600], tokens.light_text || "#ffffff") < (tokens.min_white_contrast || 3);
        },
        get primaryContrast() {
          var base = this.shades.brand[600];
          var text = themeColor.contrastText(base, tokens);
          return themeColor.contrast(base, text).toFixed(1).replace(".", ",") + ":1";
        },

        // ---------------------------------------------------------------- variables CSS de l'aperçu
        get previewStyle() {
          var t = tokens, c = this.colors, vars = [];
          var shades = this.shades;
          var primary = shades.brand[600];
          ["brand", "accent"].forEach(function (name) {
            Object.keys(shades[name]).forEach(function (shade) {
              vars.push("--" + name + "-" + shade + ": " + themeColor.triplet(shades[name][shade]));
            });
          });
          var dark = this.pvDark;
          var card = dark ? themeColor.mix(t.dark_card || "#0f172a", primary, 0.05) : (themeColor.normalize(c.card_color) || "#ffffff");
          var surface = dark ? themeColor.mix(t.dark_surface || "#020617", primary, 0.03) : (t.light_surface || "#f1f5f9");
          vars.push("--color-primary: " + primary);
          vars.push("--color-on-primary: " + themeColor.contrastText(primary, t));
          vars.push("--color-secondary: " + shades.accent[600]);
          vars.push("--color-on-secondary: " + themeColor.contrastText(shades.accent[600], t));
          vars.push("--color-card: " + card);
          vars.push("--color-on-card: " + themeColor.contrastText(card, t));
          vars.push("--color-surface: " + surface);
          var minText = t.min_text_contrast || 4.5;
          var lightBg = [card, t.light_surface || "#f1f5f9", "#ffffff"].reduce(function (low, value) {
            return themeColor.luminance(value) < themeColor.luminance(low) ? value : low;
          });
          vars.push("--pv-link: " + (dark ? themeColor.readableOn(shades.brand[400], card, minText)
                                          : themeColor.readableOn(shades.brand[700], lightBg, minText)));
          ["available", "occupied", "dirty", "maintenance"].forEach(function (status) {
            var value = themeColor.normalize(c["status_" + status + "_color"]) || "#64748b";
            vars.push("--status-" + status + ": " + value);
            vars.push("--status-" + status + "-on: " + themeColor.contrastText(value, t));
          });
          var font = (t.fonts || {})[this.font] || {};
          vars.push("--font-family: " + (this.pvRtl ? font.rtl : font.stack));
          var density = (t.densities || {})[this.density] || {};
          Object.keys(density).forEach(function (key) { vars.push("--density-" + key + ": " + density[key]); });
          return vars.join("; ");
        },

        // ---------------------------------------------------------------- textes et logos
        sample: function (key) {
          var samples = data.samples || {};
          var set = (this.pvRtl ? samples.rtl : samples.ltr) || {};
          return set[key] || "";
        },
        get displayName() {
          return (this.pvRtl && this.identity.hotel_name_ar) ? this.identity.hotel_name_ar : this.identity.hotel_name;
        },
        get displayTagline() {
          return (this.pvRtl && this.identity.slogan_ar) ? this.identity.slogan_ar : this.identity.slogan;
        },
        // Sens d'écriture : « rtl » pour la variante arabe, « auto » pour le texte de repli (comme le layout).
        get nameDir() { return (this.pvRtl && this.identity.hotel_name_ar) ? "rtl" : "auto"; },
        get taglineDir() { return (this.pvRtl && this.identity.slogan_ar) ? "rtl" : "auto"; },
        get legalLines() {
          var self = this;
          return this.legalFields.map(function (f) {
            var value = (self.legal[f.name] || "").trim();
            return value ? f.short + " : " + value : "";
          }).filter(function (line) { return line; });
        },
        // Fichier choisi (aperçu local) sinon fichier enregistré ; exact = sans repli sur le logo principal.
        logoOf: function (kind, exact) {
          var own = this.pending[kind] || this.current[kind] || "";
          if (own || exact || kind !== "logo-dark") return own;
          return this.pending.logo || this.current.logo || "";
        },
        get sidebarLogo() { return this.logoOf("logo-dark") || "/static/img/favicon.svg"; },
        // Logo des documents : comme les PDF, uniquement un logo imprimable (un SVG sans rendu PNG n'y figure pas).
        get docLogo() { return this.docLogoUnprintable ? "" : this.logoOf("logo"); },
        get pendingSvgLogo() { return this.pendingTypes.logo === "image/svg+xml"; },
        // Fichier choisi en SVG, ou logo enregistré non imprimable (data.logo_printable fourni par le serveur).
        get docLogoUnprintable() {
          if (this.pending.logo) return this.pendingSvgLogo;
          return !!this.current.logo && !data.logo_printable;
        },
        pick: function (kind, event) {
          var input = event.target;
          var file = input.files && input.files[0];
          var messages = data.messages || {};
          if (this.pending[kind]) URL.revokeObjectURL(this.pending[kind]);
          this.pending[kind] = "";
          this.pendingTypes[kind] = "";
          this.errors[kind] = "";
          if (!file) return;
          var allowed = kind === "favicon" ? ["image/png", "image/webp", "image/jpeg"]
            : ["image/png", "image/webp", "image/jpeg", "image/svg+xml"];
          if (allowed.indexOf(file.type) === -1) {
            this.errors[kind] = kind === "favicon" ? messages.favicon_type : messages.bad_type;
            return;
          }
          if (file.size > (tokens.max_upload_bytes || 2097152)) {
            this.errors[kind] = messages.too_big;
            return;
          }
          this.pending[kind] = URL.createObjectURL(file);
          this.pendingTypes[kind] = file.type;
        },
        clearPick: function (kind, event) {
          var form = event.target.closest("form");
          var input = form ? form.querySelector('input[type="file"]') : null;
          if (input) input.value = "";
          if (this.pending[kind]) URL.revokeObjectURL(this.pending[kind]);
          this.pending[kind] = "";
          this.pendingTypes[kind] = "";
          this.errors[kind] = "";
        }
      };
    });
  });
})();

// ------------------------------------------------------------ gestion des tarifs (Administration > Gestion des tarifs)
// Bloc indépendant : composant Alpine « pricingRow », recalcul en direct du prix final TTC d'une chambre quand le
// prix spécifique est modifié. Reproduit exactement app/services/pricing.py (fractions exactes, arrondi au dinar
// 0,5 → supérieur) et split_vat() de app/services/common.py (HT arrondi au plus proche, demi → pair, comme round()
// en Python). Données lues dans le bloc JSON #pricing-data (la CSP interdit tout script en ligne).
(function () {
  "use strict";

  var readPricing = function () {
    var node = document.getElementById("pricing-data");
    if (!node) return { rooms: {}, vat_rate: 0, symbol: "DA" };
    try { return JSON.parse(node.textContent || "{}") || { rooms: {} }; } catch (e) { return { rooms: {} }; }
  };

  // Prix TTC arrondi : référence × Π (100 + p) / 100, en entiers exacts (BigInt), demi-dinar au supérieur.
  var finalPrice = function (reference, percents) {
    var num = BigInt(Math.max(0, Math.round(reference)));
    var den = BigInt(1);
    (percents || []).forEach(function (p) {
      num = num * BigInt(100 + p);
      den = den * BigInt(100);
    });
    if (num <= BigInt(0)) return 0;
    return Number((BigInt(2) * num + den) / (BigInt(2) * den));
  };

  var roundHalfEven = function (x) {
    var f = Math.floor(x);
    var diff = x - f;
    if (diff > 0.5) return f + 1;
    if (diff < 0.5) return f;
    return f % 2 === 0 ? f : f + 1;
  };

  var splitVat = function (ttc, rate) {
    if (!rate) return [ttc, 0];
    var ht = roundHalfEven(ttc / (1 + rate / 100));
    return [ht, ttc - ht];
  };

  var formatMoney = function (value, symbol) {
    var digits = String(Math.round(value));
    var negative = digits.charAt(0) === "-";
    if (negative) digits = digits.slice(1);
    var grouped = digits.replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    return (negative ? "-" : "") + grouped + " " + (symbol || "DA");
  };

  window.pricingEngine = { finalPrice: finalPrice, splitVat: splitVat, formatMoney: formatMoney };

  document.addEventListener("alpine:init", function () {
    var Alpine = window.Alpine;
    var data = null;

    Alpine.data("pricingRow", function (roomId, initial) {
      if (data === null) data = readPricing();
      var info = (data.rooms || {})[roomId] || { type_price: 0, percents: [] };
      return {
        override: initial === undefined || initial === null ? "" : String(initial),
        valid: function () {
          var value = String(this.override === null ? "" : this.override).trim();
          return value === "" || (/^\d+$/.test(value) && Number(value) <= 10000000);
        },
        reference: function () {
          var value = String(this.override === null ? "" : this.override).trim();
          return value === "" ? info.type_price : parseInt(value, 10);
        },
        ttc: function () { return this.valid() ? finalPrice(this.reference(), info.percents) : null; },
        ht: function () { var ttc = this.ttc(); return ttc === null ? null : splitVat(ttc, data.vat_rate)[0]; },
        vat: function () { var ttc = this.ttc(); return ttc === null ? null : splitVat(ttc, data.vat_rate)[1]; },
        money: function (value) { return value === null ? "—" : formatMoney(value, data.symbol); }
      };
    });
  });
})();

// ============================================================ numérisation des pièces d'identité
// Bloc autonome (composant Alpine « idScanner », partiel templates/pms/_id_scan.html) : douchette MRZ avec
// détection globale de la saisie très rapide, téléversement PNG / JPG / PDF, photo webcam, vérification puis
// création ou mise à jour de la fiche client. Configuration et libellés traduits : bloc JSON #id-scan-config.
(function () {
  "use strict";

  var GAP_MS = 60;          // intervalle maximal entre deux touches d'une douchette (un humain tape bien plus lentement)
  var IDLE_MS = 220;        // fin de lecture : aucune touche pendant ce délai
  var MIN_MRZ_CHARS = 56;   // 2 × 36 (TD2) moins une marge de lecture

  var csrf = function () {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  };
  var readConfig = function () {
    var node = document.getElementById("id-scan-config");
    if (!node) return {};
    try { return JSON.parse(node.textContent || "{}") || {}; } catch (e) { return {}; }
  };
  var cleanMrz = function (text) {
    return String(text || "").toUpperCase().replace(/[«‹〈＜]/g, "<").replace(/[^A-Z0-9<\n]/g, "");
  };
  // Motif MRZ : au moins une ligne longue faite de A-Z, 0-9 et « < », avec du remplissage « << ».
  var looksLikeMrz = function (text) {
    var flat = cleanMrz(text).replace(/\n/g, "");
    return flat.length >= MIN_MRZ_CHARS && flat.indexOf("<<") !== -1 && /^[PIACVD]/.test(flat);
  };
  var isField = function (el) {
    return !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
  };
  var BLANK = { last_name: "", first_name: "", last_name_ar: "", first_name_ar: "", birth_date: "", birth_place: "",
                gender: "", nationality: "", id_type: "cni", id_number: "", id_expiry_date: "", id_issue_date: "",
                id_issue_place: "" };
  var FORMAT_LABELS = { td1: "MRZ TD1 (3 × 30)", td2: "MRZ TD2 (2 × 36)", td3: "MRZ TD3 (2 × 44)",
                        aamva: "PDF417 AAMVA", json: "QR / JSON", cles: "QR / PDF417", texte: "OCR" };

  function emptySlot() { return { file: null, source: "image_upload", name: "", pdf: false, url: "" }; }
  var CONFLICT_KEYS = ["id_number", "birth_date", "id_expiry_date", "gender", "last_name", "first_name"];

  document.addEventListener("alpine:init", function () {
    window.Alpine.data("idScanner", function () {
      var cfg = readConfig();
      var msg = cfg.messages || {};
      return {
        cfg: cfg, isOpen: false, tab: "mrz", step: "capture", busy: false, error: "", info: "",
        mrzText: "", side: "recto", slots: { recto: emptySlot(), verso: emptySlot() },
        camera: { active: false, stream: null }, fieldRank: {},
        status: { loaded: false, tesseract: true, pdf: true },
        result: null, fields: Object.assign({}, BLANK, cfg.guest_fields || {}), warnings: [], notes: [], scanIds: [], files: [], rawText: "",
        applied: null, burst: null,

        init: function () {
          var self = this;
          // Écouteur global (phase de capture) : une rafale de touches très rapides qui forme une MRZ est
          // interceptée, même hors du champ de capture et même fenêtre fermée ; la touche Entrée envoyée par la
          // douchette ne soumet alors aucun formulaire et ne déclenche pas la recherche universelle.
          document.addEventListener("keydown", function (event) { self.onKey(event); }, true);
          window.addEventListener("beforeunload", function () { self.stopCamera(); self.clearPreview(); });
        },

        // ---------------------------------------------------------------- détection de la douchette
        onKey: function (event) {
          if (event.ctrlKey || event.altKey || event.metaKey) return;
          var now = performance.now();
          var b = this.burst;
          if (!b || now - b.last > GAP_MS) {
            b = this.burst = { text: "", last: now, keys: 0, target: event.target,
                               saved: isField(event.target) ? event.target.value : null, timer: null };
          }
          b.last = now;
          if (event.key === "Enter") {
            var line = b.text.split("\n").pop();
            if (b.keys >= 20 && cleanMrz(line).length >= 28 && line.indexOf("<") !== -1) {
              event.preventDefault();
              event.stopImmediatePropagation();
              b.text += "\n";
            }
          } else if (event.key && event.key.length === 1) {
            b.text += event.key;
            b.keys += 1;
          } else {
            return;
          }
          var self = this;
          window.clearTimeout(b.timer);
          b.timer = window.setTimeout(function () { self.finishBurst(b); }, IDLE_MS);
        },
        finishBurst: function (b) {
          if (this.burst === b) this.burst = null;
          if (b.keys < 40 || !looksLikeMrz(b.text)) return;
          // Saisie tombée dans un autre champ de la page : on lui rend sa valeur d'origine.
          if (isField(b.target) && b.target !== this.$refs.capture && b.saved !== null) {
            b.target.value = b.saved;
            b.target.dispatchEvent(new Event("input", { bubbles: true }));
          }
          var text = cleanMrz(b.text).replace(/\n+$/, "");
          if (!this.isOpen) this.open("mrz");
          this.tab = "mrz";
          this.step = "capture";
          this.mrzText = text;
          this.info = msg.detected || "";
          this.analyseMrz(text);
        },

        // ---------------------------------------------------------------- fenêtre
        open: function (tab) {
          this.reset();
          this.isOpen = true;
          this.switchTab(tab || "mrz");
          this.loadStatus();
        },
        close: function () {
          this.stopCamera();
          this.isOpen = false;
        },
        finish: function () {
          var reload = this.applied && this.cfg.context !== "new";
          this.close();
          if (reload) window.location.reload();
        },
        reset: function () {
          this.stopCamera();
          this.clearPreview();
          this.tab = "mrz"; this.step = "capture"; this.busy = false; this.error = ""; this.info = "";
          this.mrzText = ""; this.side = "recto"; this.result = null;
          this.fields = Object.assign({}, BLANK, this.cfg.guest_fields || {});
          this.warnings = []; this.notes = []; this.scanIds = []; this.files = []; this.applied = null; this.rawText = "";
          this.fieldRank = {};
        },
        restart: function () { this.reset(); this.focusCapture(); },
        switchTab: function (tab) {
          this.tab = tab;
          this.error = "";
          if (tab !== "photo") this.stopCamera();
          if (tab === "mrz") this.focusCapture();
        },
        focusCapture: function () {
          var self = this;
          this.$nextTick(function () { if (self.$refs.capture) self.$refs.capture.focus(); });
        },
        manual: function () {
          this.stopCamera();
          this.error = "";
          this.result = null;
          this.step = "verify";
        },
        addOtherSide: function () {
          this.side = this.side === "recto" ? "verso" : "recto";
          this.step = "capture";
          this.info = "";
          this.error = "";
          this.clearPreview();
          this.switchTab(this.tab === "mrz" ? "mrz" : "photo");
        },
        loadStatus: async function () {
          if (this.status.loaded) return;
          try {
            var response = await fetch("/api/id-scan/status", { headers: { Accept: "application/json" } });
            var data = await response.json();
            this.status = { loaded: true, tesseract: !!data.tesseract, pdf: !!data.pdf };
            if (data.max_bytes) this.cfg.max_bytes = data.max_bytes;
          } catch (e) {
            this.status = { loaded: true, tesseract: false, pdf: false };
          }
        },

        // ---------------------------------------------------------------- échanges avec le serveur
        request: async function (url, options) {
          options.headers = Object.assign({ Accept: "application/json", "X-CSRF-Token": csrf() }, options.headers || {});
          options.credentials = "same-origin";
          var response;
          try {
            response = await fetch(url, options);
          } catch (e) {
            return { ok: false, message: msg.network };
          }
          var data = null;
          try { data = await response.json(); } catch (e) { data = null; }
          if (!data) return { ok: false, message: response.status === 403 ? msg.session : msg.network };
          if (!response.ok && data.ok === undefined) data.ok = false;
          if (!data.ok && !data.message) data.message = response.status === 403 ? msg.session : msg.network;
          return data;
        },
        receive: function (data) {
          var result = data.result;
          if (data.scan_id && this.scanIds.indexOf(data.scan_id) === -1) this.scanIds.push(data.scan_id);
          if (data.file_url) {
            this.files.push({ url: data.file_url, image: String(data.mime_type || "").indexOf("image/") === 0,
                              sideLabel: msg[this.side] || this.side });
          }
          if (!result) return;
          this.result = result;
          // La première lecture remplace les valeurs de la fiche existante ; une lecture suivante (autre face) ne
          // complète que les champs encore vides, sans écraser ce que la réception a déjà vérifié ou corrigé.
          // Priorité des sources : MRZ (sommes de contrôle) > texte lu sur la face sans MRZ. Une MRZ lue sur le verso
          // corrige donc ce que la lecture approximative du recto avait proposé ; un désaccord sur une donnée clé est
          // signalé pour vérification.
          var self = this;
          var rank = ["td1", "td2", "td3", "aamva"].indexOf(result.format) !== -1 ? 2 : 1;
          Object.keys(BLANK).forEach(function (key) {
            var value = (result.fields || {})[key];
            if (!value) return;
            var current = self.fields[key];
            var known = self.fieldRank[key] || 0;
            if (known && current && current !== value && CONFLICT_KEYS.indexOf(key) !== -1) {
              var w = (msg.conflict || "{field} : {a} / {b}").replace("{field}", (msg.labels || {})[key] || key)
                .replace("{a}", current).replace("{b}", value);
              if (self.warnings.indexOf(w) === -1) self.warnings.push(w);
            }
            if (!known || !current || current === BLANK[key] || rank > known) {
              self.fields[key] = value;
              self.fieldRank[key] = Math.max(rank, known);
            }
          });
          (result.warnings || []).forEach(function (w) { if (self.warnings.indexOf(w) === -1) self.warnings.push(w); });
          (result.notes || []).forEach(function (n) { if (self.notes.indexOf(n) === -1) self.notes.push(n); });
          // Le verso (MRZ) a fourni les noms latins : l'invitation à lire l'autre face n'a plus lieu d'être.
          if (rank === 2 && self.fields.last_name && msg.cni_recto) {
            self.notes = self.notes.filter(function (n) { return n !== msg.cni_recto; });
          }
          if (result.raw_text) self.rawText = (self.rawText ? self.rawText + "\n――――\n" : "") + result.raw_text;
          this.step = "verify";
        },
        analyseMrz: async function (text) {
          if (!cleanMrz(text).replace(/\n/g, "").length) { this.error = msg.empty; return; }
          this.busy = true;
          this.error = "";
          var data = await this.request("/api/id-scan/mrz", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: text, reservation_id: this.cfg.reservation_id, guest_id: this.cfg.guest_id })
          });
          this.busy = false;
          this.info = "";
          if (!data.ok) { this.error = data.message; return; }
          this.receive(data);
        },

        // ---------------------------------------------------------------- fichier et caméra
        clearPreview: function (side) {
          var self = this;
          (side ? [side] : ["recto", "verso"]).forEach(function (name) {
            var slot = self.slots[name];
            if (slot.url) URL.revokeObjectURL(slot.url);
            self.slots[name] = emptySlot();
            document.querySelectorAll('input[data-id-scan-file][data-side="' + name + '"]').forEach(function (input) {
              input.value = "";
            });
          });
        },
        get hasFiles() { return !!(this.slots.recto.file || this.slots.verso.file); },
        setFile: function (file, source, side) {
          side = side || this.side;
          this.clearPreview(side);
          this.error = "";
          if (!file) return;
          if (["image/png", "image/jpeg", "application/pdf"].indexOf(file.type) === -1) { this.error = msg.bad_type; return; }
          if (file.size > (this.cfg.max_bytes || 8388608)) { this.error = msg.too_big; return; }
          var pdf = file.type === "application/pdf";
          this.slots[side] = { file: file, source: source, name: file.name || "", pdf: pdf,
                               url: pdf ? "" : URL.createObjectURL(file) };
        },
        pickFile: function (event, side) {
          var input = event.target;
          this.setFile(input.files && input.files[0], "image_upload", side);
          if (!this.slots[side].file) input.value = "";
        },
        startCamera: async function (side) {
          if (side) this.side = side;
          this.error = "";
          if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            this.error = msg.camera_insecure;
            return;
          }
          try {
            var stream = await navigator.mediaDevices.getUserMedia({
              video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false
            });
            this.camera.stream = stream;
            this.camera.active = true;
            var self = this;
            this.$nextTick(function () { if (self.$refs.video) self.$refs.video.srcObject = stream; });
          } catch (e) {
            var name = e && e.name;
            this.error = (name === "NotAllowedError" || name === "SecurityError") ? msg.camera_denied
              : (name === "NotFoundError" || name === "OverconstrainedError") ? msg.camera_missing : msg.camera_error;
          }
        },
        stopCamera: function () {
          if (this.camera.stream) this.camera.stream.getTracks().forEach(function (track) { track.stop(); });
          this.camera.stream = null;
          this.camera.active = false;
          if (this.$refs && this.$refs.video) this.$refs.video.srcObject = null;
        },
        capturePhoto: function () {
          var video = this.$refs.video;
          if (!video || !video.videoWidth) return;
          var canvas = document.createElement("canvas");
          canvas.width = video.videoWidth;
          canvas.height = video.videoHeight;
          canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
          var self = this;
          canvas.toBlob(function (blob) {
            if (!blob) { self.error = msg.camera_error; return; }
            self.setFile(new File([blob], "webcam-" + self.side + ".jpg", { type: "image/jpeg" }), "webcam", self.side);
            self.stopCamera();
          }, "image/jpeg", 0.92);
        },
        uploadSide: async function (side) {
          var slot = this.slots[side];
          var form = new FormData();
          form.append("file", slot.file, slot.name || ("piece-" + side + ".jpg"));
          form.append("source", slot.source);
          form.append("side", side);
          form.append("csrf_token", csrf());
          if (this.cfg.reservation_id) form.append("reservation_id", this.cfg.reservation_id);
          if (this.cfg.guest_id) form.append("guest_id", this.cfg.guest_id);
          var data = await this.request("/api/id-scan/upload", { method: "POST", body: form });
          if (!data.ok) { this.error = data.message; return false; }
          this.side = side;
          this.receive(data);
          if (data.result && data.result.status === "failed" && data.result.message) this.info = data.result.message;
          return true;
        },
        // Recto et verso choisis ensemble : une seule lecture, les deux faces fusionnées (la MRZ l'emporte).
        upload: async function () {
          if (!this.hasFiles) { this.error = msg.no_file; return; }
          this.busy = true;
          this.error = "";
          var sides = ["recto", "verso"].filter(function (name) { return this.slots[name].file; }, this);
          for (var i = 0; i < sides.length; i++) {
            if (!(await this.uploadSide(sides[i]))) break;
          }
          this.busy = false;
          if (!this.error) this.clearPreview();
        },

        // ---------------------------------------------------------------- vérification et validation
        get expired() {
          return !!this.fields.id_expiry_date && !!this.cfg.today && this.fields.id_expiry_date < this.cfg.today;
        },
        get formatLabel() {
          return this.result ? (FORMAT_LABELS[this.result.format] || this.result.format) : "";
        },
        apply: async function () {
          this.busy = true;
          this.error = "";
          var data = await this.request("/api/id-scan/apply", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ fields: this.fields, scan_ids: this.scanIds,
                                   reservation_id: this.cfg.reservation_id, guest_id: this.cfg.guest_id })
          });
          this.busy = false;
          if (!data.ok) { this.error = data.message; return; }
          this.applied = data;
          if (this.cfg.context === "new") {
            data.message = (data.message || "") + " " + (msg.applied_new || "");
            window.dispatchEvent(new CustomEvent("id-scan-applied", { detail: data.guest }));
          }
          this.step = "done";
        }
      };
    });
  });
})();
