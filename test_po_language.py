"""Screen ki bhasha EN/Hindi hi rahe — Hinglish sirf Telegram ke msg me.

Ye baat aankh se nahi pakdi jaati: koi bhi naya button Hinglish me likh dena
aasan hai. Isliye test khud saare PO templates padhta hai aur wahan Hinglish
ke aam shabd dhoondhta hai. Code ke comment (jo Hinglish me hain aur rehne
chahiye) chhod diye jaate hain.

    python3 test_po_language.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

fails = []


def check(label, got, want=True):
    if got != want:
        fails.append(f"{label}\n    got:  {got!r}\n    want: {want!r}")


# Hinglish ke wo shabd jo screen pe kabhi nahi aane chahiye. Chhote shabd
# (jaise "hai") jaan-boojh ke nahi liye — wo angrezi shabdon ke andar chhupe
# milte hain aur jhoothi shikayat karte hain.
HINGLISH = [
    "nahi", "karo", "kar do", "dabao", "bhejo", "poochho", "chahiye",
    "banao", "hota hai", "jayega", "jayegi", "gaya", "gayi", "wala", "wali",
    "abhi", "saare", "sirf", "pehle", "yahan", "wahan", "koi ", "jaise",
]

COMMENT_RE = re.compile(r"\{#.*?#\}", re.S)


def visible_text(path):
    """Template ka wo hissa jo user ko dikh sakta hai — comment hata ke."""
    with open(path, encoding="utf8") as fh:
        return COMMENT_RE.sub(" ", fh.read())


tpl_dir = os.path.join(HERE, "templates")
po_templates = sorted(f for f in os.listdir(tpl_dir)
                      if f.startswith("po_") or f == "categories.html")
check("PO ke templates mil gaye", len(po_templates) >= 8)

for name in po_templates:
    text = visible_text(os.path.join(tpl_dir, name)).lower()
    found = sorted({w.strip() for w in HINGLISH if w in text})
    check(f"{name} me Hinglish nahi hai", found, [])

# Har t('...') key sach me maujood ho — warna screen pe key hi chhap jaati hai
from translations import TRANSLATIONS                      # noqa: E402

# Sirf poore likhe hue key — t('po_status_' + x) jaise jude hue key alag se
# neeche jaanche jaate hain.
KEY_RE = re.compile(r"t\(\s*'([a-z0-9_]+)'\s*[,)]")
missing = set()
for name in po_templates:
    with open(os.path.join(tpl_dir, name), encoding="utf8") as fh:
        for key in KEY_RE.findall(fh.read()):
            if key not in TRANSLATIONS["en"]:
                missing.add(f"{name}:{key}")
check("har t() key translations me hai", sorted(missing), [])

# Jude hue key: har status ka apna naam aur apna button
import po_module                                           # noqa: E402
for st in po_module.PO_STATUSES:
    check(f"{st} ka naam hai", "po_status_" + st in TRANSLATIONS["en"])
    check(f"{st} ka button hai", "po_move_" + st in TRANSLATIONS["en"])

# Dono bhashaon me ek jaisi keys — warna Hindi me angrezi jhalakti hai
en_po = {k for k in TRANSLATIONS["en"] if k.startswith("po_") or k == "nav_orders"}
hi_po = {k for k in TRANSLATIONS["hi"] if k.startswith("po_") or k == "nav_orders"}
check("Hindi me koi key chhooti nahi", sorted(en_po - hi_po), [])
check("Hindi me koi fazool key nahi", sorted(hi_po - en_po), [])

# Aur Hindi sach me Hindi ho — angrezi copy-paste na ho gayi ho
DEV = re.compile(r"[ऀ-ॿ]")
english_left = sorted(k for k in en_po
                      if len(TRANSLATIONS["en"][k]) > 8
                      and not DEV.search(TRANSLATIONS["hi"][k]))
check("har Hindi text me Devanagari hai", english_left, [])

# Telegram ke msg Hinglish hi rehne chahiye — wo labour ke liye hain
check("Telegram ke status shabd Hinglish hi hain",
      po_module.STATUS_LABEL["made"], "ban gaya")

if fails:
    print(f"FAILED {len(fails)} check(s):\n")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("language: all checks passed")
