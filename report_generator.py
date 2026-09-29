

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    KeepTogether,
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


FIELD_LABELS = {
    "invoice_number": "Invoice Number",
    "invoice_date": "Invoice Date",
    "due_date": "Due Date",
    "vendor_name": "Vendor Name",
    "customer_name": "Customer Name",
    "subtotal": "Subtotal",
    "discount": "Discount",
    "tax": "Tax Amount",
    "total": "Total Amount",
}

NAVY = colors.HexColor("#16345F")
BLUE = colors.HexColor("#2576B9")
MUTED = colors.HexColor("#61738B")
BORDER = colors.HexColor("#DDE6F2")
PALE = colors.HexColor("#F5F8FC")
WHITE = colors.white
GREEN = colors.HexColor("#20845A")
AMBER = colors.HexColor("#AD741E")


def _font_names():
    candidates = (
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    )
    for regular, bold in candidates:
        if Path(regular).is_file() and Path(bold).is_file():
            pdfmetrics.registerFont(TTFont("IS-Regular", regular))
            pdfmetrics.registerFont(TTFont("IS-Bold", bold))
            pdfmetrics.registerFontFamily("IS-Regular", normal="IS-Regular", bold="IS-Bold")
            return "IS-Regular", "IS-Bold"
    return "Helvetica", "Helvetica-Bold"


FONT, FONT_BOLD = _font_names()


def _value(value):
    if value is None:
        return "—"
    result = str(value).strip()
    return result if result else "—"


def _paragraph(value, style):
    return Paragraph(xml_escape(_value(value)).replace("\n", "<br/>"), style)


def _raw_status(detail):
    if not isinstance(detail, dict):
        return "Not Verified"
    raw = str(detail.get("status", "")).strip().lower()
    return "Valid" if raw == "valid" else "Not Verified" if raw in {"not verified", ""} else "Needs Review"


def _edit_has_source_evidence(key, detail):
    """A formula-only pass does not confirm an edited value matches the image."""
    if _raw_status(detail) != "Valid":
        return False
    if not isinstance(detail, dict):
        return False
    verified_rule = "Source " + key
    rules = {rule.strip() for rule in str(detail.get("rule", "")).split(",")}
    return verified_rule in rules and detail.get("reason_code") == "confirmed"


def evaluate_report(validation_details, changed_fields):
    """Return (report_kind, per-field status), conservatively on uncertainty."""
    validation_details = validation_details or {}
    changed = set(changed_fields or ()) & FIELD_LABELS.keys()
    statuses = {}

    for key in FIELD_LABELS:
        detail = validation_details.get(key, {})
        source_verified = key not in changed or _edit_has_source_evidence(key, detail)
        statuses[key] = (
            "Valid" if _raw_status(detail) == "Valid" and source_verified
            else "Needs Review"
        )

    if any(status != "Valid" for status in statuses.values()):
        return "needs_review", statuses
    return ("corrected_verified" if changed else "validated"), statuses



def _source_mismatch(key, detail):
    if not isinstance(detail, dict) or detail.get("reason_code") != "mismatch":
        return False
    rules = {part.strip() for part in str(detail.get("rule") or "").split(",")}
    return "Source " + key in rules


def _verified_note(key, detail, public_reason):
    rules = {part.strip() for part in str((detail or {}).get("rule") or "").split(",")}
    if "Source " + key in rules:
        return "Confirmed against source evidence."
    if any("Equation" in rule or "Calculation" in rule for rule in rules):
        return "Passed the available calculation check."
    if public_reason and "source or calculation" not in public_reason.lower():
        return public_reason
    return "Passed the available verification checks."


def _review_priority(key, detail, changed):
    detail = detail if isinstance(detail, dict) else {}
    code = str(detail.get("reason_code") or "")
    rule = str(detail.get("rule") or "")
    if code == "missing_value":
        return ("Missing information", "Check the original invoice and complete these fields.")
    if key in changed and not _edit_has_source_evidence(key, detail):
        return ("Pending verification", "These saved changes have not been independently confirmed against the original invoice.")
    if code == "mismatch" and ("Equation" in rule or "Calculation" in rule):
        return ("Calculation discrepancy", "Reconcile these amounts against the original invoice.")
    if code == "mismatch" and _source_mismatch(key, detail):
        return ("Source discrepancy", "Compare these values with the original invoice and correct any differences.")
    return ("Insufficient verification evidence", "Review these values against the original invoice before proceeding.")


def _report_theme(kind):
    if kind == "corrected_verified":
        return (
            "CORRECTED & VERIFIED", BLUE, colors.HexColor("#EAF3FF"),
            "Every correction brings greater clarity.",
            "Your saved corrections were independently verified against available source evidence, "
            "and all required fields passed their verification checks.",
            "Retain the verified corrections and proceed with your standard internal review.",
        )
    if kind == "validated":
        return (
            "VALIDATED", GREEN, colors.HexColor("#EAF8F0"),
            "Confidence in every verified detail.",
            "All nine required fields passed the available verification checks "
            "without saved corrections.",
            "This invoice is ready for your standard financial review and internal processing.",
        )
    return (
        "NEEDS REVIEW", AMBER, colors.HexColor("#FFF5E5"),
        "Turn uncertainty into informed decisions.",
        "Some information requires attention before the invoice can be considered fully verified. "
        "Items may contain discrepancies or lack sufficient independent evidence.",
        "Review the flagged fields against the original invoice, correct any discrepancies, "
        "and repeat verification.",
    )


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(BORDER)
    canvas.line(17 * mm, 19 * mm, A4[0] - 17 * mm, 19 * mm)
    canvas.setFont(FONT, 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(17 * mm, 14 * mm, "InvoSight · Automated verification, not payment approval")
    canvas.drawRightString(A4[0] - 17 * mm, 14 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _table(rows, widths, *, header=False, compact=False, cell_colors=None):
    table = LongTable(rows, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    row_padding = 3 if compact else 5
    rules = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), row_padding),
        ("BOTTOMPADDING", (0, 0), (-1, -1), row_padding),
        ("LINEBELOW", (0, 0), (-1, -1), 0.35, BORDER),
        ("BOX", (0, 0), (-1, -1), 0.55, BORDER),
    ]
    if header:
        rules += [("BACKGROUND", (0, 0), (-1, 0), NAVY)]
    if cell_colors:
        rules += [("BACKGROUND", position, position, color)
                  for position, color in cell_colors.items()]
    table.setStyle(TableStyle(rules))
    return table


def build_verification_report(
    current_fields,
    original_fields,
    validation_details,
    changed_fields,
    invoice_file_name="invoice",
    *,
    public_reasons=None,
    report_id=None,
):
    """Return a ready-to-download PDF (bytes).

    Pass COMMITTED/saved values and validation of those same values, not draft
    text-input contents. A status can only be Corrected & Verified if each edit
    has an explicit source-confirmation rule in validation_details.
    """
    current_fields = current_fields or {}
    original_fields = original_fields or {}
    validation_details = validation_details or {}
    changed = set(changed_fields or ()) & FIELD_LABELS.keys()
    public_reasons = public_reasons or {}
    kind, field_statuses = evaluate_report(validation_details, changed)
    label, accent, accent_background, tagline, intro, recommendation = _report_theme(kind)
    verified = sum(status == "Valid" for status in field_statuses.values())
    flagged = len(FIELD_LABELS) - verified
    reference = report_id or f"ISR-{uuid4().hex[:10].upper()}"
    timestamp = datetime.now().strftime("%d %b %Y · %H:%M")

    heading = ParagraphStyle(
        "ReportTitle", fontName=FONT_BOLD, fontSize=21, leading=27,
        textColor=NAVY, spaceAfter=2,
    )
    subheading = ParagraphStyle(
        "Subheading", fontName=FONT, fontSize=9, leading=14,
        textColor=MUTED,
    )
    section = ParagraphStyle(
        "SectionHeading", fontName=FONT_BOLD, fontSize=11, leading=16,
        textColor=NAVY, spaceBefore=8, spaceAfter=5,
    )
    body = ParagraphStyle(
        "Body", fontName=FONT, fontSize=9, leading=13, textColor=NAVY,
    )
    small = ParagraphStyle(
        "Small", fontName=FONT, fontSize=8.0, leading=10.3,
        textColor=MUTED,
    )
    cell = ParagraphStyle(
        "Cell", fontName=FONT, fontSize=8.25, leading=10.7,
        textColor=NAVY,
    )
    cell_bold = ParagraphStyle(
        "CellBold", parent=cell, fontName=FONT_BOLD,
    )
    white_head = ParagraphStyle(
        "HeaderCell", parent=cell_bold, textColor=WHITE,
    )
    badge = ParagraphStyle(
        "Badge", fontName=FONT_BOLD, fontSize=12.5, leading=18,
        alignment=TA_CENTER, textColor=accent,
    )
    note = ParagraphStyle(
        "Note", parent=small, alignment=TA_LEFT,
    )
    tagline_style = ParagraphStyle(
        "Tagline", fontName=FONT_BOLD, fontSize=11.3, leading=16,
        textColor=NAVY, alignment=TA_CENTER, spaceAfter=4,
    )
    banner_body = ParagraphStyle(
        "BannerBody", fontName=FONT, fontSize=8.7, leading=13,
        textColor=NAVY, alignment=TA_CENTER,
    )
    number_style = ParagraphStyle(
        "IndicatorNumber", fontName=FONT_BOLD, fontSize=17, leading=23,
        textColor=NAVY, alignment=TA_CENTER,
    )
    indicator_label = ParagraphStyle(
        "IndicatorLabel", fontName=FONT_BOLD, fontSize=7.5, leading=11,
        textColor=MUTED, alignment=TA_CENTER,
    )
    result_ok = ParagraphStyle("ResultOk", parent=cell_bold, textColor=GREEN)
    result_review = ParagraphStyle("ResultReview", parent=cell_bold, textColor=AMBER)

    content = BytesIO()
    doc = SimpleDocTemplate(
        content, pagesize=A4, rightMargin=17 * mm, leftMargin=17 * mm,
        topMargin=14 * mm, bottomMargin=23 * mm,
        title=f"InvoSight verification · {_value(current_fields.get('invoice_number'))}",
        author="InvoSight",
    )
    story = []
    story.append(_paragraph("InvoSight", heading))
    story.append(_paragraph("INVOICE VERIFICATION REPORT", subheading))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1.6, color=BLUE))
    story.append(Spacer(1, 8))

    overview = [
        [_paragraph("Report ID", cell_bold), _paragraph(reference, cell),
         _paragraph("Generated", cell_bold), _paragraph(timestamp, cell)],
        [_paragraph("Invoice No.", cell_bold),
         _paragraph(current_fields.get("invoice_number"), cell),
         "", ""],
    ]
    overview_table = _table(overview, [29 * mm, 52 * mm, 29 * mm, 66 * mm])
    overview_table.setStyle(TableStyle([("SPAN", (1, 1), (3, 1))]))
    story.append(overview_table)
    story.append(Spacer(1, 7))

    status_banner = Table([[
        [_paragraph(label, badge), Spacer(1, 4), _paragraph(tagline, tagline_style),
         _paragraph(intro, banner_body)]
    ]], colWidths=[176 * mm])
    status_banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), accent_background),
        ("BOX", (0, 0), (-1, -1), 1, accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(status_banner)
    story.append(Spacer(1, 7))

    story.append(_paragraph("VERIFICATION AT A GLANCE", section))
    indicators = [
        [_paragraph("REQUIRED FIELDS", indicator_label), _paragraph("VERIFIED", indicator_label),
         _paragraph("REQUIRING REVIEW", indicator_label), _paragraph("SAVED EDITS", indicator_label)],
        [_paragraph(str(len(FIELD_LABELS)), number_style),
         _paragraph(str(verified), number_style), _paragraph(str(flagged), number_style),
         _paragraph(str(len(changed)), number_style)],
    ]
    ind_table = Table(indicators, colWidths=[44 * mm] * 4)
    ind_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PALE),
        ("BOX", (0, 0), (-1, -1), 0.55, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(ind_table)

    story.append(_paragraph("FIELD-BY-FIELD RESULTS", section))
    result_rows = [[_paragraph(t, white_head) for t in
                    ("Field", "Saved value", "Status", "Verification note")]]
    for key, field_name in FIELD_LABELS.items():
        status = field_statuses[key]
        detail = validation_details.get(key, {})
        if key in changed and not _edit_has_source_evidence(key, detail):
            reason = ("Saved edit still requires independent confirmation "
                      "against the source invoice.")
        elif status != "Valid":
            reason = public_reasons.get(key) or (
                "Review this value against the original invoice; independent verification "
                "is incomplete or identified a discrepancy."
            )
        elif key in changed:
            reason = "Saved correction confirmed against the available source evidence."
        else:
            reason = _verified_note(key, detail, public_reasons.get(key))
        result_rows.append([
            _paragraph(field_name, cell_bold),
            _paragraph(current_fields.get(key), cell),
            _paragraph(
                "Modified & Verified" if status == "Valid" and key in changed
                else "Verified" if status == "Valid"
                else "Pending Verification" if key in changed
                else "Review",
                result_ok if status == "Valid" else result_review,
            ),
            _paragraph(reason, small),
        ])
    story.append(_table(result_rows, [35 * mm, 36 * mm, 37 * mm, 68 * mm], header=True, compact=True))

    follow_up = []
    if len(changed) < 3 and (changed or flagged):
        follow_up.append(_paragraph("CORRECTIONS & NEXT STEPS" if changed else "REVIEW & NEXT STEPS", section))
    if changed:
        edits = [[_paragraph(t, white_head) for t in
                  ("Field", "Originally extracted", "Saved value")]]
        for key, field_name in FIELD_LABELS.items():
            if key in changed:
                edits.append([
                    _paragraph(field_name, cell_bold),
                    _paragraph(original_fields.get(key), cell),
                    _paragraph(current_fields.get(key), cell),
                ])
        follow_up.extend([
            _paragraph(
                "VERIFIED CORRECTIONS" if kind == "corrected_verified"
                else "SAVED CORRECTIONS · REVIEW STATUS", section,
            ),
            _table(edits, [49 * mm, 63.5 * mm, 63.5 * mm], header=True, compact=True),
            Spacer(1, 6),
        ])

    if flagged:
        review_groups = {}
        for key, field_name in FIELD_LABELS.items():
            if field_statuses[key] != "Needs Review":
                continue
            detail = validation_details.get(key, {}) or {}
            category, action = _review_priority(key, detail, changed)
            review_groups.setdefault((category, action), []).append(field_name)

        follow_up.append(_paragraph("REVIEW PRIORITIES", section))
        for (category, action), field_names in review_groups.items():
            names = ", ".join(field_names)
            follow_up.append(Paragraph(
                f"<b>{xml_escape(category)}:</b> {xml_escape(names)}. {xml_escape(action)}",
                body,
            ))
            follow_up.append(Spacer(1, 4))

    confirmed_corrections = [
        field_name for key, field_name in FIELD_LABELS.items()
        if key in changed and _edit_has_source_evidence(key, validation_details.get(key, {}))
    ]
    if confirmed_corrections and flagged:
        follow_up.append(_paragraph("VERIFIED CORRECTIONS", section))
        follow_up.append(_paragraph(
            ", ".join(confirmed_corrections) + ". These saved corrections passed independent source confirmation.",
            body,
        ))
        follow_up.append(Spacer(1, 4))

    recommendation_panel = Table([[
        [_paragraph("RECOMMENDED NEXT STEP", section),
         _paragraph(recommendation, body)]
    ]], colWidths=[176 * mm])
    recommendation_panel.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), accent_background),
        ("LINEBEFORE", (0, 0), (0, 0), 3, accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    follow_up.extend([Spacer(1, 5), recommendation_panel])

    if changed and len(changed) >= 3:
        story.append(PageBreak())
        story.append(_paragraph("CORRECTION & REVIEW DETAILS", heading))
        story.append(_paragraph(
            "Saved changes and any outstanding verification steps", subheading,
        ))
        story.append(Spacer(1, 7))

    story.append(KeepTogether(follow_up))
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    pdf_bytes = content.getvalue()
    content.close()
    return pdf_bytes
