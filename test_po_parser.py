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


# --- canonical key: same size written differently must collapse to one key ---
k = M.canonical_size(12, 18, "inch")
check("12x18 inch", k, "304.8x457.2")
check("18x12 inch same as 12x18", M.canonical_size(18, 12, "inch"), k)
check("304.8x457.2 mm same as 12x18 inch", M.canonical_size(304.8, 457.2, "mm"), k)
check("30.48x45.72 cm same as 12x18 inch", M.canonical_size(30.48, 45.72, "cm"), k)

# --- unit detection must not be fooled by thickness written as mm ---
line = "2mm foam sheet 12x18 - 500 pcs"
m = M.SIZE_RE.search(line)
check("thickness mm must not make the size mm", M.detect_size_unit(line, m, "inch"), ("inch", "party"))

line2 = "EPE 300x450 mm 200 nos"
m2 = M.SIZE_RE.search(line2)
check("explicit mm after size", M.detect_size_unit(line2, m2, "inch"), ("mm", "explicit"))

line3 = '12" x 18" 100 pcs'
m3 = M.SIZE_RE.search(line3)
check("inch symbol", M.detect_size_unit(line3, m3, "mm")[0], "inch")

# --- full line parsing ---
text = """
Purchase Order 4471
1. 2mm foam 12x18 - 500 pcs
2. 18 X 24  qty 200
3. 300x450 mm 150 nos
Delivery by 30 Aug
Thanks
"""
rows = M.parse_po_text(text, "inch")
check("only size lines picked up", len(rows), 3)
check("line 1 qty", (rows[0]["qty"], rows[0]["qty_unit"]), (500.0, "pcs"))
check("line 1 not fooled by 2mm", rows[0]["canonical_key"], "304.8x457.2")
check("line 2 qty via label", rows[1]["qty"], 200.0)
check("line 3 mm respected", rows[2]["canonical_key"], "300.0x450.0")
check("line 3 qty", rows[2]["qty"], 150.0)

# --- the ambiguous case: no explicit unit, party default decides ---
amb = M.parse_po_text("24x36 - 50 pcs", "mm")
check("party default mm wins", amb[0]["canonical_key"], "24.0x36.0")
check("unit_source is party", amb[0]["unit_source"], "party")

# --- reversed dimensions collapse ---
a = M.parse_po_text("12x18 100 pcs", "inch")[0]["canonical_key"]
b = M.parse_po_text("18x12 100 pcs", "inch")[0]["canonical_key"]
check("12x18 and 18x12 match", a, b)

# --- separators ---
for sep in ["x", "X", "*", "×"]:
    r = M.parse_po_text(f"12{sep}18 - 10 pcs", "inch")
    check(f"separator {sep!r}", r[0]["canonical_key"] if r else None, "304.8x457.2")

# --- no size line at all ---
check("line without size skipped", M.parse_po_text("Please send urgently", "inch"), [])


# --- extra real-world cases (appended) ---
def one(text, unit="inch"):
    r = M.parse_po_text(text, unit)
    return r[0] if r else None

r = one("12x18x2mm - 500 pcs")
check("thickness not read as qty", (r["qty"], r["canonical_key"]), (500.0, "304.8x457.2"))

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

if fails:
    print(f"FAILED {len(fails)} check(s):\n")
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("all parser checks passed")
