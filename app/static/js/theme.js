// Appliqué avant l'affichage pour éviter un flash de thème (préférence locale au navigateur).
(function () {
  try {
    var stored = localStorage.getItem("theme");
    var dark = stored ? stored === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    if (dark) document.documentElement.classList.add("dark");
  } catch (e) { /* stockage indisponible : thème clair */ }
})();
