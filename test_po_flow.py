"""End-to-end PO flow test: new PO -> map lines -> confirm -> dispatch.

Ye test asli app.py ke through chalta hai (test client se), stub se nahi. Ek badi
JPEG upload karke ye bhi check karta hai ki DB me chhoti hoke jaa rahi hai.

    python3 test_po_flow.py
"""
import io
import os
import sys
import re as _re2

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

# ------------------------------------------- order number khud bhar jaata hai
# Julius pehle se SEP04, SEP05 aise likh raha tha. Ab haath se likhna nahi
# padta — mahine ka naam aur uske aage ginti apne aap aati hai.
from datetime import date as _date
import po_module as _M

with app.app_context():
    mon = _M.MONTH_CODES[_date.today().month - 1]
    first = _M.next_po_number()
    check("naya mahina ho toh 01 se shuru", first, mon + "01")

r = ok("form pe number pehle se bhara", client.get("/po/new"))
form = r.data.decode("utf8", "ignore")
check("form me suggestion dikhta hai", f'value="{first}"' in form)
check("form apna suggestion yaad rakhta hai",
      f'name="po_number_auto" value="{first}"' in form)

# Number ka khaana khaali chhod do — app khud laga deti hai
ok("bina number ke order", client.post("/po/new", data={
    "customer_id": str(cust_id), "po_number": "", "size_unit": "inch",
    "raw_text": "12x18 - 5 pcs"},
    content_type="multipart/form-data", follow_redirects=True))
with app.app_context():
    check("khaali number pe bhi order bana",
          PurchaseOrder.query.filter_by(po_number=first).count(), 1)
    second = _M.next_po_number()
    check("agla number ek aage badha", second, mon + "02")

# Do aadmi ek saath form khole hon toh dono ko ek hi suggestion dikhta hai.
# Doosra bhejne wala usi number pe na chadh jaye — isliye bhejte waqt dobara
# dekha jaata hai. Yahi galti do parche pe ek number chhaap deti.
ok("wahi suggestion dobara bheja", client.post("/po/new", data={
    "customer_id": str(cust_id), "po_number": first, "po_number_auto": first,
    "size_unit": "inch", "raw_text": "12x18 - 6 pcs"},
    content_type="multipart/form-data", follow_redirects=True))
with app.app_context():
    check("purane number pe dobara nahi chadha",
          PurchaseOrder.query.filter_by(po_number=first).count(), 1)
    check("naya order agle number pe gaya",
          PurchaseOrder.query.filter_by(po_number=second).count(), 1)
    # Beech ka khaali khaana nahi bharta — us number ka parcha bahar ja chuka
    # ho sakta hai, aur ek number do parche pe hona sabse buri galti hogi.
    gone = PurchaseOrder.query.filter_by(po_number=second).first()
    db.session.delete(gone)
    db.session.commit()
    check("hataye hue number pe wapas nahi jaata", _M.next_po_number(), mon + "03")

# Party ka apna number ho toh wo waise ka waisa chalta hai
ok("apna number", client.post("/po/new", data={
    "customer_id": str(cust_id), "po_number": "PO-9001", "po_number_auto": first,
    "size_unit": "inch", "raw_text": "12x18 - 7 pcs"},
    content_type="multipart/form-data", follow_redirects=True))
with app.app_context():
    check("haath ka likha number waisa hi raha",
          PurchaseOrder.query.filter_by(po_number="PO-9001").count(), 1)

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

# --------------------------------------------- form ko party ka maal milta hai
# Code yaad rakhna aadmi ka kaam nahi. Party chunte hi uska saara maal aata hai,
# aur line pe wo chunte hi size, unit aur pichla rate khud bhar jaate hain.
r = ok("party ka maal", client.get(f"/po/party/{cust_id}/products"))
data = r.get_json()
check("maal ki list aayi", data["ok"] and len(data["products"]) >= 1, True)
first = next(p for p in data["products"] if p["code"] == "ST01")
check("code ke saath naam bhi", bool(first["label"]), True)
check("size bhi saath aata hai", bool(first["size"]), True)
check("jaayaz units bhi", len(first["units"]) >= 1, True)
# Yahi wo cheez hai jisse form pe order ki keemat dikh jaati hai
check("pichli baar ka rate uski unit pe aata hai", first["rates"].get("pcs"), 20.0)
check("jis unit ka rate nahi, wo list me nahi", "box" in first["rates"], False)

r = ok("naya order ka form", client.get("/po/new"))
form = r.data.decode("utf8", "ignore")
check("form me maal chunne ki list hai", 'id="productList"' in form)
check("aur har line usi list se judi hai", 'list="productList"' in form)

# Style badalne pe browser purani file pakde rehta tha aur naya page toota hua
# dikhta tha — jab tak aadmi khud hard-refresh na kare. Ab link pe file ka apna
# nishan lagta hai, isliye nayi style apne aap aati hai.
check("style ke link pe nishan lagta hai",
      bool(_re2.search(r'style\.css\?v=\d+', form)))

# ---------------------------- form se aaya code: size product ka, form ka nahi
# Form ka size khud product ki list se bhara hai. Uspe "size kis naap me likha
# hai" wala chunav dobara lagana galat tha — usi ek galti se product ka apna
# size hi "code aur size match nahi karte" dikhne lagta tha. Yahan party ka
# naap jaan-boojh ke alag rakha hai (mm), jabki product inch me darj hua tha.
with app.app_context():
    st01 = PartyProductMap.query.filter_by(customer_id=cust_id, item_code="ST01").first()
    st01_key, st01_size = st01.canonical_key, st01.raw_size_text

ok("code se order, doosre naap me", client.post("/po/new", data={
    "customer_id": str(cust_id), "po_number": "PO-SIZE-1", "size_unit": "mm",
    "entry_mode": "rows",
    "line_code": ["ST01"], "line_size": [st01_size],
    "line_qty": ["10"], "line_unit": ["pcs"],
}, content_type="multipart/form-data", follow_redirects=True))
with app.app_context():
    sp = PurchaseOrder.query.filter_by(po_number="PO-SIZE-1").first()
    check("key product se aayi, form ke naap se nahi", sp.lines[0].canonical_key, st01_key)
    check("aur code se match hua", sp.lines[0].match_status, "code")
    check("jhoota size ka warning nahi aaya", sp.lines[0].size_mismatch, False)
    check("rate bhi yaad se bhar gaya", sp.lines[0].rate, 20.0)
    client.post(f"/po/{sp.id}/reject", follow_redirects=True)

# Wahi size doosre naap me paste ho toh bhi ek jaisa likha hua size jhagda
# nahi hai — farq sirf paimane ka hai, aur product ka apna naap hi sahi hai.
ok("paste se wahi size", client.post("/po/new", data={
    "customer_id": str(cust_id), "po_number": "PO-SIZE-2", "size_unit": "mm",
    "raw_text": f"ST01 {st01_size} - 10 pcs",
}, content_type="multipart/form-data", follow_redirects=True))
with app.app_context():
    sp2 = PurchaseOrder.query.filter_by(po_number="PO-SIZE-2").first()
    check("ek jaisa likha size jhagda nahi hai", sp2.lines[0].size_mismatch, False)
    client.post(f"/po/{sp2.id}/reject", follow_redirects=True)

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

# Bill sirf invoices list se nahi khulta — order ke andar se bhi, aur bill
# edit karte waqt bhi. Teenon jagah ek jaisa khulna chahiye. Invoices list
# theek karne ke baad bhi ye do jagah purani reh gayi thi, aur ye baat test ne
# nahi, live screen ne pakdi. Isliye ab har jagah ki apni ginti hai.
for where, url in [("order ke andar", f"/po/{po_id}"),
                   ("bill edit karte waqt", f"/invoices/{old_inv_id}/edit")]:
    page = ok(where, client.get(url)).data.decode("utf8", "ignore")
    check(f"{where} bill overlay se khulta hai", f'data-bill="{old_inv_id}"' in page)
    check(f"{where} overlay saath me aata hai", 'id="billOverlay"' in page)
    # Overlay ke andar khud ek "naye tab me kholo" wala raasta hai — wo tabhi
    # dikhta hai jab overlay na khul paye. Isliye ginti overlay se pehle wale
    # hisse ki hai. Yahi wo galti thi: link seedha printer pe le jaata tha.
    above = page.split('id="billOverlay"', 1)[0]
    check(f"{where} naye tab me nahi jaata",
          '/print" target="_blank"' in above, False)

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

# ------------------------------------------------- rang (black / white)
# Foam black bhi jaata hai aur white bhi. Rang line ka apna faisla hai, product
# ka nahi — isliye order bharte waqt chunna padta hai, aur wahi rang aage
# operator, bill aur challan tak jaata hai.
from models import Category as _Cat2
import po_module as _M2
with app.app_context():
    foam = _Cat2.query.filter_by(name="Foam").first()
    if not foam:
        foam = _Cat2(name="Foam", units="pcs, roll")
        db.session.add(foam)
        db.session.flush()
    foam.colours = "black, white"
    fitem = Item(name="FM01 sheet", category_id=foam.id, track_stock=False)
    db.session.add(fitem)
    db.session.flush()
    fprod = _M2.PartyProductMap(customer_id=cust_id, item_code="FM01",
                                canonical_key="50.0x300.0x450.0",
                                raw_size_text="30x45x5", label="FM01 (30x45x5)",
                                item_id=fitem.id, image_data=b"\xff\xd8\xff-foam")
    db.session.add(fprod)
    db.session.commit()
    check("category pe rang lag gaye", foam.colour_list(), ["black", "white"])

r = ok("rang bhi list me jaata hai", client.get(f"/po/party/{cust_id}/products"))
frow = [x for x in r.get_json()["products"] if x["code"] == "FM01"][0]
check("product ke saath uske rang aate hain", frow["colours"], ["black", "white"])
# Jis maal ki category me rang nahi likhe, uske liye khaali — tab form pe
# rang ka khaana khulta hi nahi.
with app.app_context():
    noc = _Cat2.query.filter_by(name="Thermacol").first()
    if not noc:
        noc = _Cat2(name="Thermacol", units="pcs")
        db.session.add(noc)
        db.session.flush()
    noc.colours = ""
    nitem = Item(name="NC01 sheet", category_id=noc.id, track_stock=False)
    db.session.add(nitem)
    db.session.flush()
    db.session.add(_M2.PartyProductMap(customer_id=cust_id, item_code="NC01",
                                       canonical_key="10.0x10.0",
                                       raw_size_text="1x1", label="NC01",
                                       item_id=nitem.id))
    db.session.commit()
r = ok("bina rang wale maal ki list", client.get(f"/po/party/{cust_id}/products"))
nrow = [x for x in r.get_json()["products"] if x["code"] == "NC01"][0]
check("jis maal me rang nahi, uske liye khaali", nrow["colours"], [])

# Bina rang bheja toh order banna hi nahi chahiye — warna operator ko phone
# karna padega ki black banana hai ya white.
before = None
with app.app_context():
    before = PurchaseOrder.query.count()
r = ok("bina rang wala order", client.post("/po/new", data={
    "customer_id": str(cust_id), "po_number": "PO-COL-1", "size_unit": "cm",
    "entry_mode": "rows",
    "line_code": ["FM01"], "line_size": [""], "line_colour": [""],
    "line_qty": ["10"], "line_unit": ["pcs"],
}, content_type="multipart/form-data", follow_redirects=True))
with app.app_context():
    check("rang bina order nahi bana", PurchaseOrder.query.count(), before)
    check("us number pe kuch nahi bana",
          PurchaseOrder.query.filter_by(po_number="PO-COL-1").count(), 0)
check("screen pe saaf wajah likhi hai",
      "FM01" in r.data.decode("utf8", "ignore")
      and "colour" in r.data.decode("utf8", "ignore").lower())

ok("rang ke saath order", client.post("/po/new", data={
    "customer_id": str(cust_id), "po_number": "PO-COL-2", "size_unit": "cm",
    "entry_mode": "rows",
    "line_code": ["FM01"], "line_size": [""], "line_colour": ["black"],
    "line_qty": ["10"], "line_unit": ["pcs"],
}, content_type="multipart/form-data", follow_redirects=True))
with app.app_context():
    cpo = PurchaseOrder.query.filter_by(po_number="PO-COL-2").first()
    check("rang ke saath order ban gaya", bool(cpo), True)
    check("line pe rang laga", cpo.lines[0].colour, "black")
    cpo_id = cpo.id
    # Operator ke card pe rang dikhna chahiye — banane wale ko pehle hi pata ho
    card = _M2.order_card_text(cpo, cpo.lines[0])
    check("operator ke card pe rang hai", "black" in card)
    check("rate ke sawaal me bhi rang hai", "black" in _M2.line_detail(cpo.lines[0]))

# Galat ya chhoot gaya rang screen se sudharta hai — photo/paste se aaye
# order me rang likha hi nahi hota, wo yahin se lagta hai.
with app.app_context():
    cpo = db.session.get(PurchaseOrder, cpo_id)
    lid = cpo.lines[0].id
ok("rang white kar do", client.post(f"/po/{cpo_id}/rates",
                                   data={f"colour_{lid}": "white",
                                         f"unit_{lid}": "pcs"},
                                   follow_redirects=True))
with app.app_context():
    check("rang badal gaya", db.session.get(PurchaseOrder, cpo_id).lines[0].colour, "white")
    # Jo rang list me hi nahi, wo nahi lagna chahiye
    client.post(f"/po/{cpo_id}/rates", data={f"colour_{lid}": "pink",
                                             f"unit_{lid}": "pcs"},
                follow_redirects=True)
    check("anjaan rang nahi lagta", db.session.get(PurchaseOrder, cpo_id).lines[0].colour, "white")

page = ok("order screen pe rang ka chunav", client.get(f"/po/{cpo_id}")).data.decode("utf8", "ignore")
check("screen pe rang ka khaana hai", f'name="colour_{lid}"' in page)

# Aur wahi rang bill ki line tak pahunchna chahiye — bina iske kaagaz pe rang
# aata hi nahi, chahe order me chuna gaya ho.
ok("rate daal do", client.post(f"/po/{cpo_id}/rates",
                               data={f"rate_{lid}": "40", f"unit_{lid}": "pcs",
                                     f"colour_{lid}": "white"},
                               follow_redirects=True))
with app.app_context():
    cpo = db.session.get(PurchaseOrder, cpo_id)
    cinv, made = _M2.make_invoice(cpo)
    check("rang order se bill ki line me utar aaya", cinv.items[0].colour, "white")
    cinv_id = cinv.id
cpage = ok("us bill ka kaagaz",
           client.get(f"/invoices/{cinv_id}/preview")).data.decode("utf8", "ignore")
check("kaagaz pe wahi rang chhapa", "white" in cpage)

# Ye bill sirf jaanch ke liye tha — hata dete hain, warna aage ke ginti wale
# test me ek extra bill ghus jaata hai.
with app.app_context():
    cpo = db.session.get(PurchaseOrder, cpo_id)
    cpo.invoice_id = None
    cinv = db.session.get(Invoice, cinv_id)
    for it in list(cinv.items):
        db.session.delete(it)
    db.session.delete(cinv)
    db.session.commit()
client.post(f"/po/{cpo_id}/reject", follow_redirects=True)

# ------------------------------------- bina naap wala maal (tape, blister)
# Julius ne pakda: "blister aur tape size ke hisaab se nahi jaate". Aise maal
# ki pehchan sirf code se hoti hai — naap ka khaana khaali rehta hai, aur us
# wajah se na photo rukni chahiye, na record.
with app.app_context():
    tape = _M2.PartyProductMap(customer_id=cust_id, item_code="TP01",
                               canonical_key="", raw_size_text="",
                               label=_M2.product_label("TP01", "", "2 inch clear"),
                               image_data=b"\xff\xd8\xff-tape")
    db.session.add(tape)
    db.session.commit()
    check("bina naap ka naam saaf banta hai", tape.label, "TP01 — 2 inch clear")

r = ok("bina naap ke maal ki list", client.get(f"/po/party/{cust_id}/products"))
rows = r.get_json()["products"]
tape_row = [x for x in rows if x["code"] == "TP01"][0]
check("list me uska size khaali jaata hai", tape_row["size"], "")
check("par naam poora jaata hai", tape_row["label"], "TP01 — 2 inch clear")

ok("bina naap wala order", client.post("/po/new", data={
    "customer_id": str(cust_id), "po_number": "PO-TAPE-1", "size_unit": "cm",
    "entry_mode": "rows",
    "line_code": ["TP01"], "line_size": [""],
    "line_qty": ["24"], "line_unit": ["pcs"],
}, content_type="multipart/form-data", follow_redirects=True))
with app.app_context():
    tp = PurchaseOrder.query.filter_by(po_number="PO-TAPE-1").first()
    check("order bana", bool(tp), True)
    line = tp.lines[0]
    check("line code se product se jud gayi", line.match_status, "code")
    check("aur wo tape hi hai", line.mapping.item_code, "TP01")
    check("naap na hone pe galat chetavni nahi aati", line.size_mismatch, False)
    tp_id = tp.id
page = ok("bina naap wala order khulta hai", client.get(f"/po/{tp_id}")).data.decode("utf8", "ignore")
check("screen pe 'naya maal' ka jhanda nahi lagta",
      "MATCHED BY ITEM CODE" in page.upper())
with app.app_context():
    client.post(f"/po/{tp_id}/reject", follow_redirects=True)

# ------------------------------------------- challan: maal ke saath, bina bhav
# Jo kaagaz maal ke saath jaata hai usme rate nahi dikhna chahiye — driver ya
# party ka aadmi sirf ginti milata hai. Par bill ka record wahi rehna chahiye.
with app.app_context():
    inv_now = db.session.get(Invoice, old_inv_id)
    rates = sorted({li.rate for li in inv_now.items})
    total_txt = "%.2f" % inv_now.grand_total
    was_hidden = inv_now.hide_pricing
    # Maal ka naam aur ginti challan pe dikhni chahiye — isliye naam asli
    # line se hi uthate hain, apne banaye hue se nahi.
    first_line = inv_now.items[0]
    line_desc = (first_line.description or "").strip()
    line_qty = "%g" % (first_line.qty or 0)

    # Category bhi kaagaz pe chhapni chahiye — bill pe aur challan pe dono.
    # Ek hi naap ka 2mm aur 4mm maal alag hota hai; ye farq kaagaz pe dikhna
    # chahiye. Category line me likhi nahi hoti, Item master se aati hai —
    # isliye category baad me theek karo toh purane bill bhi sudhar jaate
    # hain, aur bill ka apna record kabhi nahi badalta.
    from models import Category as _Cat
    _c = _Cat(name="EPE Foam Sheet", units="pcs")
    db.session.add(_c)
    db.session.flush()
    check("bill ki line kisi item se judi hai", bool(first_line.item_id), True)
    first_line.item.category_id = _c.id
    db.session.commit()
    check("line ab apni category batati hai", first_line.category_name, "EPE Foam Sheet")
    _c_id = _c.id

r = ok("challan wala preview", client.get(f"/invoices/{old_inv_id}/preview?mode=challan"))
chal = r.data.decode("utf8", "ignore")
check("challan me koi rate nahi dikhta",
      any("%.2f" % rt in chal for rt in rates if rt), False)
check("aur na hi kul jama", total_txt in chal, False)
check("par maal ka naam wahin hai", line_desc and line_desc in chal)
check("aur ginti bhi wahin hai", line_qty in chal)
check("challan pe category bhi chhapti hai", "EPE Foam Sheet" in chal)

# Rang bhi kaagaz pe jaana chahiye — party ke aadmi ko dikhna chahiye ki black
# aaya hai ya white. Rang line ka apna hai, isliye bill ki line pe likha jaata
# hai (category ki tarah item master se nahi aata).
with app.app_context():
    inv_now = db.session.get(Invoice, old_inv_id)
    inv_now.items[0].colour = "black"
    db.session.commit()
chal2 = ok("rang wala challan",
           client.get(f"/invoices/{old_inv_id}/preview?mode=challan")).data.decode("utf8", "ignore")
check("challan pe rang chhapta hai", "black" in chal2)
full2 = ok("rang wala bill",
           client.get(f"/invoices/{old_inv_id}/preview")).data.decode("utf8", "ignore")
check("bill pe bhi rang chhapta hai", "black" in full2)
check("category aur rang ek hi lakeer me", "EPE Foam Sheet · black" in full2)
with app.app_context():
    inv_now = db.session.get(Invoice, old_inv_id)
    inv_now.items[0].colour = ""
    db.session.commit()

r = ok("poora bill abhi bhi rate ke saath", client.get(f"/invoices/{old_inv_id}/preview"))
full = r.data.decode("utf8", "ignore")
check("poore bill me kul jama hai", total_txt in full)
check("bill pe bhi category chhapti hai", "EPE Foam Sheet" in full)

# Category na lagi ho toh us jagah kuch nahi chhapna chahiye — khaali line
# kaagaz pe sirf gandagi hai.
with app.app_context():
    inv_now = db.session.get(Invoice, old_inv_id)
    for _li in inv_now.items:
        if _li.item:
            _li.item.category_id = None
    db.session.commit()
bare = ok("bina category wala bill",
          client.get(f"/invoices/{old_inv_id}/preview")).data.decode("utf8", "ignore")
check("category na ho toh khaali khaana nahi banta", 'class="pinv-cat"' in bare, False)
with app.app_context():
    inv_now = db.session.get(Invoice, old_inv_id)
    inv_now.items[0].item.category_id = _c_id
    db.session.commit()

# Sabse zaroori: chhapne ke tareeke se bill ka record nahi badalna chahiye
with app.app_context():
    inv_after = db.session.get(Invoice, old_inv_id)
    check("bill ka apna hide_pricing waisa hi hai", inv_after.hide_pricing, was_hidden)
    check("bill ka total bhi nahi hila", "%.2f" % inv_after.grand_total, total_txt)
    check("lines ke rate bhi jyon ke tyon",
          sorted({li.rate for li in inv_after.items}), rates)

r = ok("challan ka PDF", client.get(f"/invoices/{old_inv_id}/pdf?mode=challan"))
check("wo bhi asli PDF hai", r.data[:5], b"%PDF-")
check("uske naam me challan likha hai",
      "challan" in r.headers.get("Content-Disposition", ""))

r = ok("challan wala print page", client.get(f"/invoices/{old_inv_id}/print?mode=challan"))
pp = r.data.decode("utf8", "ignore")
check("print page pe bhi rate nahi", total_txt in pp, False)

r = ok("bina mode ke print page", client.get(f"/invoices/{old_inv_id}/print"))
check("aam print page pe rate hai", total_txt in r.data.decode("utf8", "ignore"))

r = ok("overlay me challan ka chunav hai", client.get("/invoices"))
ov = r.data.decode("utf8", "ignore")
check("overlay me 'kya chhapna hai' wala khaana", 'id="billMode"' in ov)

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

# ------------------------------------------ ek naam se do party nahi banti
# Ye Julius ne pakda: "ek naam se do party kaise ban sakti hai". Ban jaati thi —
# aur uska natija sabse mehnga hota hai: udhaari, rate aur products do jagah
# bat jaate hain, aur pata mahine baad chalta hai.
r = ok("nayi party", client.post("/customers", data={
    "name": "SARTHAK", "phone": "9000000001", "city": "Aligarh"},
    follow_redirects=True))
with app.app_context():
    check("party ban gayi", Customer.query.filter_by(name="SARTHAK").count(), 1)
    check("sheher bhi save hua",
          Customer.query.filter_by(name="SARTHAK").first().city, "Aligarh")

r = ok("wahi naam dobara", client.post("/customers", data={"name": "SARTHAK"},
                                       follow_redirects=True))
with app.app_context():
    check("doosri party nahi bani", Customer.query.filter_by(name="SARTHAK").count(), 1)
check("screen pe wajah likhi hai", "already on the list" in r.data.decode("utf8", "ignore"))

# Chhote-bade akshar aur beech ka extra space koi farq nahi karte
ok("chhote akshar aur extra space", client.post("/customers", data={
    "name": "  sarthak  "}, follow_redirects=True))
with app.app_context():
    check("wo bhi nahi bani",
          len([c for c in Customer.query.all()
               if " ".join(c.name.split()).lower() == "sarthak"]), 1)

# Sach me do alag party hon toh naam me farq karke ban jaati hai
ok("naam me farq", client.post("/customers", data={
    "name": "SARTHAK (Hathras)", "city": "Hathras"}, follow_redirects=True))
with app.app_context():
    check("alag naam wali ban gayi",
          Customer.query.filter_by(name="SARTHAK (Hathras)").count(), 1)
    sar_id = Customer.query.filter_by(name="SARTHAK").first().id
    other_id = Customer.query.filter_by(name="SARTHAK (Hathras)").first().id

# Edit karke bhi doosri party ka naam nahi le sakte
r = ok("edit se naam takrana", client.post(f"/customers/{other_id}/edit", data={
    "name": "sarthak", "credit_days": "30", "opening_balance": "0"},
    follow_redirects=True))
with app.app_context():
    check("edit se bhi naam nahi badla",
          db.session.get(Customer, other_id).name, "SARTHAK (Hathras)")

# ---- jagah (GPS) — driver ke liye
ok("jagah save", client.post(f"/customers/{sar_id}/edit", data={
    "name": "SARTHAK", "city": "Aligarh", "credit_days": "30",
    "opening_balance": "0", "map_link": "27.897,78.088"},
    follow_redirects=True))
with app.app_context():
    sar = db.session.get(Customer, sar_id)
    check("lat,long save hua", sar.map_link, "27.897,78.088")
    check("usse map ka link banta hai",
          sar.map_url, "https://www.google.com/maps/search/?api=1&query=27.897,78.088")

    # Google Maps ka apna link waise ka waisa chalta hai
    sar.map_link = "https://maps.app.goo.gl/abc123"
    db.session.commit()
    check("maps ka link waise ka waisa", sar.map_url, "https://maps.app.goo.gl/abc123")

    # Aur jo link jaisa hai hi nahi, wo click karne layak nahi banta —
    # kisi bhi likhe hue text ko link bana dena khatarnak hai.
    for bad in ("javascript:alert(1)", "http://kahin-aur.com", "kuch bhi likha",
                "999,999", ""):
        sar.map_link = bad
        db.session.commit()
        check(f"anjaan text link nahi banta ({bad or 'khaali'})", sar.map_url, "")
    sar.map_link = "27.897,78.088"
    db.session.commit()

page = ok("party ki list", client.get("/customers")).data.decode("utf8", "ignore")
check("list me sheher ka khaana hai", ">Aligarh<" in page)
check("list me map ka button hai", "maps/search/" in page and "27.897,78.088" in page)
check("sheher se search bhi chalta hai",
      b"SARTHAK" in ok("sheher se dhoondho", client.get("/customers?q=aligarh")).data)

# ============================================================ Hissa 1: dikhna aur hifazat

# ------------------------------------------------- challan ka halat sach bole
# Dashboard pe challan "₹0.00 · Unpaid" dikhta tha. Dono jhooth hain: rate
# abhi tay hi nahi hua, isliye na koi raqam hai aur na kisi ne paisa maanga
# hai. Ab wahan lakeer aur "rate baaki" dikhta hai.
with app.app_context():
    ch = Invoice.query.filter_by(hide_pricing=True, consolidated_into_id=None).first()
    if ch is None:
        _c0 = Customer.query.first()
        ch = Invoice(invoice_no="CH-UI-1", date="2026-01-05", customer_id=_c0.id,
                     subtotal=0, grand_total=0, payment_status="unpaid",
                     hide_pricing=True)
        db.session.add(ch)
        db.session.commit()
    ch_no = ch.invoice_no

home = ok("dashboard", client.get("/")).data.decode("utf8", "ignore")
check("dashboard pe challan ke saath ₹0.00 nahi likha",
      ("₹0.00" in home and "amt-none" not in home), False)
check("dashboard pe 'rate baaki' wala nishaan hai", "badge-info" in home)
check("dashboard pe raqam ki jagah lakeer hai", "amt-none" in home)

invl = ok("bill ki list", client.get("/invoices")).data.decode("utf8", "ignore")
check("list pe bhi wahi nishaan", "badge-info" in invl)
check("challan ka number list me hai", ch_no in invl)

# Bill jispe rate hai, uska halat waisa hi rehna chahiye.
with app.app_context():
    paid_inv = Invoice.query.filter_by(hide_pricing=False).first()
    if paid_inv is not None:
        paid_inv.payment_status = "paid"
        db.session.commit()
invl2 = client.get("/invoices").data.decode("utf8", "ignore")
check("bhugtan hua bill hara hi rehta hai", "badge-ok" in invl2)

# Order ka nishaan bhi ek hi jagah se banta hai.
po_pg = ok("order ki list", client.get("/po/")).data.decode("utf8", "ignore")
check("order ki list pe naya nishaan", "badge-" in po_pg)

# Rang ke bina bhi halat padha jaana chahiye — har nishaan me shabd hai.
css = open(os.path.join(os.path.dirname(__file__), "static", "style.css")).read()
for tone in ("badge-ok", "badge-warn", "badge-bad", "badge-info",
             "badge-blue", "badge-teal", "badge-muted", "amt-none"):
    check(f"{tone} ka rang CSS me hai", "." + tone in css)

# ------------------------------------------------------- chhoti screen ka menu
# Phone pe 8 tab ek line me nahi aate. Ab ek button hai; nav uske neeche
# khulta hai. JS band ho toh nav khula hi rehna chahiye — warna phone pe
# software se bahar nikalne ka raasta hi nahi bachta.
check("menu ka button hai", 'id="navToggle"' in home)
check("button batata hai wo kis cheez ko kholta hai", 'aria-controls="mainnav"' in home)
check("shuru me band dikhta hai", 'aria-expanded="false"' in home)
check("nav ko naam mila", 'id="mainnav"' in home)
check("abhi ki screen par nishaan lagta hai", 'aria-current="page"' in home)
check("CSS me menu ka button chhupa hai (bade screen pe)", ".nav-toggle{display:none" in css)
check("JS chale tabhi nav band hota hai", ".topbar.nav-js .tabs{display:none;}" in css)
check("chhoti screen pe button dikhta hai", ".nav-toggle{display:inline-flex;}" in css)
check("keyboard wale ko focus dikhta hai", "focus-visible" in css)

# ------------------------------------------------------------- hifazat ke sar
r = client.get("/")
check("file ka type browser khud nahi taadta",
      r.headers.get("X-Content-Type-Options"), "nosniff")
check("bahar ki site hamara page apne andar nahi khol sakti",
      r.headers.get("X-Frame-Options"), "SAMEORIGIN")
check("pata bahar nahi jaata", r.headers.get("Referrer-Policy"), "same-origin")
csp = r.headers.get("Content-Security-Policy", "")
check("bahar se script nahi aa sakti", "script-src 'self' 'unsafe-inline'" in csp)
check("form kisi aur site pe nahi ja sakta", "form-action 'self'" in csp)
check("apna bill preview phir bhi khulta hai", "frame-ancestors 'self'" in csp)
check("font Google se aata hai isliye wo khula hai",
      "https://fonts.gstatic.com" in csp)
# Bill ka preview iframe me khulta hai — DENY laga diya toh wo khaali dikhega.
check("X-Frame-Options DENY nahi hai", r.headers.get("X-Frame-Options") != "DENY")

# "Ab se hamesha https" wala sar — Railway pe taala uske darwaze pe khulta hai
# aur hamare tak sada http aata hai, isliye Flask ko https dikhta hi nahi.
# Sach X-Forwarded-Proto me likha hota hai.
check("sade http pe HSTS nahi jaata",
      client.get("/").headers.get("Strict-Transport-Security"), None)
r_https = client.get("/", headers={"X-Forwarded-Proto": "https"})
check("asli server (https ke peeche) pe HSTS jaata hai",
      "max-age=31536000" in (r_https.headers.get("Strict-Transport-Security") or ""))
# Kayi proxy ho toh sar me list aati hai — pehla wala asli hota hai.
r_list = client.get("/", headers={"X-Forwarded-Proto": "https, http"})
check("proxy ki list me pehla wala padha jaata hai",
      "max-age=31536000" in (r_list.headers.get("Strict-Transport-Security") or ""))
check("jhoothe http pe HSTS nahi",
      client.get("/", headers={"X-Forwarded-Proto": "http"})
            .headers.get("Strict-Transport-Security"), None)

# Login ki parchi ke taale
check("parchi JS se nahi padhi ja sakti", app.config["SESSION_COOKIE_HTTPONLY"], True)
check("bahar ke form hamare naam pe kaam nahi karte",
      app.config["SESSION_COOKIE_SAMESITE"], "Lax")
# Laptop/test pe sqlite hai, jahan https hota hi nahi — wahan Secure band
# rehna chahiye warna local pe login hi na ho.
check("sqlite pe Secure band (warna local login toot jaata)",
      app.config["SESSION_COOKIE_SECURE"], False)
import app as _appmod2                                       # noqa: E402
src = open(os.path.join(os.path.dirname(__file__), "app.py")).read()
check("asli server (postgres) pe Secure chalu ho jaata hai",
      'app.config["SESSION_COOKIE_SECURE"] = not db_url.startswith("sqlite")' in src)

# --------------------------------------------------------- login ka form theek
lg = ok("login ka page", client.get("/login")).data.decode("utf8", "ignore")
check("password manager ko username samajh aata hai", 'autocomplete="username"' in lg)
check("password manager ko password samajh aata hai",
      'autocomplete="current-password"' in lg)
check("label input se juda hua hai", 'for="username"' in lg and 'id="username"' in lg)
check("naam ka pehla akshar apne aap bada nahi hota",
      'autocapitalize="none"' in lg)

# ---------------------------------------------- purani screens abhi bhi chalti hain
for path in ("/", "/invoices", "/customers", "/items", "/accounts"):
    ok(f"GET {path} (regression)", client.get(path))

# ------------------------------------------- page ka JS toota hua na jaye
# Screen ke andar ka JS Jinja se ban ke nikalta hai, isliye ek galat quote ya
# adhoora bracket sirf live pe pakda jaata tha — page chup-chaap aadha kaam
# karta. Ab har page ka JS yahin parse karke dekh lete hain. Node na ho toh
# ye jaanch chhod di jaati hai; baaki test phir bhi chalte hain.
import shutil as _sh                                       # noqa: E402
import subprocess as _sp                                   # noqa: E402
import tempfile as _tf                                     # noqa: E402

if _sh.which("node"):
    for path in ("/po/new", "/invoices", f"/po/{po_id}", "/po/bills", "/po/telegram"):
        html = client.get(path).data.decode("utf8", "ignore")
        js = "\n;\n".join(_re2.findall(r"<script>(.*?)</script>", html, _re2.S))
        if not js.strip():
            continue
        with _tf.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            tmp = fh.name
        res = _sp.run(["node", "--check", tmp], capture_output=True)
        check(f"{path} ka JS parse hota hai", res.returncode, 0)
        if res.returncode:
            print(res.stderr.decode("utf8", "ignore")[:400])
        os.unlink(tmp)

if fails:
    print(f"FAILED {len(fails)} check(s):\n")
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("PO flow: all checks passed")
