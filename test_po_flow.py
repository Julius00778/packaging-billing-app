"""End-to-end PO flow test: new PO -> map lines -> confirm -> dispatch.

Ye test asli app.py ke through chalta hai (test client se), stub se nahi. Ek badi
JPEG upload karke ye bhi check karta hai ki DB me chhoti hoke jaa rahi hai.

    python3 test_po_flow.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
DB = os.path.join(os.path.dirname(__file__), "test_po.db")
os.environ["DATABASE_URL"] = "sqlite:///" + DB
if os.path.exists(DB):
    os.remove(DB)

from app import app                                  # noqa: E402
import app as app_mod                                # noqa: E402
from models import db, Customer, Item, Invoice       # noqa: E402
import po_module                                    # noqa: E402
from po_module import PartyProductMap, PurchaseOrder, POLine, PartyPOConfig  # noqa: E402

fails = []
client = app.test_client()


def check(label, got, want=True):
    if got != want:
        fails.append(f"{label}\n    got:  {got!r}\n    want: {want!r}")


def ok(label, resp, expect=200):
    check(f"{label} -> HTTP", resp.status_code, expect)
    return resp


def big_jpeg(px=2400):
    """~2400px ki photo, jaisi phone se aati hai."""
    from PIL import Image
    import random
    img = Image.new("RGB", (px, int(px * 0.75)))
    pix = img.load()
    # thoda noise, warna flat image itni compress ho jaati hai ki test bekaar ho jaye
    for y in range(0, img.height, 4):
        for x in range(0, img.width, 4):
            pix[x, y] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


# ---------------------------------------------------------------- setup + login
ok("GET /setup", client.get("/setup"))
ok("POST /setup", client.post("/setup", data={"firm_name": "Test Packaging", "name": "Rahul",
                                              "username": "owner", "password": "secret123"},
                              follow_redirects=True))
ok("login", client.post("/login", data={"username": "owner", "password": "secret123"},
                        follow_redirects=True))

with app.app_context():
    db.session.add_all([Customer(name="Shree Traders"), Item(name="EPE Foam Sheet 2mm")])
    db.session.commit()
    cust_id = Customer.query.filter_by(name="Shree Traders").first().id
    item_id = Item.query.first().id

# ------------------------------------------------------------------ nav + pages
r = ok("GET /po/", client.get("/po/"))
check("nav me PO tab dikh raha hai", b'href="/po/"' in r.data)
ok("GET /po/new", client.get("/po/new"))
ok("GET /po/mappings", client.get("/po/mappings"))
ok("GET /po/dispatch", client.get("/po/dispatch"))

# ------------------------------------------------------------------- naya PO
scan = big_jpeg(2400)
check("test scan sach me bada hai (>1MB)", len(scan) > 1_000_000)
r = ok("POST /po/new", client.post("/po/new", data={
    "customer_id": str(cust_id), "po_number": "PO-4471", "size_unit": "inch",
    "source": "whatsapp", "note": "30 Aug tak chahiye",
    "raw_text": "Purchase Order 4471\n1. 2mm foam 12x18 - 500 pcs\n2. 18 X 24  qty 200\n"
                "3. 300x450 mm 150 nos\nDelivery by 30 Aug\nThanks",
    "scan": (io.BytesIO(scan), "po.jpg"),
}, content_type="multipart/form-data", follow_redirects=True))

with app.app_context():
    po = PurchaseOrder.query.filter_by(po_number="PO-4471").first()
    check("PO ban gaya", po is not None)
    check("3 lines padhi gayin", len(po.lines), 3)
    check("scan DB me hai", bool(po.scan_data))
    check(f"scan compress hua ({len(scan)//1024} KB -> {len(po.scan_data or b'')//1024} KB)",
          len(po.scan_data) < len(scan) // 2)
    check("scan JPEG me convert hua", po.scan_mime, "image/jpeg")
    check("party ka unit yaad raha", PartyPOConfig.unit_for(cust_id), "inch")
    check("shuru me saari lines unresolved", po.unresolved_count, 3)
    po_id = po.id
    line_ids = [l.id for l in po.lines]
    keys = [l.canonical_key for l in po.lines]
check("line 1 ka key (12x18 inch)", keys[0], "304.8x457.2")
check("line 3 ka key (300x450 mm)", keys[2], "300.0x450.0")

# duplicate PO wapas usi PO pe le jaata hai, naya nahi banta
client.post("/po/new", data={"customer_id": str(cust_id), "po_number": "PO-4471",
                             "size_unit": "inch", "raw_text": "12x18 - 5 pcs"},
            content_type="multipart/form-data", follow_redirects=True)
with app.app_context():
    check("duplicate PO nahi bana", PurchaseOrder.query.filter_by(po_number="PO-4471").count(), 1)

# ------------------------------------------- ek-ek line wala raasta
# Photo dhundhli ho toh text paste karna kaam nahi aata — aadmi dekh ke ek
# line bharta hai. Yahan kuch andaza nahi lagta, sab khaano me aata hai.
r = ok("naye order ka page", client.get("/po/new"))
page = r.data.decode("utf8", "ignore")
check("page ek form hai — lines ka khaana hamesha dikhta hai", 'id="rowsBody"' in page)
check("photo se padhne ka button hai", 'id="scanPick"' in page)
check("paste ka rasta bhi hai", 'id="pasteDo"' in page)
check("form seedha rows bhejta hai", 'name="entry_mode" value="rows"' in page)

ok("rows se order", client.post("/po/new", data={
    "customer_id": str(cust_id), "po_number": "PO-ROW-1", "size_unit": "cm",
    "entry_mode": "rows",
    "line_code": ["hm 01", "", "  "],          # chhota-bada aur space chalega
    "line_size": ["", "23x14x5", ""],          # bina code, sirf size
    "line_qty": ["120", "40", ""],             # teesri row poori khaali
    "line_unit": ["pcs", "box", "pcs"],
}, content_type="multipart/form-data", follow_redirects=True))
with app.app_context():
    rp = PurchaseOrder.query.filter_by(po_number="PO-ROW-1").first()
    check("khaali row chhod di gayi", len(rp.lines), 2)
    check("code saaf hoke aaya", rp.lines[0].item_code, "HM01")
    check("code wali line ki qty", rp.lines[0].qty, 120.0)
    check("uski unit bhi wahi", rp.lines[0].qty_unit, "pcs")
    check("bina size ke koi key nahi", rp.lines[0].canonical_key, "")
    check("size wali line ka key bana (cm)", rp.lines[1].canonical_key, "50.0x140.0x230.0")
    check("box wali unit rahi", rp.lines[1].qty_unit, "box")
    check("unit yahan andaze se nahi aayi",
          [l.unit_source for l in rp.lines], ["", "form"])
    check("raw_text bhi bana", "HM01" in rp.lines[0].raw_text)
    ok("rows wala order khulta hai", client.get(f"/po/{rp.id}"))
    client.post(f"/po/{rp.id}/reject", follow_redirects=True)

r = ok("sab khaali rows", client.post("/po/new", data={
    "customer_id": str(cust_id), "po_number": "PO-ROW-2", "size_unit": "cm",
    "entry_mode": "rows", "line_code": ["", ""], "line_size": ["", ""],
    "line_qty": ["", ""], "line_unit": ["pcs", "pcs"],
}, content_type="multipart/form-data", follow_redirects=True))
check("khaali form pe wajah batayi", "No order line was found" in r.data.decode("utf8", "ignore"))
with app.app_context():
    check("khaali form se order nahi bana",
          PurchaseOrder.query.filter_by(po_number="PO-ROW-2").count(), 0)

ok("GET /po/<id> review", client.get(f"/po/{po_id}"))

# confirm abhi block hona chahiye
r = client.post(f"/po/{po_id}/confirm", follow_redirects=True)
with app.app_context():
    check("unmapped lines ke saath confirm block hua",
          db.session.get(PurchaseOrder, po_id).status, "pending")

# --------------------------------------------------- har line ko map karo
photo = big_jpeg(2000)
for i, lid in enumerate(line_ids, start=1):
    ok(f"map line {i}", client.post(f"/po/{po_id}/line/{lid}/map", data={
        "label": f"Product {i}", "item_code": f"ST0{i}",
        "item_id": str(item_id) if i == 1 else "",
        "image": (io.BytesIO(photo), f"p{i}.jpg"),
    }, content_type="multipart/form-data", follow_redirects=True))

with app.app_context():
    maps = PartyProductMap.query.all()
    check("3 mappings bane", len(maps), 3)
    m = maps[0]
    check("photo DB me hai", bool(m.image_data))
    check(f"photo compress hui ({len(photo)//1024} KB -> {len(m.image_data)//1024} KB)",
          len(m.image_data) <= 160 * 1024)
    check("thumbnail bana", bool(m.image_thumb))
    check(f"thumb chhota hai ({len(m.image_thumb)//1024} KB)", len(m.image_thumb) <= 20 * 1024)
    check("thumb main image se chhota hai", len(m.image_thumb) < len(m.image_data))
    check("item link hua", maps[0].item_id, item_id)
    check("sab lines resolve ho gayin", db.session.get(PurchaseOrder, po_id).unresolved_count, 0)
    map_id = m.id

# ------------------------------------------------ har product ka apna Item
# Pehle PO ke product aur Item master do alag duniya the — isliye bill me item
# ki jagah khaali jaati thi aur naam description me thus jaata tha.
with app.app_context():
    maps = PartyProductMap.query.all()
    check("har product Item master me hai", [m.item_id is not None for m in maps], [True] * 3)
    m2 = [m for m in maps if m.label == "Product 2"][0]
    check("naye Item ka naam product ka naam hai", m2.item.name, "Product 2")
    check("stock track nahi hota", m2.item.track_stock, False)
    check("GST abhi off hai", m2.item.gst_rate, 0.0)
    # dobara chalane se duplicate Item nahi banta
    before = Item.query.count()
    po_module.ensure_item_for(m2)
    db.session.commit()
    check("dobara jodne se naya item nahi banta", Item.query.count(), before)

ok("GET /po/map/<id>/image", client.get(f"/po/map/{map_id}/image"))
r = ok("GET /po/map/<id>/thumb", client.get(f"/po/map/{map_id}/thumb"))
check("thumb response chhota hai", len(r.data) <= 20 * 1024)
ok("GET /po/<id>/scan", client.get(f"/po/{po_id}/scan"))

# ------------------------------------------ dusra PO: ab auto-match hona chahiye
ok("POST /po/new (dusra PO)", client.post("/po/new", data={
    "customer_id": str(cust_id), "po_number": "PO-4472", "size_unit": "inch",
    "raw_text": "18x12 - 300 pcs",     # ulta likha hua wahi size
}, content_type="multipart/form-data", follow_redirects=True))
with app.app_context():
    po2 = PurchaseOrder.query.filter_by(po_number="PO-4472").first()
    l = po2.lines[0]
    check("18x12 ne 12x18 ka mapping auto-match kiya", l.match_status, "size")
    check("mapping juda hua", bool(l.map_id))
    check("kuch bhi unresolved nahi", po2.unresolved_count, 0)
    po2_id = po2.id

# ------------------------------------------- item code se match, aur mismatch
ok("POST /po/new (item code wala PO)", client.post("/po/new", data={
    "customer_id": str(cust_id), "po_number": "PO-4473", "size_unit": "inch",
    "raw_text": "ST01 - 250 pcs\nST02 (99x99) - 40 pcs",
}, content_type="multipart/form-data", follow_redirects=True))
with app.app_context():
    po3 = PurchaseOrder.query.filter_by(po_number="PO-4473").first()
    a, b = po3.lines[0], po3.lines[1]
    check("bina size ke, sirf code se match hua", a.match_status, "code")
    check("code wali line ka product sahi", a.mapping.item_code, "ST01")
    check("code wali line ki qty", a.qty, 250.0)
    check("code wali line pe mismatch flag nahi", a.size_mismatch, False)
    check("code + galat size bhi code se hi match hua", b.match_status, "code")
    check("par mismatch flag lag gaya", b.size_mismatch, True)
    check("mismatch ke bawajood line resolved hai", po3.unresolved_count, 0)
    po3_id = po3.id

r = ok("review screen mismatch dikhata hai", client.get(f"/po/{po3_id}"))
check("mismatch ka warning text aaya",
      "code and size do not match" in r.data.decode("utf8", "ignore"))

ok("reject PO-4473", client.post(f"/po/{po3_id}/reject", follow_redirects=True))

# -------------------------------------------------------------------- qty
# Qty 0 ka matlab bill me amount 0 — party ko khaali bill chala jayega.
with app.app_context():
    po = db.session.get(PurchaseOrder, po_id)
    saved_qty = po.lines[0].qty
    po.lines[0].qty = 0
    db.session.commit()
    check("qty 0 pakdi gayi", po.no_qty_count, 1)
r = ok("confirm bina qty ke", client.post(f"/po/{po_id}/confirm", follow_redirects=True))
with app.app_context():
    check("bina qty ke order pending hi raha",
          db.session.get(PurchaseOrder, po_id).status, "pending")
check("qty ki wajah batayi gayi", "has no quantity" in r.data.decode("utf8", "ignore"))
r = ok("review screen pe qty ka warning", client.get(f"/po/{po_id}"))
body = r.data.decode("utf8", "ignore")
check("screen pe qty ki baat likhi hai", "has no quantity" in body)
check("bhejne ka button band hai", "disabled" in body)
with app.app_context():
    po = db.session.get(PurchaseOrder, po_id)
    po.lines[0].qty = saved_qty
    db.session.commit()
    check("qty wapas aa gayi", db.session.get(PurchaseOrder, po_id).no_qty_count, 0)

# -------------------------------------------------------- category + unit
# Category batati hai ki maal kis unit me bikta hai. Foam piece ya roll me,
# scrap sirf kilo me — is rok ke bina roll ka rate piece pe lag sakta hai.
from models import Category                                  # noqa: E402
with app.app_context():
    foam = Category.query.filter_by(name="Foam").first()
    check("shuruaati categories ban gayin", foam is not None)
    check("foam ki units", foam.unit_list(), ["pcs", "roll"])
    check("scrap sirf kilo me",
          Category.query.filter_by(name="Scrap").first().unit_list(), ["kg"])
    check("category na ho toh koi rok nahi", Category(name="x").unit_list(), [])
    foam_id = foam.id
    map_ids = [m.id for m in PartyProductMap.query.order_by(PartyProductMap.id).all()]

r = ok("GET /po/mappings", client.get("/po/mappings"))
check("category tay na hone ki baat likhi hai",
      "have no category yet" in r.data.decode("utf8", "ignore"))

# Categories screen se units badalna
r = ok("GET /items/categories", client.get("/items/categories"))
check("units ka khana screen pe hai", 'name="units_' in r.data.decode("utf8", "ignore"))
ok("units badlo", client.post("/items/categories/units",
                              data={f"units_{foam_id}": "pcs, roll, sheet"},
                              follow_redirects=True))
with app.app_context():
    check("nayi units save hui",
          db.session.get(Category, foam_id).unit_list(), ["pcs", "roll", "sheet"])
ok("units wapas", client.post("/items/categories/units",
                              data={f"units_{foam_id}": "pcs, roll"},
                              follow_redirects=True))

ok("sab products ko Foam me daalo",
   client.post("/po/mappings/categories",
               data={f"cat_{mid}": str(foam_id) for mid in map_ids},
               follow_redirects=True))
with app.app_context():
    m = db.session.get(PartyProductMap, map_ids[0])
    check("category lag gayi", m.item.category_id, foam_id)
    check("ab sirf foam wali units", m.unit_choices(), ["pcs", "roll"])
    check("item ki unit category ke andar hai", m.item.unit in ["pcs", "roll"], True)

# unit badlo — us unit ka apna rate hona chahiye, piece wala nahi
with app.app_context():
    line = db.session.get(PurchaseOrder, po_id).lines[0]
    line_id, old_unit = line.id, line.qty_unit
    po_module.set_line_rate(line, 30.0)
ok("unit ko roll karo",
   client.post(f"/po/{po_id}/rates", data={f"unit_{line_id}": "roll"},
               follow_redirects=True))
with app.app_context():
    line = db.session.get(POLine, line_id)
    check("unit badal gayi", line.qty_unit, "roll")
    check("piece wala rate roll pe nahi chipka", line.rate, 0.0)
    check("piece wala rate ab bhi yaad hai",
          po_module.PartyRate.look_up(line.map_id, old_unit).rate, 30.0)

ok("roll ka apna rate do",
   client.post(f"/po/{po_id}/rates", data={f"rate_{line_id}": "900"},
               follow_redirects=True))
with app.app_context():
    line = db.session.get(POLine, line_id)
    check("roll ka rate laga", line.rate, 900.0)
ok("wapas piece karo",
   client.post(f"/po/{po_id}/rates", data={f"unit_{line_id}": old_unit},
               follow_redirects=True))
with app.app_context():
    line = db.session.get(POLine, line_id)
    check("unit wapas aa gayi", line.qty_unit, old_unit)
    check("piece ka purana rate khud bhar gaya", line.rate, 30.0)
    check("aur wo yaad se aaya hai", line.rate_from_memory, True)

# category ke bahar ki unit nahi lag sakti
ok("kilo me daalne ki koshish",
   client.post(f"/po/{po_id}/rates", data={f"unit_{line_id}": "kg"},
               follow_redirects=True))
with app.app_context():
    check("foam kilo me nahi bikta — unit nahi badli",
          db.session.get(POLine, line_id).qty_unit, old_unit)

with app.app_context():
    line = db.session.get(POLine, line_id)
    line.rate = 0.0
    line.rate_from_memory = False
    db.session.commit()

# ------------------------------------------------------------------- rate
# Rate ke bina order aage nahi badhna chahiye — bill isi se banta hai.
r = ok("confirm bina rate ke", client.post(f"/po/{po_id}/confirm", follow_redirects=True))
with app.app_context():
    check("bina rate ke order pending hi raha",
          db.session.get(PurchaseOrder, po_id).status, "pending")
check("rate ki wajah batayi gayi", "has no rate" in r.data.decode("utf8", "ignore"))
r = ok("review screen pe rate ka warning", client.get(f"/po/{po_id}"))
body = r.data.decode("utf8", "ignore")
check("screen pe rate ki baat likhi hai", "still needs a rate" in body)
check("rate baaki ho toh bhejne ka button band", "disabled" in body)

with app.app_context():
    lines = db.session.get(PurchaseOrder, po_id).lines
    rate_form = {f"rate_{l.id}": str(20 + i * 5) for i, l in enumerate(lines)}
    qtys = [(l.item_code, l.qty, l.qty_unit) for l in lines]
ok("rate save karo", client.post(f"/po/{po_id}/rates", data=rate_form, follow_redirects=True))

with app.app_context():
    po = db.session.get(PurchaseOrder, po_id)
    check("saare rate lag gaye", po.no_rate_count, 0)
    check("pehli line ka rate", po.lines[0].rate, 20.0)
    check("amount = qty x rate", po.lines[0].amount, round(po.lines[0].qty * 20.0, 2))
    check("total sahi", po.total, round(sum(l.qty * l.rate for l in po.lines), 2))
    # rate party + code + unit pe yaad rehna chahiye
    known = po_module.PartyRate.look_up(po.lines[0].map_id, po.lines[0].qty_unit)
    check("rate yaad raha", known.rate if known else None, 20.0)
    check("rate product se juda hai, line ke code se nahi",
          known.map_id, po.lines[0].map_id)
    check("dusre unit ka rate alag hi rehta hai",
          po_module.PartyRate.look_up(po.lines[0].map_id, "box"), None)

# Naya PO usi party ka — rate apne aap bhar jaana chahiye
ok("POST /po/new (rate memory test)", client.post("/po/new", data={
    "customer_id": str(cust_id), "po_number": "PO-4474", "size_unit": "inch",
    "raw_text": "ST01 - 10 pcs",
}, content_type="multipart/form-data", follow_redirects=True))
with app.app_context():
    po4 = PurchaseOrder.query.filter_by(po_number="PO-4474").first()
    check("purana rate apne aap bhar gaya", po4.lines[0].rate, 20.0)
    check("aur flag laga ki ye yaad se aaya", po4.lines[0].rate_from_memory, True)
    check("is order ka rate baaki nahi", po4.no_rate_count, 0)

# ------------------------------------------------------ operator ko bhejna
# Telegram set nahi hai — order aage nahi badhna chahiye, wajah saaf honi chahiye.
r = ok("confirm bina Telegram ke", client.post(f"/po/{po_id}/confirm", follow_redirects=True))
with app.app_context():
    check("bina operator group ke order aage nahi badha",
          db.session.get(PurchaseOrder, po_id).status, "pending")
check("Telegram ki wajah batayi gayi",
      "Operator group chuna nahi gaya" in r.data.decode("utf8", "ignore"))

# Aage ka safar office ki taraf se — Telegram wala hissa test_po_telegram.py me hai.
with app.app_context():
    po = db.session.get(PurchaseOrder, po_id)
    moved, _ = po_module.move_status(po, "with_operator")
    check("office se operator ke paas", (moved, po.status), (True, "with_operator"))
    moved, _ = po_module.move_status(po, "in_production", who="Ramesh")
    check("operation me", (moved, po.status), (True, "in_production"))
    moved, _ = po_module.move_status(po, "made", who="Ramesh")
    check("ban gaya", (moved, po.status), (True, "made"))
    check("made_at set", bool(po.made_at))
    check("times_used badha", db.session.get(PartyProductMap, map_id).times_used, 1)
    check("galat chhalang nahi chalti", po_module.move_status(po, "pending")[0], False)

    # ------------------------------------------------------------------ bill
    check("ban gaya toh bill bhi ban gaya", bool(po.invoice_id))
    inv = db.session.get(Invoice, po.invoice_id)
    check("bill usi party ka", inv.customer_id, cust_id)
    check("bill me utni hi lines", len(inv.items), len(po.lines))
    check("bill ka total order ke total jitna", inv.grand_total, po.total)
    check("GST abhi off hai", (inv.cgst_amount, inv.sgst_amount, inv.igst_amount), (0.0, 0.0, 0.0))
    check("invoice number mil gaya", bool(inv.invoice_no))
    check("bill me order ka zikr", "PO-4471" in (inv.notes or ""))
    # Bill ki har line asli Item se judi ho — warna invoice edit me Item ka
    # khana khaali rehta hai aur naam sirf description me dikhta hai.
    check("bill ki har line ka apna item hai",
          [bool(li.item_id) for li in inv.items], [True] * len(inv.items))
    check("bill ki line ka item wahi hai jo product ka hai",
          sorted(li.item_id for li in inv.items),
          sorted(l.mapping.item_id for l in po.lines))
    check("bill me unit line ki unit hai",
          sorted(li.unit for li in inv.items), sorted(l.qty_unit for l in po.lines))
    old_inv_id = po.invoice_id
    inv_no = inv.invoice_no
    po_module.move_status(po, "in_production")
    po_module.move_status(po, "made")
    check("dobara made karne se naya bill nahi banta", po.invoice_id, old_inv_id)
    check("ek hi bill bana", Invoice.query.filter_by(customer_id=cust_id).count(), 1)

ok("bill print khulta hai", client.get(f"/invoices/{old_inv_id}/print"))

# ------------------------------------------------- bill usi screen pe khulta hai
# Naya tab nahi — invoices list se overlay khulta hai jo yahi hissa laata hai.
r = ok("invoices list", client.get("/invoices"))
body = r.data.decode("utf8", "ignore")
check("list se bill khulta hai, naye tab me nahi", "data-bill=" in body)
check("overlay page pe maujood hai", 'id="billOverlay"' in body)
# List me har bill ka link overlay kholta hai. Overlay ke andar ek "naye tab me
# kholo" wala raasta jaan-boojh ke hai — wo tabhi dikhta hai jab overlay khud
# na khul paye, isliye use ginti me nahi le rahe.
import re as _re                                            # noqa: E402
table = body.split("<table", 1)[-1].split("</table>", 1)[0]
check("list me koi bill naye tab me nahi khulta", 'target="_blank"' in table, False)
check("har bill overlay se khulta hai",
      len(_re.findall(r'data-bill="\d+"', table)) >= 1)
check("bill number attribute me saaf jaata hai", f'data-bill-no="{inv_no}"' in table)
# Inline onclick me bill number apne quote leke aata tha aur attribute beech me
# kat jaata tha — link chup-chaap naye page pe le jaata. Isliye row me inline JS nahi.
check("bill ke link me inline JS nahi", "onclick=" in table, False)

r = ok("bill ka preview", client.get(f"/invoices/{old_inv_id}/preview"))
prev = r.data.decode("utf8", "ignore")
check("preview me poora page nahi aata", "<html" not in prev.lower())
check("preview me dono copy hain", prev.count('class="pinv-half"'), 2)
check("preview me bill number hai", inv_no in prev)

r = ok("sirf party copy", client.get(f"/invoices/{old_inv_id}/preview?copies=party"))
prev = r.data.decode("utf8", "ignore")
check("ek hi copy aayi", prev.count('class="pinv-half"'), 1)
check("aur wo party wali hai", "PARTY" in prev.upper() and "OFFICE" not in prev.upper())

r = ok("sirf office copy", client.get(f"/invoices/{old_inv_id}/preview?copies=office"))
check("office wali copy aayi",
      "OFFICE" in r.data.decode("utf8", "ignore").upper())

r = ok("ulta-seedha copies value", client.get(f"/invoices/{old_inv_id}/preview?copies=xyz"))
check("anjaan value pe dono copy", r.data.decode("utf8", "ignore").count('class="pinv-half"'), 2)

# PDF asli file bane — browser ke "Save as PDF" pe nahi chhodna
r = ok("PDF banta hai", client.get(f"/invoices/{old_inv_id}/pdf"))
check("PDF hi hai", r.data[:5], b"%PDF-")
check("PDF download hota hai", "attachment" in r.headers.get("Content-Disposition", ""))
both_pdf = len(r.data)
r = ok("ek copy ka PDF", client.get(f"/invoices/{old_inv_id}/pdf?copies=party"))
check("wo bhi PDF hai", r.data[:5], b"%PDF-")
check("ek copy ka PDF chhota hai", len(r.data) < both_pdf)

r = ok("bill print screen", client.get("/po/bills?show=all"))
check("wahan bhi overlay hai", 'id="billOverlay"' in r.data.decode("utf8", "ignore"))

# ------------------------------------------------------------- dashboard
# Pehla sawaal "ab tak kitna" nahi, "aaj kya karna hai" hota hai.
r = ok("dashboard khulta hai", client.get("/"))
dash = r.data.decode("utf8", "ignore")
check("aaj wala hissa upar hai", "Today" in dash)
check("aaj ka bill total dikh raha hai", "Bills made today" in dash)
check("aaj ka paisa dikh raha hai", "Received today" in dash)
check("har stage ka apna raasta hai", '/po/?status=made' in dash or 'status=made' in dash)
with app.app_context():
    snap = app_mod._today_snapshot(db.session.get(Invoice, old_inv_id).date)
    check("aaj ke bill gine gaye", snap["bill_count"] >= 1)
    check("bill ka total sahi", snap["bill_total"] > 0)
    check("stage ki ginti aayi", len(snap["stages"]), 4)
    check("print baaki ki ginti bhi", snap["to_print"] >= 0)
    khaali = app_mod._today_snapshot("2020-01-01")
    check("purane din ka koi bill nahi", khaali["bill_count"], 0)
    check("purane din kuch aaya bhi nahi", khaali["received"], 0)

# Invoice edit screen pe item ka naam Item ke khane me aana chahiye
r = ok("bill edit khulta hai", client.get(f"/invoices/{old_inv_id}/edit"))
body = r.data.decode("utf8", "ignore")
check("edit me koi line item_id null nahi hai", "item_id: null" not in body)
with app.app_context():
    names = [db.session.get(Item, li.item_id).name
             for li in db.session.get(Invoice, old_inv_id).items]
# Item ka dropdown JS se bharta hai, isliye list wahin se check karo
master = ok("item master API", client.get("/api/items")).get_json()
master_names = [it["name"] for it in master]
check("bill ke saare item master list me hain", all(n in master_names for n in names))

# ------------------------------------------------------- accountant ka print page
r = ok("GET /po/bills", client.get("/po/bills"))
check("aaj ka bill list me hai", b"PO-4471" in r.data)
check("print baaki dikh raha hai", ">left<" in r.data.decode("utf8", "ignore"))

r = ok("sab ek saath print", client.get("/po/bills/print"))
body = r.data.decode("utf8", "ignore")
check("print page pe bill number hai", inv_no in body)
check("do copy banti hain (party + office)",
      body.count('class="pinv-half"'), 2)     # ek hi bill hai, uski do copy
check("har bill apne kaagaz pe", 'class="bill-sheet"' in body)

# nishan lagao
ok("print ho gaya nishan", client.post(f"/po/bills/{po_id}/printed",
                                       data={"printed": "1"}, follow_redirects=True))
with app.app_context():
    check("nishan lag gaya", bool(db.session.get(PurchaseOrder, po_id).bill_printed_at))
r = ok("ab 'baaki' list khaali", client.get("/po/bills"))
check("print ho chuka bill baaki list me nahi", b"PO-4471" not in r.data)
r = ok("saare dikhao", client.get("/po/bills?show=all"))
check("saare wali list me hai", b"PO-4471" in r.data)
check("print ho gaya dikh raha hai", ">printed<" in r.data.decode("utf8", "ignore"))

ok("nishan hata do", client.post(f"/po/bills/{po_id}/printed",
                                 data={"printed": "0"}, follow_redirects=True))
with app.app_context():
    check("nishan hat gaya", db.session.get(PurchaseOrder, po_id).bill_printed_at, None)

# aur din ka koi bill nahi
r = ok("purane din ka page", client.get("/po/bills?date=2020-01-01&show=all"))
check("us din kuch nahi tha", "No bill was made this day" in r.data.decode("utf8", "ignore"))
r = ok("purane din ka baaki-wala page", client.get("/po/bills?date=2020-01-01"))
check("us din print karne ko bhi kuch nahi",
      "No bill left to print" in r.data.decode("utf8", "ignore"))
r = ok("bina bill ke print", client.get("/po/bills/print?date=2020-01-01", follow_redirects=True))
check("print karne ko kuch nahi hai toh wapas bhej deta hai",
      "no bill left to print" in r.data.decode("utf8", "ignore"))


r = ok("GET /po/dispatch", client.get("/po/dispatch"))
check("ban gaya order dispatch list me hai", b"PO-4471" in r.data)

ok("mark dispatched", client.post(f"/po/{po_id}/dispatched", follow_redirects=True))
with app.app_context():
    check("PO dispatched", db.session.get(PurchaseOrder, po_id).status, "dispatched")

ok("cancel dusra PO", client.post(f"/po/{po2_id}/reject",
                                  data={"note": "party ne cancel kiya"}, follow_redirects=True))
with app.app_context():
    check("PO cancel hua", db.session.get(PurchaseOrder, po2_id).status, "rejected")

for st in po_module.PO_STATUSES:
    ok(f"GET /po/?status={st}", client.get(f"/po/?status={st}"))
ok("GET /po/mappings?customer_id=", client.get(f"/po/mappings?customer_id={cust_id}"))

# ---------------------------------------------- purani screens abhi bhi chalti hain
for path in ("/", "/invoices", "/customers", "/items", "/accounts"):
    ok(f"GET {path} (regression)", client.get(path))

if fails:
    print(f"FAILED {len(fails)} check(s):\n")
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("PO flow: all checks passed")
