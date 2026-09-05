"""Backup ka poora chakkar: banao -> wapas laao -> milaao.

Ek backup jo kabhi wapas laake dekha hi nahi gaya, wo backup nahi — bas ek
file hai. Ye test wahi karta hai jo asli museebat me karna padega:

  1. Ek bhara hua database banao — party, maal, bill, product ki photo,
     rate, Telegram ke group, order.
  2. Uska backup utaaro.
  3. Ek BILKUL KHAALI doosra database banao.
  4. Usme backup wapas laao.
  5. Dono ko milaao — ginti, rupaya, aur sabse zaroori: photo ke bytes.

Do database ek hi process me nahi khul sakte, isliye har kadam apne alag
process me chalta hai (jaise production ki rehearsal me hota hai).

    python3 test_backup.py
"""
import json
import os
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_FULL = os.path.join(HERE, "test_backup_full.db")
DB_EMPTY = os.path.join(HERE, "test_backup_empty.db")
ZIP_PATH = os.path.join(HERE, "test_backup_out.zip")

fails = []


def check(label, got, want=True):
    if got != want:
        fails.append(f"{label}\n    got:  {got!r}\n    want: {want!r}")


def run(script, db_path):
    """Ek chhota script us database pe chalao, aur uska output lauta do."""
    env = dict(os.environ, DATABASE_URL="sqlite:///" + db_path, PYTHONPATH=HERE)
    for k in ("TELEGRAM_BOT_TOKEN", "GOOGLE_SERVICE_ACCOUNT_JSON"):
        env.pop(k, None)
    r = subprocess.run([sys.executable, "-c", script], cwd=HERE, env=env,
                       capture_output=True, text=True)
    if r.returncode:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])
        raise SystemExit(f"step failed on {os.path.basename(db_path)}")
    return r.stdout.strip()


for p in (DB_FULL, DB_EMPTY, ZIP_PATH):
    if os.path.exists(p):
        os.remove(p)


# --------------------------------------------------------- 1. bhara hua database
SEED = '''
from app import app
from models import db, Customer, Item, Invoice, InvoiceItem, Category, Settings, User
from werkzeug.security import generate_password_hash
import po_module as M
import json

with app.app_context():
    db.create_all()
    s = Settings.get(); s.firm_name = "TEST PACKAGING"
    db.session.add(User(username="owner", name="Rahul", role="owner",
                        password_hash=generate_password_hash("secret123")))
    # App khud shuru hote hi kuch category bo deta hai — usi ko uthao,
    # warna "Foam" do baar banane ki koshish hoti hai.
    cat = Category.query.filter_by(name="Foam").first()
    if not cat:
        cat = Category(name="Foam", units="pcs, roll")
        db.session.add(cat)
    cat.colours = "black, white"
    db.session.flush()

    c1 = Customer(name="GB METALS", city="Aligarh")
    c2 = Customer(name="SR INDUSTRIES", city="Hathras")
    db.session.add_all([c1, c2]); db.session.flush()

    it = Item(name="Foam box 23x14x5", unit="pcs", category_id=cat.id,
              sale_price=40, current_stock=12, track_stock=True)
    db.session.add(it); db.session.flush()

    # Do product mapping, dono pe apni photo — yahi cheez purane backup me
    # kabhi nahi jaati thi.
    PHOTO_A = bytes(range(256)) * 40          # 10 KB, pehchaanne layak
    PHOTO_B = bytes(reversed(range(256))) * 25
    THUMB_A = b"\\xff\\xd8thumbA" + b"\\x00" * 100

    m1 = M.PartyProductMap(customer_id=c1.id, item_code="GB01",
                           canonical_key="50.0x140.0x230.0", label="GB01 (23x14x5)",
                           raw_size_text="23x14x5", item_id=it.id,
                           image_data=PHOTO_A, image_mime="image/jpeg",
                           image_thumb=THUMB_A, drive_file_id="drv-aaa")
    m2 = M.PartyProductMap(customer_id=c2.id, item_code="SR07",
                           canonical_key="50.0x140.0x230.0", label="SR07 (23x14x5)",
                           raw_size_text="23x14x5", item_id=it.id,
                           image_data=PHOTO_B, image_mime="image/jpeg")
    db.session.add_all([m1, m2]); db.session.flush()

    # Ek hi maal, do party, alag rate — brief wali baat
    db.session.add_all([
        M.PartyRate(customer_id=c1.id, map_id=m1.id, item_code="GB01",
                    qty_unit="pcs", rate=41.5),
        M.PartyRate(customer_id=c2.id, map_id=m2.id, item_code="SR07",
                    qty_unit="pcs", rate=38.0),
        M.PartyRate(customer_id=c1.id, map_id=m1.id, item_code="GB01",
                    qty_unit="roll", rate=900.0),
    ])

    db.session.add_all([
        M.TelegramChat(chat_id="-1001", title="Foam group", roles="operator",
                       categories=str(cat.id)),
        M.TelegramChat(chat_id="777", title="Julius", roles="owner"),
        M.TelegramPerson(tg_user_id="9001", name="Lokesh"),
        M.PartyPOConfig(customer_id=c1.id, size_unit="cm"),
        M.PartyFolder(customer_id=c1.id, folder_id="fold-1", folder_name="GB METALS"),
    ])
    M.POSetting.put("drive_root_folder", "root-abc")
    M.POSetting.put("telegram_webhook_secret", "TOP-SECRET-VALUE")

    inv = Invoice(invoice_no="INV-0001", date="2026-09-01", customer_id=c1.id,
                  subtotal=4150, grand_total=4150, payment_status="unpaid")
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(invoice_id=inv.id, item_id=it.id,
                               description="GB01 — Foam box", qty=100, unit="pcs",
                               rate=41.5, taxable_amount=4150, line_total=4150,
                               colour="black"))

    po = M.PurchaseOrder(po_number="SEP01", customer_id=c1.id, po_date="2026-09-01",
                         status="pending", scan_data=b"SCAN-BYTES-HERE" * 20,
                         scan_mime="image/jpeg", scan_name="po.jpg")
    db.session.add(po); db.session.flush()
    db.session.add(M.POLine(po_id=po.id, line_no=1, item_code="GB01",
                            canonical_key="50.0x140.0x230.0", qty=100,
                            qty_unit="pcs", colour="black", map_id=m1.id, rate=41.5))
    db.session.add(M.POPart(po_id=po.id, chat_id="-1001", label="Foam group",
                            status="pending", line_ids="1"))
    db.session.commit()

    print(json.dumps({
        "photo_a": len(PHOTO_A), "photo_b": len(PHOTO_B), "thumb_a": len(THUMB_A),
    }))
'''
seeded = json.loads(run(SEED, DB_FULL))
print("1. bhara hua database ban gaya")


# -------------------------------------------------------------- 2. backup utaaro
EXPORT = '''
from app import app
with app.app_context():
    with app.test_client() as c:
        page = c.get("/login").data.decode("utf8", "ignore")
        import re
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        c.post("/login", data={"username": "owner", "password": "secret123",
                               "_csrf": tok}, follow_redirects=True)
        r = c.get("/settings/backup")
        assert r.status_code == 200, r.status_code
        open(%r, "wb").write(r.data)
        print(r.headers.get("Content-Disposition", ""))
''' % ZIP_PATH
disp = run(EXPORT, DB_FULL)
check("backup ZIP ban gaya", os.path.exists(ZIP_PATH))
check("uska naam .zip pe khatam hota hai", ".zip" in disp)

z = zipfile.ZipFile(ZIP_PATH)
names = z.namelist()
data = json.loads(z.read("backup.json").decode("utf8"))

check("PADHO.txt andar hai", "PADHO.txt" in names)
check("backup.json andar hai", "backup.json" in names)

# ------------------------------------------------- order ka poora hissa aaya kya
for tbl in ("party_product_maps", "party_rates", "telegram_chats", "telegram_people",
            "purchase_orders", "po_lines", "po_parts", "party_folders",
            "party_po_configs", "po_settings"):
    check(f"{tbl} backup me hai", tbl in data["tables"])

# Agar koi table hai hi nahi toh aage ki har jaanch us par latak jayegi aur
# asli wajah output me kahin dikhegi nahi.
if fails:
    print(f"FAILED {len(fails)} check(s):\n")
    for f in fails:
        print(" - " + f)
    sys.exit(1)

check("dono product mapping aaye", len(data["tables"]["party_product_maps"]), 2)
check("teeno rate aaye", len(data["tables"]["party_rates"]), 3)
check("dono Telegram group aaye", len(data["tables"]["telegram_chats"]), 2)
check("order aur uski line aayi",
      (len(data["tables"]["purchase_orders"]), len(data["tables"]["po_lines"])), (1, 1))

# ----------------------------------------------------------- photos sach me aayi
photo_files = [n for n in names if n.startswith("photos/")]
scan_files = [n for n in names if n.startswith("scans/")]
check("photo alag file banke aayi (3: do photo + ek thumb)", len(photo_files), 3)
check("order ki parchi bhi aayi", len(scan_files), 1)

# Aur bytes waise ke waise hain — yahi asli sawaal hai
sizes = sorted(len(z.read(n)) for n in photo_files)
check("photo ke bytes poore hain",
      sizes, sorted([seeded["photo_a"], seeded["photo_b"], seeded["thumb_a"]]))

# JSON me bytes ki jagah file ka naam hona chahiye, "b'\\xff...'" nahi
one = data["tables"]["party_product_maps"][0]
check("JSON me photo ki jagah file ka pata hai", isinstance(one["image_data"], dict))
check("aur usme file ka naam bhi", "file" in one["image_data"])
raw = z.read("backup.json").decode("utf8")
check("bytes galti se text banke nahi aaye", "\\\\xff\\\\xd8" in raw, False)

# ------------------------------------------------------------ taale bahar rahein
keys = [r["key"] for r in data["tables"]["po_settings"]]
check("Drive ka folder backup me hai", "drive_root_folder" in keys)
check("Telegram ka secret backup me NAHI hai", "telegram_webhook_secret" in keys, False)
check("TOP-SECRET wali value kahin nahi hai", "TOP-SECRET-VALUE" in raw, False)
check("password ka nishaan bhi nahi", "password_hash" in raw, False)
readme = z.read("PADHO.txt").decode("utf8")
check("PADHO.txt batata hai ki secret chhoda gaya", "telegram_webhook_secret" in readme)
check("aur ye ki password dobara banane honge", "password" in readme.lower())

print("2. backup me sab kuch aa gaya (photos samet)")


# --------------------------------------------- 3. khaali database me wapas laao
INIT_EMPTY = '''
from app import app
from models import db, Settings, User
from werkzeug.security import generate_password_hash
with app.app_context():
    db.create_all()
    # Ek maalik chahiye taaki login ho sake — asli museebat me bhi pehla kaam
    # yahi hota hai. Baaki database bilkul khaali hai.
    db.session.add(User(username="owner", name="Rahul", role="owner",
                        password_hash=generate_password_hash("secret123")))
    db.session.commit()
    print("ready")
'''
run(INIT_EMPTY, DB_EMPTY)

RESTORE = '''
from app import app
from models import db
import re
with app.app_context():
    with app.test_client() as c:
        page = c.get("/login").data.decode("utf8", "ignore")
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        c.post("/login", data={"username": "owner", "password": "secret123",
                               "_csrf": tok}, follow_redirects=True)
        blob = open(%r, "rb").read()
        import io
        # Pehle sirf jaanch — isse kuch badalna nahi chahiye
        r = c.post("/settings/restore", data={
            "mode": "check", "_csrf": tok,
            "backup": (io.BytesIO(blob), "backup.zip")},
            content_type="multipart/form-data", follow_redirects=True)
        from models import Customer
        print("after_check_customers", Customer.query.count())
        # Ab sach me wapas laao
        r = c.post("/settings/restore", data={
            "mode": "restore", "_csrf": tok,
            "backup": (io.BytesIO(blob), "backup.zip")},
            content_type="multipart/form-data", follow_redirects=True)
        print("restore_status", r.status_code)
''' % ZIP_PATH
out = run(RESTORE, DB_EMPTY)
check("sirf jaanch se kuch nahi badla", "after_check_customers 0" in out)
check("wapas laane ka page khula", "restore_status 200" in out)


# ------------------------------------------------------------------ 4. milaao
COMPARE = '''
from app import app
from models import db, Customer, Item, Invoice, InvoiceItem, Category
import po_module as M
import json
with app.app_context():
    maps = M.PartyProductMap.query.order_by(M.PartyProductMap.id).all()
    out = {
        "customers": Customer.query.count(),
        "items": Item.query.count(),
        "categories": Category.query.count(),
        "invoices": Invoice.query.count(),
        "invoice_items": InvoiceItem.query.count(),
        "maps": len(maps),
        "rates": M.PartyRate.query.count(),
        "chats": M.TelegramChat.query.count(),
        "people": M.TelegramPerson.query.count(),
        "orders": M.PurchaseOrder.query.count(),
        "po_lines": M.POLine.query.count(),
        "po_parts": M.POPart.query.count(),
        "folders": M.PartyFolder.query.count(),
        "photo_sizes": sorted(len(m.image_data or b"") for m in maps),
        "thumb_sizes": sorted(len(m.image_thumb or b"") for m in maps),
        "map_codes": sorted(m.item_code for m in maps),
        "map_item_ids": sorted(set(m.item_id for m in maps)),
        "map_ids": sorted(m.id for m in maps),
        "rates_by_unit": sorted((r.qty_unit, r.rate) for r in M.PartyRate.query.all()),
        "billed": round(sum(i.grand_total for i in Invoice.query.all()), 2),
        "colour": (InvoiceItem.query.first().colour if InvoiceItem.query.first() else None),
        "scan_len": len(M.PurchaseOrder.query.first().scan_data or b""),
        "chat_cats": sorted(c.categories or "" for c in M.TelegramChat.query.all()),
        "drive_root": M.POSetting.get("drive_root_folder"),
        "secret": M.POSetting.get("telegram_webhook_secret"),
    }
    print(json.dumps(out))
'''
before = json.loads(run(COMPARE, DB_FULL))
after = json.loads(run(COMPARE, DB_EMPTY))

for key in ("customers", "items", "categories", "invoices", "invoice_items",
            "maps", "rates", "chats", "people", "orders", "po_lines",
            "po_parts", "folders", "billed", "colour", "map_codes",
            "map_ids", "rates_by_unit", "chat_cats", "drive_root"):
    check(f"wapas aane ke baad wahi: {key}", after[key], before[key])

# Sabse zaroori: photo ke bytes waise ke waise
check("har photo ke bytes waise ke waise", after["photo_sizes"], before["photo_sizes"])
check("thumb bhi waisa ka waisa", after["thumb_sizes"], before["thumb_sizes"])
check("order ki parchi ke bytes bhi", after["scan_len"], before["scan_len"])
check("mapping apne maal se judi hi rahi", after["map_item_ids"], before["map_item_ids"])

# Taala wapas nahi aana chahiye — wo backup me tha hi nahi
check("Telegram ka secret wapas nahi aaya", after["secret"], "")

print("3. sab kuch wapas aa gaya — photos ke bytes tak")


# ------------------------------- 5. bhare hue database pe restore mana karna chahiye
REFUSE = '''
from app import app
from models import db, Customer
import re, io
with app.app_context():
    n0 = Customer.query.count()
    with app.test_client() as c:
        page = c.get("/login").data.decode("utf8", "ignore")
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        c.post("/login", data={"username": "owner", "password": "secret123",
                               "_csrf": tok}, follow_redirects=True)
        blob = open(%r, "rb").read()
        r = c.post("/settings/restore", data={
            "mode": "restore", "_csrf": tok,
            "backup": (io.BytesIO(blob), "backup.zip")},
            content_type="multipart/form-data", follow_redirects=True)
        body = r.data.decode("utf8", "ignore")
    print("refused", "Refused" in body or "मना" in body)
    print("count_same", Customer.query.count() == n0)
''' % ZIP_PATH
out = run(REFUSE, DB_FULL)
check("bhare hue database pe restore mana karta hai", "refused True" in out)
check("aur usne kuch mitaya nahi", "count_same True" in out)

# Kachra file pe saaf jawab
BADFILE = '''
from app import app
import re, io
with app.app_context():
    with app.test_client() as c:
        page = c.get("/login").data.decode("utf8", "ignore")
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        c.post("/login", data={"username": "owner", "password": "secret123",
                               "_csrf": tok}, follow_redirects=True)
        r = c.post("/settings/restore", data={
            "mode": "check", "_csrf": tok,
            "backup": (io.BytesIO(b"main zip nahi hoon"), "kachra.zip")},
            content_type="multipart/form-data", follow_redirects=True)
        body = r.data.decode("utf8", "ignore")
    print("status", r.status_code)
    print("said_bad", ("could not be read" in body) or ("पढ़ी नहीं" in body))
'''
out = run(BADFILE, DB_FULL)
check("kachra file pe app girti nahi", "status 200" in out)
check("aur saaf batati hai ki file padhi nahi gayi", "said_bad True" in out)

print("4. galat haalat me bhi theek se pesh aata hai")

for p in (DB_FULL, DB_EMPTY, ZIP_PATH):
    if os.path.exists(p):
        os.remove(p)

if fails:
    print(f"\nFAILED {len(fails)} check(s):\n")
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("backup: all checks passed")
