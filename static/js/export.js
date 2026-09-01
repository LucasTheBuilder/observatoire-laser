// Export CSV, partagé par les onglets qui proposent un téléchargement.

import { toast } from "./ui.js";

function toCSV(rows, columns) {
  const cell = value => {
    const str = String(value ?? "");
    return /[",;\n]/.test(str) ? `"${str.replace(/"/g, '""')}"` : str;
  };
  const lines = [columns.map(c => cell(c.label)).join(",")];
  for (const row of rows) lines.push(columns.map(c => cell(row[c.key])).join(","));
  return lines.join("\r\n");
}

export function downloadCSV(filename, rows, columns) {
  if (!rows.length) { toast("Rien à exporter pour le moment."); return; }
  // BOM en tête, pour qu'Excel ouvre le texte accentué en UTF-8 au lieu de le deviner.
  const blob = new Blob(["﻿" + toCSV(rows, columns)], {type: "text/csv;charset=utf-8;"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
