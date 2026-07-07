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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    hsn_code = db.Column(db.String(20), default="")
    unit = db.Column(db.String(20), default="pcs")
    gst_rate = db.Column(db.Float, default=18.0)
    sale_price = db.Column(db.Float, default=0.0)
    current_stock = db.Column(db.Float, default=0.0)
    reorder_level = db.Column(db.Float, default=0.0)
    track_stock = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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

    customer = db.relationship("Customer")
    creator = db.relationship("User")
    items = db.relationship("InvoiceItem", backref="invoice", cascade="all, delete-orphan")


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


# ---------------- Accounts module (new) ----------------

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
    invoice = db.relationship("Invoice")


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
