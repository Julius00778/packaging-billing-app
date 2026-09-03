"""Drive sync ke tests — asli Drive ke bina.

Drive ki jagah ek nakli service use hoti hai jo wahi shakal lautati hai jo Google
lautata hai, aur jinke naam Julius ke asli SAMBHAV folder se liye gaye hain.
Isse network ke bina bhi ye check ho jaata hai ki filename se code aur size sahi
nikal rahe hain, dobara sync pe photo dobara download nahi hoti, aur ek hi size
ke do code alag alag products rehte hain.

    python3 test_po_drive.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
DB = os.path.join(os.path.dirname(__file__), "test_drive.db")
os.environ["DATABASE_URL"] = "sqlite:///" + DB
if os.path.exists(DB):
    os.remove(DB)

from app import app                                    # noqa: E402
from models import db, Customer                        # noqa: E402
import drive_sync                                      # noqa: E402
import po_module as M                                   # noqa: E402

fails = []


def check(label, got, want=True):
    if got != want:
        fails.append(f"{label}\n    got:  {got!r}\n    want: {want!r}")


def a_photo(color):
    from PIL import Image, ImageDraw
    import random
    img = Image.new("RGB", (2400, 1800), color)
    d = ImageDraw.Draw(img)
    d.rectangle([300, 300, 2100, 1500], fill=(255, 255, 255))
    p = img.load()
    for y in range(0, 1800, 4):
        for x in range(0, 2400, 4):
            v = p[x, y]
            n = random.randint(-8, 8)
            p[x, y] = tuple(max(0, min(255, c + n)) for c in v)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


PHOTO = a_photo((190, 205, 225))

# Naam asli folder se — GM ENTERPREISES aur SAMEER METALS
TREE = {
    "root": [
        {"id": "f_gm", "name": "GM ENTERPREISES", "mimeType": drive_sync.MIME_FOLDER},
        {"id": "f_sm", "name": "SAMEER METALS", "mimeType": drive_sync.MIME_FOLDER},
    ],
    "f_gm": [
        {"id": "g1", "name": "GME01(31x15x5).jpeg", "mimeType": "image/jpeg", "modifiedTime": "t1"},
        {"id": "g2", "name": "GME02(23x14x4).jpeg", "mimeType": "image/jpeg", "modifiedTime": "t1"},
        {"id": "g3", "name": "GME03(23x14x4).jpeg", "mimeType": "image/jpeg", "modifiedTime": "t1"},
        {"id": "g4", "name": "GME 04 (26x15x6).jpeg", "mimeType": "image/jpeg", "modifiedTime": "t1"},
        {"id": "g5", "name": "GME05(32x24x3.5).jpeg", "mimeType": "image/jpeg", "modifiedTime": "t1"},
        {"id": "g6", "name": "rate list.pdf", "mimeType": "application/pdf", "modifiedTime": "t1"},
        {"id": "g7", "name": "random photo.jpeg", "mimeType": "image/jpeg", "modifiedTime": "t1"},
    ],
    "f_sm": [
        {"id": "s1", "name": "SM01(32x24x3.5).jpeg", "mimeType": "image/jpeg", "modifiedTime": "t1"},
        {"id": "s2", "name": "SM02(32x28x4).jpeg", "mimeType": "image/jpeg", "modifiedTime": "t1"},
    ],
}


class FakeDrive:
    """Drive jitna hi kaam karti hai jitna hume chahiye. Downloads ginti hai."""

    def __init__(self):
        self.downloads = []

    def children(self, folder_id, only_folders=False):
        rows = TREE.get(folder_id, [])
        if only_folders:
            rows = [r for r in rows if r["mimeType"] == drive_sync.MIME_FOLDER]
        return rows


FAKE = FakeDrive()
drive_sync._list_children = lambda service, fid, only_folders=False: FAKE.children(fid, only_folders)
drive_sync.download_file = lambda service, fid, max_bytes=None: (
    FAKE.downloads.append(fid) or PHOTO)


# ---------------------------------------------------------------- filename parsing
def parse(name):
    return drive_sync.parse_sample_filename(
        name, M.canonical_size, M.SIZE_RE, M.size_dims, M.normalize_code)


code, dims, raw = parse("GME01(31x15x5).jpeg")
check("code nikla", code, "GME01")
check("teen dimensions nikle", dims, [31.0, 15.0, 5.0])
check("raw size text", raw, "31x15x5")

check("space wala naam", parse("GME 04 (26x15x6).jpeg")[0], "GME04")
check("chhote akshar", parse("sm02(32x28x4).jpeg")[0], "SM02")
check("hyphen wala", parse("SM-03 (12x8x3.5).jpeg")[0], "SM03")
check("decimal size", parse("GME05(32x24x3.5).jpeg")[1], [32.0, 24.0, 3.5])
check("bina code wali file chhodi jaati hai", parse("random photo.jpeg"), None)
check("bina size ke code", parse("SM09.jpeg")[1], [])

check("link se folder id", drive_sync.folder_id_from_link(
    "https://drive.google.com/drive/folders/1MRqefYB?usp=sharing"), "1MRqefYB")
check("sirf id di ho toh wahi", drive_sync.folder_id_from_link("1MRqefYB"), "1MRqefYB")

# ---------------------------------------------------------------------- poora sync
with app.app_context():
    db.create_all()
    db.session.add_all([Customer(name="GM ENTERPREISES"), Customer(name="Sameer Metals Pvt")])
    db.session.commit()

    found, added = M.sync_party_folders(None, "root")
    check("dono party folder mile", len(found), 2)
    check("dono naye the", added, 2)

    gm = M.PartyFolder.query.filter_by(folder_name="GM ENTERPREISES").first()
    sm = M.PartyFolder.query.filter_by(folder_name="SAMEER METALS").first()
    check("naam bilkul mila toh party khud jud gayi", gm.customer.name, "GM ENTERPREISES")
    check("naam alag tha toh nahi juda", sm.customer_id, None)

    # GM sync — unit cm
    gm.size_unit = "cm"
    db.session.commit()
    r = M.sync_one_folder(None, gm)
    check("saat file dekhi (PDF chhod ke)", r["seen"], 6)
    check("paanch product bane", r["added"], 5)
    check("paanch photo aayi", r["photos"], 5)
    check("bina code wali file chhodi gayi", len(r["skipped"]), 1)

    maps = M.PartyProductMap.query.filter_by(customer_id=gm.customer_id).all()
    check("DB me paanch product", len(maps), 5)
    codes = sorted(m.item_code for m in maps)
    check("saare code", codes, ["GME01", "GME02", "GME03", "GME04", "GME05"])

    one = M.map_by_code(gm.customer_id, "GME01")
    check("31x15x5 cm ka key", one.canonical_key, "50.0x150.0x310.0")
    check("photo compress hoke aayi", len(one.image_data) <= 160 * 1024, True)
    check("thumbnail bana", bool(one.image_thumb), True)
    check("thumb chhota hai", len(one.image_thumb) <= 20 * 1024, True)
    check("label khud bana", one.label, "GME01 (31x15x5)")

    # Ek hi size ke do code alag rehne chahiye
    two, three = M.map_by_code(gm.customer_id, "GME02"), M.map_by_code(gm.customer_id, "GME03")
    check("GME02 aur GME03 ka size ek hi", two.canonical_key, three.canonical_key)
    check("par products do alag hain", two.id != three.id, True)
    cands = M.candidates_for(gm.customer_id, two.canonical_key)
    check("us size pe do option milte hain", len(cands), 2)

    # code se match, aur us size ka order code ke bina
    status, chosen, mism = M.match_line(gm.customer_id, "GME02", None)
    check("code se seedha match", (status, chosen.item_code), ("code", "GME02"))
    status, chosen, mism = M.match_line(gm.customer_id, None, two.canonical_key)
    check("bina code ke, do option — operator chunega", status, "multiple")

    # Dobara sync: kuch badla nahi, photo dobara download nahi honi chahiye
    FAKE.downloads.clear()
    r2 = M.sync_one_folder(None, gm)
    check("dobara sync me koi naya product nahi", r2["added"], 0)
    check("paanch update hue", r2["updated"], 5)
    check("ek bhi photo dobara download nahi hui", len(FAKE.downloads), 0)

    # Drive pe file badli — sirf wahi dobara aani chahiye
    TREE["f_gm"][0]["modifiedTime"] = "t2"
    FAKE.downloads.clear()
    r3 = M.sync_one_folder(None, gm)
    check("sirf badli hui file dobara aayi", FAKE.downloads, ["g1"])

    # Party jodne ke baad doosra folder
    sm.customer_id = Customer.query.filter_by(name="Sameer Metals Pvt").first().id
    sm.size_unit = "cm"
    db.session.commit()
    r4 = M.sync_one_folder(None, sm)
    check("SAMEER ke do product", r4["added"], 2)
    check("dono party ke code alag alag rehte hain",
          M.map_by_code(sm.customer_id, "GME01"), None)

    # Dono party me ek hi size ho toh bhi aapas me na ghusein
    gme05 = M.map_by_code(gm.customer_id, "GME05")
    sm01 = M.map_by_code(sm.customer_id, "SM01")
    check("32x24x3.5 dono jagah same key", gme05.canonical_key, sm01.canonical_key)
    check("par doosri party ke size pe pehli party ka product nahi aata",
          [m.item_code for m in M.candidates_for(sm.customer_id, sm01.canonical_key)], ["SM01"])

    # party unit sync se set ho jaata hai
    check("party ka unit cm set hua", M.PartyPOConfig.unit_for(gm.customer_id), "cm")

# ------------------------------------------------------------------ saaf messages
os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)
try:
    drive_sync.credentials_from_env()
    check("key na ho toh error aana chahiye", "koi error nahi", "DriveError")
except drive_sync.DriveError as exc:
    check("key na ho toh saaf message", "set nahi hai" in str(exc), True)

os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = "{ ye JSON nahi hai"
try:
    drive_sync.credentials_from_env()
    check("adhoora JSON pakda jaana chahiye", "koi error nahi", "DriveError")
except drive_sync.DriveError as exc:
    check("adhoore JSON pe saaf message", "poora JSON nahi" in str(exc), True)

if fails:
    print(f"FAILED {len(fails)} check(s):\n")
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("drive sync: all checks passed")
