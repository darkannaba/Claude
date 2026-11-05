/** Configuration Tailwind CSS – compilée localement (aucun CDN). Voir package.json : npm run build:css */

// Couleurs de la charte pilotées par des variables CSS (triplets « r g b ») : la feuille dynamique
// /static/css/theme-custom.css (app/services/theme_manager.py) les redéfinit selon les réglages de l'hôtel,
// sans recompiler Tailwind. Valeurs par défaut (charte teal / sky d'origine) : app/static/src/input.css.
const SHADES = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950];
const cssVarPalette = (name) =>
  Object.fromEntries(SHADES.map((shade) => [shade, `rgb(var(--${name}-${shade}) / <alpha-value>)`]));

module.exports = {
  darkMode: "class",
  content: [
    "./app/templates/**/*.html",
    "./app/static/js/**/*.js",
    "./app/templating.py",
    "./app/routers/**/*.py",
  ],
  // Classes composées dynamiquement dans les templates (ex. room-{{ room.status }}) : Tailwind ne peut
  // pas les détecter en lisant les fichiers, elles doivent être déclarées ici.
  safelist: [
    "room-available", "room-occupied", "room-dirty", "room-maintenance",
    "bar-confirmed", "bar-checked_in", "bar-checked_out",
    "flash-success", "flash-error", "flash-info", "flash-warning", "flash-danger",
  ],
  theme: {
    extend: {
      colors: { brand: cssVarPalette("brand"), accent: cssVarPalette("accent") },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "Roboto", "Helvetica Neue", "Arial", "Noto Sans Arabic", "Tahoma", "sans-serif"],
      },
    },
  },
  plugins: [],
};
