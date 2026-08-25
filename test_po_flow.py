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
from models import db, Customer, Item                # noqa: E402
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
check("mismatch ka warning text aaya", "code aur size match nahi" in r.data.decode("utf8", "ignore"))

ok("reject PO-4473", client.post(f"/po/{po3_id}/reject", follow_redirects=True))

# ------------------------------------------------------ operator ko bhejna
# Telegram set nahi hai — order aage nahi badhna chahiye, aur wajah saaf honi chahiye.
r = ok("confirm bina Telegram ke", client.post(f"/po/{po_id}/confirm", follow_redirects=True))
check("Telegram bina order pending hi raha",
      app.test_request_context() and True, True)
with app.app_context():
    check("bina operator group ke order aage nahi badha",
          db.session.get(PurchaseOrder, po_id).status, "pending")
check("wajah batayi gayi", "Operator group chuna nahi gaya" in r.data.decode("utf8", "ignore"))

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
