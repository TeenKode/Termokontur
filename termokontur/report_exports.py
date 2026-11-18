"""Excel and PDF reports, generated on demand without persistent public files."""
from __future__ import annotations

from html import escape
from io import BytesIO
from pathlib import Path

import xlsxwriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable


def _cell(value):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    return value


def workbook(report: dict) -> bytes:
    """Multi-sheet workbook: compact summary and full inspectable raw rows."""
    output = BytesIO()
    wb = xlsxwriter.Workbook(output, {
        "in_memory": True, "strings_to_formulas": False, "strings_to_urls": False,
    })
    title = wb.add_format({"bold": True, "font_size": 16, "font_color": "#174C50"})
    note = wb.add_format({"font_color": "#53657A", "text_wrap": True})
    header = wb.add_format({"bold": True, "bg_color": "#176F72",
                            "font_color": "white", "text_wrap": True,
                            "valign": "vcenter", "bottom": 1, "bottom_color": "#114C4E"})
    stripe = wb.add_format({"bg_color": "#F0F7F7"})
    measure = wb.add_format({"num_format": "0.00"})
    percent = wb.add_format({"num_format": "0.0\"%\""})

    def sheet(name, columns, rows):
        ws = wb.add_worksheet(name)
        ws.hide_gridlines(2)
        ws.set_zoom(90)
        ws.freeze_panes(1, 2)
        for j, (label, key, width) in enumerate(columns):
            ws.set_column(j, j, width)
            ws.write(0, j, label, header)
        ws.set_row(0, 34)
        for i, row in enumerate(rows, 1):
            for j, (_, key, _) in enumerate(columns):
                value = row.get(key)
                fmt = stripe if i % 2 == 0 else None
                if isinstance(value, bool):
                    ws.write_string(i, j, _cell(value), fmt)
                elif value is None:
                    ws.write_blank(i, j, None, fmt)
                elif isinstance(value, (float,int)):
                    ws.write_number(i, j, value, percent if key == "coverage" else
                                    measure if isinstance(value, float) else fmt)
                else:
                    ws.write_string(i, j, str(value), fmt)
        ws.autofilter(0, 0, max(1, len(rows)), len(columns)-1)
        return ws

    overview = wb.add_worksheet("Сводка")
    overview.hide_gridlines(2)
    overview.set_column("A:A", 38)
    overview.set_column("B:B", 28)
    overview.merge_range("A1:B2", "Термоконтур · Отчёт", title)
    info = [
        ("Начало периода", report["from"]), ("Конец периода", report["to"]),
        ("Часовой пояс", report["timezone"]),
        ("Корпус", report["building"] or "Все здания"),
        ("Количество кабинетов", report["rooms_count"]),
        ("Кабинето-дней за период", report["summary"]["rooms_total"]),
        ("Кабинето-дней с измерениями", report["summary"]["rooms_measured"]),
        ("Без показаний", report["summary"]["missing"]),
        ("Сдача показаний, %", report["summary"]["coverage"]),
        ("Средняя температура, °C", report["summary"]["average"]),
        ("Отклонений по среднесуточной", report["summary"]["out_of_range"]),
        ("Измерений вне порогов", report["summary"]["deviations"]),
    ]
    for i, (label,value) in enumerate(info, 3):
        overview.write(i, 0, label)
        overview.write(i, 1, _cell(value))
    overview.merge_range(18, 0, 21, 1, report["note"], note)

    sheet("Сдача", [
        ("Дата","date",14), ("Кабинето-дней","rooms_total",19),
        ("С измерениями","rooms_measured",18), ("Пропуски","missing",16),
        ("Охват, %","coverage",14), ("Средняя, °C","average",16),
        ("Вне диапазона","out_of_range",19), ("Измерений","readings",16),
    ], report["daily"])
    sheet("Температура", [
        ("Здание","building",24), ("Кабинет","room",26),
        ("Дней","days_total",12), ("Дней с данными","days_measured",19),
        ("Пропуски","days_missing",14), ("Охват, %","coverage",14),
        ("Средняя, °C","average",17), ("Мин., °C","minimum",14),
        ("Макс., °C","maximum",14), ("Дней вне диапазона","out_of_range_days",22),
        ("Замеров вне диапазона","deviations",23),
    ], report["rooms"])
    sheet("По кабинетам", [
        ("Дата","date",14), ("Здание","building",22),
        ("Кабинет","room",26), ("Средняя, °C","average",16),
        ("Мин., °C","minimum",16), ("Макс., °C","maximum",16),
        ("Измерений","readings",16), ("Отклонений","deviations",17),
        ("Вне нормы по среднему","out_of_range",25),
        ("Последнее измерение","measured_at",26),
        ("Кто передал","reporter_name",34),
    ], report["room_daily"])
    sheet("Здания", [
        ("Дата","date",15), ("Здание","building",25),
        ("Кабинетов","rooms_total",16), ("С измерениями","rooms_measured",19),
        ("Пропуски","missing",14), ("Охват, %","coverage",14),
        ("Средняя, °C","average",17), ("Минимум, °C","minimum",17),
        ("Максимум, °C","maximum",17), ("Вне нормы","out_of_range",16),
    ], report["building_daily"])
    staff_rows = [{**row, "rooms": "; ".join(row["rooms"])} for row in report["staff"]]
    sheet("Сотрудники", [
        ("Сотрудник MAX","name",29), ("MAX ID","user_id",20),
        ("Кабинеты","rooms",55), ("Закреплено кабинетов","rooms_count",23),
        ("Кабинето-дней","assigned_room_days",18),
        ("Измерено кабинето-дней","measured_room_days",25),
        ("Пропусков в кабинетах","missing_room_days",25),
        ("Охват, %","coverage",14),
        ("Личных показаний","authored_readings",21),
        ("Дней с личным вводом","days_with_submission",25),
    ], staff_rows)
    sheet("Пропуски", [
        ("Дата","date",15), ("Здание","building",30),
        ("Кабинет","room",30), ("ID кабинета","room_id",17),
    ], report["missing"])
    wb.close()
    return output.getvalue()


def _font() -> tuple[str, str]:
    paths = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    bold = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
    ]
    for regular, heavy in zip(paths, bold):
        if regular.is_file() and heavy.is_file():
            if "TermokonturRegular" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("TermokonturRegular", str(regular)))
                pdfmetrics.registerFont(TTFont("TermokonturBold", str(heavy)))
            return "TermokonturRegular", "TermokonturBold"
    raise ValueError("На сервере отсутствует шрифт для PDF. Установите fonts-dejavu-core")


def pdf(report: dict) -> bytes:
    normal_font, bold_font = _font()
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=36,
                            leftMargin=36, topMargin=36, bottomMargin=38,
                            title="Термоконтур — отчёт по температуре")
    heading = ParagraphStyle("TitleTermo", fontName=bold_font, fontSize=17, leading=23, spaceAfter=12)
    subheading = ParagraphStyle("HeadingTermo", fontName=bold_font, fontSize=11, leading=16, spaceBefore=16, spaceAfter=9)
    body = ParagraphStyle("BodyTermo", fontName=normal_font, fontSize=8.2, leading=12, spaceAfter=6)
    small = ParagraphStyle("CellTermo", fontName=normal_font, fontSize=7.2, leading=10, wordWrap="CJK")
    cells_head = ParagraphStyle("CellHeadTermo", parent=small, fontName=bold_font, textColor=colors.white)
    items = [Paragraph("Термоконтур · Отчёт по температуре", heading),
             Paragraph(f"Период: {report['from']} — {report['to']} · {escape(report['timezone'])}", body),
             Paragraph("Здание: " + escape(report["building"] or "Все здания"), body)]

    def add_table(title_text, headings, rows, widths=None):
        items.append(Paragraph(title_text, subheading))
        matrix = [[Paragraph(escape(str(x)), cells_head) for x in headings]]
        for row in rows:
            matrix.append([Paragraph(escape(str(_cell(x))), small) for x in row])
        table = LongTable(matrix, colWidths=widths,
                          repeatRows=1, hAlign="LEFT", splitByRow=1)
        table.setStyle([
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#176F72")),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F0F7F7")]),
            ("BOTTOMPADDING",(0,0),(-1,-1),6),
            ("TOPPADDING",(0,0),(-1,-1),6),
            ("LEFTPADDING",(0,0),(-1,-1),6),
            ("RIGHTPADDING",(0,0),(-1,-1),6),
            ("LINEBELOW",(0,0),(-1,0),0.6,colors.HexColor("#105053")),
        ])
        items.append(table)

    s = report["summary"]
    add_table("Общие показатели", ["Показатель", "Значение"], [
        ("Кабинето-дней",s["rooms_total"]),
        ("С измерениями",s["rooms_measured"]),
        ("Не измерено",s["missing"]),
        ("Охват",f"{s['coverage']:g}%"),
        ("Средняя температура",f"{s['average']:g} °C" if s["average"] is not None else "—"),
        ("Дней кабинетов вне порогов",s["out_of_range"]),
        ("Отдельных измерений вне порогов",s["deviations"]),
    ], [342,181])
    add_table("Сравнение зданий",["Здание","Охват","Средняя","Пропуски","Откл."],[
        (r["building"],f"{r['coverage']:g}%",
         f"{r['average']:g} °C" if r["average"] is not None else "—",
         r["missing"],r["out_of_range"]) for r in report["building_summary"]
    ],[205,75,95,77,71])
    add_table("Температура и сдача по кабинетам",["Здание / кабинет","Охват","Средняя","Пропуски","Откл."],[
        (r["building"]+" / "+r["room"],f"{r['coverage']:g}%",
         f"{r['average']:g} °C" if r["average"] is not None else "—",
         r["days_missing"],r["out_of_range_days"]) for r in report["rooms"]
    ],[205,75,95,77,71])
    if report["staff"]:
        add_table("Сотрудники MAX",["Сотрудник","Кабинетов","Измерено","Лично","Пропуски"],[
            (r["name"],r["rooms_count"],r["measured_room_days"],
             r["authored_readings"],r["missing_room_days"]) for r in report["staff"]
        ],[205,78,83,78,79])
    if report["missing"]:
        add_table("Пропуски (первые 100; полный список в Excel)",
                  ["Дата","Здание","Кабинет"],[
                      (r["date"],r["building"],r["room"]) for r in report["missing"][:100]
                  ],[105,205,213])
    items.append(Spacer(1,10))
    items.append(Paragraph(escape(report["note"]),body))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(normal_font,8)
        canvas.setFillColor(colors.HexColor("#607580"))
        canvas.drawString(36,21,"Термоконтур · Отчёт сформирован из сохранённых данных")
        canvas.drawRightString(A4[0]-36,21,str(document.page))
        canvas.restoreState()

    doc.build(items, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
