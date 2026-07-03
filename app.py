import os
import json
from datetime import datetime, date
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)

from models import (
    db, User, Settings, Customer, Item, StockEntry, Invoice, InvoiceItem,
    STATE_NAMES
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")

db_url = os.environ.get("DATABASE_URL", "sqlite:///app.db")
# Railway/Render sometimes give old-style postgres:// URLs; SQLAlchemy needs postgresql://
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


def owner_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_owner:
            flash("Sirf Owner is page ko access kar sakta hai.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_globals():
    settings = Settings.get()
    return dict(firm_settings=settings, state_names=STATE_NAMES, today=date.today().isoformat())


# ---------------- setup / auth ----------------
@app.route("/setup", methods=["GET", "POST"])
def setup():
    if User.query.count() > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not username or len(password) < 6:
            flash("Sab fields fill karein, password kam se kam 6 character ka ho.", "error")
            return render_template("setup.html")
        u = User(name=name, username=username, role="owner")
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        flash("Owner account ban gaya. Ab login karein.", "success")
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
        flash("Username ya password galat hai.", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------- dashboard ----------------
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


# ---------------- customers ----------------
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
            flash("Customer name zaroori hai.", "error")
        else:
            db.session.add(c)
            db.session.commit()
            flash("Customer add ho gaya.", "success")
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
        db.session.commit()
        flash("Customer update ho gaya.", "success")
        return redirect(url_for("customers"))
    return render_template("customer_form.html", customer=c)


@app.route("/customers/<int:cid>/delete", methods=["POST"])
@login_required
@owner_required
def delete_customer(cid):
    c = Customer.query.get_or_404(cid)
    if Invoice.query.filter_by(customer_id=c.id).first():
        flash("Ye customer kisi invoice me use ho raha hai, delete nahi ho sakta.", "error")
    else:
        db.session.delete(c)
        db.session.commit()
        flash("Customer delete ho gaya.", "success")
    return redirect(url_for("customers"))


# ---------------- items ----------------
@app.route("/items", methods=["GET", "POST"])
@login_required
def items():
    if request.method == "POST":
        it = Item(
            name=request.form.get("name", "").strip(),
            hsn_code=request.form.get("hsn_code", "").strip(),
            unit=request.form.get("unit", "pcs").strip(),
            gst_rate=float(request.form.get("gst_rate") or 0),
            sale_price=float(request.form.get("sale_price") or 0),
            current_stock=float(request.form.get("current_stock") or 0),
            reorder_level=float(request.form.get("reorder_level") or 0),
            track_stock=bool(request.form.get("track_stock")),
        )
        if not it.name:
            flash("Item name zaroori hai.", "error")
        else:
            db.session.add(it)
            db.session.commit()
            flash("Item add ho gaya.", "success")
        return redirect(url_for("items"))

    q = request.args.get("q", "").strip().lower()
    rows = Item.query.order_by(Item.name).all()
    if q:
        rows = [i for i in rows if q in i.name.lower()]
    return render_template("items.html", items=rows, q=q)


@app.route("/items/<int:iid>/edit", methods=["GET", "POST"])
@login_required
def edit_item(iid):
    it = Item.query.get_or_404(iid)
    if request.method == "POST":
        it.name = request.form.get("name", "").strip()
        it.hsn_code = request.form.get("hsn_code", "").strip()
        it.unit = request.form.get("unit", "pcs").strip()
        it.gst_rate = float(request.form.get("gst_rate") or 0)
        it.sale_price = float(request.form.get("sale_price") or 0)
        it.reorder_level = float(request.form.get("reorder_level") or 0)
        it.track_stock = bool(request.form.get("track_stock"))
        db.session.commit()
        flash("Item update ho gaya.", "success")
        return redirect(url_for("items"))
    return render_template("item_form.html", item=it)


@app.route("/items/<int:iid>/delete", methods=["POST"])
@login_required
@owner_required
def delete_item(iid):
    it = Item.query.get_or_404(iid)
    if InvoiceItem.query.filter_by(item_id=it.id).first():
        flash("Ye item kisi invoice me use ho raha hai, delete nahi ho sakta.", "error")
    else:
        db.session.delete(it)
        db.session.commit()
        flash("Item delete ho gaya.", "success")
    return redirect(url_for("items"))


@app.route("/items/<int:iid>/stock", methods=["POST"])
@login_required
def add_stock(iid):
    it = Item.query.get_or_404(iid)
    qty = float(request.form.get("qty") or 0)
    note = request.form.get("note", "").strip()
    if qty == 0:
        flash("Quantity 0 nahi ho sakti.", "error")
        return redirect(url_for("items"))
    it.current_stock = (it.current_stock or 0) + qty
    entry = StockEntry(item_id=it.id, qty=qty, entry_type="in" if qty > 0 else "adjust",
                        note=note, created_by=current_user.id)
    db.session.add(entry)
    db.session.commit()
    flash(f"Stock update ho gaya. Naya stock: {it.current_stock} {it.unit}", "success")
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


# ---------------- GST calculation helper ----------------
def calc_invoice_totals(line_items, discount_type, discount_value, other_charges, firm_state, customer_state):
    """line_items: list of dicts with qty, rate, gst_rate. Returns computed totals + per-line tax."""
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


# ---------------- invoices ----------------
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
        flash("Customer select karein.", "error")
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
        flash("Kam se kam ek valid item add karein.", "error")
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
        # restore stock from old lines before replacing
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
    flash("Invoice save ho gaya.", "success")
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
    flash("Invoice delete ho gaya.", "success")
    return redirect(url_for("invoices"))


@app.route("/invoices/<int:invid>/print")
@login_required
def print_invoice(invid):
    inv = Invoice.query.get_or_404(invid)
    settings = Settings.get()
    return render_template("invoice_print.html", inv=inv, settings=settings)


# ---------------- settings & users (owner only) ----------------
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
        flash("Settings save ho gayi.", "success")
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
            flash("Sab fields fill karein, password kam se kam 6 character.", "error")
        elif User.query.filter_by(username=username).first():
            flash("Ye username already use ho raha hai.", "error")
        else:
            u = User(username=username, name=name, role=role)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            flash("User add ho gaya.", "success")
        return redirect(url_for("users_page"))
    return render_template("users.html", users=User.query.order_by(User.created_at).all())


@app.route("/users/<int:uid>/toggle", methods=["POST"])
@login_required
@owner_required
def toggle_user(uid):
    u = User.query.get_or_404(uid)
    if u.id == current_user.id:
        flash("Apne hi account ko deactivate nahi kar sakte.", "error")
    else:
        u.active = not u.active
        db.session.commit()
        flash("User status update ho gaya.", "success")
    return redirect(url_for("users_page"))


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
