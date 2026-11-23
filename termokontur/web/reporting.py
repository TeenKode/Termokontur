"""Dashboard, school calendar, and reports for authorized users."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import daily, db, report_exports, report_schedule, reports, school_calendar, settings
from .access import require_admin, scope_building, scoped_access

router = APIRouter(prefix="/api")


class ReportSubscriptionIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    building: str = Field(default="", max_length=100)
    frequency: Literal["daily", "weekly"]
    send_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekday: int = Field(default=0, ge=0, le=6)
    enabled: bool = True


class ReportSubscriptionEnabledIn(BaseModel):
    enabled: bool


@router.get("/dashboard")
def dashboard(scope: str = Depends(scoped_access)):
    return db.dashboard(int(settings.get("stale_hours", "26")), scope)


@router.get("/dashboard/history")
def dashboard_history(days: int = Query(14, ge=7, le=90),
                      building: str = Query("", max_length=100), scope: str = Depends(scoped_access)):
    """Calendar-day aggregates; each cabinet counts once per day."""
    if days not in (7, 14, 30, 90):
        raise HTTPException(422, "Выберите период: 7, 14, 30 или 90 дней")
    return db.dashboard_history(
        days=days, building=scope_building(building, scope), timezone_name=settings.get("timezone", "UTC")
    )


@router.get("/daily")
def daily_report(scope: str = Depends(scoped_access)):
    return daily.summary(building=scope)


@router.get("/school-calendar", dependencies=[Depends(require_admin)])
def calendar_settings():
    calendar, weekdays = school_calendar.load()
    return {**calendar.model_dump(), "default_weekdays": sorted(weekdays), "buildings": school_calendar.buildings()}


@router.put("/school-calendar", dependencies=[Depends(require_admin)])
def save_calendar(payload: school_calendar.Calendar):
    try:
        school_calendar.save(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return calendar_settings()


def _report_snapshot(start: str, end: str, building: str) -> dict:
    try:
        return reports.build(start, end, building)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/reports")
def get_report(start: str, end: str, building: str = Query("", max_length=100), scope: str = Depends(scoped_access)):
    return JSONResponse(_report_snapshot(start, end, scope_building(building, scope)),
                        headers={"Cache-Control": "no-store"})


@router.get("/reports/export.xlsx")
def export_report_excel(start: str, end: str, building: str = Query("", max_length=100), scope: str = Depends(scoped_access)):
    report = _report_snapshot(start, end, scope_building(building, scope))
    return Response(
        content=report_exports.workbook(report),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="termokontur-report-{start}-{end}.xlsx"',
                 "Cache-Control": "private, no-store"},
    )


@router.get("/reports/export.pdf")
def export_report_pdf(start: str, end: str, building: str = Query("", max_length=100), scope: str = Depends(scoped_access)):
    report = _report_snapshot(start, end, scope_building(building, scope))
    try:
        content = report_exports.pdf(report)
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc
    return Response(
        content=content, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="termokontur-report-{start}-{end}.pdf"',
                 "Cache-Control": "private, no-store"},
    )


@router.get("/reports/subscriptions", dependencies=[Depends(require_admin)])
def get_report_subscriptions():
    return JSONResponse(report_schedule.subscriptions(),
                        headers={"Cache-Control": "no-store"})


@router.post("/reports/subscriptions", dependencies=[Depends(require_admin)], status_code=201)
def create_report_subscription(payload: ReportSubscriptionIn):
    if not settings.get("max_token", ""):
        raise HTTPException(409, "Для автоматических отчётов сначала настройте токен MAX")
    try:
        return report_schedule.create(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/reports/subscriptions/{subscription_id}", dependencies=[Depends(require_admin)])
def change_report_subscription(subscription_id: int, payload: ReportSubscriptionEnabledIn):
    if not report_schedule.set_enabled(subscription_id, payload.enabled):
        raise HTTPException(404, "Расписание не найдено")
    return {"ok": True, "enabled": payload.enabled}


@router.delete("/reports/subscriptions/{subscription_id}", dependencies=[Depends(require_admin)])
def delete_report_subscription(subscription_id: int):
    if not report_schedule.remove(subscription_id):
        raise HTTPException(404, "Расписание не найдено")
    return {"ok": True}
