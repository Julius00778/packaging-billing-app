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
import os
import re
from datetime import datetime, date

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort, Response
)
from flask_login import login_required, current_user

from models import db, Customer, Item

import drive_sync

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
    # Party ka apna code — "HM01". Yahi pehchan ka pehla zariya hai; size sirf
    # check karne ke liye. Normalized rakha jaata hai (bina space, bade akshar).
    item_code = db.Column(db.String(40), default="", index=True)
    raw_size_text = db.Column(db.String(80), default="")   # jaisa PO me pehli baar aaya
    canonical_key = db.Column(db.String(60), nullable=False, index=True)
    label = db.Column(db.String(200), nullable=False)      # operator ko dikhne wala naam
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=True)
    image_data = db.Column(db.LargeBinary, nullable=True)
    image_mime = db.Column(db.String(60), default="")
    # Alag chhota thumbnail: review screen pe 20 lines ek saath khulti hain, wahan
    # 20 × 120 KB load karne ka koi matlab nahi jab 96px ka square dikhana hai.
    image_thumb = db.Column(db.LargeBinary, nullable=True)
    # Drive se aaya hai toh yaad rakho — dobara sync pe wahi file phir se
    # download na ho jab tak Drive pe badli na ho.
    drive_file_id = db.Column(db.String(120), default="", index=True)
    drive_modified = db.Column(db.String(40), default="")
    times_used = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))

    customer = db.relationship("Customer")
    item = db.relationship("Item")

    @property
    def has_image(self):
        return bool(self.image_data)


class POSetting(db.Model):
    """Chhoti chhoti settings — abhi sirf Drive ka main folder."""
    __tablename__ = "po_setting"
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.String(500), default="")

    @staticmethod
    def get(key, default=""):
        row = db.session.get(POSetting, key)
        return row.value if row and row.value else default

    @staticmethod
    def put(key, value):
        row = db.session.get(POSetting, key)
        if not row:
            row = POSetting(key=key)
            db.session.add(row)
        row.value = (value or "").strip()
        return row


class PartyFolder(db.Model):
    """Drive ka ek party folder, aur wo app ki kaunsi party hai.

    Folder ka naam ("GM ENTERPREISES") aur app me customer ka naam alag ho sakte
    hain, isliye jodna ek baar haath se hota hai. Naam bilkul mil jaye toh apne
    aap jud jaata hai.
    """
    __tablename__ = "party_folder"
    id = db.Column(db.Integer, primary_key=True)
    folder_id = db.Column(db.String(120), unique=True, nullable=False)
    folder_name = db.Column(db.String(200), default="")
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=True)
    size_unit = db.Column(db.String(10), default="cm")
    last_synced = db.Column(db.DateTime, nullable=True)
    last_result = db.Column(db.String(300), default="")

    customer = db.relationship("Customer")


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
    item_code = db.Column(db.String(40), default="")     # line me jo code likha tha
    raw_size_text = db.Column(db.String(80), default="")
    canonical_key = db.Column(db.String(60), default="", index=True)
    unit_source = db.Column(db.String(12), default="")   # explicit/party/magnitude
    qty = db.Column(db.Float, default=0.0)
    qty_unit = db.Column(db.String(20), default="pcs")
    # code    — item code se mila (sabse pakka)
    # size    — code nahi tha, size se ek hi product mila
    # multiple— kai product fit hue, operator chunega
    # none    — kuch nahi mila
    # manual  — operator ne khud chuna ya naya map banaya
    match_status = db.Column(db.String(12), default="none")
    # Code aur size dono likhe the par aapas me match nahi hue — chupchaap aage
    # badhne se accha hai operator ko dikha do.
    size_mismatch = db.Column(db.Boolean, default=False)
    map_id = db.Column(db.Integer, db.ForeignKey("party_product_map.id"), nullable=True)

    mapping = db.relationship("PartyProductMap")


# --------------------------------------------------------------------------
# Size normalisation
# --------------------------------------------------------------------------

# Size 2 ya 3 dimension ka ho sakta hai: sheet "12x18", dabba "23x14x5".
# Teesra number optional hai, par jab likha ho toh wo alag product hai —
# 23x14x5 aur 23x14x8 ek cheez nahi hain.
_NUM = r"\d+(?:\.\d+)?"
_SEP = r"\s*[x×*X]\s*"


def _inline_unit(n):
    """Size ke beech me bhi unit aa sakta hai: 12" x 18", 300 mm x 450."""
    return rf"""(?:\s*(?P<u{n}>mm|cm|inches|inch|in\b|"|”|''))?"""


SIZE_RE = re.compile(
    rf"(?P<d1>{_NUM}){_inline_unit(1)}{_SEP}"
    rf"(?P<d2>{_NUM}){_inline_unit(2)}"
    rf"(?:{_SEP}(?P<d3>{_NUM}){_inline_unit(3)})?"
)

# Item code: do se chaar akshar, phir number — HM01, HM 01, hm-03, ABCD1234.
# Ye sirf tab bharosa karne layak hai jab party ke code list se milaya jaye,
# warna "PO 8801" aur "Item 3" bhi code jaise dikhte hain.
CODE_RE = re.compile(r"\b([A-Za-z]{2,4})\s*-?\s*(\d{1,4})\b")

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


def _unit_word(word):
    word = (word or "").strip().lower()
    if not word:
        return None
    if word.startswith("mm"):
        return "mm"
    if word.startswith("cm"):
        return "cm"
    return "inch"


def size_dims(match):
    """Match me se 2 ya 3 numbers nikalo."""
    dims = [float(match.group("d1")), float(match.group("d2"))]
    if match.groupdict().get("d3"):
        dims.append(float(match.group("d3")))
    return dims


def detect_size_unit(text, match, party_default="inch"):
    """Unit sirf size ke andar ya turant baad dekho — poori line me nahi.

    Ye important hai: "2mm foam 12x18" me agar poori line me 'mm' dhoondhoge toh
    12x18 ko galti se mm maan loge, jabki wo mm kisi aur cheez ka tha.

    Jab unit size ke andar kahin bhi likha ho — "23x14x5 cm" ya "300 mm x 450" —
    toh wo poore size pe lagta hai, sirf us ek number pe nahi. Log aise hi
    likhte hain.
    """
    # 1) Size ke andar kahin bhi likha unit
    for n in (1, 2, 3):
        unit = _unit_word(match.groupdict().get(f"u{n}"))
        if unit:
            return unit, "explicit"

    # 2) Size ke turant baad wala unit — poori line me nahi.
    tail = text[match.end(): match.end() + 10].lower()
    if re.match(r"\s*(mm|milli)", tail):
        return "mm", "explicit"
    if re.match(r"\s*cm", tail):
        return "cm", "explicit"
    if re.match(r"""\s*(inches|inch|in\b|"|”|'')""", tail):
        return "inch", "explicit"

    # 3) Party ka apna convention — aam taur pe yahi chalta hai
    if party_default in ("inch", "mm", "cm"):
        return party_default, "party"

    # 4) Aakhri sahara: bade numbers aam taur pe mm hote hain. Ye heuristic
    #    galat ho sakta hai, isliye unit_source='magnitude' operator ko flag dikhata hai.
    dims = size_dims(match)
    return ("mm", "magnitude") if max(dims) > 100 else ("inch", "magnitude")


TO_MM = {"inch": 25.4, "cm": 10.0, "mm": 1.0}


def canonical_size(dims, unit):
    """Saare dimensions mm me badlo aur sort karo.

    Sort isliye ki 23x14x5 aur 5x23x14 ek hi dabba hai — party jis kram me bhi
    likhe. Do-number aur teen-number wali keys ki lambai alag hoti hai, isliye
    12x18 kabhi 12x18x5 se nahi takrayega.
    """
    factor = TO_MM.get(unit, 1.0)
    mm = sorted(round(float(d) * factor, 1) for d in dims)
    return "x".join(f"{v:.1f}" for v in mm)


def normalize_code(text):
    """'HM 01', 'hm-01', 'HM01' — teeno ka ek hi matlab: HM01."""
    return re.sub(r"[\s\-_.]+", "", (text or "")).upper()


def find_item_code(line, known_codes):
    """Line me se us party ka item code dhoondho.

    Sirf un codes pe bharosa karte hain jo us party ke liye pehle se maujood
    hain. Generic regex pe bharosa karte toh "PO 8801" aur "Item 3" bhi code
    ban jaate. Isliye pehle line ke saare code-jaise tokens nikaalo, phir
    dekho ki unme se koi party ke list me hai ya nahi.
    """
    if not known_codes:
        return None
    known = {normalize_code(c) for c in known_codes if c}
    for m in CODE_RE.finditer(line or ""):
        candidate = normalize_code(m.group(1) + m.group(2))
        if candidate in known:
            return candidate, m.span()
    return None


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


def parse_po_text(text, party_default_unit="inch", known_codes=None):
    """PO ka text lo, har line se item code + size + qty nikalo.

    Ek line tabhi order-line maani jaati hai jab usme ya toh us party ka item
    code ho, ya koi size ho. Baaki lines — headers, "Thanks", delivery note —
    chhod di jaati hain.

    `known_codes` us party ke maujooda codes ki list hai. Ye na do toh sirf
    size se kaam chalega (purana behaviour).

    OCR lagane ke baad bhi yahi function chalega — bas input badlega.
    """
    out = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        code_hit = find_item_code(line, known_codes)
        m = SIZE_RE.search(line)
        if not code_hit and not m:
            continue

        # Line me se size aur code dono nikaal do, taaki unke numbers qty na ban jayen
        rest_parts = []
        cut = []
        if m:
            cut.append(m.span())
        if code_hit:
            cut.append(code_hit[1])
        pos = 0
        for start, end in sorted(cut):
            rest_parts.append(line[pos:start])
            pos = end
        rest_parts.append(line[pos:])
        rest = " ".join(rest_parts)
        # Size ke turant baad ka unit word bhi qty parsing se hata do
        rest = re.sub(r"""^\s*(?:mm|cm|inches|inch|in\b|"|”|'')""", " ", rest, count=1, flags=re.I)

        row = {
            "raw_text": line,
            "item_code": code_hit[0] if code_hit else "",
            "raw_size_text": m.group(0).strip() if m else "",
            "canonical_key": "",
            "unit_source": "",
            "qty": 0.0,
            "qty_unit": "pcs",
        }
        if m:
            unit, unit_source = detect_size_unit(line, m, party_default_unit)
            row["canonical_key"] = canonical_size(size_dims(m), unit)
            row["unit_source"] = unit_source

        row["qty"], row["qty_unit"] = parse_qty(rest)
        out.append(row)
    return out


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def known_codes_for(customer_id):
    """Us party ke saare item codes — parser ko yahi list deni hoti hai."""
    rows = (db.session.query(PartyProductMap.item_code)
            .filter(PartyProductMap.customer_id == customer_id,
                    PartyProductMap.item_code != "")
            .distinct().all())
    return [r[0] for r in rows]


def map_by_code(customer_id, item_code):
    if not item_code:
        return None
    return (PartyProductMap.query
            .filter_by(customer_id=customer_id, item_code=normalize_code(item_code))
            .first())


def candidates_for(customer_id, canonical_key):
    if not canonical_key:
        return []
    return (PartyProductMap.query
            .filter_by(customer_id=customer_id, canonical_key=canonical_key)
            .order_by(PartyProductMap.times_used.desc(), PartyProductMap.label)
            .all())


def match_line(customer_id, item_code=None, canonical_key=None):
    """(status, chosen_map_or_None, size_mismatch) lautata hai.

    Code sabse pehle dekha jaata hai — wo party ki apni pehchan hai. Size tab
    dekha jaata hai jab code na ho. Dono hon toh size sirf check karta hai:
    na mile toh product to code wala hi rehta hai, par flag lag jaata hai
    taaki operator apni aankhon se dekh le.
    """
    by_code = map_by_code(customer_id, item_code)
    if by_code:
        mismatch = bool(canonical_key and by_code.canonical_key
                        and canonical_key != by_code.canonical_key)
        return "code", by_code, mismatch

    rows = candidates_for(customer_id, canonical_key)
    if len(rows) == 1:
        return "size", rows[0], False
    if len(rows) > 1:
        return "multiple", None, False
    return "none", None, False


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


DRIVE_ROOT_KEY = "drive_root_folder"


def sync_party_folders(service, root_folder_id):
    """Main folder ke party folders DB me le aao. Naya folder mila toh naam se
    customer dhoondhne ki koshish karo — na mile toh khaali chhod do, operator
    baad me jod dega."""
    found = drive_sync.list_party_folders(service, root_folder_id)
    added = 0
    for f in found:
        row = PartyFolder.query.filter_by(folder_id=f["id"]).first()
        if not row:
            row = PartyFolder(folder_id=f["id"])
            db.session.add(row)
            added += 1
        row.folder_name = f["name"]
        if not row.customer_id:
            guess = (Customer.query
                     .filter(db.func.lower(Customer.name) == f["name"].strip().lower())
                     .first())
            if guess:
                row.customer_id = guess.id
    db.session.commit()
    return found, added


def sync_one_folder(service, folder):
    """Ek party folder ke saare samples DB me laao.

    Har file ka naam code aur size deta hai. Wahi code pehle se ho toh row
    update hoti hai, nayi nahi banti — isliye baar baar sync karna safe hai.
    Photo tabhi dobara download hoti hai jab Drive pe file badli ho.
    """
    result = {"seen": 0, "added": 0, "updated": 0, "photos": 0, "skipped": []}
    if not folder.customer_id:
        raise ValueError("folder kisi party se juda nahi hai")

    unit = folder.size_unit if folder.size_unit in ("inch", "mm", "cm") else "cm"
    PartyPOConfig.set_unit(folder.customer_id, unit)

    for f in drive_sync.list_sample_files(service, folder.folder_id):
        result["seen"] += 1
        parsed = drive_sync.parse_sample_filename(
            f["name"], canonical_size, SIZE_RE, size_dims, normalize_code)
        if not parsed:
            result["skipped"].append(f"{f['name']} — naam me item code nahi mila")
            continue
        code, dims, raw_size = parsed
        if not dims:
            result["skipped"].append(f"{f['name']} — naam me size nahi mila")
            continue

        row = PartyProductMap.query.filter_by(
            customer_id=folder.customer_id, item_code=code).first()
        is_new = row is None
        if is_new:
            row = PartyProductMap(customer_id=folder.customer_id, item_code=code,
                                  times_used=0)
            db.session.add(row)
            result["added"] += 1
        else:
            result["updated"] += 1

        row.canonical_key = canonical_size(dims, unit)
        row.raw_size_text = raw_size
        if not row.label:
            row.label = f"{code} ({raw_size})"

        # Photo tabhi laao jab pehle na aayi ho ya Drive pe badli ho
        changed = (row.drive_file_id != f["id"]
                   or row.drive_modified != (f.get("modifiedTime") or ""))
        if changed or not row.image_data:
            try:
                data = drive_sync.download_file(service, f["id"])
            except Exception as exc:
                result["skipped"].append(f"{f['name']} — photo nahi aayi ({exc})")
                data = None
            if data:
                main = _compress_image(data, IMAGE_MAX_DIM, IMAGE_TARGET_BYTES)
                thumb = _compress_image(data, THUMB_MAX_DIM, THUMB_TARGET_BYTES)
                if main:
                    row.image_data, row.image_mime = main
                    row.image_thumb = thumb[0] if thumb else None
                else:   # Pillow na ho toh original hi rakh lo
                    row.image_data = data
                    row.image_mime = f.get("mimeType") or "image/jpeg"
                    row.image_thumb = None
                result["photos"] += 1
                row.drive_file_id = f["id"]
                row.drive_modified = f.get("modifiedTime") or ""

    folder.last_synced = datetime.utcnow()
    folder.last_result = (f"{result['seen']} file, {result['added']} naye, "
                          f"{result['updated']} update, {result['photos']} photo")
    db.session.commit()
    return result


def _apply_match(line, customer_id):
    status, chosen, mismatch = match_line(customer_id, line.item_code, line.canonical_key)
    line.match_status = status
    line.map_id = chosen.id if chosen else None
    line.size_mismatch = mismatch


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
        parsed = parse_po_text(raw_text, unit, known_codes_for(customer_id))
        if not parsed:
            flash("Text me koi order line nahi mili — item code (HM01) ya size "
                  "(23x14x5) likhna zaroori hai.", "error")
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
    line.size_mismatch = False
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

    item_code = normalize_code(request.form.get("item_code", "") or line.item_code)
    if item_code and map_by_code(line.po.customer_id, item_code):
        flash(f"Code {item_code} is party ke liye pehle se hai — dusra code do.", "error")
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
        item_code=item_code,
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
    line.size_mismatch = False
    db.session.commit()
    remembered = f"code {item_code}" if item_code else "ye size"
    flash(f"'{label}' map ho gaya — is party ke liye {remembered} ab yaad rahega.", "success")
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


@po_bp.route("/drive")
@login_required
def drive_page():
    folders = PartyFolder.query.order_by(PartyFolder.folder_name).all()
    counts = {}
    for f in folders:
        if f.customer_id:
            counts[f.id] = PartyProductMap.query.filter_by(
                customer_id=f.customer_id).filter(PartyProductMap.item_code != "").count()
    return render_template(
        "po_drive.html",
        root_link=POSetting.get(DRIVE_ROOT_KEY),
        folders=folders, counts=counts,
        customers=Customer.query.order_by(Customer.name).all(),
        key_set=bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()),
    )


@po_bp.route("/drive/folder", methods=["POST"])
@login_required
def drive_set_folder():
    """Main folder ka link save karo aur uske andar ke party folders padh lo."""
    link = request.form.get("root_link", "").strip()
    folder_id = drive_sync.folder_id_from_link(link)
    if not folder_id:
        flash("Drive folder ka link ya ID daalo.", "error")
        return redirect(url_for("po.drive_page"))
    try:
        service = drive_sync.drive_service()
        meta = drive_sync.check_access(service, folder_id)
        found, added = sync_party_folders(service, folder_id)
    except drive_sync.DriveError as exc:
        flash(str(exc), "error")
        return redirect(url_for("po.drive_page"))
    POSetting.put(DRIVE_ROOT_KEY, link)
    db.session.commit()
    flash(f"'{meta['name']}' khul gaya — {len(found)} party folder mile"
          + (f", {added} naye." if added else "."), "success")
    return redirect(url_for("po.drive_page"))


@po_bp.route("/drive/folder/<int:folder_id>/link", methods=["POST"])
@login_required
def drive_link_folder(folder_id):
    """Drive folder ko app ki party se jodo, aur uska size unit tay karo."""
    folder = db.session.get(PartyFolder, folder_id) or abort(404)
    cid = request.form.get("customer_id") or ""
    folder.customer_id = int(cid) if cid.isdigit() else None
    unit = request.form.get("size_unit", "cm")
    folder.size_unit = unit if unit in ("inch", "mm", "cm") else "cm"
    if folder.customer_id:
        PartyPOConfig.set_unit(folder.customer_id, folder.size_unit)
    db.session.commit()
    flash("Folder jud gaya. Ab sync chala do.", "success")
    return redirect(url_for("po.drive_page"))


@po_bp.route("/drive/sync", methods=["POST"])
@login_required
def drive_sync_now():
    """Sab jude hue folders sync karo (ya sirf ek, agar folder_id bheja ho)."""
    only = request.form.get("folder_id") or ""
    q = PartyFolder.query.filter(PartyFolder.customer_id.isnot(None))
    if only.isdigit():
        q = q.filter(PartyFolder.id == int(only))
    folders = q.all()
    if not folders:
        flash("Pehle kam se kam ek folder ko party se jodo.", "error")
        return redirect(url_for("po.drive_page"))

    try:
        service = drive_sync.drive_service()
    except drive_sync.DriveError as exc:
        flash(str(exc), "error")
        return redirect(url_for("po.drive_page"))

    total = {"seen": 0, "added": 0, "updated": 0, "photos": 0}
    skipped = []
    for folder in folders:
        try:
            r = sync_one_folder(service, folder)
        except Exception as exc:
            skipped.append(f"{folder.folder_name} — {exc}")
            continue
        for k in total:
            total[k] += r[k]
        skipped.extend(r["skipped"])

    flash(f"Sync ho gaya — {total['seen']} file dekhi, {total['added']} naye product, "
          f"{total['updated']} update, {total['photos']} photo aayi.", "success")
    for s in skipped[:8]:
        flash("Chhod diya: " + s, "error")
    if len(skipped) > 8:
        flash(f"…aur {len(skipped) - 8} aur chhodi gayin.", "error")
    return redirect(url_for("po.drive_page"))


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
