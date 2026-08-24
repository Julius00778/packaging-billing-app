"""
PO automation module for packaging-billing-app.

Flow: PO aata hai -> parse -> party ke product map se size match -> operator review
-> confirm -> dispatch list.

Design notes
------------
* Ye module models.py ko chhuta nahi hai. Saari nayi tables yahin define hain, aur
  party-level config ek alag table (party_po_config) me hai — isliye Customer model
  waisa ka waisa rehta hai.
* Images DB me store hote hain (LargeBinary), disk pe nahi. Railway ka filesystem
  ephemeral hai — har redeploy pe disk wipe ho jaata hai, toh disk pe rakhi images
  gayab ho jaatin. Store karne se pehle har image server pe resize + JPEG me
  re-encode hoti hai, aur ek alag chhota thumbnail bhi banta hai — isliye DB
  utna nahi badhta (details `_compress_image` ke paas).
* Matching filename ya folder pe nahi, canonical_key pe hoti hai. Filename kabhi bhi
  source of truth nahi banna chahiye.
"""

import io
import re
from datetime import datetime, date

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort, Response
)
from flask_login import login_required, current_user

from models import db, Customer, Item

try:
    from PIL import Image, ImageOps
except ImportError:            # Pillow na ho toh module chale, bas compress na ho
    Image = ImageOps = None

po_bp = Blueprint("po", __name__, url_prefix="/po")

# Phone se aane wali photo 4-8 MB ki hoti hai. Reject karne ke bajaye accept karke
# server pe chhoti kar dete hain — operator ko resize ka jhanjhat nahi.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024   # app.config["MAX_CONTENT_LENGTH"] ke barabar
MAX_IMAGE_BYTES = 2 * 1024 * 1024    # Pillow na ho toh yahi purana hard cap lagta hai

# Ye numbers hi tay karte hain ki DB kitna badhega:
# ~120 KB main + ~12 KB thumb  ⇒  500 mappings ≈ 65 MB (pehle ~150 MB).
IMAGE_MAX_DIM = 900          # product photo ka lamba side
IMAGE_TARGET_BYTES = 160 * 1024
THUMB_MAX_DIM = 200          # list/review screens pe dikhne wala chhota square
THUMB_TARGET_BYTES = 20 * 1024
SCAN_MAX_DIM = 1800          # PO scan — isme text padhna hota hai, isliye bada
SCAN_TARGET_BYTES = 500 * 1024

PO_STATUSES = ("pending", "confirmed", "rejected", "dispatched")


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class PartyPOConfig(db.Model):
    """Per-party PO settings. Alag table isliye taaki models.py untouched rahe."""
    __tablename__ = "party_po_config"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), unique=True, nullable=False)
    size_unit = db.Column(db.String(10), default="inch")  # 'inch' | 'mm' | 'cm'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer")

    @staticmethod
    def unit_for(customer_id):
        cfg = PartyPOConfig.query.filter_by(customer_id=customer_id).first()
        return (cfg.size_unit if cfg else "inch") or "inch"

    @staticmethod
    def set_unit(customer_id, unit):
        cfg = PartyPOConfig.query.filter_by(customer_id=customer_id).first()
        if not cfg:
            cfg = PartyPOConfig(customer_id=customer_id)
            db.session.add(cfg)
        cfg.size_unit = unit if unit in ("inch", "mm", "cm") else "inch"
        return cfg


class PartyProductMap(db.Model):
    """Ek party ke liye: 'ye size' ka matlab 'ye product + ye photo'.

    Ek hi (customer_id, canonical_key) pe multiple rows allowed hain — kyunki ek hi
    size ke 2mm aur 4mm dono variants ho sakte hain. Isliye unique constraint
    jaan-boojh ke nahi lagaya gaya.
    """
    __tablename__ = "party_product_map"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    raw_size_text = db.Column(db.String(80), default="")   # jaisa PO me pehli baar aaya
    canonical_key = db.Column(db.String(40), nullable=False, index=True)
    label = db.Column(db.String(200), nullable=False)      # operator ko dikhne wala naam
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=True)
    image_data = db.Column(db.LargeBinary, nullable=True)
    image_mime = db.Column(db.String(60), default="")
    # Alag chhota thumbnail: review screen pe 20 lines ek saath khulti hain, wahan
    # 20 × 120 KB load karne ka koi matlab nahi jab 96px ka square dikhana hai.
    image_thumb = db.Column(db.LargeBinary, nullable=True)
    times_used = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))

    customer = db.relationship("Customer")
    item = db.relationship("Item")

    @property
    def has_image(self):
        return bool(self.image_data)


class PurchaseOrder(db.Model):
    __tablename__ = "purchase_order"
    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(60), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    po_date = db.Column(db.String(10), default="")
    status = db.Column(db.String(16), default="pending", index=True)
    source = db.Column(db.String(20), default="upload")   # upload/email/whatsapp/manual
    raw_text = db.Column(db.Text, default="")
    scan_data = db.Column(db.LargeBinary, nullable=True)
    scan_mime = db.Column(db.String(60), default="")
    scan_name = db.Column(db.String(200), default="")
    note = db.Column(db.String(500), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    confirmed_at = db.Column(db.DateTime, nullable=True)
    confirmed_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    dispatched_at = db.Column(db.DateTime, nullable=True)

    customer = db.relationship("Customer")
    lines = db.relationship("POLine", backref="po", cascade="all, delete-orphan",
                            order_by="POLine.line_no")

    # Duplicate PO guard — wahi PO email se bhi aa sakta hai aur WhatsApp se bhi.
    __table_args__ = (db.UniqueConstraint("customer_id", "po_number", name="uq_po_customer_number"),)

    @property
    def has_scan(self):
        return bool(self.scan_data)

    @property
    def unresolved_count(self):
        return sum(1 for l in self.lines if not l.map_id)


class POLine(db.Model):
    __tablename__ = "po_line"
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.Integer, db.ForeignKey("purchase_order.id"), nullable=False)
    line_no = db.Column(db.Integer, default=0)
    raw_text = db.Column(db.String(400), default="")
    raw_size_text = db.Column(db.String(80), default="")
    canonical_key = db.Column(db.String(40), default="", index=True)
    unit_source = db.Column(db.String(12), default="")   # explicit/party/magnitude
    qty = db.Column(db.Float, default=0.0)
    qty_unit = db.Column(db.String(20), default="pcs")
    match_status = db.Column(db.String(12), default="none")  # exact/multiple/none/manual
    map_id = db.Column(db.Integer, db.ForeignKey("party_product_map.id"), nullable=True)

    mapping = db.relationship("PartyProductMap")


# --------------------------------------------------------------------------
# Size normalisation
# --------------------------------------------------------------------------

# Size ke beech me bhi unit aa sakta hai: 12" x 18", 300 mm x 450. Isliye pehle
# number ke baad ek optional unit token allow kiya gaya hai.
INLINE_UNIT = r"""(?:\s*(?P<u1>mm|cm|inches|inch|in\b|"|”|''))?"""
SIZE_RE = re.compile(
    r"(?P<w>\d+(?:\.\d+)?)" + INLINE_UNIT + r"\s*[x×*X]\s*(?P<h>\d+(?:\.\d+)?)"
)

QTY_KEYWORD_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(pcs|pc|nos|no\.?|pieces|piece|kg|box|boxes|bundle|bundles|"
    r"roll|rolls|sheet|sheets|set|sets|dozen|mtr|ltr)\b",
    re.I,
)
QTY_LABEL_RE = re.compile(r"(?:qty|quantity|qnty)\s*[:\-]?\s*(\d+(?:\.\d+)?)", re.I)
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

UNIT_ALIASES = {
    "pc": "pcs", "pcs": "pcs", "piece": "pcs", "pieces": "pcs",
    "no": "pcs", "no.": "pcs", "nos": "pcs",
    "box": "box", "boxes": "box",
    "bundle": "bundle", "bundles": "bundle",
    "roll": "roll", "rolls": "roll",
    "sheet": "sheet", "sheets": "sheet",
    "set": "set", "sets": "set",
    "kg": "kg", "dozen": "dozen", "mtr": "mtr", "ltr": "ltr",
}


def detect_size_unit(text, match, party_default="inch"):
    """Size ke turant baad wala unit token dekho — poori line me nahi.

    Ye important hai: "2mm foam 12x18" me agar poori line me 'mm' dhoondhoge toh
    12x18 ko galti se mm maan loge, jabki wo inch hai aur 'mm' thickness ka tha.
    """
    # 1) Size ke andar likha unit — "12\" x 18\"", "300 mm x 450"
    inline = (match.groupdict().get("u1") or "").strip().lower()
    if inline:
        if inline.startswith("mm"):
            return "mm", "explicit"
        if inline.startswith("cm"):
            return "cm", "explicit"
        return "inch", "explicit"

    # 2) Size ke turant baad wala unit — poori line me nahi.
    tail = text[match.end(): match.end() + 10].lower()
    if re.match(r"\s*(mm|milli)", tail):
        return "mm", "explicit"
    if re.match(r"\s*cm", tail):
        return "cm", "explicit"
    if re.match(r"""\s*(inches|inch|in\b|"|”|'')""", tail):
        return "inch", "explicit"

    # 3) Party ka apna convention
    if party_default in ("inch", "mm", "cm"):
        return party_default, "party"

    # 4) Aakhri sahara: bade numbers aam taur pe mm hote hain. Ye heuristic
    #    galat ho sakta hai, isliye unit_source='magnitude' operator ko flag dikhata hai.
    w, h = float(match.group("w")), float(match.group("h"))
    return ("mm", "magnitude") if max(w, h) > 100 else ("inch", "magnitude")


def canonical_size(width, height, unit):
    """Dono dimensions ko mm me badlo aur sort karo, taaki 12x18 == 18x12."""
    if unit == "inch":
        mm = [width * 25.4, height * 25.4]
    elif unit == "cm":
        mm = [width * 10.0, height * 10.0]
    else:
        mm = [width, height]
    a, b = sorted(round(v, 1) for v in mm)
    return f"{a:.1f}x{b:.1f}"


def parse_qty(rest_text):
    """Line me se (qty, unit) nikalo — size hata dene ke baad."""
    m = QTY_LABEL_RE.search(rest_text)
    if m:
        return float(m.group(1)), "pcs"
    m = QTY_KEYWORD_RE.search(rest_text)
    if m:
        unit = UNIT_ALIASES.get(m.group(2).lower().rstrip("."), "pcs")
        return float(m.group(1)), unit
    numbers = NUMBER_RE.findall(rest_text)
    if numbers:
        return float(numbers[-1]), "pcs"
    return 0.0, "pcs"


def parse_po_text(text, party_default_unit="inch"):
    """PO ka text lo, har line se size + qty nikalo.

    Sirf wahi lines lautati hai jinme ek size pattern mila. OCR laganae ke baad
    bhi yahi function use hoga — bas input badlega.
    """
    out = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = SIZE_RE.search(line)
        if not m:
            continue
        unit, unit_source = detect_size_unit(line, m, party_default_unit)
        key = canonical_size(float(m.group("w")), float(m.group("h")), unit)
        tail = line[m.end():]
        # Teesra dimension (thickness) — "12x18x2mm". Ise hata do warna 2 ko qty
        # samajh liya jayega.
        tail = re.sub(r"""^\s*(?:mm|cm|inch(?:es)?|in\b|"|”|'')?\s*[x×*X]\s*\d+(?:\.\d+)?\s*(?:mm|cm)?""",
                      " ", tail, count=1, flags=re.I)
        # Size ka apna unit word bhi qty parsing se hata do
        tail = re.sub(r"""^\s*(?:mm|cm|inches|inch|in\b|"|”|'')""", " ", tail, count=1, flags=re.I)
        rest = line[:m.start()] + " " + tail
        qty, qty_unit = parse_qty(rest)
        out.append({
            "raw_text": line,
            "raw_size_text": m.group(0),
            "canonical_key": key,
            "unit_source": unit_source,
            "qty": qty,
            "qty_unit": qty_unit,
        })
    return out


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def candidates_for(customer_id, canonical_key):
    if not canonical_key:
        return []
    return (PartyProductMap.query
            .filter_by(customer_id=customer_id, canonical_key=canonical_key)
            .order_by(PartyProductMap.times_used.desc(), PartyProductMap.label)
            .all())


def match_line(customer_id, canonical_key):
    """(status, chosen_map_or_None) lautata hai."""
    rows = candidates_for(customer_id, canonical_key)
    if len(rows) == 1:
        return "exact", rows[0]
    if len(rows) > 1:
        return "multiple", None
    return "none", None


def party_is_mixed(customer_id):
    """True agar is party ke kisi ek size ke ek se zyada product variants hain.

    Manually set karne ki zaroorat nahi — data se khud pata chal jaata hai.
    """
    rows = (db.session.query(PartyProductMap.canonical_key, db.func.count(PartyProductMap.id))
            .filter(PartyProductMap.customer_id == customer_id)
            .group_by(PartyProductMap.canonical_key).all())
    return any(c > 1 for _, c in rows)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _compress_image(data, max_dim, target_bytes):
    """Image ko resize + JPEG me re-encode karke (bytes, mime) lautata hai.

    Kyun: photo seedha DB me jaati hai. Phone ki 5 MB JPEG aur 900px ki 120 KB
    JPEG dono me operator ko ek hi cheez dikhti hai (96px ka square), par DB me
    farq 40 guna ka hai.

    Pillow na ho, ya file image na ho (PDF), ya kuch bhi phate — toh None lautata
    hai aur caller original bytes rakh leta hai.
    """
    if Image is None or not data:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)          # phone ki rotated photo seedhi
        if img.mode in ("RGBA", "LA", "P"):         # transparency ko safed background
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)

        # Quality tab tak girao jab tak target ke andar na aa jaye. 55 se neeche
        # nahi jaate — usse product pehchanna mushkil ho jaata hai.
        for quality in (82, 72, 62, 55):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
            out = buf.getvalue()
            if len(out) <= target_bytes or quality == 55:
                return out, "image/jpeg"
    except Exception:
        return None
    return None


def _read_upload(file_storage, max_dim=IMAGE_MAX_DIM, target_bytes=IMAGE_TARGET_BYTES):
    """(bytes, mime, filename) — compress karke. Kuch na mile toh (None, '', '').

    Image ho toh resize+re-encode hoti hai, isliye 8 MB tak ki file bhi chalti hai.
    PDF/doc jaisi cheez compress nahi hoti — us par purana 2 MB cap lagta hai.
    """
    if not file_storage or not file_storage.filename:
        return None, "", ""
    data = file_storage.read()
    if not data:
        return None, "", ""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("file_too_big")

    shrunk = _compress_image(data, max_dim, target_bytes)
    if shrunk:
        return shrunk[0], shrunk[1], file_storage.filename

    # Compress nahi ho paya (PDF, ya Pillow missing) — original rakho, purana cap.
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("file_too_big")
    return data, (file_storage.mimetype or "application/octet-stream"), file_storage.filename


def _apply_match(line, customer_id):
    status, chosen = match_line(customer_id, line.canonical_key)
    line.match_status = status
    line.map_id = chosen.id if chosen else None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@po_bp.route("/")
@login_required
def po_list():
    status = request.args.get("status", "pending")
    q = PurchaseOrder.query
    if status in PO_STATUSES:
        q = q.filter_by(status=status)
    rows = q.order_by(PurchaseOrder.created_at.desc()).all()
    counts = {s: PurchaseOrder.query.filter_by(status=s).count() for s in PO_STATUSES}
    return render_template("po_list.html", pos=rows, status=status, counts=counts)


@po_bp.route("/new", methods=["GET", "POST"])
@login_required
def po_new():
    customers = Customer.query.order_by(Customer.name).all()
    if request.method == "POST":
        customer_id = request.form.get("customer_id") or ""
        po_number = request.form.get("po_number", "").strip()
        if not customer_id.isdigit() or not po_number:
            flash("Party aur PO number dono chahiye.", "error")
            return render_template("po_new.html", customers=customers, today=date.today().isoformat())
        customer_id = int(customer_id)

        dupe = PurchaseOrder.query.filter_by(customer_id=customer_id, po_number=po_number).first()
        if dupe:
            flash(f"Ye PO pehle se hai ({po_number}) — wahi khol raha hoon.", "error")
            return redirect(url_for("po.po_review", po_id=dupe.id))

        unit = request.form.get("size_unit", "inch")
        PartyPOConfig.set_unit(customer_id, unit)

        try:
            scan_data, scan_mime, scan_name = _read_upload(
                request.files.get("scan"), SCAN_MAX_DIM, SCAN_TARGET_BYTES)
        except ValueError:
            flash("Scan bahut bada hai (8 MB se zyada) — chhota karke dobara try karo.", "error")
            return render_template("po_new.html", customers=customers, today=date.today().isoformat())

        raw_text = request.form.get("raw_text", "")
        parsed = parse_po_text(raw_text, unit)
        if not parsed:
            flash("Text me koi size line nahi mili (jaise '12x18 - 500 pcs').", "error")
            return render_template("po_new.html", customers=customers, today=date.today().isoformat())

        po = PurchaseOrder(
            po_number=po_number, customer_id=customer_id,
            po_date=request.form.get("po_date") or date.today().isoformat(),
            source=request.form.get("source", "upload"),
            raw_text=raw_text, scan_data=scan_data, scan_mime=scan_mime, scan_name=scan_name,
            note=request.form.get("note", "").strip(),
            created_by=current_user.id, status="pending",
        )
        db.session.add(po)
        db.session.flush()

        for i, p in enumerate(parsed, start=1):
            line = POLine(po_id=po.id, line_no=i, **p)
            _apply_match(line, customer_id)
            db.session.add(line)

        db.session.commit()
        flash(f"PO {po_number} padh liya — {len(parsed)} line(s). Ab review karo.", "success")
        return redirect(url_for("po.po_review", po_id=po.id))

    return render_template("po_new.html", customers=customers, today=date.today().isoformat())


@po_bp.route("/<int:po_id>")
@login_required
def po_review(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    line_view = []
    for line in po.lines:
        line_view.append({
            "line": line,
            "candidates": candidates_for(po.customer_id, line.canonical_key),
        })
    return render_template(
        "po_review.html", po=po, line_view=line_view,
        items=Item.query.order_by(Item.name).all(),
        mixed=party_is_mixed(po.customer_id),
        party_unit=PartyPOConfig.unit_for(po.customer_id),
    )


@po_bp.route("/<int:po_id>/line/<int:line_id>/choose", methods=["POST"])
@login_required
def po_line_choose(po_id, line_id):
    """Operator ne maujuda mapping me se ek chuna."""
    line = POLine.query.filter_by(id=line_id, po_id=po_id).first_or_404()
    map_id = request.form.get("map_id") or ""
    if not map_id.isdigit():
        flash("Koi option select nahi hua.", "error")
        return redirect(url_for("po.po_review", po_id=po_id))
    m = PartyProductMap.query.get(int(map_id))
    if not m or m.customer_id != line.po.customer_id:
        abort(404)
    line.map_id = m.id
    line.match_status = "manual"
    db.session.commit()
    return redirect(url_for("po.po_review", po_id=po_id))


@po_bp.route("/<int:po_id>/line/<int:line_id>/map", methods=["POST"])
@login_required
def po_line_map(po_id, line_id):
    """Naya mapping banao — size ko product + photo se jodo. Agli baar auto match hoga."""
    line = POLine.query.filter_by(id=line_id, po_id=po_id).first_or_404()
    label = request.form.get("label", "").strip()
    if not label:
        flash("Product ka naam likhna zaroori hai.", "error")
        return redirect(url_for("po.po_review", po_id=po_id))

    try:
        img_data, img_mime, _ = _read_upload(request.files.get("image"))
    except ValueError:
        flash("Photo bahut badi hai (8 MB se zyada) — chhoti karke dobara try karo.", "error")
        return redirect(url_for("po.po_review", po_id=po_id))
    thumb = _compress_image(img_data, THUMB_MAX_DIM, THUMB_TARGET_BYTES) if img_data else None

    item_id = request.form.get("item_id") or ""
    m = PartyProductMap(
        customer_id=line.po.customer_id,
        raw_size_text=line.raw_size_text,
        canonical_key=line.canonical_key,
        label=label,
        item_id=int(item_id) if item_id.isdigit() else None,
        image_data=img_data, image_mime=img_mime,
        image_thumb=thumb[0] if thumb else None,
        created_by=current_user.id,
    )
    db.session.add(m)
    db.session.flush()
    line.map_id = m.id
    line.match_status = "manual"
    db.session.commit()
    flash(f"'{label}' map ho gaya — is party ke liye ye size ab yaad rahega.", "success")
    return redirect(url_for("po.po_review", po_id=po_id))


@po_bp.route("/<int:po_id>/confirm", methods=["POST"])
@login_required
def po_confirm(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    if po.status != "pending":
        flash("Ye PO pehle hi process ho chuka hai.", "error")
        return redirect(url_for("po.po_review", po_id=po.id))
    if po.unresolved_count:
        flash(f"{po.unresolved_count} line abhi map nahi hui — pehle wo poori karo.", "error")
        return redirect(url_for("po.po_review", po_id=po.id))

    for line in po.lines:
        if line.mapping:
            line.mapping.times_used = (line.mapping.times_used or 0) + 1
    po.status = "confirmed"
    po.confirmed_at = datetime.utcnow()
    po.confirmed_by = current_user.id
    db.session.commit()
    flash(f"PO {po.po_number} confirm ho gaya — dispatch list me chala gaya.", "success")
    return redirect(url_for("po.po_dispatch"))


@po_bp.route("/<int:po_id>/reject", methods=["POST"])
@login_required
def po_reject(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    po.status = "rejected"
    po.note = (request.form.get("note", "").strip() or po.note)
    db.session.commit()
    flash(f"PO {po.po_number} reject kar diya.", "success")
    return redirect(url_for("po.po_list"))


@po_bp.route("/dispatch")
@login_required
def po_dispatch():
    rows = (PurchaseOrder.query.filter_by(status="confirmed")
            .order_by(PurchaseOrder.confirmed_at.desc()).all())
    return render_template("po_dispatch.html", pos=rows)


@po_bp.route("/<int:po_id>/dispatched", methods=["POST"])
@login_required
def po_mark_dispatched(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    if po.status != "confirmed":
        flash("Sirf confirmed PO hi dispatch ho sakta hai.", "error")
        return redirect(url_for("po.po_dispatch"))
    po.status = "dispatched"
    po.dispatched_at = datetime.utcnow()
    db.session.commit()
    flash(f"PO {po.po_number} dispatched mark ho gaya.", "success")
    return redirect(url_for("po.po_dispatch"))


@po_bp.route("/map/<int:map_id>/image")
@login_required
def map_image(map_id):
    m = PartyProductMap.query.get_or_404(map_id)
    if not m.image_data:
        abort(404)
    return Response(m.image_data, mimetype=m.image_mime or "image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@po_bp.route("/map/<int:map_id>/thumb")
@login_required
def map_thumb(map_id):
    """Chhota version. Purane rows (jinme thumb nahi hai) ke liye full image."""
    m = PartyProductMap.query.get_or_404(map_id)
    data = m.image_thumb or m.image_data
    if not data:
        abort(404)
    return Response(data, mimetype="image/jpeg" if m.image_thumb else (m.image_mime or "image/jpeg"),
                    headers={"Cache-Control": "private, max-age=86400"})


@po_bp.route("/<int:po_id>/scan")
@login_required
def po_scan(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    if not po.scan_data:
        abort(404)
    return Response(po.scan_data, mimetype=po.scan_mime or "application/octet-stream")


@po_bp.route("/mappings")
@login_required
def mappings_page():
    cid = request.args.get("customer_id") or ""
    rows = PartyProductMap.query
    if cid.isdigit():
        rows = rows.filter_by(customer_id=int(cid))
    rows = rows.order_by(PartyProductMap.customer_id, PartyProductMap.canonical_key).all()
    return render_template("po_mappings.html", maps=rows, cid=cid,
                           customers=Customer.query.order_by(Customer.name).all())


@po_bp.route("/mappings/<int:map_id>/delete", methods=["POST"])
@login_required
def delete_mapping(map_id):
    m = PartyProductMap.query.get_or_404(map_id)
    if not current_user.is_owner:
        flash("Sirf owner mapping delete kar sakta hai.", "error")
        return redirect(url_for("po.mappings_page"))
    if POLine.query.filter_by(map_id=m.id).first():
        flash("Ye mapping kisi PO me use ho chuki hai — delete nahi kar sakte.", "error")
        return redirect(url_for("po.mappings_page"))
    db.session.delete(m)
    db.session.commit()
    flash("Mapping delete ho gayi.", "success")
    return redirect(url_for("po.mappings_page"))
