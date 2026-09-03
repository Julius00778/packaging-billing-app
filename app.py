import os
import json
from datetime import datetime, date
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort, session, send_file, Response
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)

from models import (
    db, User, Settings, Customer, Item, StockEntry, Invoice, InvoiceItem,
    Payment, Vendor, Expense, Category, STATE_NAMES, INVOICE_FONTS
)
from translations import t
from po_module import po_bp

# Common packaging-firm units. Item.unit stays a free-text column — this list just
# drives the dropdown; "Other" in the form reveals a text box for anything not listed.
COMMON_UNITS = ["pcs", "box", "bundle", "roll", "kg", "mtr", "ltr", "dozen",
                "set", "sheet", "pouch", "carton"]

# Har tarah ka maal apni hi unit me jaata hai — foam piece ya roll me, scrap
# sirf tol ke. Ye list nayi installation pe ek baar ban jaati hai; baad me
# Categories screen se badli ja sakti hai. Pehle se maujood category ko haath
# nahi lagaya jaata.
SEED_CATEGORY_UNITS = [
    ("Foam",         "pcs, roll"),
    ("Thermacol",    "pcs"),
    ("HM",           "pouch, roll"),
    ("Bubble",       "pouch, roll"),
    ("Tape",         "carton"),
    ("Scrap",        "kg"),
    ("Stretch roll", "kg"),
    ("Blister",      "pcs"),
]

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")

db_url = os.environ.get("DATABASE_URL", "sqlite:///app.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# PO scan / product photo phone se aati hai — 8 MB tak accept karo. po_module server
# pe use resize kar deta hai, isliye DB me chhoti hi jaati hai.
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
app.register_blueprint(po_bp)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def owner_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_owner:
            flash(t("flash_owner_only"), "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


def _can_edit_price(settings):
    return current_user.is_owner or bool(settings.staff_can_edit_price)


def _can_give_discount(settings):
    return current_user.is_owner or bool(settings.staff_can_give_discount)


def _can_edit_invoice(settings):
    return current_user.is_owner or bool(settings.staff_can_edit_invoice)


def _asset_v(name):
    """Style file ka apna nishan — file badli toh nishan badla.

    Iske bina browser purani style.css pakde rehta hai aur naya page tootа hua
    dikhta hai, jab tak aadmi khud hard-refresh na kare. Wo aadmi ka kaam nahi
    hona chahiye.
    """
    try:
        path = os.path.join(app.static_folder, name)
        return str(int(os.path.getmtime(path)))
    except OSError:
        return "0"


@app.context_processor
def inject_globals():
    settings = Settings.get()
    return dict(firm_settings=settings, state_names=STATE_NAMES, today=date.today().isoformat(),
                t=t, current_lang=session.get("lang", "en"), invoice_fonts=INVOICE_FONTS,
                asset_v=_asset_v)


@app.route("/lang/<code>")
def set_lang(code):
    if code in ("en", "hi"):
        session["lang"] = code
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if User.query.count() > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        firm_name = request.form.get("firm_name", "").strip()
        firm_address = request.form.get("address", "").strip()
        firm_gstin = request.form.get("gstin", "").strip()  # optional — not every party is GST-registered
        firm_phone = request.form.get("phone", "").strip()
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        if not firm_name or not name or not username or len(password) < 6:
            flash(t("flash_setup_fields"), "error")
            return render_template("setup.html")
        settings = Settings.get()
        settings.firm_name = firm_name
        settings.address = firm_address
        settings.gstin = firm_gstin
        settings.phone = firm_phone
        u = User(name=name, username=username, role="owner")
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        flash(t("flash_owner_created"), "success")
        return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if User.query.count() == 0:
        return redirect(url_for("setup"))
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "owner")  # "owner" (Admin) or "staff" — selected on the login form
        user = User.query.filter_by(username=username).first()
        if user and user.active and user.check_password(password):
            # Extra guard the user asked for: the login form makes you pick Admin vs
            # Staff up front, and it must match the account's real role — avoids a
            # staff member accidentally landing on (or being told they're on) the
            # wrong side, and vice versa.
            if role == "owner" and not user.is_owner:
                flash(t("flash_role_is_staff"), "error")
                return render_template("login.html")
            if role == "staff" and user.is_owner:
                flash(t("flash_role_is_admin"), "error")
                return render_template("login.html")
            login_user(user)
            return redirect(url_for("dashboard"))
        flash(t("flash_bad_login"), "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


def _today_snapshot(today):
    """Aaj ka din ek nazar me — kya atka hai aur kya ho chuka hai.

    Dashboard ka pehla sawaal "ab tak kitna" nahi hota, "aaj kya karna hai"
    hota hai. Isliye upar wahi cheezein aati hain jo aaj haath me hain, aur
    kul-jama neeche.
    """
    from po_module import PurchaseOrder

    stages = []
    for key in ("pending", "with_operator", "in_production", "made"):
        stages.append({"key": key,
                       "count": PurchaseOrder.query.filter_by(status=key).count()})

    todays = [i for i in Invoice.query.filter_by(date=today).all()
              if not i.hide_pricing]
    printed = 0
    for po in PurchaseOrder.query.filter(PurchaseOrder.invoice_id.isnot(None)).all():
        inv = db.session.get(Invoice, po.invoice_id)
        if inv and inv.date == today and po.bill_printed_at:
            printed += 1

    payments = Payment.query.filter_by(date=today).all()
    return {
        "stages": stages,
        "bill_count": len(todays),
        "bill_total": round(sum(i.grand_total or 0 for i in todays), 2),
        "to_print": max(0, len(todays) - printed),
        "received": round(sum(p.amount or 0 for p in payments), 2),
        "receipt_count": len(payments),
    }


@app.route("/")
@login_required
def dashboard():
    invoices = Invoice.query.all()
    count = len(invoices)
    billed = sum(i.grand_total for i in invoices)
    received = sum(i.amount_received for i in invoices)
    pending = billed - received

    this_month = date.today().strftime("%Y-%m")
    month_invoices = [i for i in invoices if i.date.startswith(this_month)]
    cgst = sum(i.cgst_amount for i in month_invoices)
    sgst = sum(i.sgst_amount for i in month_invoices)
    igst = sum(i.igst_amount for i in month_invoices)

    recent = sorted(invoices, key=lambda i: i.created_at, reverse=True)[:8]
    low_stock = Item.query.filter(Item.track_stock == True, Item.current_stock <= Item.reorder_level).all()
    pending_challans = Invoice.query.filter_by(hide_pricing=True, consolidated_into_id=None).count()

    return render_template(
        "dashboard.html", count=count, billed=billed, received=received, pending=pending,
        cgst=cgst, sgst=sgst, igst=igst, recent=recent, low_stock=low_stock,
        pending_challans=pending_challans,
        # `today` naam global me pehle se ek tareekh hai — usi naam se yahan
        # dictionary bhejna baad me kisi ko dhokha dega.
        today_view=_today_snapshot(date.today().isoformat()),
    )


@app.route("/entry")
@login_required
def new_entry():
    """Unified voucher-type picker — Tally-style single starting point for every
    kind of transaction (Sale, Purchase, Payment Received, Payment Made)."""
    return render_template("entry_new.html")


@app.route("/customers", methods=["GET", "POST"])
@login_required
def customers():
    settings = Settings.get()
    if request.method == "POST":
        c = Customer(
            name=request.form.get("name", "").strip(),
            address=request.form.get("address", "").strip(),
            phone=request.form.get("phone", "").strip(),
            gstin=request.form.get("gstin", "").strip(),
            state=request.form.get("state", "").strip() or settings.default_customer_state,
            credit_days=settings.default_credit_days or 30,
        )
        if not c.name:
            flash(t("flash_customer_name_required"), "error")
        else:
            db.session.add(c)
            db.session.commit()
            flash(t("flash_customer_added"), "success")
        return redirect(url_for("customers"))

    q = request.args.get("q", "").strip().lower()
    items = Customer.query.order_by(Customer.name).all()
    if q:
        items = [c for c in items if q in c.name.lower() or q in (c.phone or "")]
    return render_template("customers.html", customers=items, q=q, settings=settings)


@app.route("/customers/<int:cid>/edit", methods=["GET", "POST"])
@login_required
def edit_customer(cid):
    c = Customer.query.get_or_404(cid)
    if request.method == "POST":
        c.name = request.form.get("name", "").strip()
        c.address = request.form.get("address", "").strip()
        c.phone = request.form.get("phone", "").strip()
        c.gstin = request.form.get("gstin", "").strip()
        c.state = request.form.get("state", "").strip()
        c.opening_balance = float(request.form.get("opening_balance") or 0)
        c.credit_days = int(request.form.get("credit_days") or 30)
        db.session.commit()
        flash(t("flash_customer_updated"), "success")
        return redirect(url_for("customers"))
    return render_template("customer_form.html", customer=c)


@app.route("/customers/<int:cid>/delete", methods=["POST"])
@login_required
@owner_required
def delete_customer(cid):
    c = Customer.query.get_or_404(cid)
    if Invoice.query.filter_by(customer_id=c.id).first():
        flash(t("flash_customer_in_use"), "error")
    else:
        db.session.delete(c)
        db.session.commit()
        flash(t("flash_customer_deleted"), "success")
    return redirect(url_for("customers"))


def _resolve_unit(form):
    """Unit select sends either a common unit value, or 'other' + a free-text field."""
    unit = form.get("unit", "pcs").strip()
    if unit == "other":
        unit = form.get("unit_other", "").strip() or "pcs"
    return unit


def _resolve_category_id(form):
    cid = form.get("category_id") or ""
    return int(cid) if cid.isdigit() else None


@app.route("/items", methods=["GET", "POST"])
@login_required
def items():
    settings = Settings.get()
    if request.method == "POST":
        sale_price = float(request.form.get("sale_price") or 0)
        if not _can_edit_price(settings):
            sale_price = 0.0
        it = Item(
            name=request.form.get("name", "").strip(),
            hsn_code=request.form.get("hsn_code", "").strip(),
            unit=_resolve_unit(request.form),
            category_id=_resolve_category_id(request.form),
            gst_rate=float(request.form.get("gst_rate") or settings.default_gst_rate or 0),
            sale_price=sale_price,
            current_stock=float(request.form.get("current_stock") or 0),
            reorder_level=float(request.form.get("reorder_level") or settings.default_reorder_level or 0),
            track_stock=bool(request.form.get("track_stock")),
        )
        if not it.name:
            flash(t("flash_item_name_required"), "error")
        else:
            db.session.add(it)
            db.session.commit()
            flash(t("flash_item_added"), "success")
        return redirect(url_for("items"))

    q = request.args.get("q", "").strip().lower()
    cat_filter = request.args.get("cat") or ""
    rows = Item.query.order_by(Item.name).all()
    if q:
        rows = [i for i in rows if q in i.name.lower()]
    if cat_filter.isdigit():
        rows = [i for i in rows if i.category_id == int(cat_filter)]
    elif cat_filter == "none":
        rows = [i for i in rows if not i.category_id]
    categories = Category.query.order_by(Category.name).all()
    return render_template("items.html", items=rows, q=q, categories=categories,
                           cat_filter=cat_filter, common_units=COMMON_UNITS, settings=settings,
                           can_edit_price=_can_edit_price(settings))


@app.route("/items/<int:iid>/edit", methods=["GET", "POST"])
@login_required
def edit_item(iid):
    it = Item.query.get_or_404(iid)
    settings = Settings.get()
    can_price = _can_edit_price(settings)
    if request.method == "POST":
        it.name = request.form.get("name", "").strip()
        it.hsn_code = request.form.get("hsn_code", "").strip()
        it.unit = _resolve_unit(request.form)
        it.category_id = _resolve_category_id(request.form)
        it.gst_rate = float(request.form.get("gst_rate") or 0)
        if can_price:
            it.sale_price = float(request.form.get("sale_price") or 0)
        it.reorder_level = float(request.form.get("reorder_level") or 0)
        it.track_stock = bool(request.form.get("track_stock"))
        db.session.commit()
        flash(t("flash_item_updated"), "success")
        return redirect(url_for("items"))
    categories = Category.query.order_by(Category.name).all()
    return render_template("item_form.html", item=it, categories=categories, common_units=COMMON_UNITS,
                           can_edit_price=can_price)


@app.route("/items/categories", methods=["GET", "POST"])
@login_required
def categories_page():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash(t("flash_category_name_required"), "error")
        elif Category.query.filter(db.func.lower(Category.name) == name.lower()).first():
            flash(t("flash_category_exists"), "error")
        else:
            db.session.add(Category(name=name, units=request.form.get("units", "").strip()))
            db.session.commit()
            flash(t("flash_category_added"), "success")
        return redirect(url_for("categories_page"))
    cats = Category.query.order_by(Category.name).all()
    counts = {c.id: Item.query.filter_by(category_id=c.id).count() for c in cats}
    return render_template("categories.html", categories=cats, counts=counts)


@app.route("/items/categories/units", methods=["POST"])
@login_required
def category_units():
    """Har category ki units ek saath save — poori table ek hi form hai."""
    for c in Category.query.all():
        field = f"units_{c.id}"
        if field in request.form:
            c.units = (request.form.get(field) or "").strip()
    db.session.commit()
    flash(t("flash_category_units_saved"), "success")
    return redirect(url_for("categories_page"))


@app.route("/items/categories/<int:cid>/delete", methods=["POST"])
@login_required
@owner_required
def delete_category(cid):
    c = Category.query.get_or_404(cid)
    if Item.query.filter_by(category_id=c.id).first():
        flash(t("flash_category_in_use"), "error")
    else:
        db.session.delete(c)
        db.session.commit()
        flash(t("flash_category_deleted"), "success")
    return redirect(url_for("categories_page"))


@app.route("/items/<int:iid>/delete", methods=["POST"])
@login_required
@owner_required
def delete_item(iid):
    it = Item.query.get_or_404(iid)
    if InvoiceItem.query.filter_by(item_id=it.id).first():
        flash(t("flash_item_in_use"), "error")
    else:
        db.session.delete(it)
        db.session.commit()
        flash(t("flash_item_deleted"), "success")
    return redirect(url_for("items"))


@app.route("/items/<int:iid>/stock", methods=["POST"])
@login_required
def add_stock(iid):
    it = Item.query.get_or_404(iid)
    qty = float(request.form.get("qty") or 0)
    note = request.form.get("note", "").strip()
    if qty == 0:
        flash(t("flash_qty_nonzero"), "error")
        return redirect(url_for("items"))
    it.current_stock = (it.current_stock or 0) + qty
    entry = StockEntry(item_id=it.id, qty=qty, entry_type="in" if qty > 0 else "adjust",
                        note=note, created_by=current_user.id)
    db.session.add(entry)
    db.session.commit()
    flash(f"{t('flash_stock_updated')} {it.current_stock} {it.unit}", "success")
    return redirect(url_for("items"))


@app.route("/api/items")
@login_required
def api_items():
    rows = Item.query.order_by(Item.name).all()
    return jsonify([{
        "id": i.id, "name": i.name, "unit": i.unit, "rate": i.sale_price,
        "gst_rate": i.gst_rate, "hsn_code": i.hsn_code or "",
        "stock": i.current_stock, "track_stock": i.track_stock
    } for i in rows])


def calc_invoice_totals(line_items, discount_type, discount_value, other_charges, firm_state, customer_state):
    subtotal = sum((li["qty"] * li["rate"]) for li in line_items)
    if discount_type == "percent":
        discount_amount = subtotal * (discount_value / 100.0)
    else:
        discount_amount = discount_value
    discount_amount = min(discount_amount, subtotal) if subtotal > 0 else 0

    same_state = bool(firm_state) and bool(customer_state) and (firm_state.strip().lower() == customer_state.strip().lower())

    total_cgst = total_sgst = total_igst = 0.0
    computed_lines = []
    for li in line_items:
        line_taxable = li["qty"] * li["rate"]
        share = (line_taxable / subtotal * discount_amount) if subtotal > 0 else 0
        taxable_after_disc = max(0, line_taxable - share)
        rate = li["gst_rate"] or 0
        if same_state:
            cgst = taxable_after_disc * (rate / 2) / 100
            sgst = cgst
            igst = 0.0
        else:
            cgst = sgst = 0.0
            igst = taxable_after_disc * rate / 100
        line_total = taxable_after_disc + cgst + sgst + igst
        total_cgst += cgst
        total_sgst += sgst
        total_igst += igst
        computed_lines.append({
            **li, "taxable_amount": taxable_after_disc,
            "cgst_amount": cgst, "sgst_amount": sgst, "igst_amount": igst,
            "line_total": line_total
        })

    taxable_amount = subtotal - discount_amount
    grand_total = taxable_amount + total_cgst + total_sgst + total_igst + (other_charges or 0)
    return {
        "subtotal": subtotal, "discount_amount": discount_amount, "taxable_amount": taxable_amount,
        "cgst_amount": total_cgst, "sgst_amount": total_sgst, "igst_amount": total_igst,
        "grand_total": grand_total, "lines": computed_lines, "same_state": same_state
    }


@app.route("/invoices")
@login_required
def invoices():
    q = request.args.get("q", "").strip().lower()
    filt = request.args.get("type", "")  # '', 'challan', 'invoice'
    rows = Invoice.query.order_by(Invoice.created_at.desc()).all()
    if q:
        rows = [i for i in rows if q in i.invoice_no.lower() or q in i.customer.name.lower() or q in i.date]
    if filt == "challan":
        rows = [i for i in rows if i.hide_pricing]
    elif filt == "invoice":
        rows = [i for i in rows if not i.hide_pricing]
    return render_template("invoices.html", invoices=rows, q=q, filt=filt)


@app.route("/invoices/new", methods=["GET", "POST"])
@login_required
def new_invoice():
    settings = Settings.get()
    if request.method == "POST":
        return _save_invoice(None, settings)

    next_no = f"{settings.invoice_prefix}-{str(settings.next_invoice_no).zfill(4)}"
    next_challan_no = f"{settings.challan_prefix}-{str(settings.next_challan_no).zfill(4)}"
    return render_template("invoice_form.html", invoice=None, next_invoice_no=next_no,
                           next_challan_no=next_challan_no,
                           customers=Customer.query.order_by(Customer.name).all(),
                           can_give_discount=_can_give_discount(settings))


@app.route("/invoices/<int:invid>/edit", methods=["GET", "POST"])
@login_required
def edit_invoice(invid):
    inv = Invoice.query.get_or_404(invid)
    settings = Settings.get()
    if inv.consolidated_into_id:
        flash(t("flash_challan_consolidated_locked"), "error")
        return redirect(url_for("invoices"))
    if not _can_edit_invoice(settings):
        flash(t("flash_owner_only"), "error")
        return redirect(url_for("invoices"))
    if request.method == "POST":
        return _save_invoice(inv, settings)
    return render_template("invoice_form.html", invoice=inv, next_invoice_no=inv.invoice_no,
                           next_challan_no=inv.invoice_no,
                           customers=Customer.query.order_by(Customer.name).all(),
                           can_give_discount=_can_give_discount(settings))


def _save_invoice(existing_invoice, settings):
    customer_id = request.form.get("customer_id")
    customer = Customer.query.get(int(customer_id)) if customer_id else None
    if not customer:
        flash(t("flash_select_customer"), "error")
        return redirect(request.referrer or url_for("new_invoice"))

    try:
        line_items_raw = json.loads(request.form.get("items_json") or "[]")
    except ValueError:
        line_items_raw = []
    line_items = []
    for li in line_items_raw:
        qty = float(li.get("qty") or 0)
        if not li.get("description") or qty <= 0:
            continue
        line_items.append({
            "item_id": li.get("item_id"),
            "description": li.get("description"),
            "hsn_code": li.get("hsn_code", ""),
            "unit": li.get("unit", "pcs"),
            "qty": qty,
            "rate": float(li.get("rate") or 0),
            "gst_rate": float(li.get("gst_rate") or 0),
        })
    if not line_items:
        flash(t("flash_valid_item_required"), "error")
        return redirect(request.referrer or url_for("new_invoice"))

    discount_type = request.form.get("discount_type", "amount")
    discount_value = float(request.form.get("discount_value") or 0)
    if not _can_give_discount(settings):
        discount_value = 0.0
    other_charges = float(request.form.get("other_charges") or 0)
    hide_pricing = bool(request.form.get("hide_pricing"))

    totals = calc_invoice_totals(line_items, discount_type, discount_value, other_charges,
                                  settings.state, customer.state)

    payment_status = request.form.get("payment_status", "unpaid")
    if payment_status == "paid":
        amount_received = totals["grand_total"]
    elif payment_status == "partial":
        amount_received = float(request.form.get("amount_received") or 0)
    else:
        amount_received = 0.0

    if existing_invoice:
        for old_li in existing_invoice.items:
            if old_li.item_id:
                item = Item.query.get(old_li.item_id)
                if item and item.track_stock:
                    item.current_stock = (item.current_stock or 0) + old_li.qty
        InvoiceItem.query.filter_by(invoice_id=existing_invoice.id).delete()
        inv = existing_invoice
        manual_no = request.form.get("invoice_no", inv.invoice_no).strip()
        inv.invoice_no = manual_no or inv.invoice_no
    else:
        # The Invoice No. field on the New Invoice form is always pre-filled with the
        # "next" number computed at page-load time, so it is almost never actually blank.
        # If the same loaded page gets submitted twice (browser back button, double
        # click, etc.) that pre-filled number can be stale and already taken — resolve
        # to a guaranteed-free number instead of letting a duplicate-key crash the request.
        manual_no = request.form.get("invoice_no", "").strip()
        prefix = settings.challan_prefix if hide_pricing else settings.invoice_prefix
        counter_attr = "next_challan_no" if hide_pricing else "next_invoice_no"
        auto_suggestion = f"{prefix}-{str(getattr(settings, counter_attr) or 1).zfill(4)}"
        if manual_no and manual_no != auto_suggestion and not Invoice.query.filter_by(invoice_no=manual_no).first():
            # A genuinely custom number the user typed themselves, and it's free.
            inv = Invoice(invoice_no=manual_no)
        else:
            n = getattr(settings, counter_attr) or 1
            while Invoice.query.filter_by(invoice_no=f"{prefix}-{str(n).zfill(4)}").first():
                n += 1
            inv = Invoice(invoice_no=f"{prefix}-{str(n).zfill(4)}")
            setattr(settings, counter_attr, n + 1)
        inv.created_by = current_user.id

    inv.date = request.form.get("date") or date.today().isoformat()
    inv.customer_id = customer.id
    inv.subtotal = totals["subtotal"]
    inv.discount_type = discount_type
    inv.discount_value = discount_value
    inv.discount_amount = totals["discount_amount"]
    inv.other_charges = other_charges
    inv.taxable_amount = totals["taxable_amount"]
    inv.cgst_amount = totals["cgst_amount"]
    inv.sgst_amount = totals["sgst_amount"]
    inv.igst_amount = totals["igst_amount"]
    inv.grand_total = totals["grand_total"]
    inv.payment_status = payment_status
    inv.amount_received = amount_received
    inv.notes = request.form.get("notes", "").strip()
    inv.hide_pricing = hide_pricing

    if not existing_invoice:
        db.session.add(inv)
    db.session.flush()

    for li in totals["lines"]:
        item_id = li.get("item_id")
        item = Item.query.get(int(item_id)) if item_id else None
        if item and item.track_stock:
            item.current_stock = (item.current_stock or 0) - li["qty"]
        db.session.add(InvoiceItem(
            invoice_id=inv.id, item_id=item.id if item else None,
            description=li["description"], hsn_code=li.get("hsn_code", ""),
            qty=li["qty"], unit=li.get("unit", "pcs"), rate=li["rate"], gst_rate=li["gst_rate"],
            taxable_amount=li["taxable_amount"], cgst_amount=li["cgst_amount"],
            sgst_amount=li["sgst_amount"], igst_amount=li["igst_amount"], line_total=li["line_total"]
        ))

    db.session.commit()
    flash(t("flash_invoice_saved"), "success")
    return redirect(url_for("invoices"))


@app.route("/invoices/<int:invid>/delete", methods=["POST"])
@login_required
@owner_required
def delete_invoice(invid):
    inv = Invoice.query.get_or_404(invid)
    for li in inv.items:
        if li.item_id:
            item = Item.query.get(li.item_id)
            if item and item.track_stock:
                item.current_stock = (item.current_stock or 0) + li.qty
    db.session.delete(inv)
    db.session.commit()
    flash(t("flash_invoice_deleted"), "success")
    return redirect(url_for("invoices"))


@app.route("/invoices/<int:invid>/print")
@login_required
def print_invoice(invid):
    inv = Invoice.query.get_or_404(invid)
    settings = Settings.get()
    return render_template("invoice_print.html",
                           inv=_for_print(inv, request.args.get("mode")),
                           settings=settings)


def _copies_choice(raw):
    """Kaunsi copy chahiye. Kuch bhi anjaan aaye toh dono — wahi aam zaroorat hai."""
    return raw if raw in ("both", "party", "office") else "both"


class _AsChallan:
    """Wahi bill, par bina bhav ke — sirf chhapne ke liye.

    Kabhi maal ke saath jo kaagaz jaata hai usme rate nahi dikhna chahiye:
    driver, labour, ya party ka aadmi jo sirf ginti milata hai. Par bill ka
    record wahi ka wahi rehna chahiye — paisa, hisaab, khaata sab pehle jaisa.

    Isliye yahan DB me kuch nahi badalta. Ye ek parda hai jo sirf itna kehta
    hai ki "iska hide_pricing sach maano"; baaki har cheez asli bill se hi
    aati hai. Template aur PDF dono pehle se hide_pricing samajhte hain, isliye
    unme ek line badalne ki bhi zaroorat nahi padi.
    """

    def __init__(self, inv):
        object.__setattr__(self, "_inv", inv)

    @property
    def hide_pricing(self):
        return True

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inv"), name)


def _for_print(inv, raw_mode):
    """Bill jaisa ka waisa, ya challan ki tarah (bina rate ke)."""
    return _AsChallan(inv) if raw_mode == "challan" else inv


@app.route("/invoices/<int:invid>/preview")
@login_required
def invoice_preview(invid):
    """Sirf bill ka hissa, bina page ke — overlay isi ko andar la ke dikhata hai."""
    inv = Invoice.query.get_or_404(invid)
    return render_template("invoice_preview.html",
                           inv=_for_print(inv, request.args.get("mode")),
                           settings=Settings.get(),
                           copies=_copies_choice(request.args.get("copies")))


@app.route("/invoices/<int:invid>/pdf")
@login_required
def invoice_pdf(invid):
    inv = Invoice.query.get_or_404(invid)
    settings = Settings.get()
    from invoice_pdf import build_invoice_pdf
    mode = request.args.get("mode")
    buf = build_invoice_pdf(_for_print(inv, mode), settings, t,
                            copies=_copies_choice(request.args.get("copies")))
    tag = "-challan" if mode == "challan" else ""
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                      download_name=f"{inv.invoice_no}{tag}.pdf")


@app.route("/invoices/consolidate", methods=["GET", "POST"])
@login_required
def consolidate_challans():
    """Merge several no-rate delivery challans for one customer into a single, fully
    priced Invoice — matches the business's 1-2 month billing cadence: goods go out
    without a rate, and get billed together once payment is due."""
    settings = Settings.get()
    if request.method == "POST":
        customer_id = request.form.get("customer_id")
        challan_ids = request.form.getlist("challan_ids")
        customer = Customer.query.get(int(customer_id)) if customer_id else None
        if not customer or not challan_ids:
            flash(t("flash_select_challans"), "error")
            return redirect(url_for("consolidate_challans"))

        challans = Invoice.query.filter(
            Invoice.id.in_([int(x) for x in challan_ids]),
            Invoice.customer_id == customer.id,
            Invoice.hide_pricing == True,
            Invoice.consolidated_into_id.is_(None),
        ).all()
        if not challans:
            flash(t("flash_select_challans"), "error")
            return redirect(url_for("consolidate_challans"))

        n = settings.next_invoice_no or 1
        while Invoice.query.filter_by(invoice_no=f"{settings.invoice_prefix}-{str(n).zfill(4)}").first():
            n += 1
        new_no = f"{settings.invoice_prefix}-{str(n).zfill(4)}"
        inv = Invoice(
            invoice_no=new_no, date=date.today().isoformat(), customer_id=customer.id,
            created_by=current_user.id, payment_status="unpaid", hide_pricing=False,
            notes=t("consolidated_from_note") + " " + ", ".join(c.invoice_no for c in challans),
        )
        db.session.add(inv)
        settings.next_invoice_no = n + 1
        db.session.flush()

        line_items = []
        for ch in challans:
            for li in ch.items:
                item = Item.query.get(li.item_id) if li.item_id else None
                rate = li.rate or (item.sale_price if item else 0.0)
                gst_rate = li.gst_rate or (item.gst_rate if item else 0.0)
                line_items.append({
                    "item_id": li.item_id, "description": li.description, "hsn_code": li.hsn_code,
                    "unit": li.unit, "qty": li.qty, "rate": rate, "gst_rate": gst_rate,
                })
            ch.consolidated_into_id = inv.id

        totals = calc_invoice_totals(line_items, "amount", 0, 0, settings.state, customer.state)
        inv.subtotal = totals["subtotal"]
        inv.discount_amount = 0
        inv.taxable_amount = totals["taxable_amount"]
        inv.cgst_amount = totals["cgst_amount"]
        inv.sgst_amount = totals["sgst_amount"]
        inv.igst_amount = totals["igst_amount"]
        inv.grand_total = totals["grand_total"]

        for li in totals["lines"]:
            db.session.add(InvoiceItem(
                invoice_id=inv.id, item_id=li.get("item_id"),
                description=li["description"], hsn_code=li.get("hsn_code", ""),
                qty=li["qty"], unit=li.get("unit", "pcs"), rate=li["rate"], gst_rate=li["gst_rate"],
                taxable_amount=li["taxable_amount"], cgst_amount=li["cgst_amount"],
                sgst_amount=li["sgst_amount"], igst_amount=li["igst_amount"], line_total=li["line_total"]
            ))
        db.session.commit()
        flash(t("flash_consolidated_ok"), "success")
        return redirect(url_for("edit_invoice", invid=inv.id))

    customer_id = request.args.get("customer_id")
    customer = Customer.query.get(int(customer_id)) if customer_id and customer_id.isdigit() else None
    pending_by_customer = {}
    for ch in Invoice.query.filter_by(hide_pricing=True, consolidated_into_id=None).all():
        pending_by_customer.setdefault(ch.customer_id, []).append(ch)
    customers_with_pending = Customer.query.filter(Customer.id.in_(pending_by_customer.keys())).order_by(Customer.name).all()
    selected_challans = pending_by_customer.get(customer.id, []) if customer else []
    return render_template("invoice_consolidate.html", customers_with_pending=customers_with_pending,
                           pending_by_customer=pending_by_customer, customer=customer,
                           selected_challans=selected_challans)


@app.route("/settings", methods=["GET", "POST"])
@login_required
@owner_required
def settings_page():
    s = Settings.get()
    if request.method == "POST":
        s.firm_name = request.form.get("firm_name", "").strip()
        s.address = request.form.get("address", "").strip()
        s.phone = request.form.get("phone", "").strip()
        s.email = request.form.get("email", "").strip()
        s.gstin = request.form.get("gstin", "").strip()
        s.state = request.form.get("state", "").strip()
        s.bank_name = request.form.get("bank_name", "").strip()
        s.bank_acc = request.form.get("bank_acc", "").strip()
        s.ifsc = request.form.get("ifsc", "").strip()
        s.invoice_prefix = request.form.get("invoice_prefix", "INV").strip() or "INV"
        s.next_invoice_no = int(request.form.get("next_invoice_no") or 1)

        # Invoice / print settings
        s.show_gstin_on_invoice = bool(request.form.get("show_gstin_on_invoice"))
        s.default_print_copies = request.form.get("default_print_copies", "2")
        s.invoice_default_notes = request.form.get("invoice_default_notes", "").strip()
        s.challan_prefix = request.form.get("challan_prefix", "DC").strip() or "DC"
        s.next_challan_no = int(request.form.get("next_challan_no") or 1)

        # Item / stock defaults
        s.default_unit = request.form.get("default_unit", "pcs").strip() or "pcs"
        s.default_gst_rate = float(request.form.get("default_gst_rate") or 0)
        s.default_reorder_level = float(request.form.get("default_reorder_level") or 0)

        # Party defaults
        s.default_customer_state = request.form.get("default_customer_state", "").strip()
        s.default_credit_days = int(request.form.get("default_credit_days") or 30)

        # Appearance
        s.invoice_font = request.form.get("invoice_font", "helvetica")
        if s.invoice_font not in INVOICE_FONTS:
            s.invoice_font = "helvetica"
        s.theme_color = request.form.get("theme_color", "#A8722E").strip() or "#A8722E"

        # Staff permissions
        s.staff_can_edit_price = bool(request.form.get("staff_can_edit_price"))
        s.staff_can_give_discount = bool(request.form.get("staff_can_give_discount"))
        s.staff_can_edit_invoice = bool(request.form.get("staff_can_edit_invoice"))

        db.session.commit()
        flash(t("flash_settings_saved"), "success")
        return redirect(url_for("settings_page"))
    return render_template("settings.html", s=s, common_units=COMMON_UNITS, invoice_fonts=INVOICE_FONTS)


def _row_to_dict(row):
    """Serialize a db.Model instance to a plain dict using its own column list —
    keeps this generic so new columns/tables get picked up automatically without
    hand-maintaining a field list per model."""
    out = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if isinstance(val, (datetime, date)):
            val = val.isoformat()
        out[col.name] = val
    return out


@app.route("/settings/backup")
@login_required
@owner_required
def backup_export():
    """One-click full data export (JSON) — a safety net before testing changes or
    just for periodic offline backup. Owner-only. Excludes password hashes."""
    data = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "firm": Settings.get().firm_name,
        "customers": [_row_to_dict(r) for r in Customer.query.all()],
        "categories": [_row_to_dict(r) for r in Category.query.all()],
        "items": [_row_to_dict(r) for r in Item.query.all()],
        "stock_entries": [_row_to_dict(r) for r in StockEntry.query.all()],
        "invoices": [_row_to_dict(r) for r in Invoice.query.all()],
        "invoice_items": [_row_to_dict(r) for r in InvoiceItem.query.all()],
        "payments": [_row_to_dict(r) for r in Payment.query.all()],
        "vendors": [_row_to_dict(r) for r in Vendor.query.all()],
        "expenses": [_row_to_dict(r) for r in Expense.query.all()],
        "settings": [_row_to_dict(r) for r in Settings.query.all()],
        "users": [{k: v for k, v in _row_to_dict(r).items() if k != "password_hash"} for r in User.query.all()],
    }
    body = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    fname = f"backup_{date.today().isoformat()}.json"
    return Response(
        body, mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )


@app.route("/users", methods=["GET", "POST"])
@login_required
@owner_required
def users_page():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "staff")
        if not username or not name or len(password) < 6:
            flash(t("flash_users_fields"), "error")
        elif User.query.filter_by(username=username).first():
            flash(t("flash_username_taken"), "error")
        else:
            u = User(username=username, name=name, role=role)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            flash(t("flash_user_added"), "success")
        return redirect(url_for("users_page"))
    return render_template("users.html", users=User.query.order_by(User.created_at).all())


@app.route("/users/<int:uid>/toggle", methods=["POST"])
@login_required
@owner_required
def toggle_user(uid):
    u = User.query.get_or_404(uid)
    if u.id == current_user.id:
        flash(t("flash_self_deactivate"), "error")
    else:
        u.active = not u.active
        db.session.commit()
        flash(t("flash_user_status_updated"), "success")
    return redirect(url_for("users_page"))


def customer_ledger_entries(customer):
    entries = []
    for inv in Invoice.query.filter_by(customer_id=customer.id).all():
        if inv.hide_pricing:
            # Challans carry no price yet, so they never move the running balance —
            # but still show them so pending deliveries are visible right here in
            # the ledger, not just on the separate Invoices page.
            entries.append({
                "date": inv.date, "kind": "challan", "ref": inv.invoice_no,
                "debit": 0.0, "credit": 0.0, "sort_key": (inv.date, inv.created_at),
                "link": url_for("print_invoice", invid=inv.id),
                "consolidated": bool(inv.consolidated_into_id),
            })
            continue
        entries.append({
            "date": inv.date, "kind": "invoice", "ref": inv.invoice_no,
            "debit": inv.grand_total, "credit": 0.0, "sort_key": (inv.date, inv.created_at),
            "link": url_for("print_invoice", invid=inv.id),
        })
        if inv.amount_received:
            entries.append({
                "date": inv.date, "kind": "invoice_payment", "ref": f"{inv.invoice_no} ({t('paid')})",
                "debit": 0.0, "credit": inv.amount_received, "sort_key": (inv.date, inv.created_at),
                "link": None,
            })
    for p in Payment.query.filter_by(customer_id=customer.id, invoice_id=None).all():
        entries.append({
            "date": p.date, "kind": "payment", "ref": p.note or t("record_payment"),
            "debit": 0.0, "credit": p.amount, "sort_key": (p.date, p.created_at), "id": p.id,
            "link": None,
        })
    entries.sort(key=lambda e: e["sort_key"])
    balance = customer.opening_balance or 0.0
    for e in entries:
        balance += e["debit"] - e["credit"]
        e["balance"] = balance
    return entries, balance


@app.route("/accounts")
@login_required
def accounts_home():
    customers = Customer.query.order_by(Customer.name).all()
    rows = []
    total_outstanding = 0.0
    for c in customers:
        _, balance = customer_ledger_entries(c)
        rows.append({"customer": c, "balance": balance})
        total_outstanding += balance
    rows.sort(key=lambda r: -abs(r["balance"]))
    return render_template("accounts_home.html", rows=rows, total_outstanding=total_outstanding)


@app.route("/accounts/customer/<int:cid>")
@login_required
def customer_khata(cid):
    c = Customer.query.get_or_404(cid)
    entries, balance = customer_ledger_entries(c)
    entries.reverse()
    pending_challans = Invoice.query.filter_by(customer_id=c.id, hide_pricing=True, consolidated_into_id=None).count()
    return render_template("customer_khata.html", customer=c, entries=entries, balance=balance,
                           pending_challans=pending_challans)


@app.route("/accounts/payments")
@login_required
def payments_page():
    recent = Payment.query.filter_by(invoice_id=None).order_by(Payment.created_at.desc()).limit(50).all()
    return render_template("payments.html", payments=recent)


@app.route("/accounts/payments/new", methods=["GET", "POST"])
@login_required
def new_payment_entry():
    if request.method == "POST":
        customer_id = request.form.get("customer_id")
        amount = float(request.form.get("amount") or 0)
        customer = Customer.query.get(int(customer_id)) if customer_id else None
        if not customer or amount <= 0:
            flash(t("flash_payment_invalid"), "error")
            return redirect(url_for("new_payment_entry"))
        p = Payment(
            customer_id=customer.id, date=request.form.get("date") or date.today().isoformat(),
            amount=amount, method=request.form.get("method", "cash"),
            note=request.form.get("note", "").strip(), created_by=current_user.id,
        )
        db.session.add(p)
        db.session.commit()
        flash(t("flash_payment_saved"), "success")
        return redirect(url_for("view_payment", pid=p.id))

    return render_template("payment_new.html", customers=Customer.query.order_by(Customer.name).all())


@app.route("/accounts/payments/<int:pid>")
@login_required
def view_payment(pid):
    p = Payment.query.get_or_404(pid)
    return render_template("payment_view.html", p=p)


@app.route("/accounts/payments/<int:pid>/print")
@login_required
def print_payment(pid):
    p = Payment.query.get_or_404(pid)
    settings = Settings.get()
    return render_template("payment_print.html", p=p, settings=settings)


@app.route("/accounts/payments/<int:pid>/delete", methods=["POST"])
@login_required
@owner_required
def delete_payment(pid):
    p = Payment.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    flash(t("flash_payment_deleted"), "success")
    return redirect(request.referrer or url_for("payments_page"))


@app.route("/accounts/vendors", methods=["GET", "POST"])
@login_required
def vendors_page():
    if request.method == "POST":
        v = Vendor(
            name=request.form.get("name", "").strip(),
            phone=request.form.get("phone", "").strip(),
            address=request.form.get("address", "").strip(),
            gstin=request.form.get("gstin", "").strip(),
        )
        if not v.name:
            flash(t("flash_vendor_name_required"), "error")
        else:
            db.session.add(v)
            db.session.commit()
            flash(t("flash_vendor_added"), "success")
        return redirect(url_for("vendors_page"))
    return render_template("vendors.html", vendors=Vendor.query.order_by(Vendor.name).all())


@app.route("/accounts/expenses")
@login_required
def expenses_page():
    q = request.args.get("q", "").strip().lower()
    rows = Expense.query.order_by(Expense.date.desc(), Expense.created_at.desc()).all()
    if q:
        rows = [e for e in rows if q in (e.description or "").lower() or (e.vendor and q in e.vendor.name.lower())]
    total = sum(e.amount for e in rows)
    return render_template("expenses.html", expenses=rows, q=q, total=total)


@app.route("/accounts/expenses/new", methods=["GET", "POST"])
@login_required
def new_expense_entry():
    if request.method == "POST":
        amount = float(request.form.get("amount") or 0)
        if amount <= 0:
            flash(t("flash_expense_invalid"), "error")
            return redirect(url_for("new_expense_entry", voucher=request.args.get("voucher")))
        vendor_id = request.form.get("vendor_id") or None
        payment_status = request.form.get("payment_status", "paid")
        amount_paid = amount if payment_status == "paid" else float(request.form.get("amount_paid") or 0)
        e = Expense(
            date=request.form.get("date") or date.today().isoformat(),
            category=request.form.get("category", "general"),
            vendor_id=int(vendor_id) if vendor_id else None,
            description=request.form.get("description", "").strip(),
            amount=amount, payment_status=payment_status, amount_paid=amount_paid,
            method=request.form.get("method", "cash"), created_by=current_user.id,
        )
        db.session.add(e)
        db.session.commit()
        flash(t("flash_expense_saved"), "success")
        return redirect(url_for("view_expense", eid=e.id))

    return render_template("expense_new.html", vendors=Vendor.query.order_by(Vendor.name).all())


@app.route("/accounts/expenses/<int:eid>")
@login_required
def view_expense(eid):
    e = Expense.query.get_or_404(eid)
    return render_template("expense_view.html", e=e)


@app.route("/accounts/expenses/<int:eid>/print")
@login_required
def print_expense(eid):
    e = Expense.query.get_or_404(eid)
    settings = Settings.get()
    return render_template("expense_print.html", e=e, settings=settings)


@app.route("/accounts/expenses/<int:eid>/delete", methods=["POST"])
@login_required
@owner_required
def delete_expense(eid):
    e = Expense.query.get_or_404(eid)
    db.session.delete(e)
    db.session.commit()
    flash(t("flash_expense_deleted"), "success")
    return redirect(url_for("expenses_page"))


@app.route("/accounts/reports")
@login_required
def reports_page():
    sel_date = request.args.get("date") or date.today().isoformat()

    day_invoices = Invoice.query.filter_by(date=sel_date, hide_pricing=False).all()
    day_challans = Invoice.query.filter_by(date=sel_date, hide_pricing=True).all()
    day_payments = [p for p in Payment.query.filter_by(date=sel_date).all() if p.invoice_id is None]
    day_expenses = Expense.query.filter_by(date=sel_date).all()

    money_in = sum(i.amount_received for i in day_invoices) + sum(p.amount for p in day_payments)
    money_out = sum(e.amount_paid for e in day_expenses)

    # Tally-style Day Book: every voucher of every type for the day, in one
    # chronological list. Each row links to that voucher's own view page — from
    # there the user can Print; we never print directly off this list.
    day_book_entries = []
    for inv in day_invoices:
        day_book_entries.append({
            "kind": "sale", "ref": inv.invoice_no, "party": inv.customer.name,
            "amount": inv.grand_total, "sort_key": inv.created_at,
            "view_url": url_for("edit_invoice", invid=inv.id),
        })
    for ch in day_challans:
        day_book_entries.append({
            "kind": "challan", "ref": ch.invoice_no, "party": ch.customer.name,
            "amount": None, "sort_key": ch.created_at,
            "view_url": url_for("edit_invoice", invid=ch.id),
        })
    for p in day_payments:
        day_book_entries.append({
            "kind": "receipt", "ref": f"PMT-{p.id}", "party": p.customer.name,
            "amount": p.amount, "sort_key": p.created_at,
            "view_url": url_for("view_payment", pid=p.id),
        })
    for e in day_expenses:
        kind = "purchase" if e.category == "raw_material" else "payment_made"
        day_book_entries.append({
            "kind": kind, "ref": f"EXP-{e.id}", "party": e.vendor.name if e.vendor else "—",
            "amount": e.amount, "sort_key": e.created_at,
            "view_url": url_for("view_expense", eid=e.id),
        })
    day_book_entries.sort(key=lambda x: x["sort_key"])

    this_month = sel_date[:7]
    month_invoices = Invoice.query.filter(Invoice.date.startswith(this_month), Invoice.hide_pricing == False).all()
    month_expenses = Expense.query.filter(Expense.date.startswith(this_month)).all()
    month_billed = sum(i.grand_total for i in month_invoices)
    month_received = sum(i.amount_received for i in month_invoices)
    month_expense_total = sum(e.amount for e in month_expenses)

    return render_template(
        "reports.html", sel_date=sel_date, day_book_entries=day_book_entries,
        money_in=money_in, money_out=money_out,
        month_billed=month_billed, month_received=month_received, month_expense_total=month_expense_total,
        net_position=month_received - month_expense_total,
    )


def _add_column_if_missing(inspector, table, column, ddl):
    """db.create_all() only creates tables that do not exist yet — it will NOT add a
    new column to a table that already exists in production. This adds one column
    safely (idempotent) on both SQLite and Postgres."""
    from sqlalchemy import text
    existing_tables = inspector.get_table_names()
    if table not in existing_tables:
        return
    cols = [c["name"] for c in inspector.get_columns(table)]
    if column in cols:
        return
    with db.engine.connect() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
        conn.commit()


def _run_startup_migrations():
    from sqlalchemy import inspect
    # Create any brand-new tables first (e.g. category) so the item.category_id
    # foreign key below has somewhere valid to point.
    db.create_all()
    inspector = inspect(db.engine)
    _add_column_if_missing(inspector, "customer", "opening_balance", "opening_balance FLOAT DEFAULT 0.0")
    _add_column_if_missing(inspector, "customer", "credit_days", "credit_days INTEGER DEFAULT 30")
    _add_column_if_missing(inspector, "item", "category_id", "category_id INTEGER")

    _add_column_if_missing(inspector, "settings", "show_gstin_on_invoice", "show_gstin_on_invoice BOOLEAN DEFAULT TRUE")
    _add_column_if_missing(inspector, "settings", "default_print_copies", "default_print_copies VARCHAR(4) DEFAULT '2'")
    _add_column_if_missing(inspector, "settings", "invoice_default_notes", "invoice_default_notes VARCHAR(500) DEFAULT ''")
    _add_column_if_missing(inspector, "settings", "challan_prefix", "challan_prefix VARCHAR(20) DEFAULT 'DC'")
    _add_column_if_missing(inspector, "settings", "next_challan_no", "next_challan_no INTEGER DEFAULT 1")
    _add_column_if_missing(inspector, "settings", "default_unit", "default_unit VARCHAR(20) DEFAULT 'pcs'")
    _add_column_if_missing(inspector, "settings", "default_gst_rate", "default_gst_rate FLOAT DEFAULT 18.0")
    _add_column_if_missing(inspector, "settings", "default_reorder_level", "default_reorder_level FLOAT DEFAULT 0.0")
    _add_column_if_missing(inspector, "settings", "default_customer_state", "default_customer_state VARCHAR(80) DEFAULT ''")
    _add_column_if_missing(inspector, "settings", "default_credit_days", "default_credit_days INTEGER DEFAULT 30")
    _add_column_if_missing(inspector, "settings", "invoice_font", "invoice_font VARCHAR(20) DEFAULT 'helvetica'")
    _add_column_if_missing(inspector, "settings", "theme_color", "theme_color VARCHAR(10) DEFAULT '#A8722E'")
    _add_column_if_missing(inspector, "settings", "staff_can_edit_price", "staff_can_edit_price BOOLEAN DEFAULT TRUE")
    _add_column_if_missing(inspector, "settings", "staff_can_give_discount", "staff_can_give_discount BOOLEAN DEFAULT TRUE")
    _add_column_if_missing(inspector, "settings", "staff_can_edit_invoice", "staff_can_edit_invoice BOOLEAN DEFAULT TRUE")

    _add_column_if_missing(inspector, "invoice", "hide_pricing", "hide_pricing BOOLEAN DEFAULT FALSE")
    _add_column_if_missing(inspector, "invoice", "consolidated_into_id", "consolidated_into_id INTEGER")

    # PO module. Tables khud db.create_all() se banti hain, par jo environment
    # pehle se chal raha hai wahan naye columns haath se jodne padte hain.
    _add_column_if_missing(inspector, "party_product_map", "item_code", "item_code VARCHAR(40) DEFAULT ''")
    _add_column_if_missing(inspector, "po_line", "item_code", "item_code VARCHAR(40) DEFAULT ''")
    _add_column_if_missing(inspector, "po_line", "size_mismatch", "size_mismatch BOOLEAN DEFAULT FALSE")
    _add_column_if_missing(inspector, "party_product_map", "drive_file_id", "drive_file_id VARCHAR(120) DEFAULT ''")
    _add_column_if_missing(inspector, "party_product_map", "drive_modified", "drive_modified VARCHAR(40) DEFAULT ''")

    _add_column_if_missing(inspector, "purchase_order", "tg_chat_id", "tg_chat_id VARCHAR(40) DEFAULT ''")
    _add_column_if_missing(inspector, "purchase_order", "tg_message_ids", "tg_message_ids VARCHAR(300) DEFAULT ''")
    _add_column_if_missing(inspector, "purchase_order", "sent_at", "sent_at TIMESTAMP")
    _add_column_if_missing(inspector, "purchase_order", "accepted_at", "accepted_at TIMESTAMP")
    _add_column_if_missing(inspector, "purchase_order", "made_at", "made_at TIMESTAMP")
    _add_column_if_missing(inspector, "purchase_order", "operator_name", "operator_name VARCHAR(120) DEFAULT ''")
    _add_column_if_missing(inspector, "purchase_order", "tg_rate_summary_id", "tg_rate_summary_id VARCHAR(40) DEFAULT ''")
    _add_column_if_missing(inspector, "purchase_order", "invoice_id", "invoice_id INTEGER")
    _add_column_if_missing(inspector, "po_line", "rate", "rate FLOAT DEFAULT 0.0")
    _add_column_if_missing(inspector, "po_line", "rate_from_memory", "rate_from_memory BOOLEAN DEFAULT FALSE")
    _add_column_if_missing(inspector, "po_line", "tg_rate_msg_id", "tg_rate_msg_id VARCHAR(40) DEFAULT ''")
    _add_column_if_missing(inspector, "purchase_order", "bill_printed_at", "bill_printed_at TIMESTAMP")
    _add_column_if_missing(inspector, "category", "units", "units VARCHAR(200) DEFAULT ''")
    _add_column_if_missing(inspector, "purchase_order", "rates_ok_at", "rates_ok_at TIMESTAMP")
    _add_column_if_missing(inspector, "telegram_chat", "roles", "roles VARCHAR(200) DEFAULT ''")
    # Manager group wale card ki pehchan — usi card pe dispatch ka button hai,
    # aur dispatch hote hi button hatana padta hai.
    _add_column_if_missing(inspector, "purchase_order", "tg_manager_msg_ids",
                           "tg_manager_msg_ids VARCHAR(1000) DEFAULT ''")
    # Kaunsa operator group kaunsa maal banata hai. Khaali = sab kuch, isliye
    # purana ek-group wala setup bina chhede chalta rehta hai.
    _add_column_if_missing(inspector, "telegram_chat", "categories",
                           "categories VARCHAR(300) DEFAULT ''")
    # Ek chat ke kai role ho sakte hain, aur ek order kai group me jaata hai.
    # Isliye msg ki pehchan ab "chat:msg" jodon me rehti hai — purane khaane
    # uske liye chhote pad gaye the.
    _widen_column(inspector, "purchase_order", "tg_message_ids", 1000)
    _widen_column(inspector, "purchase_order", "tg_rate_summary_id", 300)
    _widen_column(inspector, "po_line", "tg_rate_msg_id", 300)

    # Purana "confirmed" ab "made" kehlata hai (operator ne bana diya). Ye ek
    # baar ka sudhaar hai — dobara chalane par kuch nahi milta, isliye safe.
    _rename_po_status(inspector, "confirmed", "made")
    _seed_category_units(inspector)
    _link_products_to_items(inspector)
    _copy_single_role_to_roles(inspector)


def _widen_column(inspector, table, column, size):
    """Column ko lamba karo. Sirf wahan chalta hai jahan iski zaroorat hai.

    SQLite VARCHAR ki lambai maanta hi nahi, isliye wahan kuch karne ki
    zaroorat nahi. Postgres pe ALTER TYPE chalti hai aur dobara chalane par
    bhi kuch nahi bigadta.
    """
    from sqlalchemy import text
    if db.engine.dialect.name == "sqlite":
        return
    if table not in inspector.get_table_names():
        return
    for col in inspector.get_columns(table):
        if col["name"] != column:
            continue
        length = getattr(col["type"], "length", None)
        if length and length >= size:
            return
        with db.engine.connect() as conn:
            conn.execute(text(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE VARCHAR({size})"))
            conn.commit()
        return


def _copy_single_role_to_roles(inspector):
    """Purana ek-role wala khaana nayi list me le aao.

    Pehle har chat ka ek hi role hota tha. Ab kai ho sakte hain, par jo pehle
    se set hai wo waise ka waisa chalta rehna chahiye — kisi ko dobara set
    karne ki zaroorat na pade. Ek baar ka kaam; dobara chalane par kuch nahi
    milta kyunki tab roles bhara hua hota hai.
    """
    if "telegram_chat" not in inspector.get_table_names():
        return
    cols = [c["name"] for c in inspector.get_columns("telegram_chat")]
    if "roles" not in cols or "role" not in cols:
        return
    from po_module import TelegramChat
    touched = 0
    for row in TelegramChat.query.all():
        if (row.roles or "").strip():
            continue
        if (row.role or "").strip():
            row.roles = row.role.strip()
            touched += 1
    if touched:
        db.session.commit()


def _seed_category_units(inspector):
    """Categories ki shuruaati list.

    Do halat sambhalni hoti hain. Category hai hi nahi — bana do. Ya category
    pehle se hai par uski units khaali hain (kyunki `units` column abhi juda
    hai) — bhar do. Jis category me user ne khud units daal rakhi hain use
    haath nahi lagate.
    """
    if "category" not in inspector.get_table_names():
        return
    touched = 0
    for name, units in SEED_CATEGORY_UNITS:
        row = Category.query.filter_by(name=name).first()
        if row is None:
            db.session.add(Category(name=name, units=units))
            touched += 1
        elif not (row.units or "").strip():
            row.units = units
            touched += 1
    if touched:
        db.session.commit()


def _link_products_to_items(inspector):
    """Purane PO products jo Item master me the hi nahi — unke Item bana do.

    Iske bina bill me item ki jagah khaali jaati thi aur naam description me
    thus jaata tha. Ek baar ka sudhaar; dobara chalane par kuch nahi milta.
    """
    tables = inspector.get_table_names()
    if "party_product_map" not in tables or "item" not in tables:
        return
    try:
        from po_module import PartyProductMap, ensure_item_for
    except Exception:
        return
    rows = PartyProductMap.query.filter(PartyProductMap.item_id.is_(None)).all()
    if not rows:
        return
    for row in rows:
        ensure_item_for(row)
    db.session.commit()


def _rename_po_status(inspector, old, new):
    from sqlalchemy import text
    if "purchase_order" not in inspector.get_table_names():
        return
    with db.engine.connect() as conn:
        conn.execute(text("UPDATE purchase_order SET status = :new, made_at = "
                          "COALESCE(made_at, confirmed_at) WHERE status = :old"),
                     {"new": new, "old": old})
        conn.commit()


with app.app_context():
    _run_startup_migrations()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
