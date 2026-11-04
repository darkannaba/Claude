"""Exports comptables : CSV (séparateur « ; », UTF-8 avec BOM pour Excel) et classeur Excel."""
from __future__ import annotations

import csv
import io
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

COLUMNS = [
    ("date", "Date"), ("journal", "Journal"), ("type", "Type"), ("piece", "Pièce"),
    ("reservation", "Réservation"), ("client", "Client"), ("libelle", "Libellé"),
    ("taux_tva", "Taux TVA %"), ("ht", "Montant HT"), ("tva", "TVA"), ("ttc", "Montant TTC"),
    ("mode", "Mode de règlement"),
]


def entries_to_csv(rows: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow([label for _, label in COLUMNS])
    for row in rows:
        values = []
        for key, _ in COLUMNS:
            value = row.get(key, "")
            values.append(value.strftime("%d/%m/%Y") if isinstance(value, date) else value)
        writer.writerow(values)
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def entries_to_xlsx(rows: list[dict], start: date, end: date, hotel_name: str, currency: str) -> bytes:
    workbook = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="0F766E")

    sheet = workbook.active
    sheet.title = "Écritures"
    sheet.append([f"{hotel_name} – écritures du {start:%d/%m/%Y} au {end:%d/%m/%Y} (exclu) – montants en {currency}"])
    sheet["A1"].font = Font(bold=True, size=12)
    sheet.append([label for _, label in COLUMNS])
    for cell in sheet[2]:
        cell.font, cell.fill, cell.alignment = header_font, header_fill, Alignment(horizontal="center")
    for row in rows:
        sheet.append([row.get(key, "") for key, _ in COLUMNS])
    for row_cells in sheet.iter_rows(min_row=3, min_col=1, max_col=1):
        for cell in row_cells:
            cell.number_format = "DD/MM/YYYY"
    for col in (9, 10, 11):
        for row_cells in sheet.iter_rows(min_row=3, min_col=col, max_col=col):
            for cell in row_cells:
                cell.number_format = "#,##0"
    widths = [12, 10, 22, 18, 13, 26, 48, 10, 14, 12, 14, 20]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A3"

    summary = workbook.create_sheet("Synthèse")
    summary.append(["Rubrique", "Montant HT", "TVA", "Montant TTC"])
    for cell in summary[1]:
        cell.font, cell.fill = header_font, header_fill
    totals: dict[str, list[int]] = {}
    for row in rows:
        key = f"{row['journal']} – {row['type']}" + (f" – {row['mode']}" if row["mode"] else "")
        bucket = totals.setdefault(key, [0, 0, 0])
        bucket[0] += row["ht"]
        bucket[1] += row["tva"]
        bucket[2] += row["ttc"]
    for key in sorted(totals):
        summary.append([key, *totals[key]])
    for col in ("B", "C", "D"):
        for cell in summary[col][1:]:
            cell.number_format = "#,##0"
    summary.column_dimensions["A"].width = 52
    for col in ("B", "C", "D"):
        summary.column_dimensions[col].width = 16

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()
