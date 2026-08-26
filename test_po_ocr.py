"""Photo/PDF se form bharne ka test.

Do baatein asli hain:
  1. OCR akela galat padhta hai — `GME01` ko `GMEO1`. Ye test wahi galti
     jaan-boojh ke banata hai aur dekhta hai ki party ke code se milane ke baad
     sahi nikalti hai ya nahi.
  2. Jab OCR ka saamaan hi na ho, screen ko saaf mana karna chahiye — chup
     nahi rehna chahiye.

    python3 test_po_ocr.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_ocr.db")
os.environ["DATABASE_URL"] = "sqlite:///" + DB
if os.path.exists(DB):
    os.remove(DB)

from app import app                                    # noqa: E402
from models import db, Customer                        # noqa: E402
import ocr_read                                        # noqa: E402
import po_module as M                                  # noqa: E402

fails = []


def check(label, got, want=True):
    if got != want:
        fails.append(f"{label}\n    got:  {got!r}\n    want: {want!r}")


# ---------------------------------------------- galat padhe code ka sudhaar
KNOWN = ["GME01", "GME02", "GME03", "GME04", "GME05"]
check("seedha code",            M.nearest_known_code("GME01", KNOWN), "GME01")
check("chhota-bada chalega",    M.nearest_known_code("gme02", KNOWN), "GME02")
check("space aur dash chalega", M.nearest_known_code("GME - 03", KNOWN), "GME03")
# OCR ki asli galtiyan
check("O ki jagah 0",           M.nearest_known_code("GMEO1", KNOWN), "GME01")
check("D ki jagah 0",           M.nearest_known_code("GMED2", KNOWN), "GME02")
check("D ki jagah 0 (doosra)",  M.nearest_known_code("GMED4", KNOWN), "GME04")
check("l ki jagah 1",           M.nearest_known_code("GME0l", KNOWN), "GME01")
# Jo aisi galti ho jise sudhaarne ka koi bharosemand tareeka na ho, use chhod
# dena hi theek hai — form me aadmi khud bhar lega
check("bahut door ki galti nahi sudhaarte",
      M.nearest_known_code("GMEI1", KNOWN), None)
# Jo bilkul door hai wo nahi chipakna chahiye
check("anjaan token nahi chipakta", M.nearest_known_code("XYZ99", KNOWN), None)
check("khaali list pe kuch nahi",   M.nearest_known_code("GME01", []), None)
check("khaali token pe kuch nahi",  M.nearest_known_code("", KNOWN), None)

# ---------------------------------------------- fuzzy sirf photo ke liye
ocr_line = "1.GMEO1 34x15x5 500 pos"
check("aadmi ke likhe me chhoot nahi",
      M.parse_po_text(ocr_line, "cm", KNOWN)[0]["item_code"], "")
check("photo se aaye me chhoot hai",
      M.parse_po_text(ocr_line, "cm", KNOWN, fuzzy=True)[0]["item_code"], "GME01")


def po_image():
    """Ek chhapa hua sa PO, jaisa phone se aata hai."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1000, 700), (252, 251, 247))
    d = ImageDraw.Draw(img)
    for y, text in [(40, "GM ENTERPREISES"), (90, "PURCHASE ORDER  PO-77"),
                    (170, "1.  GME01   34x15x5    500 pcs"),
                    (220, "2.  GME02   23x14x4    300 pcs"),
                    (270, "3.  GME04   26x15x6     40 roll"),
                    (350, "Delivery by 30 Aug"), (390, "Thanks")]:
        d.text((60, y), text, fill=(20, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------- poora raasta: file -> lines
client = app.test_client()
client.post("/setup", data={"firm_name": "T", "name": "R",
                            "username": "owner", "password": "secret123"},
            follow_redirects=True)
client.post("/login", data={"username": "owner", "password": "secret123"},
            follow_redirects=True)

with app.app_context():
    cust = Customer(name="GM ENTERPREISES")
    db.session.add(cust)
    db.session.commit()
    cid = cust.id
    for code, size in [("GME01", "34x15x5"), ("GME02", "23x14x4"),
                       ("GME04", "26x15x6")]:
        m = M.PartyProductMap(customer_id=cid, item_code=code, label=f"{code} ({size})",
                              raw_size_text=size,
                              canonical_key=M.canonical_size(
                                  M.size_dims(M.SIZE_RE.search(size)), "cm"))
        db.session.add(m)
        M.ensure_item_for(m)
    db.session.commit()

check("is machine pe OCR hai", ocr_read.available())

r = client.post("/po/read-file", data={
    "customer_id": str(cid), "size_unit": "cm",
    "file": (io.BytesIO(po_image()), "po.png"),
}, content_type="multipart/form-data")
check("file padh li -> HTTP", r.status_code, 200)
data = r.get_json()
check("padhna kaamyaab raha", data.get("ok"), True)
codes = [row["code"] for row in data.get("rows", [])]
check("teeno code sahi nikle (OCR ne galat padhe the)",
      codes, ["GME01", "GME02", "GME04"])
qtys = [row["qty"] for row in data["rows"]]
check("qty bhi aayi", qtys, [500.0, 300.0, 40.0])
# OCR ne yahan "roll" ko "rll" padha tha — unit bhi usi tarah list se mili
check("aakhri line ki unit roll hai", data["rows"][2]["unit"], "roll")

# Unit ka sudhaar bhi sirf photo ke liye
check("aadmi ke likhe me unit ka andaza nahi",
      M.parse_po_text("GME04 26x15x6 40 rll", "cm", KNOWN)[0]["qty_unit"], "pcs")

# bina party ke bhi girna nahi chahiye — bas code na mile
r = client.post("/po/read-file", data={
    "size_unit": "cm", "file": (io.BytesIO(po_image()), "po.png"),
}, content_type="multipart/form-data")
check("bina party ke bhi jawab aata hai", r.status_code, 200)

# bina file ke
r = client.post("/po/read-file", data={"customer_id": str(cid)},
                content_type="multipart/form-data")
check("bina file ke mana kar deta hai", r.status_code, 400)
check("aur wajah bhi batata hai", bool(r.get_json().get("error")))

# PDF jisme text pehle se hai — yahan OCR ki zaroorat hi nahi
try:
    from reportlab.pdfgen import canvas as _c
    buf = io.BytesIO()
    c = _c.Canvas(buf)
    for i, line in enumerate(["GME01 34x15x5 500 pcs", "GME02 23x14x4 300 pcs"]):
        c.drawString(60, 760 - i * 24, line)
    c.showPage(); c.save()
    text = ocr_read.read_text(buf.getvalue(), "application/pdf", "po.pdf")
    check("PDF ka text layer seedha padha gaya", "GME01" in text)
except ImportError:
    pass

# ---------------------------------------------- paste bhi usi jagah pahunchta hai
r = client.post("/po/read-text", data={
    "customer_id": str(cid), "size_unit": "cm",
    "text": "GME02 - 250 pcs\nGME04 26x15x6 - 12 roll\nThanks",
})
rows = r.get_json()["rows"]
check("paste se do line bani", len(rows), 2)
check("paste me code waisa hi rehta hai", [x["code"] for x in rows], ["GME02", "GME04"])
check("code se size bhar gaya", rows[0]["size"], "23x14x4")

# ---------------------------------------------- OCR band ho toh saaf mana karo
real = ocr_read.have_tool
ocr_read.have_tool = lambda name: False
try:
    r = client.post("/po/read-file", data={
        "customer_id": str(cid), "size_unit": "cm",
        "file": (io.BytesIO(po_image()), "po.png"),
    }, content_type="multipart/form-data")
    check("OCR na ho toh 503", r.status_code, 503)
    check("aur screen ko wajah milti hai",
          "not switched on" in (r.get_json().get("error") or ""))
finally:
    ocr_read.have_tool = real

if fails:
    print(f"FAILED {len(fails)} check(s):\n")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("ocr: all checks passed")
