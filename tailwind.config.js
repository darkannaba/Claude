/** Configuration Tailwind CSS – compilée localement (aucun CDN). Voir package.json : npm run build:css */
const colors = require("tailwindcss/colors");

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
      colors: { brand: colors.teal },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "Roboto", "Helvetica Neue", "Arial", "Noto Sans Arabic", "Tahoma", "sans-serif"],
      },
    },
  },
  plugins: [],
};
