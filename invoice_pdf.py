"""Server-side PDF generation for invoices — two copies (Party + Office) on one A4 page.

Uses reportlab (pure Python, no system packages needed) so it installs cleanly on
Railway via pip alone, unlike HTML-to-PDF converters that need native libraries.
"""
import io

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

INK = colors.HexColor("#1C2541")
INK_SOFT = colors.HexColor("#5B6378")
BORDER = colors.HexColor("#D9DCD0")
ACCENT = colors.HexColor("#A8722E")
FAINT = colors.HexColor("#8C93A3")

STATUS_LABEL = {"paid": "PAID", "unpaid": "UNPAID", "partial": "PARTIAL"}


def _wrap(text, font, size, max_width):
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if stringWidth(trial, font, size) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _draw_copy(c, page_w, y_bottom, y_top, inv, settings, label):
    hide_pricing = bool(getattr(inv, "hide_pricing", False))
    margin = 26
    x0, x1 = margin, page_w - margin
    y = y_top - 20

    c.setFont("Helvetica-Bold", 13)
    c.setFillColor(INK)
    c.drawString(x0, y, settings.firm_name or "My Firm")

    c.setFont("Helvetica", 7.5)
    c.setFillColor(INK_SOFT)
    y2 = y - 11
    if settings.address:
        for line in _wrap(settings.address, "Helvetica", 7.5, 260)[:2]:
            c.drawString(x0, y2, line)
            y2 -= 9
    meta_bits = []
    if settings.phone:
        meta_bits.append(f"Ph: {settings.phone}")
    if settings.gstin and getattr(settings, "show_gstin_on_invoice", True):
        meta_bits.append(f"GSTIN: {settings.gstin}")
    if meta_bits:
        c.drawString(x0, y2, "   ".join(meta_bits))
        y2 -= 9

    c.setFont("Helvetica-Bold", 8.5)
    c.setFillColor(ACCENT)
    c.drawRightString(x1, y, label)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(INK_SOFT)
    c.drawRightString(x1, y - 12, "Challan No." if hide_pricing else "Invoice No.")
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(INK)
    c.drawRightString(x1, y - 23, inv.invoice_no)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(INK_SOFT)
    c.drawRightString(x1, y - 34, f"Date: {inv.date}")

    y_head_bottom = min(y2, y - 44) - 6
    c.setStrokeColor(INK)
    c.setLineWidth(1.1)
    c.line(x0, y_head_bottom, x1, y_head_bottom)

    y = y_head_bottom - 12
    c.setFont("Helvetica", 6.5)
    c.setFillColor(INK_SOFT)
    c.drawString(x0, y, "DELIVER TO" if hide_pricing else "BILL TO")
    y -= 10
    c.setFont("Helvetica-Bold", 8.5)
    c.setFillColor(INK)
    c.drawString(x0, y, inv.customer.name)
    c.setFont("Helvetica-Bold", 7.5)
    c.setFillColor(ACCENT)
    c.drawRightString(x1, y, "CHALLAN — RATE TO FOLLOW" if hide_pricing else STATUS_LABEL.get(inv.payment_status, ""))
    y -= 9
    c.setFont("Helvetica", 7)
    c.setFillColor(INK_SOFT)
    if inv.customer.address:
        c.drawString(x0, y, inv.customer.address[:75])
        y -= 9
    extra = []
    if inv.customer.phone:
        extra.append(f"Ph: {inv.customer.phone}")
    if inv.customer.gstin and getattr(settings, "show_gstin_on_invoice", True):
        extra.append(f"GSTIN: {inv.customer.gstin}")
    if extra:
        c.drawString(x0, y, "   ".join(extra))
        y -= 9

    y -= 4
    table_top = y
    col_rate_x = x0 + (x1 - x0) * 0.68
    col_amt_x = x0 + (x1 - x0) * 0.84

    c.setFillColor(INK)
    c.rect(x0, table_top - 12, x1 - x0, 12, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 6.8)
    c.drawString(x0 + 3, table_top - 9, "DESCRIPTION")
    if hide_pricing:
        c.drawRightString(x1 - 3, table_top - 9, "QTY")
    else:
        c.drawRightString(col_rate_x - 4, table_top - 9, "QTY")
        c.drawRightString(col_amt_x - 4, table_top - 9, "RATE")
        c.drawRightString(x1 - 3, table_top - 9, "AMOUNT")

    row_h = 11.5
    y = table_top - 12
    reserved_bottom = y_bottom + (36 if hide_pricing else 78)
    c.setFont("Helvetica", 7)
    for li in inv.items:
        if y - row_h <= reserved_bottom:
            c.setFont("Helvetica-Oblique", 6.5)
            c.setFillColor(INK_SOFT)
            c.drawString(x0 + 3, y - 9, "... (see full document in system for more lines)")
            y -= row_h
            break
        y -= row_h
        c.setStrokeColor(BORDER)
        c.setLineWidth(0.4)
        c.line(x0, y, x1, y)
        c.setFont("Helvetica", 7)
        c.setFillColor(INK)
        c.drawString(x0 + 3, y + 3, li.description[:44])
        if hide_pricing:
            c.drawRightString(x1 - 3, y + 3, f"{li.qty:g} {li.unit}")
        else:
            c.drawRightString(col_rate_x - 4, y + 3, f"{li.qty:g} {li.unit}")
            c.drawRightString(col_amt_x - 4, y + 3, f"{li.rate:,.2f}")
            c.drawRightString(x1 - 3, y + 3, f"{li.line_total:,.2f}")

    c.setStrokeColor(INK)
    c.setLineWidth(0.8)
    c.line(x0, y, x1, y)

    if hide_pricing:
        c.setFont("Helvetica-Oblique", 6.8)
        c.setFillColor(INK_SOFT)
        c.drawString(x0, y - 11, "Rate & total to be billed later.")
        if inv.notes:
            c.drawString(x0, y_bottom + 8, ("Note: " + inv.notes)[:95])
        return

    ty = [y - 11]

    def total_row(lbl, val, bold=False, big=False):
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 8.5 if big else 7.2)
        c.setFillColor(INK)
        c.drawString(col_rate_x - 60, ty[0], lbl)
        c.drawRightString(x1 - 3, ty[0], f"Rs. {val:,.2f}")
        ty[0] -= 12.5 if big else 10.5

    total_row("Subtotal", inv.subtotal)
    if inv.discount_amount:
        total_row("Discount", -inv.discount_amount)
    if inv.other_charges:
        total_row("Other Charges", inv.other_charges)
    c.setStrokeColor(INK)
    c.setLineWidth(0.6)
    c.line(col_rate_x - 62, ty[0] + 9, x1, ty[0] + 9)
    total_row("Grand Total", inv.grand_total, bold=True, big=True)
    if inv.payment_status == "partial":
        total_row("Received", inv.amount_received)
        total_row("Balance Due", inv.grand_total - inv.amount_received)

    if inv.notes:
        c.setFont("Helvetica-Oblique", 6.5)
        c.setFillColor(INK_SOFT)
        c.drawString(x0, y_bottom + 8, ("Note: " + inv.notes)[:95])


def build_invoice_pdf(inv, settings, t=None, copies="both"):
    """Returns a BytesIO with an A4 PDF of the invoice (or delivery challan, if
    inv.hide_pricing is set).

    `copies` picks what goes on the page: "both" puts the party and office copies
    on one sheet with a cut line between them, while "party" or "office" gives
    that single copy the whole page — a lone half-page with white space under it
    would only look like something went wrong.
    """
    buf = io.BytesIO()
    page_w, page_h = A4
    c = canvas.Canvas(buf, pagesize=A4)

    if copies == "party":
        _draw_copy(c, page_w, 0, page_h, inv, settings, "PARTY COPY")
    elif copies == "office":
        _draw_copy(c, page_w, 0, page_h, inv, settings, "OFFICE COPY")
    else:
        half = page_h / 2
        _draw_copy(c, page_w, half, page_h, inv, settings, "PARTY COPY")
        _draw_copy(c, page_w, 0, half, inv, settings, "OFFICE COPY")

        c.setDash(4, 3)
        c.setStrokeColor(FAINT)
        c.setLineWidth(0.7)
        c.line(20, half, page_w - 20, half)
        c.setDash()
        c.setFont("Helvetica", 6.5)
        c.setFillColor(FAINT)
        c.drawCentredString(page_w / 2, half + 3,
                            "- - - - - - - - - -  CUT HERE  - - - - - - - - - -")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf
