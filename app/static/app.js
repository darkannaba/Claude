// Interactions légères : thème clair/sombre, confirmations, recherche client, scanner.
(function () {
  "use strict";

  // Bascule de thème (préférence mémorisée dans le navigateur uniquement).
  document.addEventListener("click", function (event) {
    var toggle = event.target.closest("[data-theme-toggle]");
    if (toggle) {
      var dark = document.documentElement.classList.toggle("dark");
      try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch (e) { /* navigation privée */ }
    }
    var confirmBtn = event.target.closest("[data-confirm]");
    if (confirmBtn && !window.confirm(confirmBtn.getAttribute("data-confirm"))) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);

  // Recherche client (autocomplétion) sur la page de nouvelle réservation.
  document.addEventListener("DOMContentLoaded", function () {
    var input = document.getElementById("guest-search");
    if (!input) return;
    var results = document.getElementById("guest-results");
    var hidden = document.getElementById("guest_id");
    var selected = document.getElementById("guest-selected");
    var timer = null;

    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var q = input.value.trim();
        results.textContent = "";
        if (q.length < 2) return;
        fetch("/api/guests/search?q=" + encodeURIComponent(q), { credentials: "same-origin" })
          .then(function (r) { return r.ok ? r.json() : []; })
          .then(function (items) {
            results.textContent = "";
            items.forEach(function (g) {
              var b = document.createElement("button");
              b.type = "button";
              b.className = "btn btn-sm";
              b.style.width = "100%";
              b.style.justifyContent = "flex-start";
              b.style.marginTop = ".25rem";
              b.textContent = g.label;
              b.addEventListener("click", function () {
                hidden.value = g.id;
                selected.textContent = "Client sélectionné : " + g.label;
                results.textContent = "";
                input.value = "";
              });
              results.appendChild(b);
            });
          });
      }, 250);
    });
  });

  // Lecteur de pièce d'identité USB : focus automatique sur le champ de recherche.
  document.addEventListener("DOMContentLoaded", function () {
    var scan = document.querySelector("[data-autofocus]");
    if (scan) scan.focus();
  });
})();
