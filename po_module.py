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

import difflib
import io
import os
import re
import uuid
from datetime import datetime, date

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort, Response, jsonify
)
from flask_login import login_required, current_user

from models import db, Customer, Item, Invoice, Category, Settings
from translations import t

import drive_sync
import ocr_read
import telegram_bot

try:
    from PIL import Image, ImageOps
except ImportError:            # Pillow na ho toh module chale, bas compress na ho
    Image = ImageOps = None

po_bp = Blueprint("po", __name__, url_prefix="/po")


@po_bp.app_context_processor
def _po_template_globals():
    """`common_units` har template me mile.

    Jis product ki category tay na ho uske liye koi rok nahi — wahan yahi
    poori list dikhti hai.
    """
    from app import COMMON_UNITS
    return {"common_units": COMMON_UNITS}

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

# Order ki poori zindagi. Naam wahi rakhe hain jo Julius bolte hain.
#   pending       — office me review chal raha hai
#   with_operator — card operator group me chala gaya, jawab ka intezaar
#   in_production — operator ne OK kiya (yellow dot)
#   made          — operator ne Done kiya (green dot), dispatch list me
#   dispatched    — maal nikal gaya
#   rejected      — office ne cancel kiya
PO_STATUSES = ("pending", "with_operator", "in_production", "made",
               "dispatched", "rejected")

# Ye Hinglish jaan-boojh ke hai — sirf Telegram ke msg me chalti hai, jahan
# operator aur manager padhte hain. Screen ka har shabd translations.py se
# aata hai (EN/Hindi), yahan se nahi.
STATUS_LABEL = {
    "pending": "review chal raha hai",
    "with_operator": "operator ke paas",
    "in_production": "operation me",
    "made": "ban gaya",
    "dispatched": "dispatch ho gaya",
    "rejected": "cancel",
}

# Kaunsi status se kaunsi pe jaa sakte hain. Iske bahar kuch nahi hota — na
# button se, na screen se.
NEXT_STATUS = {
    "pending": ("with_operator", "rejected"),
    "with_operator": ("in_production", "rejected"),
    "in_production": ("made", "with_operator", "rejected"),
    "made": ("dispatched", "in_production", "rejected"),
    "dispatched": ("made",),
    "rejected": ("pending",),
}


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

    def unit_choices(self):
        """Is product ki jaayaz units.

        Item ki category batati hai ki ye maal kis cheez me bikta hai — foam
        piece ya roll me, scrap sirf kilo me. Category na juddi ho toh saari
        aam units khuli rehti hain, kyunki galat rok lagane se accha hai koi
        rok na lagana.
        """
        cat = self.item.category if self.item else None
        return cat.unit_list() if cat else []


def ensure_item_for(mapping, category_id=None):
    """Har PO product ka Item master me apna ghar ho.

    Pehle PO ke product aur Item master do alag duniya the — isliye bill me
    item ki jagah khaali jaati thi, naam description me thus jaata tha, aur
    kisi product pe unit ka niyam laga hi nahi sakte the. Ab har mapping ke
    saath ek Item banta hai.

    Stock track nahi hota: "operation me daal do" sirf status badalta hai,
    maal ghatata nahi — ye Julius ne shuru me hi tay kiya tha.
    """
    if mapping.item_id and db.session.get(Item, mapping.item_id):
        if category_id is not None:
            item = db.session.get(Item, mapping.item_id)
            item.category_id = category_id or None
            _align_item_unit(item)
        return db.session.get(Item, mapping.item_id)

    name = (mapping.label or mapping.item_code or mapping.raw_size_text or "").strip()
    if not name:
        return None

    # Wahi naam pehle se ho toh nayi entry mat banao — usi se jod do.
    item = Item.query.filter_by(name=name).first()
    if item is None:
        settings = Settings.get()
        item = Item(
            name=name[:200],
            unit=settings.default_unit or "pcs",
            gst_rate=0.0,                 # GST abhi off hai
            sale_price=0.0,
            track_stock=False,
            category_id=category_id or None,
        )
        db.session.add(item)
        db.session.flush()
    elif category_id is not None:
        item.category_id = category_id or None

    _align_item_unit(item)
    mapping.item_id = item.id
    return item


def _align_item_unit(item):
    """Item ki unit uski category ke hisaab se rakho.

    Category badle aur purani unit us list me na ho toh pehli jaayaz unit pe
    le aao — warna item apni hi category ke niyam todta rehta hai.
    """
    cat = item.category if item.category_id else None
    if not cat:
        return
    allowed = cat.unit_list()
    if allowed and item.unit not in allowed:
        item.unit = allowed[0]


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


class PartyRate(db.Model):
    """Kis party ko kaunsa product kis rate pe jaata hai.

    Rate **product** se juda hai (party_product_map se), line me likhe code se
    nahi. Wajah: order me kabhi item code likha hota hai, kabhi sirf size. Dono
    haalat me product wahi hota hai, isliye rate bhi wahi milna chahiye.

    Rate us unit pe rakha jaata hai jis unit me order aata hai — 'pcs' ka rate
    alag, 'box' ka alag. Koi hisaab khud nahi lagaya jaata (1 box me kitne pc
    hain, ye system nahi jaanta), isliye bill kabhi apne aap galat nahi banega.
    """
    __tablename__ = "party_rate"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    map_id = db.Column(db.Integer, db.ForeignKey("party_product_map.id"), nullable=False)
    item_code = db.Column(db.String(40), default="")   # sirf padhne ke liye
    qty_unit = db.Column(db.String(20), default="pcs")
    rate = db.Column(db.Float, default=0.0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("map_id", "qty_unit",
                                          name="uq_rate_product_unit"),)

    mapping = db.relationship("PartyProductMap")

    @staticmethod
    def look_up(map_id, qty_unit):
        if not map_id:
            return None
        return PartyRate.query.filter_by(map_id=map_id,
                                         qty_unit=qty_unit or "pcs").first()

    @staticmethod
    def remember(mapping, qty_unit, rate):
        if not mapping or not rate:
            return None
        row = PartyRate.look_up(mapping.id, qty_unit)
        if not row:
            row = PartyRate(customer_id=mapping.customer_id, map_id=mapping.id,
                            qty_unit=qty_unit or "pcs")
            db.session.add(row)
        row.item_code = mapping.item_code or ""
        row.rate = float(rate)
        row.updated_at = datetime.utcnow()
        return row


class TelegramChat(db.Model):
    """Har wo chat/group jisme bot ko dala gaya hai.

    Chat ID dhoondhna aam aadmi ke liye mushkil hai, isliye bot khud yaad rakhta
    hai ki wo kahan kahan hai. Aapko bas chunna hai ki kaunsa group operator ka
    hai aur kaunsa manager ka.
    """
    __tablename__ = "telegram_chat"
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.String(40), unique=True, nullable=False)
    title = db.Column(db.String(200), default="")
    chat_type = db.Column(db.String(20), default="")   # group/supergroup/private
    role = db.Column(db.String(20), default="")        # operator/manager/owner
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)


class TelegramPerson(db.Model):
    """Jo log bot se baat karte hain. Sirf 'operator' wale button daba sakte hain."""
    __tablename__ = "telegram_person"
    id = db.Column(db.Integer, primary_key=True)
    tg_user_id = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(120), default="")
    username = db.Column(db.String(120), default="")
    is_operator = db.Column(db.Boolean, default=False)
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)


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
    # Telegram ka card kahan gaya — taaki wahi card update ho, naya na bhejna pade
    tg_chat_id = db.Column(db.String(40), default="")
    tg_message_ids = db.Column(db.String(300), default="")   # comma se jude
    tg_rate_summary_id = db.Column(db.String(40), default="")  # aapke chat wala card
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=True)
    # Accountant ka nishan — "ye bill nikal chuka". App printer se nahi poochhta.
    bill_printed_at = db.Column(db.DateTime, nullable=True)
    # Rate pe aapki mohar. Bill isi rate se banta hai, isliye order operator ke
    # paas tabhi jaata hai jab aap Telegram pe haan kar dete ho. Rate baad me
    # badla toh ye nishan hat jaata hai — purani mohar naye rate pe nahi chalti.
    rates_ok_at = db.Column(db.DateTime, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    accepted_at = db.Column(db.DateTime, nullable=True)      # operator ne OK kiya
    made_at = db.Column(db.DateTime, nullable=True)          # operator ne Done kiya
    operator_name = db.Column(db.String(120), default="")

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

    @property
    def no_rate_count(self):
        return sum(1 for l in self.lines if not l.rate)

    @property
    def no_qty_count(self):
        """Jin lines me kitne banane hain wahi nahi likha.

        Qty 0 ka matlab bill me amount 0 — party ko khaali bill chala jayega.
        Isliye ise rate ki tarah hi rok maana hai.
        """
        return sum(1 for l in self.lines if not l.qty or l.qty <= 0)

    @property
    def total(self):
        return round(sum(l.amount for l in self.lines), 2)


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
    rate = db.Column(db.Float, default=0.0)
    rate_from_memory = db.Column(db.Boolean, default=False)  # pichli baar wala rate
    tg_rate_msg_id = db.Column(db.String(40), default="")    # kis msg ka reply rate hai

    mapping = db.relationship("PartyProductMap")

    @property
    def amount(self):
        return round((self.qty or 0) * (self.rate or 0), 2)


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


# Jahan OCR ank ki jagah akshar padh deta hai. Ye map sirf code ke ank wale
# hisse pe lagta hai — poore token pe lagate toh "GME" ka "G" bhi "6" ban
# jaata aur code pehchana hi na jaata.
LOOKS_LIKE_DIGIT = str.maketrans({"O": "0", "D": "0", "Q": "0",
                                  "I": "1", "L": "1", "|": "1",
                                  "S": "5", "B": "8", "Z": "2",
                                  "G": "6", "T": "7"})


def nearest_known_code(token, known_codes):
    """Galat padhe hue code ko us party ke asli code se milao.

    OCR akela bharose ka nahi — `GME01` ko `GMEO1` padh dena aam baat hai. Par
    hum jaante hain ki is party ke code kaun se hain, aur wo list chhoti hoti
    hai. Isliye sawaal "ye kya likha hai" nahi rehta, "in paanch me se kaunsa
    hai" ban jaata hai — aur wo sawaal aasan hai.

    Code ki shakal hoti hai: kuch akshar, phir kuch ank. Isliye har maujooda
    code ko usi tarah kaat ke dekha jaata hai — akshar wala hissa waisa hi
    milna chahiye, aur ank wale hisse me O/0 jaisi chhoot di jaati hai.

    Kuch bhi na baithe toh None — galat code lagane se accha kuch na lagana.
    """
    known = {normalize_code(c) for c in (known_codes or []) if c}
    if not known:
        return None
    t = normalize_code(token)
    if not t:
        return None
    if t in known:
        return t

    for code in sorted(known):
        if len(t) != len(code):
            continue
        head = 0
        while head < len(code) and code[head].isalpha():
            head += 1
        if t[:head] != code[:head]:
            continue
        if t[head:].translate(LOOKS_LIKE_DIGIT) == code[head:]:
            return code

    # Aakhri koshish: sabse paas ka. Cutoff ooncha rakha hai — GME01 aur GME02
    # me ek hi akshar ka farq hai, isliye dhili chhoot khatarnak hogi.
    close = difflib.get_close_matches(t, sorted(known), n=1, cutoff=0.86)
    return close[0] if close else None


def find_item_code(line, known_codes, fuzzy=False):
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
    if not fuzzy:
        return None
    # Photo se aaya text — ab galat padhe hue token ko list se milane do
    for m in CODE_RE.finditer(line or ""):
        fixed = nearest_known_code(m.group(1) + m.group(2), known)
        if fixed:
            return fixed, m.span()
    return None


def nearest_unit(word):
    """Galat padhe hue unit ko jaani-pehchani list se milao.

    Wahi tarkeeb jo code pe lagti hai: OCR `roll` ko `rll` padh deta hai, par
    units ki list chhoti aur tay hai. Kuch na baithe toh None — galat unit
    lagane se accha kuch na lagana, kyunki rate unit ke saath bandha hai.
    """
    w = (word or "").strip(".,;:").lower()
    if not w or len(w) < 2:
        return None
    if w in UNIT_ALIASES:
        return UNIT_ALIASES[w]
    close = difflib.get_close_matches(w, sorted(UNIT_ALIASES), n=1, cutoff=0.75)
    return UNIT_ALIASES[close[0]] if close else None


UNIT_GUESS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([A-Za-z.]{2,10})")


def parse_qty(rest_text, fuzzy=False):
    """Line me se (qty, unit) nikalo — size hata dene ke baad."""
    m = QTY_LABEL_RE.search(rest_text)
    if m:
        return float(m.group(1)), "pcs"
    m = QTY_KEYWORD_RE.search(rest_text)
    if m:
        unit = UNIT_ALIASES.get(m.group(2).lower().rstrip("."), "pcs")
        return float(m.group(1)), unit
    if fuzzy:
        # Photo se aaya hai: number ke baad wala shabd unit ho sakta hai, bas
        # galat padha gaya ho. Use list se milane ki chhoot yahin milti hai.
        for m in UNIT_GUESS_RE.finditer(rest_text or ""):
            unit = nearest_unit(m.group(2))
            if unit:
                return float(m.group(1)), unit
    numbers = NUMBER_RE.findall(rest_text)
    if numbers:
        return float(numbers[-1]), "pcs"
    return 0.0, "pcs"


def parse_po_text(text, party_default_unit="inch", known_codes=None, fuzzy=False):
    """PO ka text lo, har line se item code + size + qty nikalo.

    Ek line tabhi order-line maani jaati hai jab usme ya toh us party ka item
    code ho, ya koi size ho. Baaki lines — headers, "Thanks", delivery note —
    chhod di jaati hain.

    `known_codes` us party ke maujooda codes ki list hai. Ye na do toh sirf
    size se kaam chalega (purana behaviour).

    `fuzzy` sirf tab chalu karo jab text photo/PDF se aaya ho. Tab galat padhe
    hue code ko us party ki list se milane ki chhoot mil jaati hai — aadmi ke
    likhe order me ye chhoot dena galat hoga.
    """
    out = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        code_hit = find_item_code(line, known_codes, fuzzy=fuzzy)
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

        row["qty"], row["qty_unit"] = parse_qty(rest, fuzzy=fuzzy)
        out.append(row)
    return out


def lines_from_rows(form, party_unit="inch"):
    """Ek-ek karke bhare hue form se lines banao.

    Jab photo dhundhli ho ya likhawat tedhi, tab poora text paste karna kaam
    nahi aata — aadmi photo dekh ke ek line bharta hai. Yahan andaza lagane ki
    zaroorat hi nahi: code, size, qty aur unit sab alag alag khaano me aaye
    hain, isliye unit "guessed" bhi nahi hoti.

    Nateeja bilkul wahi shakal ka hai jo parse_po_text deta hai, taaki aage ka
    raasta ek hi rahe.
    """
    codes = form.getlist("line_code")
    sizes = form.getlist("line_size")
    qtys = form.getlist("line_qty")
    units = form.getlist("line_unit")

    out = []
    for i in range(max(len(codes), len(sizes), len(qtys))):
        code = normalize_code(codes[i] if i < len(codes) else "")
        size_text = (sizes[i] if i < len(sizes) else "").strip()
        raw_qty = (qtys[i] if i < len(qtys) else "").strip().replace(",", "")
        unit = (units[i] if i < len(units) else "").strip() or "pcs"

        # Na code, na size — ye khaali row hai, chhod do
        if not code and not size_text:
            continue
        try:
            qty = float(raw_qty) if raw_qty else 0.0
        except ValueError:
            qty = 0.0

        row = {
            "raw_text": " ".join(x for x in (code, size_text,
                                             f"{qty:g} {unit}" if qty else "") if x),
            "item_code": code,
            "raw_size_text": size_text,
            "canonical_key": "",
            "unit_source": "",
            "qty": qty,
            "qty_unit": unit,
        }
        m = SIZE_RE.search(size_text)
        if m:
            row["canonical_key"] = canonical_size(size_dims(m), party_unit)
            row["unit_source"] = "form"     # form me unit khud chuni gayi hai
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
        # Item master me bhi ghar do — bina iske bill me item khaali jaata hai
        # aur unit ka koi niyam lag hi nahi sakta.
        ensure_item_for(row)

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


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

TG_SECRET_KEY = "telegram_webhook_secret"


def chat_for_role(role):
    return TelegramChat.query.filter_by(role=role).first()


def is_operator(tg_user_id):
    p = TelegramPerson.query.filter_by(tg_user_id=str(tg_user_id)).first()
    return bool(p and p.is_operator)


def remember_chat(chat, role_hint=None):
    if not chat or not chat.get("id"):
        return None
    row = TelegramChat.query.filter_by(chat_id=str(chat["id"])).first()
    if not row:
        row = TelegramChat(chat_id=str(chat["id"]), role=role_hint or "")
        db.session.add(row)
    row.title = chat.get("title") or telegram_bot.person_name(chat) or row.title
    row.chat_type = chat.get("type") or row.chat_type
    row.last_seen = datetime.utcnow()
    return row


def remember_person(user):
    if not user or not user.get("id"):
        return None
    row = TelegramPerson.query.filter_by(tg_user_id=str(user["id"])).first()
    if not row:
        row = TelegramPerson(tg_user_id=str(user["id"]))
        db.session.add(row)
    row.name = telegram_bot.person_name(user) or row.name
    row.username = user.get("username") or row.username
    row.last_seen = datetime.utcnow()
    return row


def order_card_text(po, line=None):
    """Operator ke liye card ka text. Chhota rakho — phone pe padhna hai."""
    head = f"<b>{po.po_number}</b> — {po.customer.name}"
    if line is None:
        rows = []
        for l in po.lines:
            name = l.mapping.label if l.mapping else (l.item_code or l.raw_size_text)
            rows.append(f"• <b>{l.item_code or '—'}</b>  {name}  ×  "
                        f"{l.qty:g} {l.qty_unit}")
        body = "\n".join(rows)
        note = f"\n\n<i>{po.note}</i>" if po.note else ""
        return f"{head}\n\n{body}{note}"

    name = line.mapping.label if line.mapping else (line.item_code or line.raw_size_text)
    return (f"{head}\n\n<b>{line.item_code or '—'}</b>\n{name}\n"
            f"<b>{line.qty:g} {line.qty_unit}</b>")


def order_buttons(po):
    """Status ke hisaab se button. Ho chuka kaam dobara nahi dikhta."""
    if po.status == "with_operator":
        return [[{"text": "✅ OK — bana raha hoon", "callback_data": f"ok:{po.id}"}]]
    if po.status == "in_production":
        return [[{"text": "🟢 Done — ban gaya", "callback_data": f"done:{po.id}"}]]
    return []


def status_line(po):
    marks = {"with_operator": "🕐", "in_production": "🟡", "made": "🟢",
             "dispatched": "📦", "rejected": "❌"}
    who = f" — {po.operator_name}" if po.operator_name else ""
    return f"\n\n{marks.get(po.status, '')} <b>{STATUS_LABEL.get(po.status, po.status)}</b>{who}"


def fill_remembered_rates(po):
    """Jo rate pehle diye the wo apne aap bhar do. Nayi cheez khaali rehti hai."""
    filled = 0
    for line in po.lines:
        if line.rate or not line.map_id:
            continue
        known = PartyRate.look_up(line.map_id, line.qty_unit)
        if known and known.rate:
            line.rate = known.rate
            line.rate_from_memory = True
            filled += 1
    return filled


def rate_summary_text(po):
    rows = []
    for line in po.lines:
        name = line.mapping.label if line.mapping else (line.item_code or line.raw_size_text)
        head = f"<b>{line.item_code or '—'}</b>  {name}  ×  {line.qty:g} {line.qty_unit}"
        if not line.rate:
            rows.append(f"{head}\n   ⬜ <b>rate chahiye</b>")
        elif line.rate_from_memory:
            rows.append(f"{head}\n   ₹{line.rate:g}/{line.qty_unit} (pichli baar)"
                        f" = ₹{line.amount:,.0f}")
        else:
            rows.append(f"{head}\n   ₹{line.rate:g}/{line.qty_unit} = ₹{line.amount:,.0f}")
    body = "\n\n".join(rows)
    tail = ""
    if not po.no_rate_count:
        tail = f"\n\n<b>Total ₹{po.total:,.0f}</b>"
    else:
        tail = (f"\n\n{po.no_rate_count} line ka rate baaki hai — neeche wale msg pe "
                f"reply karke sirf number bhej do.")
    return (f"<b>{po.po_number}</b> — {po.customer.name}\n"
            f"<i>naya order aaya hai</i>\n\n{body}{tail}")


def rate_buttons(po):
    if po.status != "pending" or po.no_rate_count:
        return []
    return [[{"text": "✅ Rate theek hain — operator ko bhejo",
              "callback_data": f"rates:{po.id}"}]]


def ask_rates(po, token=None):
    """Aapke apne chat me order bhejo — jahan rate chahiye wahan poochho.

    Har us line ka apna msg jaata hai jiska rate nahi pata. Uske reply me sirf
    number bhejna hota hai — bot ko pata rehta hai ki wo reply kis line ka hai.
    """
    chat = chat_for_role("owner")
    if not chat:
        raise telegram_bot.TelegramError(
            "Aapka apna chat chuna nahi gaya. Bot ko private me /start bhejo, phir "
            "Telegram page pe use 'Aapka chat' bana do."
        )
    fill_remembered_rates(po)
    db.session.commit()

    res = telegram_bot.send_message(chat.chat_id, rate_summary_text(po),
                                    buttons=rate_buttons(po), token=token)
    po.tg_rate_summary_id = str(res.get("message_id")) if res else ""

    for line in po.lines:
        if line.rate:
            continue
        name = line.mapping.label if line.mapping else line.item_code
        ask = telegram_bot.send_message(
            chat.chat_id,
            f"<b>{line.item_code or '—'}</b> — {name}\n{line.qty:g} {line.qty_unit}\n\n"
            f"Is msg ka reply karke rate bhejo (per {line.qty_unit}).",
            token=token)
        line.tg_rate_msg_id = str(ask.get("message_id")) if ask else ""
    db.session.commit()
    return po.no_rate_count


def refresh_rate_summary(po, token=None):
    chat = chat_for_role("owner")
    if not chat or not po.tg_rate_summary_id:
        return
    try:
        telegram_bot.edit_message(chat.chat_id, po.tg_rate_summary_id,
                                  rate_summary_text(po), buttons=rate_buttons(po),
                                  token=token)
    except telegram_bot.TelegramError:
        pass


def set_line_rate(line, rate):
    """Ek jagah se rate lagta hai — screen se ho ya Telegram se."""
    line.rate = float(rate)
    line.rate_from_memory = False
    line.po.rates_ok_at = None      # naya rate, nayi mohar chahiye
    PartyRate.remember(line.mapping, line.qty_unit, line.rate)
    db.session.commit()


def set_line_unit(line, unit):
    """Line ki unit badlo, aur rate ko us unit ke hisaab se dobara dekho.

    Rate hamesha unit ke saath bandha hota hai — piece ka ₹25 roll pe lagana
    galat hai. Isliye unit badalte hi rate dobara dekha jaata hai, chahe wo
    yaad se aaya ho ya user ne khud daala ho: dono soorat me wo *purani* unit
    ka rate tha. Nayi unit ka rate mile toh lag jaata hai, na mile toh line
    khaali rehti hai taaki poochha ja sake.

    Purana rate kahin gaya nahi — wo apni unit pe yaad hai, aur unit wapas
    karte hi laut aata hai.
    """
    unit = (unit or "").strip()
    if not unit or unit == line.qty_unit:
        return False
    allowed = line.mapping.unit_choices() if line.mapping else []
    if allowed and unit not in allowed:
        return False
    line.qty_unit = unit
    line.po.rates_ok_at = None      # unit badli toh rate bhi badla — mohar hat gayi
    known = PartyRate.look_up(line.map_id, unit) if line.map_id else None
    if known and known.rate:
        line.rate = known.rate
        line.rate_from_memory = True
    else:
        line.rate = 0.0
        line.rate_from_memory = False
    return True


def make_invoice(po):
    """Order ban jaane par bill. Ek order ka ek hi bill — dobara nahi banta.

    Number wahi tareeke se milta hai jaise baaki app me: prefix + 4 ank, aur
    agla number pehle se use ho toh aage badh jaata hai.
    """
    from models import Settings, InvoiceItem
    if po.invoice_id:
        return db.session.get(Invoice, po.invoice_id), False
    if po.no_rate_count:
        raise ValueError("kuch lines ka rate nahi hai")
    if po.no_qty_count:
        raise ValueError("kuch lines me qty nahi hai")

    settings = Settings.get()
    n = settings.next_invoice_no or 1
    while Invoice.query.filter_by(
            invoice_no=f"{settings.invoice_prefix}-{str(n).zfill(4)}").first():
        n += 1
    inv = Invoice(
        invoice_no=f"{settings.invoice_prefix}-{str(n).zfill(4)}",
        date=date.today().isoformat(),
        customer_id=po.customer_id,
        created_by=po.confirmed_by or po.created_by,
        notes=f"Order {po.po_number}",
    )
    db.session.add(inv)
    db.session.flush()

    subtotal = 0.0
    for line in po.lines:
        name = line.mapping.label if line.mapping else (line.item_code or line.raw_size_text)
        desc = f"{line.item_code} — {name}" if line.item_code else name
        db.session.add(InvoiceItem(
            invoice_id=inv.id,
            item_id=line.mapping.item_id if line.mapping else None,
            description=desc[:200], qty=line.qty, unit=line.qty_unit,
            rate=line.rate, gst_rate=0.0,
            taxable_amount=line.amount, line_total=line.amount,
        ))
        subtotal += line.amount

    # GST abhi off hai — jab chalu hoga tab yahin rate aur amounts jud jayenge.
    inv.subtotal = round(subtotal, 2)
    inv.taxable_amount = inv.subtotal
    inv.grand_total = inv.subtotal
    settings.next_invoice_no = n + 1
    po.invoice_id = inv.id
    db.session.commit()
    return inv, True


def send_order_to_operator(po, token=None):
    """Har line ka apna card, photo ke saath. Aakhir me buttons wala card."""
    chat = chat_for_role("operator")
    if not chat:
        raise telegram_bot.TelegramError(
            "Operator group chuna nahi gaya. Telegram page pe jaake batao ki "
            "kaunsa group operator ka hai."
        )
    msg_ids = []
    for line in po.lines:
        photo = line.mapping.image_data if line.mapping else None
        res = telegram_bot.send_photo(chat.chat_id, photo, order_card_text(po, line),
                                      token=token)
        if res and res.get("message_id"):
            msg_ids.append(str(res["message_id"]))

    # Aakhir me ek summary card — buttons isi pe rehte hain, taaki har line pe
    # button na aaye aur operator galti se aadha order aage na badha de.
    po.status = "with_operator"
    po.sent_at = datetime.utcnow()
    po.tg_chat_id = chat.chat_id
    res = telegram_bot.send_message(
        chat.chat_id, order_card_text(po) + status_line(po),
        buttons=order_buttons(po), token=token)
    if res and res.get("message_id"):
        msg_ids.append(str(res["message_id"]))
    po.tg_message_ids = ",".join(msg_ids)
    db.session.commit()
    return len(msg_ids)


def refresh_order_card(po, token=None):
    """Aakhri (button wale) card ko nayi status ke saath update karo."""
    if not po.tg_chat_id or not po.tg_message_ids:
        return
    last = po.tg_message_ids.split(",")[-1]
    try:
        telegram_bot.edit_message(po.tg_chat_id, last,
                                  order_card_text(po) + status_line(po),
                                  buttons=order_buttons(po), token=token)
    except telegram_bot.TelegramError:
        pass   # card update na ho paye toh order to badal hi chuka hai


def notify_manager(po, token=None):
    chat = chat_for_role("manager")
    if not chat:
        return False
    lines = "\n".join(
        f"• {l.item_code or '—'}  {l.mapping.label if l.mapping else ''}  ×  "
        f"{l.qty:g} {l.qty_unit}" for l in po.lines)
    text = (f"📦 <b>Dispatch ke liye taiyaar</b>\n\n"
            f"<b>{po.po_number}</b> — {po.customer.name}\n{lines}")
    if po.operator_name:
        text += f"\n\nBanaya: {po.operator_name}"
    try:
        telegram_bot.send_message(chat.chat_id, text, token=token)
        return True
    except telegram_bot.TelegramError:
        return False


def move_status(po, new_status, who=""):
    """Ek hi jagah se status badalta hai — button se ho ya screen se.

    Galat chhalang (jaise pending se seedha dispatched) yahin ruk jaati hai.
    """
    if new_status == po.status:
        return False, f"Ye order pehle se {STATUS_LABEL[new_status]} hai."
    if new_status not in NEXT_STATUS.get(po.status, ()):
        return False, (f"{STATUS_LABEL.get(po.status, po.status)} se seedha "
                       f"{STATUS_LABEL.get(new_status, new_status)} nahi ho sakta.")

    now = datetime.utcnow()
    if new_status == "in_production":
        po.accepted_at = now
        po.operator_name = who or po.operator_name
    elif new_status == "made":
        po.made_at = now
        po.operator_name = who or po.operator_name
        for line in po.lines:
            if line.mapping:
                line.mapping.times_used = (line.mapping.times_used or 0) + 1
    elif new_status == "dispatched":
        po.dispatched_at = now
    po.status = new_status
    db.session.commit()

    # Ban gaya matlab bill ban sakta hai. Bill na ban paye (rate nahi hai, ya kuch
    # aur) toh order phir bhi ban chuka hai — usko rokna galat hoga.
    if new_status == "made" and not po.invoice_id:
        try:
            make_invoice(po)
        except Exception:
            db.session.rollback()
    return True, STATUS_LABEL[new_status]


def handle_update(update, token=None):
    """Telegram se aaya ek update. Kabhi exception nahi phenkta — webhook ko
    hamesha 200 chahiye, warna Telegram baar baar wahi update bhejta rehta hai."""
    chat = telegram_bot.chat_from_update(update)
    user = telegram_bot.user_from_update(update)
    if chat:
        remember_chat(chat)
    if user and not user.get("is_bot"):
        remember_person(user)
    db.session.commit()

    cq = update.get("callback_query")
    if not cq:
        return handle_rate_reply(update, token=token)

    data = cq.get("data") or ""
    action, _, po_id = data.partition(":")
    who = telegram_bot.person_name(cq.get("from"))

    def reply(text, alert=False):
        try:
            telegram_bot.answer_callback(cq.get("id"), text, alert, token=token)
        except telegram_bot.TelegramError:
            pass
        return text

    po = db.session.get(PurchaseOrder, int(po_id)) if po_id.isdigit() else None
    if not po:
        return reply("Ye order ab nahi hai.", alert=True)

    # Rate wala button aapke apne chat me hota hai, operator group me nahi.
    if action == "rates":
        owner = chat_for_role("owner")
        here = str(((cq.get("message") or {}).get("chat") or {}).get("id"))
        if not owner or here != owner.chat_id:
            return reply("Ye button sirf aapke chat me chalta hai.", alert=True)
        if po.no_rate_count:
            return reply("Abhi kuch lines ka rate baaki hai.", alert=True)
        po.rates_ok_at = datetime.utcnow()
        db.session.commit()
        try:
            send_order_to_operator(po, token=token)
        except telegram_bot.TelegramError as exc:
            return reply(str(exc), alert=True)
        refresh_rate_summary(po, token=token)
        return reply("Operator ko bhej diya.")

    if not is_operator((cq.get("from") or {}).get("id")):
        return reply("Aap operator ki list me nahi ho — office se puchho.", alert=True)

    target = {"ok": "in_production", "done": "made"}.get(action)
    if not target:
        return reply("Ye button samajh nahi aaya.")

    moved, msg = move_status(po, target, who=who)
    refresh_order_card(po, token=token)
    if moved and target == "made":
        notify_manager(po, token=token)
        if po.invoice_id:
            notify_bill_ready(po, db.session.get(Invoice, po.invoice_id), token=token)
    return reply(msg if moved else msg, alert=not moved)


RATE_TEXT_RE = re.compile(r"^\s*(?:(?P<code>[A-Za-z]{1,6}\s*-?\s*\d{1,4})\s*[-:=]?\s*)?"
                          r"(?P<num>\d+(?:\.\d+)?)\s*$")


def pending_rate_lines():
    """Jin lines ka rate abhi tak nahi aaya — purane order pehle."""
    return (POLine.query.join(PurchaseOrder)
            .filter(PurchaseOrder.status == "pending",
                    (POLine.rate == 0) | (POLine.rate.is_(None)))
            .order_by(POLine.po_id, POLine.line_no).all())


def find_rate_line(parent_msg_id, code):
    """Number kis line ka hai — teen tareeke se dhoondho.

    1. Us sawaal ka reply hai (sabse pakka)
    2. Number ke saath code likha hai ("GME01 25")
    3. Sirf number hai — tab tabhi maanenge jab ek hi line ka rate baaki ho

    Teesra isliye ki phone pe log seedha number type kar dete hain, reply karna
    yaad nahi rehta. Par ek se zyada line baaki ho toh andaza lagana khatarnaak
    hai — tab poochh lete hain.
    """
    if parent_msg_id:
        line = POLine.query.filter_by(tg_rate_msg_id=str(parent_msg_id)).first()
        if line:
            return line, ""

    waiting = pending_rate_lines()
    if code:
        hits = [l for l in waiting if l.item_code == normalize_code(code)]
        if len(hits) == 1:
            return hits[0], ""
        if len(hits) > 1:
            return None, (f"{normalize_code(code)} ki ek se zyada line rate ka "
                          f"intezaar kar rahi hai — us sawaal pe reply karke bhejo.")
        return None, f"{normalize_code(code)} ki koi line rate ka intezaar nahi kar rahi."

    if len(waiting) == 1:
        return waiting[0], ""
    if not waiting:
        return None, "Abhi kisi line ka rate baaki nahi hai."
    return None, (f"{len(waiting)} line ka rate baaki hai — jis sawaal ka rate hai "
                  f"us msg pe reply karo, ya code ke saath bhejo jaise "
                  f"<code>{waiting[0].item_code or 'GME01'} 25</code>.")


def handle_rate_reply(update, token=None):
    """Aapke apne chat me aaya number — kisi line ka rate."""
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip().replace("\u20b9", "").replace(",", "")
    chat = msg.get("chat") or {}

    owner = chat_for_role("owner")
    if not (text and owner and str(chat.get("id")) == owner.chat_id):
        return "noted"
    if text.startswith("/"):
        return "noted"

    m = RATE_TEXT_RE.match(text)
    parent = (msg.get("reply_to_message") or {}).get("message_id")
    if not m:
        if parent:
            telegram_bot.send_message(
                owner.chat_id, "Sirf number bhejo — jaise <code>25</code>.", token=token)
            return "bad number"
        return "noted"

    line, problem = find_rate_line(parent, m.group("code"))
    if problem:
        telegram_bot.send_message(owner.chat_id, problem, token=token)
        return "unclear"
    if not line:
        return "noted"

    rate = float(m.group("num"))
    if rate <= 0:
        telegram_bot.send_message(owner.chat_id, "Rate zero se bada hona chahiye.",
                                  token=token)
        return "bad number"

    set_line_rate(line, rate)
    po = line.po
    refresh_rate_summary(po, token=token)
    left = po.no_rate_count
    telegram_bot.send_message(
        owner.chat_id,
        f"\u2705 <b>{line.item_code or line.raw_size_text}</b> \u2014 "
        f"\u20b9{rate:g}/{line.qty_unit} yaad rakh liya."
        + ("" if left else "\n\nSab rate aa gaye. Upar wale card se operator ko bhej do."),
        token=token)
    return "rate set"


def notify_bill_ready(po, inv, token=None):
    """Bill ban gaya — aapko aur manager ko chhoti si khabar."""
    text = (f"🧾 <b>Bill ban gaya</b> — {inv.invoice_no}\n"
            f"{po.po_number} · {po.customer.name}\n"
            f"<b>₹{inv.grand_total:,.0f}</b>\n\n"
            f"<i>Print accountant karega.</i>")
    for role in ("owner", "manager"):
        chat = chat_for_role(role)
        if not chat:
            continue
        try:
            telegram_bot.send_message(chat.chat_id, text, token=token)
        except telegram_bot.TelegramError:
            pass


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
            flash(t("po_f_party_and_number"), "error")
            return render_template("po_new.html", customers=customers, today=date.today().isoformat())
        customer_id = int(customer_id)

        dupe = PurchaseOrder.query.filter_by(customer_id=customer_id, po_number=po_number).first()
        if dupe:
            flash(t("po_f_duplicate", no=po_number), "error")
            return redirect(url_for("po.po_review", po_id=dupe.id))

        unit = request.form.get("size_unit", "inch")
        PartyPOConfig.set_unit(customer_id, unit)

        try:
            scan_data, scan_mime, scan_name = _read_upload(
                request.files.get("scan"), SCAN_MAX_DIM, SCAN_TARGET_BYTES)
        except ValueError:
            flash(t("po_f_scan_too_big"), "error")
            return render_template("po_new.html", customers=customers, today=date.today().isoformat())

        # Screen pe jo lines dikh rahi thi, wahi yahan aati hain — chahe wo
        # photo se bhari gayi hon, paste se, ya haath se. Padhna pehle ho
        # chuka hota hai; yahan sirf wahi darj hota hai jo aadmi ne dekha.
        if request.form.get("entry_mode") == "rows":
            parsed = lines_from_rows(request.form, unit)
            raw_text = "\n".join(p["raw_text"] for p in parsed)
        else:
            raw_text = request.form.get("raw_text", "")
            parsed = parse_po_text(raw_text, unit, known_codes_for(customer_id))
        if not parsed:
            flash(t("po_f_no_lines"), "error")
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

        fill_remembered_rates(po)
        db.session.commit()

        msg = t("po_f_read_lines", no=po_number, n=len(parsed))
        # Rate aapke Telegram chat pe poochh lo — wahi flow hai jo tay hua tha.
        # Chat set na ho toh koi baat nahi, rate yahin screen pe daal do.
        if chat_for_role("owner"):
            try:
                ask_rates(po)
                msg += t("po_f_rates_asked")
            except telegram_bot.TelegramError as exc:
                flash(t("po_f_rate_ask_failed", reason=exc), "error")
        flash(msg, "success")
        return redirect(url_for("po.po_review", po_id=po.id))

    return render_template("po_new.html", customers=customers, today=date.today().isoformat())


def _rows_for_form(text, customer_id, unit, fuzzy):
    """Padhe hue text ko form ki lines me badlo.

    Photo se aaya ho ya paste kiya gaya ho — aage dono ek jaise chalte hain.
    """
    known = known_codes_for(customer_id) if customer_id else []
    rows = []
    for p in parse_po_text(text, unit, known, fuzzy=fuzzy):
        size = p["raw_size_text"]
        # Code mil gaya toh product ka apna size hi sahi hai.
        #
        # Photo se aaye ank pe bharosa nahi kiya ja sakta — OCR 26x15x6 ko
        # 26x16x6 padh deta hai, aur us ank ko jaanchne ka koi zariya nahi.
        # Code alag maamla hai: use party ki chhoti si list se milaya jaata
        # hai, isliye wo pakka hota hai. Aur is dhande me ek code = ek
        # product = ek size, toh code pata hone ka matlab size bhi pata hai.
        #
        # Haath se likhe text me ye jaayaz nahi — wahan jo likha hai wahi
        # rehna chahiye, aur size alag ho toh review screen use pakadti hai.
        if p["item_code"] and customer_id and (fuzzy or not size):
            m = map_by_code(customer_id, p["item_code"])
            if m and m.raw_size_text:
                size = m.raw_size_text
        rows.append({"code": p["item_code"], "size": size,
                     "qty": p["qty"] or "", "unit": p["qty_unit"]})
    return rows


@po_bp.route("/read-text", methods=["POST"])
@login_required
def po_read_text():
    """Paste kiya hua text — usi form ki lines me badal ke lauta do."""
    cid = request.form.get("customer_id") or ""
    rows = _rows_for_form(request.form.get("text", ""),
                          int(cid) if cid.isdigit() else None,
                          request.form.get("size_unit", "inch"),
                          fuzzy=False)
    return jsonify({"ok": True, "rows": rows})


@po_bp.route("/read-file", methods=["POST"])
@login_required
def po_read_file():
    """Photo ya PDF lo, aur form ke liye lines lauta do.

    Yahan order banta nahi. Sirf padha jaata hai, aur nateeja form me bhar
    diya jaata hai — taaki aadmi har line dekh ke haan kahe. OCR kabhi poora
    sahi nahi padhta, isliye aakhri faisla screen pe hi hota hai.
    """
    cid = request.form.get("customer_id") or ""
    unit = request.form.get("size_unit", "inch")
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "error": t("po_ocr_no_file")}), 400

    data = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return jsonify({"ok": False, "error": t("po_f_scan_too_big")}), 400

    try:
        text = ocr_read.read_text(data, upload.mimetype, upload.filename)
    except ocr_read.OcrUnavailable:
        return jsonify({"ok": False, "error": t("po_ocr_off")}), 503
    except ocr_read.OcrFailed as exc:
        return jsonify({"ok": False, "error": t("po_ocr_failed", reason=exc)}), 422

    # Photo se aaya hai, isliye galat padhe code ko party ki list se milane do
    rows = _rows_for_form(text, int(cid) if cid.isdigit() else None, unit, fuzzy=True)
    return jsonify({"ok": True, "rows": rows, "text": text})


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
    # Office se sirf peeche laane aur cancel karne wale raste dikhao — aage
    # badhana operator ka kaam hai, Telegram pe.
    forward = {"pending": "with_operator", "with_operator": "in_production",
               "in_production": "made", "made": "dispatched"}
    moves = [target for target in NEXT_STATUS.get(po.status, ())
             if target != forward.get(po.status)]
    return render_template(
        "po_review.html", po=po, line_view=line_view,
        items=Item.query.order_by(Item.name).all(),
        mixed=party_is_mixed(po.customer_id),
        party_unit=PartyPOConfig.unit_for(po.customer_id),
        status_moves=moves,
        invoice=db.session.get(Invoice, po.invoice_id) if po.invoice_id else None,
        has_owner_chat=bool(chat_for_role("owner")),
    )


@po_bp.route("/<int:po_id>/line/<int:line_id>/choose", methods=["POST"])
@login_required
def po_line_choose(po_id, line_id):
    """Operator ne maujuda mapping me se ek chuna."""
    line = POLine.query.filter_by(id=line_id, po_id=po_id).first_or_404()
    map_id = request.form.get("map_id") or ""
    if not map_id.isdigit():
        flash(t("po_f_nothing_selected"), "error")
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
        flash(t("po_f_name_required"), "error")
        return redirect(url_for("po.po_review", po_id=po_id))

    item_code = normalize_code(request.form.get("item_code", "") or line.item_code)
    if item_code and map_by_code(line.po.customer_id, item_code):
        flash(t("po_f_code_exists", code=item_code), "error")
        return redirect(url_for("po.po_review", po_id=po_id))

    try:
        img_data, img_mime, _ = _read_upload(request.files.get("image"))
    except ValueError:
        flash(t("po_f_photo_too_big"), "error")
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
    if not m.item_id:
        ensure_item_for(m)
    line.map_id = m.id
    line.match_status = "manual"
    line.size_mismatch = False
    db.session.commit()
    remembered = (t("po_f_remember_code", code=item_code) if item_code
                  else t("po_f_remember_size"))
    flash(t("po_f_mapped", label=label, what=remembered), "success")
    return redirect(url_for("po.po_review", po_id=po_id))


@po_bp.route("/<int:po_id>/rates", methods=["POST"])
@login_required
def po_set_rates(po_id):
    """Screen se rate daalna — Telegram ka doosra raasta."""
    po = PurchaseOrder.query.get_or_404(po_id)

    # Pehle unit, phir rate — kyunki unit badalne se rate dobara dekha jaata
    # hai, aur usi submit me type kiya hua rate uske upar aana chahiye.
    units_changed = 0
    for line in po.lines:
        if set_line_unit(line, request.form.get(f"unit_{line.id}", "")):
            units_changed += 1
    if units_changed:
        db.session.commit()

    changed = 0
    for line in po.lines:
        raw = (request.form.get(f"rate_{line.id}", "") or "").strip().replace(",", "")
        if not raw:
            continue
        try:
            rate = float(raw)
        except ValueError:
            flash(t("po_f_rate_not_number", what=line.item_code or line.raw_size_text), "error")
            continue
        if rate <= 0:
            flash(t("po_f_rate_positive"), "error")
            continue
        if rate != line.rate or line.rate_from_memory:
            set_line_rate(line, rate)
            changed += 1
    if changed or units_changed:
        refresh_rate_summary(po)
    if units_changed:
        flash(t("po_f_units_changed", n=units_changed), "success")
    if changed:
        flash(t("po_f_rates_saved", n=changed), "success")
    return redirect(url_for("po.po_review", po_id=po.id))


@po_bp.route("/<int:po_id>/ask-rates", methods=["POST"])
@login_required
def po_ask_rates(po_id):
    """Rate ka sawaal Telegram pe dobara bhejo.

    Zaroorat tab padti hai jab order aate waqt aapka chat set na ho, ya purane
    msg kahin dab gaye hon.
    """
    po = PurchaseOrder.query.get_or_404(po_id)
    if po.status != "pending":
        flash(t("po_f_already_moved"), "error")
        return redirect(url_for("po.po_review", po_id=po.id))
    try:
        left = ask_rates(po)
    except telegram_bot.TelegramError as exc:
        flash(str(exc), "error")
        return redirect(url_for("po.po_review", po_id=po.id))
    flash(t("po_f_asked_telegram", n=left) if left
          else t("po_f_asked_nothing"), "success")
    return redirect(url_for("po.po_review", po_id=po.id))


@po_bp.route("/<int:po_id>/confirm", methods=["POST"])
@login_required
def po_confirm(po_id):
    """Office ne review poora kiya — ab card operator group me jayega."""
    po = PurchaseOrder.query.get_or_404(po_id)
    if po.status != "pending":
        flash(t("po_f_already_moved"), "error")
        return redirect(url_for("po.po_review", po_id=po.id))
    if po.unresolved_count:
        flash(t("po_f_unmapped", n=po.unresolved_count), "error")
        return redirect(url_for("po.po_review", po_id=po.id))
    if po.no_qty_count:
        flash(t("po_f_qty_missing", n=po.no_qty_count), "error")
        return redirect(url_for("po.po_review", po_id=po.id))
    if po.no_rate_count:
        flash(t("po_f_rate_missing", n=po.no_rate_count), "error")
        return redirect(url_for("po.po_review", po_id=po.id))
    # Bill isi rate se banta hai, isliye rate pe aapki mohar chahiye — wo
    # Telegram pe lagti hai. Telegram set hi na ho toh rok lagana bemani hai:
    # jis bot pe haan karni hai wo hai hi nahi.
    if chat_for_role("owner") and not po.rates_ok_at:
        flash(t("po_f_rates_not_ok"), "error")
        return redirect(url_for("po.po_review", po_id=po.id))

    po.confirmed_at = datetime.utcnow()
    po.confirmed_by = current_user.id
    try:
        sent = send_order_to_operator(po)
    except telegram_bot.TelegramError as exc:
        db.session.rollback()
        flash(t("po_f_send_failed", reason=exc), "error")
        return redirect(url_for("po.po_review", po_id=po.id))
    flash(t("po_f_sent", no=po.po_number, n=sent), "success")
    return redirect(url_for("po.po_review", po_id=po.id))


@po_bp.route("/<int:po_id>/status", methods=["POST"])
@login_required
def po_set_status(po_id):
    """Office ki taraf se status badalna — galti sudhaarne ke liye.

    Operator Telegram pe sirf aage badhata hai. Peeche laana, cancel karna, ya
    seedha dispatched karna — sab yahin se hota hai.
    """
    po = PurchaseOrder.query.get_or_404(po_id)
    target = request.form.get("to", "")
    if target not in PO_STATUSES:
        abort(400)
    if target == "rejected":
        po.note = (request.form.get("note", "").strip() or po.note)
    moved, msg = move_status(po, target, who="")
    if moved:
        refresh_order_card(po)
        if target == "made":
            notify_manager(po)
        flash(t("po_f_status_now", no=po.po_number,
                label=t("po_status_" + target)), "success")
    else:
        flash(t("po_f_already_moved"), "error")
    back = request.form.get("back") or url_for("po.po_review", po_id=po.id)
    return redirect(back)


@po_bp.route("/<int:po_id>/reject", methods=["POST"])
@login_required
def po_reject(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    po.note = (request.form.get("note", "").strip() or po.note)
    moved, msg = move_status(po, "rejected")
    if moved:
        refresh_order_card(po)
        flash(t("po_f_cancelled", no=po.po_number), "success")
    else:
        flash(t("po_f_already_moved"), "error")
    return redirect(url_for("po.po_list"))


@po_bp.route("/dispatch")
@login_required
def po_dispatch():
    rows = (PurchaseOrder.query.filter_by(status="made")
            .order_by(PurchaseOrder.made_at.desc()).all())
    return render_template("po_dispatch.html", pos=rows)


@po_bp.route("/<int:po_id>/dispatched", methods=["POST"])
@login_required
def po_mark_dispatched(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    moved, msg = move_status(po, "dispatched")
    if moved:
        refresh_order_card(po)
        flash(t("po_f_dispatched", no=po.po_number), "success")
    else:
        flash(t("po_f_already_moved"), "error")
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
        flash(t("po_f_drive_need_link"), "error")
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
    flash(t("po_f_drive_opened", name=meta["name"], n=len(found)), "success")
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
    flash(t("po_f_drive_linked"), "success")
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
        flash(t("po_f_drive_link_first"), "error")
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

    flash(t("po_f_drive_synced", seen=total["seen"], added=total["added"],
            updated=total["updated"], photos=total["photos"]), "success")
    for s in skipped[:8]:
        flash(t("po_f_drive_skipped", list=s), "error")
    if len(skipped) > 8:
        flash(t("po_f_drive_skipped_more", n=len(skipped) - 8), "error")
    return redirect(url_for("po.drive_page"))


def bills_for(sel_date, show):
    """Us din ke order-wale bill. Ek order ka ek bill, isliye order ke saath hi laate hain."""
    q = (PurchaseOrder.query
         .filter(PurchaseOrder.invoice_id.isnot(None))
         .order_by(PurchaseOrder.made_at))
    rows = []
    total = 0.0
    for po in q.all():
        inv = db.session.get(Invoice, po.invoice_id)
        if not inv or inv.date != sel_date:
            continue
        if show == "unprinted" and po.bill_printed_at:
            continue
        rows.append({"po": po, "inv": inv})
        total += inv.grand_total or 0
    return rows, round(total, 2)


@po_bp.route("/bills")
@login_required
def bills_page():
    sel_date = request.args.get("date") or date.today().isoformat()
    show = request.args.get("show", "unprinted")
    rows, total = bills_for(sel_date, show)
    return render_template("po_bills.html", rows=rows, total=total,
                           sel_date=sel_date, show=show)


@po_bp.route("/bills/print")
@login_required
def bills_print():
    """Saare bill ek page pe, har ek apne kaagaz pe. Wahi shakal jo ek bill ki hai."""
    from models import Settings
    sel_date = request.args.get("date") or date.today().isoformat()
    show = request.args.get("show", "unprinted")
    rows, _ = bills_for(sel_date, show)
    if not rows:
        flash(t("po_f_no_bills_to_print"), "error")
        return redirect(url_for("po.bills_page", date=sel_date, show=show))
    return render_template("po_bills_print.html", rows=rows, sel_date=sel_date,
                           settings=Settings.get())


@po_bp.route("/bills/<int:po_id>/printed", methods=["POST"])
@login_required
def bill_mark_printed(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    po.bill_printed_at = datetime.utcnow() if request.form.get("printed") == "1" else None
    db.session.commit()
    return redirect(request.referrer or url_for("po.bills_page"))


@po_bp.route("/telegram")
@login_required
def telegram_page():
    bot_name = ""
    error = ""
    key_set = bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
    if key_set:
        try:
            bot_name = (telegram_bot.get_me() or {}).get("username", "")
        except telegram_bot.TelegramError as exc:
            error = str(exc)
    return render_template(
        "po_telegram.html",
        key_set=key_set, bot_name=bot_name, error=error,
        chats=TelegramChat.query.order_by(TelegramChat.last_seen.desc()).all(),
        people=TelegramPerson.query.order_by(TelegramPerson.last_seen.desc()).all(),
        hooked=bool(POSetting.get(TG_SECRET_KEY)),
    )


@po_bp.route("/telegram/chat/<int:row_id>/role", methods=["POST"])
@login_required
def telegram_set_role(row_id):
    row = db.session.get(TelegramChat, row_id) or abort(404)
    role = request.form.get("role", "")
    if role not in ("", "operator", "manager", "owner"):
        abort(400)
    if role:      # ek role sirf ek chat ka
        for other in TelegramChat.query.filter_by(role=role).all():
            if other.id != row.id:
                other.role = ""
    row.role = role
    db.session.commit()
    flash(t("po_f_role_set", name=row.title or row.chat_id,
            role=role or t("po_f_role_none")), "success")
    return redirect(url_for("po.telegram_page"))


@po_bp.route("/telegram/person/<int:row_id>/operator", methods=["POST"])
@login_required
def telegram_set_operator(row_id):
    row = db.session.get(TelegramPerson, row_id) or abort(404)
    row.is_operator = request.form.get("is_operator") == "1"
    db.session.commit()
    flash(t("po_f_operator_set", name=row.name or row.tg_user_id,
            state=t("po_f_can_press") if row.is_operator else t("po_f_cannot_press")),
          "success")
    return redirect(url_for("po.telegram_page"))


@po_bp.route("/telegram/hook", methods=["POST"])
@login_required
def telegram_set_hook():
    """Telegram ko batao ki updates kahan bhejni hain."""
    secret = POSetting.get(TG_SECRET_KEY)
    if not secret:
        secret = uuid.uuid4().hex
        POSetting.put(TG_SECRET_KEY, secret)
        db.session.commit()
    hook_url = url_for("po.telegram_webhook", secret=secret, _external=True)
    if hook_url.startswith("http://"):
        hook_url = "https://" + hook_url[len("http://"):]
    try:
        telegram_bot.set_webhook(hook_url, secret_token=secret)
    except telegram_bot.TelegramError as exc:
        flash(str(exc), "error")
        return redirect(url_for("po.telegram_page"))
    flash(t("po_f_bot_connected"),
          "success")
    return redirect(url_for("po.telegram_page"))


@po_bp.route("/telegram/hook/<secret>", methods=["POST"])
def telegram_webhook(secret):
    """Telegram yahan updates bhejta hai. Login nahi hota — isliye secret URL me
    hai, aur Telegram ka apna secret header bhi check hota hai.

    Yahan se hamesha 200 jaata hai. Error par 500 dene se Telegram wahi update
    baar baar bhejta rehta hai, aur order do baar aage badh sakta hai.
    """
    if not secret or secret != POSetting.get(TG_SECRET_KEY):
        abort(404)
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if header and header != secret:
        abort(403)
    try:
        handle_update(request.get_json(force=True, silent=True) or {})
    except Exception:
        db.session.rollback()
    return "", 200


@po_bp.route("/mappings")
@login_required
def mappings_page():
    cid = request.args.get("customer_id") or ""
    rows = PartyProductMap.query
    if cid.isdigit():
        rows = rows.filter_by(customer_id=int(cid))
    rows = rows.order_by(PartyProductMap.customer_id, PartyProductMap.canonical_key).all()
    # Jo product abhi tak Item master me nahi tha, use abhi ghar de do — screen
    # khulte hi list saaf ho jaati hai, alag se koi button dabana nahi padta.
    if any(r.item_id is None for r in rows):
        for r in rows:
            if r.item_id is None:
                ensure_item_for(r)
        db.session.commit()
    return render_template("po_mappings.html", maps=rows, cid=cid,
                           categories=Category.query.order_by(Category.name).all(),
                           customers=Customer.query.order_by(Customer.name).all())


@po_bp.route("/mappings/categories", methods=["POST"])
@login_required
def set_map_categories():
    """Ek saath kai products ki category set karo.

    Category hi batati hai ki maal kis unit me bikta hai, isliye ye screen
    ek-ek karke bharne layak nahi — poori table ek form hai, ek hi Save.
    """
    changed = 0
    for m in PartyProductMap.query.all():
        field = f"cat_{m.id}"
        if field not in request.form:
            continue                      # is baar screen pe tha hi nahi
        raw = (request.form.get(field) or "").strip()
        new_id = int(raw) if raw.isdigit() else None
        item = ensure_item_for(m)
        if item is None or item.category_id == new_id:
            continue
        item.category_id = new_id
        _align_item_unit(item)
        changed += 1
    db.session.commit()
    if changed:
        flash(t("po_f_categories_changed", n=changed), "success")
    else:
        flash(t("po_f_nothing_changed"), "success")
    return redirect(url_for("po.mappings_page",
                            customer_id=request.form.get("customer_id") or None))


@po_bp.route("/mappings/<int:map_id>/delete", methods=["POST"])
@login_required
def delete_mapping(map_id):
    m = PartyProductMap.query.get_or_404(map_id)
    if not current_user.is_owner:
        flash(t("po_f_owner_only_delete"), "error")
        return redirect(url_for("po.mappings_page"))
    if POLine.query.filter_by(map_id=m.id).first():
        flash(t("po_f_product_in_use"), "error")
        return redirect(url_for("po.mappings_page"))
    db.session.delete(m)
    db.session.commit()
    flash(t("po_f_product_deleted"), "success")
    return redirect(url_for("po.mappings_page"))
