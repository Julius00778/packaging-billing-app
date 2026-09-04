from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

# Standard GST state codes (India). Used for "Place of Supply" display on invoices.
STATES = [
    ("01", "Jammu & Kashmir"), ("02", "Himachal Pradesh"), ("03", "Punjab"),
    ("04", "Chandigarh"), ("05", "Uttarakhand"), ("06", "Haryana"),
    ("07", "Delhi"), ("08", "Rajasthan"), ("09", "Uttar Pradesh"),
    ("10", "Bihar"), ("11", "Sikkim"), ("12", "Arunachal Pradesh"),
    ("13", "Nagaland"), ("14", "Manipur"), ("15", "Mizoram"),
    ("16", "Tripura"), ("17", "Meghalaya"), ("18", "Assam"),
    ("19", "West Bengal"), ("20", "Jharkhand"), ("21", "Odisha"),
    ("22", "Chhattisgarh"), ("23", "Madhya Pradesh"), ("24", "Gujarat"),
    ("26", "Dadra and Nagar Haveli and Daman and Diu"), ("27", "Maharashtra"),
    ("29", "Karnataka"), ("30", "Goa"), ("31", "Lakshadweep"),
    ("32", "Kerala"), ("33", "Tamil Nadu"), ("34", "Puducherry"),
    ("35", "Andaman and Nicobar Islands"), ("36", "Telangana"),
    ("37", "Andhra Pradesh"), ("38", "Ladakh"),
]
STATE_NAMES = [s[1] for s in STATES]

# Small safe font choices — kept to Base14 PDF fonts (Helvetica/Times/Courier) so the
# same choice works for both the HTML print view and the reportlab PDF without extra
# font files (avoids the past non-ASCII-glyph PDF breakage).
INVOICE_FONTS = {
    "helvetica": {"label": "Helvetica (Clean)", "css": "'Inter','Noto Sans Devanagari',system-ui,sans-serif", "pdf": "Helvetica", "pdf_bold": "Helvetica-Bold"},
    "times": {"label": "Times (Classic)", "css": "'Times New Roman',Georgia,serif", "pdf": "Times-Roman", "pdf_bold": "Times-Bold"},
    "courier": {"label": "Courier (Typewriter)", "css": "'Courier New',monospace", "pdf": "Courier", "pdf_bold": "Courier-Bold"},
}


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(128), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="staff")  # 'owner' or 'staff'
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def is_owner(self):
        return self.role == "owner"


class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    firm_name = db.Column(db.String(200), default="")
    address = db.Column(db.String(300), default="")
    phone = db.Column(db.String(40), default="")
    email = db.Column(db.String(120), default="")
    gstin = db.Column(db.String(20), default="")
    state = db.Column(db.String(80), default="Maharashtra")
    bank_name = db.Column(db.String(120), default="")
    bank_acc = db.Column(db.String(60), default="")
    ifsc = db.Column(db.String(20), default="")
    invoice_prefix = db.Column(db.String(20), default="INV")
    next_invoice_no = db.Column(db.Integer, default=1)

    # --- Invoice / print settings ---
    show_gstin_on_invoice = db.Column(db.Boolean, default=True)
    default_print_copies = db.Column(db.String(4), default="2")  # "1" or "2"
    invoice_default_notes = db.Column(db.String(500), default="")
    challan_prefix = db.Column(db.String(20), default="DC")
    next_challan_no = db.Column(db.Integer, default=1)

    # --- Item / stock defaults ---
    default_unit = db.Column(db.String(20), default="pcs")
    default_gst_rate = db.Column(db.Float, default=18.0)
    default_reorder_level = db.Column(db.Float, default=0.0)

    # --- Party (customer/vendor) defaults ---
    default_customer_state = db.Column(db.String(80), default="")
    default_credit_days = db.Column(db.Integer, default=30)

    # --- Appearance ---
    invoice_font = db.Column(db.String(20), default="helvetica")
    theme_color = db.Column(db.String(10), default="#A8722E")

    # --- Staff permissions (global policy toggles) ---
    staff_can_edit_price = db.Column(db.Boolean, default=True)
    staff_can_give_discount = db.Column(db.Boolean, default=True)
    staff_can_edit_invoice = db.Column(db.Boolean, default=True)

    @staticmethod
    def get():
        s = Settings.query.first()
        if not s:
            s = Settings()
            db.session.add(s)
            db.session.commit()
        return s


class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    address = db.Column(db.String(300), default="")
    phone = db.Column(db.String(40), default="")
    gstin = db.Column(db.String(20), default="")
    state = db.Column(db.String(80), default="")
    opening_balance = db.Column(db.Float, default=0.0)  # +ve = customer owes firm
    credit_days = db.Column(db.Integer, default=30)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Category(db.Model):
    """Product category/group, e.g. 'Foam Boxes', 'LD Pouches', 'Blister Packs', 'Bubble Wrap'.

    `units` holds the units this kind of product is actually sold in, comma
    separated — foam goes out as pieces or rolls, scrap only by weight. An
    order line for an item in this category may only pick from this list, so a
    roll rate can never land on a piece quantity.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    units = db.Column(db.String(200), default="")
    # Kuch maal rang me aata hai — foam black aur white dono me. Rang order
    # bharte waqt chunna padta hai, isliye jaayaz rang yahin likhe rehte hain.
    # Khaali ka matlab "is maal me rang ka sawaal hi nahi".
    colours = db.Column(db.String(200), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def unit_list(self):
        """Allowed units, in the order they were entered. Empty means 'no rule'."""
        return [u.strip() for u in (self.units or "").split(",") if u.strip()]

    def colour_list(self):
        """Is maal ke jaayaz rang. Khaali list = rang poochha hi nahi jaata."""
        return [c.strip() for c in (self.colours or "").split(",") if c.strip()]


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    hsn_code = db.Column(db.String(20), default="")
    unit = db.Column(db.String(20), default="pcs")
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=True)
    gst_rate = db.Column(db.Float, default=18.0)
    sale_price = db.Column(db.Float, default=0.0)
    current_stock = db.Column(db.Float, default=0.0)
    reorder_level = db.Column(db.Float, default=0.0)
    track_stock = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    category = db.relationship("Category")


class StockEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False)
    qty = db.Column(db.Float, nullable=False)  # positive number
    entry_type = db.Column(db.String(10), nullable=False)  # 'in' or 'adjust'
    note = db.Column(db.String(200), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))

    item = db.relationship("Item")


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_no = db.Column(db.String(40), unique=True, nullable=False)
    date = db.Column(db.String(10), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))

    subtotal = db.Column(db.Float, default=0.0)
    discount_type = db.Column(db.String(10), default="amount")
    discount_value = db.Column(db.Float, default=0.0)
    discount_amount = db.Column(db.Float, default=0.0)
    other_charges = db.Column(db.Float, default=0.0)
    taxable_amount = db.Column(db.Float, default=0.0)
    cgst_amount = db.Column(db.Float, default=0.0)
    sgst_amount = db.Column(db.Float, default=0.0)
    igst_amount = db.Column(db.Float, default=0.0)
    grand_total = db.Column(db.Float, default=0.0)

    payment_status = db.Column(db.String(10), default="unpaid")  # unpaid/partial/paid
    amount_received = db.Column(db.Float, default=0.0)
    notes = db.Column(db.String(500), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Delivery-challan support: goods sent to a party without pricing shown, rate
    # added later when the batch is billed (common in this business — payment for
    # a customer's total deliveries is settled 1-2 months after dispatch).
    hide_pricing = db.Column(db.Boolean, default=False)
    consolidated_into_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=True)

    customer = db.relationship("Customer", foreign_keys=[customer_id])
    creator = db.relationship("User")
    items = db.relationship("InvoiceItem", backref="invoice", cascade="all, delete-orphan")
    consolidated_into = db.relationship("Invoice", remote_side=[id], foreign_keys=[consolidated_into_id])

    @property
    def is_challan(self):
        return bool(self.hide_pricing)


class InvoiceItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=True)
    description = db.Column(db.String(200), nullable=False)
    hsn_code = db.Column(db.String(20), default="")
    qty = db.Column(db.Float, default=0.0)
    unit = db.Column(db.String(20), default="pcs")
    rate = db.Column(db.Float, default=0.0)
    gst_rate = db.Column(db.Float, default=18.0)
    taxable_amount = db.Column(db.Float, default=0.0)
    cgst_amount = db.Column(db.Float, default=0.0)
    sgst_amount = db.Column(db.Float, default=0.0)
    igst_amount = db.Column(db.Float, default=0.0)
    line_total = db.Column(db.Float, default=0.0)
    # Rang line ke saath chalta hai, item ke saath nahi — ek hi foam black bhi
    # jaata hai aur white bhi. Isliye bill ki line pe hi likha jaata hai.
    colour = db.Column(db.String(40), default="")

    item = db.relationship("Item")

    @property
    def category_name(self):
        """Maal ki category — bill aur challan dono pe chhapti hai.

        Chhapte waqt Item master se aati hai, line me likhi hui nahi hoti.
        Isliye category baad me theek karo toh purane bill bhi sahi chhapte
        hain, aur bill ka apna record kabhi nahi badalta.

        Line kisi item se juddi na ho (haath se likhi gayi ho) ya us item ki
        category na lagi ho toh khaali — us jagah kuch nahi chhapega.
        """
        it = self.item
        if it and it.category_id and it.category:
            return it.category.name or ""
        return ""


# ---------------- Accounts module ----------------

class Payment(db.Model):
    """Customer receipts — can be linked to an invoice or stand-alone (advance / old udhaari)."""
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=True)
    date = db.Column(db.String(10), nullable=False)
    amount = db.Column(db.Float, nullable=False, default=0.0)
    method = db.Column(db.String(20), default="cash")  # cash/bank/upi/cheque/other
    note = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))

    customer = db.relationship("Customer")
    invoice = db.relationship("Invoice", foreign_keys=[invoice_id])
    creator = db.relationship("User")


class Vendor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(40), default="")
    address = db.Column(db.String(300), default="")
    gstin = db.Column(db.String(20), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Expense(db.Model):
    """Purchases / raw material / general business expenses (money going out)."""
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(10), nullable=False)
    category = db.Column(db.String(60), default="general")  # raw_material/rent/salary/utility/transport/general
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor.id"), nullable=True)
    description = db.Column(db.String(300), default="")
    amount = db.Column(db.Float, nullable=False, default=0.0)
    payment_status = db.Column(db.String(10), default="paid")  # paid/unpaid/partial
    amount_paid = db.Column(db.Float, default=0.0)
    method = db.Column(db.String(20), default="cash")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))

    vendor = db.relationship("Vendor")
    creator = db.relationship("User")
