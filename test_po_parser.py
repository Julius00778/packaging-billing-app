"""Parser/normaliser tests. Runs po_module.py itself, not a copy.

    python3 test_po_parser.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import po_module as M  # noqa: E402

fails = []


def check(label, got, want):
    if got != want:
        fails.append(f"{label}\n    got:  {got}\n    want: {want}")


def one(text, unit="inch", codes=None):
    r = M.parse_po_text(text, unit, codes)
    return r[0] if r else None


# ==========================================================================
# canonical key — ek hi cheez alag alag tareeke se likhi ho toh ek hi key
# ==========================================================================

k = M.canonical_size([12, 18], "inch")
check("12x18 inch", k, "304.8x457.2")
check("18x12 inch same as 12x18", M.canonical_size([18, 12], "inch"), k)
check("304.8x457.2 mm same as 12x18 inch", M.canonical_size([304.8, 457.2], "mm"), k)
check("30.48x45.72 cm same as 12x18 inch", M.canonical_size([30.48, 45.72], "cm"), k)

# --- teen dimension ka dabba ---
box = M.canonical_size([23, 14, 5], "cm")
check("23x14x5 cm", box, "50.0x140.0x230.0")
check("5x23x14 cm same dabba", M.canonical_size([5, 23, 14], "cm"), box)
check("230x140x50 mm same dabba", M.canonical_size([230, 140, 50], "mm"), box)
check("23x14x8 is a different dabba", M.canonical_size([23, 14, 8], "cm") != box, True)

# --- 2-D aur 3-D kabhi aapas me na takrayen ---
check("12x18 sheet != 12x18x5 dabba",
      M.canonical_size([12, 18], "cm") != M.canonical_size([12, 18, 5], "cm"), True)

# ==========================================================================
# unit detection
# ==========================================================================

line = "2mm foam sheet 12x18 - 500 pcs"
m = M.SIZE_RE.search(line)
check("thickness mm must not make the size mm", M.detect_size_unit(line, m, "inch"), ("inch", "party"))

line2 = "EPE 300x450 mm 200 nos"
m2 = M.SIZE_RE.search(line2)
check("explicit mm after size", M.detect_size_unit(line2, m2, "inch"), ("mm", "explicit"))

line3 = '12" x 18" 100 pcs'
m3 = M.SIZE_RE.search(line3)
check("inch symbol", M.detect_size_unit(line3, m3, "mm")[0], "inch")

line4 = "23x14x5 cm - 100 pcs"
m4 = M.SIZE_RE.search(line4)
check("cm after a 3-D size", M.detect_size_unit(line4, m4, "inch"), ("cm", "explicit"))

# ==========================================================================
# poori line — sirf order lines uthni chahiye
# ==========================================================================

text = """
Purchase Order 4471
1. 2mm foam 12x18 - 500 pcs
2. 18 X 24  qty 200
3. 300x450 mm 150 nos
Delivery by 30 Aug
Thanks
"""
rows = M.parse_po_text(text, "inch")
check("only order lines picked up", len(rows), 3)
check("line 1 qty", (rows[0]["qty"], rows[0]["qty_unit"]), (500.0, "pcs"))
check("line 1 not fooled by 2mm", rows[0]["canonical_key"], "304.8x457.2")
check("line 2 qty via label", rows[1]["qty"], 200.0)
check("line 3 mm respected", rows[2]["canonical_key"], "300.0x450.0")
check("line 3 qty", rows[2]["qty"], 150.0)

check("line without size or code skipped", M.parse_po_text("Please send urgently", "inch"), [])

# --- party default tab chalta hai jab unit likha hi na ho ---
amb = M.parse_po_text("24x36 - 50 pcs", "mm")
check("party default mm wins", amb[0]["canonical_key"], "24.0x36.0")
check("unit_source is party", amb[0]["unit_source"], "party")

# --- ulta likha size wahi cheez hai ---
check("12x18 and 18x12 match",
      one("12x18 100 pcs")["canonical_key"], one("18x12 100 pcs")["canonical_key"])
check("23x14x5 and 5x14x23 match",
      one("23x14x5 - 10 pcs", "cm")["canonical_key"],
      one("5x14x23 - 10 pcs", "cm")["canonical_key"])

for sep in ["x", "X", "*", "×"]:
    check(f"separator {sep!r}", one(f"12{sep}18 - 10 pcs")["canonical_key"], "304.8x457.2")
    check(f"separator {sep!r} 3-D",
          one(f"23{sep}14{sep}5 - 10 pcs", "cm")["canonical_key"], "50.0x140.0x230.0")

# ==========================================================================
# asli duniya ke cases
# ==========================================================================

r = one('12" x 18" 100 pcs')
check("inch marks parsed", (r["canonical_key"], r["qty"]), ("304.8x457.2", 100.0))

r = one("300 mm x 450  200 nos")
check("inline mm before x", (r["canonical_key"], r["qty"]), ("300.0x450.0", 200.0))

r = one("500 pcs of 12x18")
check("qty written before size", r["qty"], 500.0)

r = one("Item 3 - 12x18 inch - 250 sheets")
check("trailing inch word + sheets", (r["canonical_key"], r["qty"], r["qty_unit"]),
      ("304.8x457.2", 250.0, "sheet"))

r = one("12.5x18.5 - 40 box")
check("decimal sizes + box unit", (r["qty"], r["qty_unit"]), (40.0, "box"))

# Teesra number ab size ka hissa hai, qty nahi. (Pehle ise thickness maan ke
# phenk diya jaata tha — 3-D products aane ke baad wo galat ho gaya.)
r = one("12x18x2 mm - 500 pcs")
check("teesra number qty nahi banta", r["qty"], 500.0)
check("teesra number size me girta hai", r["canonical_key"], "2.0x12.0x18.0")

r = one("23x14x5 cm - 120 box", "inch")
check("3-D cm size + box qty", (r["canonical_key"], r["qty"], r["qty_unit"]),
      ("50.0x140.0x230.0", 120.0, "box"))

# ==========================================================================
# item code — party ke apne codes
# ==========================================================================

check("HM 01 normalizes", M.normalize_code("HM 01"), "HM01")
check("hm-01 normalizes", M.normalize_code("hm-01"), "HM01")
check("HM01 already normal", M.normalize_code("HM01"), "HM01")

codes = ["HM01", "HM02", "HM03"]

r = one("HM01 - 500 pcs", "cm", codes)
check("code se line uthti hai bina size ke", r["item_code"], "HM01")
check("code wali line ki qty", r["qty"], 500.0)
check("code wali line me size khaali", r["canonical_key"], "")

r = one("HM 02  qty 200", "cm", codes)
check("space wala code", r["item_code"], "HM02")
check("space wale code ki qty", r["qty"], 200.0)

r = one("hm-03 (23x14x5) - 75 box", "cm", codes)
check("chhote akshar + brackets wala code", r["item_code"], "HM03")
check("code ke saath size bhi", r["canonical_key"], "50.0x140.0x230.0")
check("code ke saath qty bhi", (r["qty"], r["qty_unit"]), (75.0, "box"))

# Anjaan code pe bharosa nahi karna — warna "PO 8801" aur "Item 3" bhi code ban jayen
r = one("PO 8801 ke against 12x18 - 5 pcs", "inch", codes)
check("anjaan code nahi uthta", r["item_code"], "")
r = one("Item 3 - 12x18 inch - 250 sheets", "inch", codes)
check("'Item 3' code nahi hai", r["item_code"], "")
r = one("12x18x2 mm - 500 pcs", "inch", codes)
check("'mm 500' code nahi banta", r["item_code"], "")

# Code list na do toh purana behaviour — sirf size
r = one("HM01 - 500 pcs", "cm")
check("bina code list ke code line skip", r, None)

# Mixed PO — kuch lines code wali, kuch sirf size wali
mixed = M.parse_po_text("""
Order from H Metals
HM01 - 500 pcs
HM02 (23x14x5) - 200 pcs
30x20x10 - 50 box
Thanks
""", "cm", codes)
check("mixed PO me teen lines", len(mixed), 3)
check("mixed line 1 code", mixed[0]["item_code"], "HM01")
check("mixed line 2 code + size", (mixed[1]["item_code"], mixed[1]["canonical_key"]),
      ("HM02", "50.0x140.0x230.0"))
check("mixed line 3 sirf size", (mixed[2]["item_code"], mixed[2]["canonical_key"]),
      ("", "100.0x200.0x300.0"))

if fails:
    print(f"FAILED {len(fails)} check(s):\n")
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("all parser checks passed")
