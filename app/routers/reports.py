"""Rapports analytiques (TO, ADR, RevPAR, CA, encaissements) et exports comptables."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.deps import AdminUser, DbSession, StaffUser
from app.models.invoice import PAYMENT_METHODS
from app.services import exports, reports
from app.services.common import parse_date
from app.services.settings import load_hotel_settings, today_of
from app.templating import render

router = APIRouter(prefix="/reports")


def _period(start: str, end: str, today):
    d_start = parse_date(start) or today.replace(day=1)
    d_end = parse_date(end) or today
    if d_end < d_start:
        d_start, d_end = d_end, d_start
    return d_start, d_end


@router.get("")
async def reports_page(request: Request, db: DbSession, user: StaffUser, start: str = "", end: str = "",
                       granularity: str = "day", year: str = ""):
    hs = await load_hotel_settings(db)
    today = today_of(hs)
    d_start, d_end = _period(start, end, today)
    end_exclusive = d_end + timedelta(days=1)
    chosen_year = int(year) if year.isdigit() else today.year
    kpi = await reports.kpis(db, d_start, end_exclusive)
    series = await reports.revenue_series(db, d_start, end_exclusive, granularity)
    monthly = await reports.monthly_series(db, chosen_year)
    payments = await reports.payments_by_method(db, d_start, end_exclusive)
    chart_data = {
        "revenue": series,
        "monthly": {"labels": [m["label"] for m in monthly], "occupancy": [m["occupancy"] for m in monthly],
                    "adr": [m["adr"] for m in monthly], "revpar": [m["revpar"] for m in monthly]},
        "payments": {"labels": [PAYMENT_METHODS[m] for m in payments], "values": list(payments.values())},
    }
    return await render(request, db, "reports/index.html", kpi=kpi, start=d_start, end=d_end, granularity=granularity,
                        year=chosen_year, monthly=monthly, payments=payments, chart_data=chart_data)


@router.get("/export.csv")
async def export_csv(db: DbSession, user: AdminUser, start: str = "", end: str = ""):
    hs = await load_hotel_settings(db)
    d_start, d_end = _period(start, end, today_of(hs))
    rows = await reports.accounting_entries(db, d_start, d_end + timedelta(days=1))
    filename = f"ecritures_{d_start:%Y%m%d}_{d_end:%Y%m%d}.csv"
    return Response(exports.entries_to_csv(rows), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/export.xlsx")
async def export_xlsx(db: DbSession, user: AdminUser, start: str = "", end: str = ""):
    hs = await load_hotel_settings(db)
    d_start, d_end = _period(start, end, today_of(hs))
    rows = await reports.accounting_entries(db, d_start, d_end + timedelta(days=1))
    content = exports.entries_to_xlsx(rows, d_start, d_end + timedelta(days=1), hs.hotel_name, hs.currency_code)
    filename = f"ecritures_{d_start:%Y%m%d}_{d_end:%Y%m%d}.xlsx"
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
