"""Export-ready supplier review packs (enhancement 1.4): PDF (reportlab) and PPTX (python-pptx).

The exporter renders the SAME dict that /api/suppliers/{id}/review-pack returns - it computes nothing itself, so the
file always matches the screen. Every file carries a traceability footer: calculation run id, KPI-definition version and
hash, data as-of date and how to retrieve the source records. The optional AI narrative is included only when it was
generated and passed the numeric grounding check.
"""
from __future__ import annotations

import io

from .db import config_hash, current_config_version, get_scorecard_config

KPIS = [("Composite score", "composite_score", ""), ("Grade", "grade", ""), ("OTIF", "otif_pct", "%"), ("On-time", "on_time_pct", "%"),
        ("In-full", "in_full_pct", "%"), ("Lead-time variance (wd)", "avg_lead_time_variance_days", ""), ("Incoming PPM", "incoming_ppm", ""),
        ("Open NCR (weighted)", "open_ncr", ""), ("Overdue CAPA (weighted)", "overdue_capa", ""), ("NCR response (days)", "avg_response_days", ""),
        ("Price variance", "price_variance_pct", "%"), ("Disruption hours", "disruption_hours", ""), ("Past-due PO lines", "past_due_lines", "")]


def _v(x, unit=""):
    if x is None:
        return "–"
    if isinstance(x, float):
        return f"{x:,.1f}{unit}"
    return f"{x}{unit}"


def _months(pack: dict) -> list[dict]:
    """Monthly rows inside the requested period only (the on-screen pack shows the full history)."""
    a, b = pack["period"].get("start"), pack["period"].get("end")
    return [m for m in pack["monthly_trends"] if m.get("lines") and (not a or m["period"][:7] >= a[:7]) and (not b or m["period"][:7] <= b[:7])]


def _footer(pack: dict) -> str:
    v, h = current_config_version()
    return (f"Data as of {pack['as_of_date']} · period {pack['period'].get('start') or 'all'} to {pack['period'].get('end') or pack['as_of_date']} · "
            f"calc run {pack['scorecard'].get('run_id') or '–'} · KPI definitions v{v} ({h}) · "
            f"source records: /api/evidence/{pack['scorecard']['supplier_id']}/<metric>")


def pdf(pack: dict, narrative: str | None = None) -> bytes:
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    sc = pack["scorecard"]
    buf = io.BytesIO()
    st = getSampleStyleSheet()
    purple = colors.HexColor("#6c3cc1")
    foot = _footer(pack)

    def on_page(c, d):
        c.setFont("Helvetica", 6.5)
        c.setFillColor(colors.grey)
        half = foot.rfind(" · ", 0, 120)
        c.drawString(15 * mm, 11 * mm, foot[:half])
        c.drawString(15 * mm, 7.5 * mm, foot[half + 3:])
        c.drawRightString(195 * mm, 7.5 * mm, f"Page {d.page}")

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=18 * mm,
                            title=f"Supplier review - {sc['supplier_name']}")
    tbl = lambda rows, widths=None: Table(rows, colWidths=widths, style=TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), purple), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    el = [Paragraph(f"Supplier business review · {sc['supplier_name']} ({sc['supplier_id']})", st["Title"]),
          Paragraph(f"{sc['material_category']} · {sc['country']} · risk tier {sc['risk_tier']} · health {sc.get('health', '–')} · "
                    f"generated {pack['generated_at'][:16].replace('T', ' ')}", st["Normal"]), Spacer(1, 6)]
    pb = pack["peer_benchmark"]
    el += [Paragraph("Scorecard", st["Heading2"]),
           tbl([["KPI", "Value"]] + [[l, _v(sc.get(k), u)] for l, k, u in KPIS], [90 * mm, 40 * mm]),
           Spacer(1, 4),
           Paragraph(f"Peer averages across {pb['suppliers']} suppliers: OTIF {pb['avg_otif_pct']}%, PPM {pb['avg_ppm']}, composite "
                     f"{pb['avg_composite']}. Weights: " + ", ".join(f"{k} {v * 100:.0f}%" for k, v in pack["weights"].items())
                     + f". PPM method: {sc.get('ppm_method', '–')}.", st["Normal"])]
    mt = _months(pack)
    if mt:
        d = Drawing(170 * mm, 55 * mm)
        ch = VerticalBarChart()
        ch.x, ch.y, ch.width, ch.height = 10 * mm, 8 * mm, 150 * mm, 40 * mm
        ch.data = [[m["otif_pct"] or 0 for m in mt]]
        ch.categoryAxis.categoryNames = [m["period"][:7] for m in mt]
        ch.valueAxis.valueMin, ch.valueAxis.valueMax = 0, 100
        ch.bars[0].fillColor = purple
        d.add(ch)
        el += [Paragraph("Monthly OTIF %", st["Heading2"]), d,
               tbl([["Month", "Lines", "OTIF %", "On-time %", "Avg delay d", "Defects", "PPM"]] +
                   [[m["period"][:7], m["lines"], _v(m["otif_pct"]), _v(m["on_time_pct"]), _v(m["avg_delay_days"]), _v(m["defects"]), _v(m["ppm"])]
                    for m in mt])]
    if narrative:
        el += [Paragraph("Executive summary (AI-drafted, figures verified against the data)", st["Heading2"])]
        el += [Paragraph(p.replace("**", ""), st["Normal"]) for p in narrative.split("\n") if p.strip()]
    el += [Paragraph("Risk alerts", st["Heading2"])]
    el += [tbl([["Severity", "Alert", "Detail"]] + [[a["severity"], Paragraph(a["title"], st["Normal"]), Paragraph(a["detail"], st["Normal"])]
                                                   for a in pack["alerts"][:10]], [20 * mm, 55 * mm, 105 * mm])] if pack["alerts"] else \
          [Paragraph("No active alerts.", st["Normal"])]
    issues = pack["open_issues"][:12]
    el += [Paragraph(f"Open NCR / CAPA ({len(pack['open_issues'])}, oldest first)", st["Heading2"])]
    if issues:
        el += [tbl([["NCR", "Defect", "Material", "Due", "Age d", "Overdue"]] +
                   [[i["ncr_id"], i["defect_name"], i["material_id"], str(i["capa_due_date"]), _v(i["open_age_days"]), "yes" if i["is_overdue"] else ""]
                    for i in issues])]
    ex = pack["exposure"]
    el += [Paragraph("Exposure", st["Heading2"]),
           Paragraph(f"{len(ex['past_due_lines'])} past-due PO lines; {ex['at_risk_production_orders']} open production orders and "
                     f"{ex['at_risk_customer_orders']} customer orders use the affected materials.", st["Normal"]),
           Paragraph("Recommendations (rule-based, config thresholds)", st["Heading2"])]
    el += [Paragraph(f"• {r}", st["Normal"]) for r in pack["recommendations"]]
    acts = pack["corrective_actions"][:10]
    if acts:
        el += [Paragraph("Corrective actions", st["Heading2"]),
               tbl([["#", "Title", "Status", "Owner", "Due"]] + [[a["id"], Paragraph(a["title"], st["Normal"]), a["status"], a["owner_role"], str(a["due_date"])]
                                                               for a in acts], [10 * mm, 95 * mm, 30 * mm, 25 * mm, 20 * mm])]
    el += [Spacer(1, 8), Paragraph("Decision support only: sourcing and award decisions remain with procurement.", st["Italic"])]
    doc.build(el, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


def pptx(pack: dict, narrative: str | None = None) -> bytes:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches, Pt

    sc = pack["scorecard"]
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    purple = RGBColor(0x6C, 0x3C, 0xC1)
    foot = _footer(pack)

    def slide(title):
        s = prs.slides.add_slide(prs.slide_layouts[5])
        s.shapes.title.text = title
        s.shapes.title.text_frame.paragraphs[0].font.size = Pt(28)
        s.shapes.title.text_frame.paragraphs[0].font.color.rgb = purple
        tb = s.shapes.add_textbox(Inches(0.4), Inches(7.05), Inches(12.5), Inches(0.35)).text_frame
        tb.text = foot
        tb.paragraphs[0].font.size = Pt(8)
        return s

    def table(s, rows, top=1.5, widths=None):
        t = s.shapes.add_table(len(rows), len(rows[0]), Inches(0.5), Inches(top), Inches(12.3), Inches(0.3 * len(rows))).table
        for i, r in enumerate(rows):
            for j, v in enumerate(r):
                t.cell(i, j).text = str(v)
                t.cell(i, j).text_frame.paragraphs[0].font.size = Pt(11 if i else 12)
        if widths:
            for j, w in enumerate(widths):
                t.columns[j].width = Inches(w)

    def bullets(s, lines, top=1.5, size=16):
        tf = s.shapes.add_textbox(Inches(0.6), Inches(top), Inches(12), Inches(5)).text_frame
        tf.word_wrap = True
        for i, l in enumerate(lines):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = l
            p.font.size = Pt(size)

    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = f"Supplier business review\n{sc['supplier_name']}"
    s.placeholders[1].text = (f"{sc['supplier_id']} · {sc['material_category']} · {sc['country']} · grade {sc['grade']} · "
                              f"health {sc.get('health', '–')}\nData as of {pack['as_of_date']}")
    s = slide("Scorecard")
    table(s, [["KPI", "Value"]] + [[l, _v(sc.get(k), u)] for l, k, u in KPIS], 1.3, [6, 3])
    mt = _months(pack)
    if mt:
        s = slide("Monthly OTIF % and PPM")
        cd = CategoryChartData()
        cd.categories = [m["period"][:7] for m in mt]
        cd.add_series("OTIF %", [m["otif_pct"] or 0 for m in mt])
        s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.5), Inches(1.4), Inches(6.2), Inches(5.2), cd)
        cd2 = CategoryChartData()
        cd2.categories = [m["period"][:7] for m in mt]
        cd2.add_series("PPM", [m["ppm"] or 0 for m in mt])
        s.shapes.add_chart(XL_CHART_TYPE.LINE_MARKERS, Inches(6.9), Inches(1.4), Inches(6), Inches(5.2), cd2)
    if narrative:
        s = slide("Executive summary (AI-drafted, figures verified)")
        bullets(s, [p.replace("**", "").lstrip("#").strip() for p in narrative.split("\n") if p.strip()], size=14)
    s = slide("Risks and open issues")
    lines = [f"[{a['severity']}] {a['title']}: {a['detail']}" for a in pack["alerts"][:6]] or ["No active alerts."]
    ex = pack["exposure"]
    lines += [f"{len(pack['open_issues'])} open NCR/CAPA ({sum(1 for i in pack['open_issues'] if i['is_overdue'])} overdue)",
              f"Exposure: {len(ex['past_due_lines'])} past-due PO lines, {ex['at_risk_production_orders']} production orders, "
              f"{ex['at_risk_customer_orders']} customer orders"]
    bullets(s, lines, size=14)
    s = slide("Recommendations and actions")
    bullets(s, [f"• {r}" for r in pack["recommendations"]] + [""] +
            [f"#{a['id']} {a['title']} - {a['status']} (due {a['due_date']})" for a in pack["corrective_actions"][:6]] +
            ["", "Decision support only: sourcing and award decisions remain with procurement."], size=14)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def meta() -> dict:
    v, h = current_config_version()
    return {"config_version": v, "config_hash": h or config_hash(get_scorecard_config())}
