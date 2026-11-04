// Copie les bibliothèques front (installées par npm) dans app/static/vendor pour un usage hors ligne.
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const vendor = path.join(root, "app", "static", "vendor");
fs.mkdirSync(vendor, { recursive: true });

const files = [
  ["node_modules/alpinejs/dist/cdn.min.js", "alpine.min.js"],
  ["node_modules/chart.js/dist/chart.umd.min.js", "chart.umd.min.js"],
];
for (const [source, target] of files) {
  fs.copyFileSync(path.join(root, source), path.join(vendor, target));
  console.log(`copié : ${source} -> app/static/vendor/${target}`);
}
