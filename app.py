import os
import json
from datetime import datetime, date
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort, session, send_file
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)

from models import (
    db, User, Settings, Customer, Item, StockEntry, Invoice, InvoiceItem,
    Payment, Vendor, Expense, Category, STATE_NAMES
)
from translations import get_text

# Common packaging-firm units. Item.unit stays a free-text column — this list just
# drives the dropdown; "Other" in the form reveals a text box for anything not listed.
COMMON_UNITS = ["pcs", "box", "bundle", "roll", "kg", "mtr", "ltr", "dozen", "set", "sheet"]

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")

db_url = os.environ.get("DATABASE_URL", "sqlite:///app.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def t(key):
    return get_text(session.get("lang", "en"), key)


def owner_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_owner:
            flash(t("flash_owner_only"), "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_globals():
    settings = Settings.get()
    return dict(firm_settings=settings, state_names=STATE_NAMES, today=date.today().isoformat(),
                t=t, current_lang=session.get("lang", "en"))


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
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not username or len(password) < 6:
            flash(t("flash_setup_fields"), "error")
            return render_template("setup.html")
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
        user = User.query.filter_by(username=username).first()
        if user and user.active and user.check_password(password):
            login_user(user)
            return redirect(url_for("dashboard"))
        flash(t("flash_bad_login"), "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


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

    return render_template(
        "dashboard.html", count=count, billed=billed, received=received, pending=pending,
        cgst=cgst, sgst=sgst, igst=igst, recent=recent, low_stock=low_stock
    )


@app.route("/customers", methods=["GET", "POST"])
@login_required
def customers():
    if request.method == "POST":
        c = Customer(
            name=request.form.get("name", "").strip(),
            address=request.form.get("address", "").strip(),
            phone=request.form.get("phone", "").strip(),
            gstin=request.form.get("gstin", "").strip(),
            state=request.form.get("state", "").strip(),
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
    return render_template("customers.html", customers=items, q=q)


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
    if request.method == "POST":
        it = Item(
            name=request.form.get("name", "").strip(),
            hsn_code=request.form.get("hsn_code", "").strip(),
            unit=_resolve_unit(request.form),
            category_id=_resolve_category_id(request.form),
            gst_rate=float(request.form.get("gst_rate") or 0),
            sale_price=float(request.form.get("sale_price") or 0),
            current_stock=float(request.form.get("current_stock") or 0),
            reorder_level=float(request.form.get("reorder_level") or 0),
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
                           cat_filter=cat_filter, common_units=COMMON_UNITS)


@app.route("/items/<int:iid>/edit", methods=["GET", "POST"])
@login_required
def edit_item(iid):
    it = Item.query.get_or_404(iid)
    if request.method == "POST":
        it.name = request.form.get("name", "").strip()
        it.hsn_code = request.form.get("hsn_code", "").strip()
        it.unit = _resolve_unit(request.form)
        it.category_id = _resolve_category_id(request.form)
        it.gst_rate = float(request.form.get("gst_rate") or 0)
        it.sale_price = float(request.form.get("sale_price") or 0)
        it.reorder_level = float(request.form.get("reorder_level") or 0)
        it.track_stock = bool(request.form.get("track_stock"))
        db.session.commit()
        flash(t("flash_item_updated"), "success")
        return redirect(url_for("items"))
    categories = Category.query.order_by(Category.name).all()
    return render_template("item_form.html", item=it, categories=categories, common_units=COMMON_UNITS)


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
            db.session.add(Category(name=name))
            db.session.commit()
            flash(t("flash_category_added"), "success")
        return redirect(url_for("categories_page"))
    cats = Category.query.order_by(Category.name).all()
    counts = {c.id: Item.query.filter_by(category_id=c.id).count() for c in cats}
    return render_template("categories.html", categories=cats, counts=counts)


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
    rows = Invoice.query.order_by(Invoice.created_at.desc()).all()
    if q:
        rows = [i for i in rows if q in i.invoice_no.lower() or q in i.customer.name.lower() or q in i.date]
    return render_template("invoices.html", invoices=rows, q=q)


@app.route("/invoices/new", methods=["GET", "POST"])
@login_required
def new_invoice():
    settings = Settings.get()
    if request.method == "POST":
        return _save_invoice(None, settings)

    next_no = f"{settings.invoice_prefix}-{str(settings.next_invoice_no).zfill(4)}"
    return render_template("invoice_form.html", invoice=None, next_invoice_no=next_no,
                           customers=Customer.query.order_by(Customer.name).all())


@app.route("/invoices/<int:invid>/edit", methods=["GET", "POST"])
@login_required
def edit_invoice(invid):
    inv = Invoice.query.get_or_404(invid)
    settings = Settings.get()
    if request.method == "POST":
        return _save_invoice(inv, settings)
    return render_template("invoice_form.html", invoice=inv, next_invoice_no=inv.invoice_no,
                           customers=Customer.query.order_by(Customer.name).all())


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
    other_charges = float(request.form.get("other_charges") or 0)

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
        inv.invoice_no = request.form.get("invoice_no", inv.invoice_no).strip()
    else:
        inv = Invoice(invoice_no=request.form.get("invoice_no", "").strip() or
                      f"{settings.invoice_prefix}-{str(settings.next_invoice_no).zfill(4)}")
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

    if not existing_invoice:
        db.session.add(inv)
        settings.next_invoice_no = (settings.next_invoice_no or 1) + 1
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
    return render_template("invoice_print.html", inv=inv, settings=settings)


@app.route("/invoices/<int:invid>/pdf")
@login_required
def invoice_pdf(invid):
    inv = Invoice.query.get_or_404(invid)
    settings = Settings.get()
    from invoice_pdf import build_invoice_pdf
    buf = build_invoice_pdf(inv, settings, t)
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                      download_name=f"{inv.invoice_no}.pdf")
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
        db.session.commit()
        flash(t("flash_settings_saved"), "success")
        return redirect(url_for("settings_page"))
    return render_template("settings.html", s=s)


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
    return render_template("customer_khata.html", customer=c, entries=entries, balance=balance)


@app.route("/accounts/payments", methods=["GET", "POST"])
@login_required
def payments_page():
    if request.method == "POST":
        customer_id = request.form.get("customer_id")
        amount = float(request.form.get("amount") or 0)
        customer = Customer.query.get(int(customer_id)) if customer_id else None
        if not customer or amount <= 0:
            flash(t("flash_payment_invalid"), "error")
        else:
            p = Payment(
                customer_id=customer.id, date=request.form.get("date") or date.today().isoformat(),
                amount=amount, method=request.form.get("method", "cash"),
                note=request.form.get("note", "").strip(), created_by=current_user.id,
            )
            db.session.add(p)
            db.session.commit()
            flash(t("flash_payment_saved"), "success")
        return redirect(url_for("payments_page"))

    recent = Payment.query.filter_by(invoice_id=None).order_by(Payment.created_at.desc()).limit(50).all()
    return render_template("payments.html", customers=Customer.query.order_by(Customer.name).all(), payments=recent)


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
@app.route("/accounts/expenses", methods=["GET", "POST"])
@login_required
def expenses_page():
    if request.method == "POST":
        amount = float(request.form.get("amount") or 0)
        if amount <= 0:
            flash(t("flash_expense_invalid"), "error")
        else:
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
        return redirect(url_for("expenses_page"))

    q = request.args.get("q", "").strip().lower()
    rows = Expense.query.order_by(Expense.date.desc(), Expense.created_at.desc()).all()
    if q:
        rows = [e for e in rows if q in (e.description or "").lower() or (e.vendor and q in e.vendor.name.lower())]
    total = sum(e.amount for e in rows)
    return render_template("expenses.html", expenses=rows, vendors=Vendor.query.order_by(Vendor.name).all(),
                           q=q, total=total)


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

    day_invoices = Invoice.query.filter_by(date=sel_date).all()
    day_payments = Payment.query.filter_by(date=sel_date).all()
    day_expenses = Expense.query.filter_by(date=sel_date).all()

    money_in = sum(i.amount_received for i in day_invoices) + sum(p.amount for p in day_payments if p.invoice_id is None)
    money_out = sum(e.amount_paid for e in day_expenses)

    this_month = sel_date[:7]
    month_invoices = Invoice.query.filter(Invoice.date.startswith(this_month)).all()
    month_expenses = Expense.query.filter(Expense.date.startswith(this_month)).all()
    month_billed = sum(i.grand_total for i in month_invoices)
    month_received = sum(i.amount_received for i in month_invoices)
    month_expense_total = sum(e.amount for e in month_expenses)

    return render_template(
        "reports.html", sel_date=sel_date,
        day_invoices=day_invoices, day_payments=[p for p in day_payments if p.invoice_id is None],
        day_expenses=day_expenses, money_in=money_in, money_out=money_out,
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
    _add_column_if_missing(inspector, "item", "category_id", "category_id INTEGER")


with app.app_context():
    _run_startup_migrations()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
