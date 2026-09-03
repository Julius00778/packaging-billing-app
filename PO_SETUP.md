# PO automation — kaise kaam karta hai

Ye module PO → order → operator confirm → dispatch ka flow add karta hai.
**App me lag chuka hai** — `app.py`, `templates/base.html` aur `requirements.txt`
ke changes is branch me shaamil hain. Neeche wali list reference ke liye hai.

**Design ka ek asool:** matching filename ya folder pe nahi hoti. Har size ek
`canonical_key` (mm me, sorted) me badal ke DB se match hoti hai — isliye `12x18`,
`18x12`, `12" x 18"` aur `304.8x457.2mm` sab ek hi cheez maane jaate hain.

---

## 1. Files

```
packaging-billing-app/
├── po_module.py              ← module (models + routes, sab yahin)
├── test_po_parser.py         ← parser ke 25+ checks
├── test_po_flow.py           ← poora flow, asli app.py ke through
└── templates/
    ├── po_list.html
    ├── po_new.html
    ├── po_review.html
    ├── po_dispatch.html
    └── po_mappings.html
```

`models.py` me **koi change nahi** hai. Party-level setting alag table
(`party_po_config`) me hai isi liye.

---

## 2. `app.py` me teen change (lag chuke hain)

```python
from po_module import po_bp                              # imports ke saath

app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024       # 8 MB per request

app.register_blueprint(po_bp)                            # login_manager ke neeche
```

`_run_startup_migrations()` me jo `db.create_all()` pehle se hai wo nayi tables
khud bana dega — import upar ho chuka hota hai. Alag migration nahi chahiye.

`requirements.txt` me `Pillow==10.4.0` add hua hai (image resize ke liye).

---

## 3. `templates/base.html` me nav link (lag chuka hai)

`nav.tabs` ke andar, Items aur Accounts ke beech:

```html
<a class="tab-btn {% if active=='po' %}active{% endif %}" href="{{ url_for('po.po_list') }}">PO</a>
```

---

## 4. Nayi tables

| Table | Kaam |
|---|---|
| `party_po_config` | Har party ka size unit (inch / mm / cm) |
| `party_product_map` | Size → product + photo. Yahi system ki "memory" hai |
| `purchase_order` | PO header — party, number, status, scan |
| `po_line` | PO ki har line — size, qty, match state |

`purchase_order` pe `(customer_id, po_number)` ka unique constraint hai — wahi PO
email se bhi aaye aur WhatsApp se bhi, duplicate nahi banega.

`party_product_map` pe `(customer_id, canonical_key)` ka unique constraint
**jaan-boojh ke nahi** lagaya — ek hi size ke 2mm aur 4mm dono variants ho sakte hain.

---

## 5. Photos DB me hain, disk pe nahi — aur chhoti karke

Railway ka filesystem ephemeral hai — har redeploy pe disk wipe ho jaata hai.
Agar images `static/` me rakhte, toh har deploy ke baad gayab ho jaatin. Isliye
images `LargeBinary` column me hain aur route se serve hoti hain.

DB na phoole, iske liye har image **server pe** resize + JPEG me re-encode hoti hai
(`_compress_image` in `po_module.py`). Operator ko kuch nahi karna padta — phone se
seedha 8 MB tak ki photo upload kar sakta hai.

| Kya | Lamba side | Target size | Route |
|---|---|---|---|
| Product photo | 900 px | ~160 KB | `/po/map/<id>/image` |
| Uska thumbnail | 200 px | ~20 KB | `/po/map/<id>/thumb` |
| PO scan | 1800 px | ~500 KB | `/po/<id>/scan` |

Asli phone photo (3024×4032, ~1.4 MB) pe naapa hua: **main 103 KB + thumb 6 KB**.
Yaani **500 mappings ≈ 54 MB** — pehle wale ~150 MB ke bajaye.

Thumbnail alag isliye hai ki review screen pe 20 lines ek saath khulti hain, aur
wahan 96px ka square hi dikhana hota hai. Bada image tab load hota hai jab operator
photo pe click kare.

Quality 82 se shuru hoti hai aur 55 tak hi girti hai — usse neeche product
pehchanna mushkil ho jaata hai. Pillow na ho toh module chalta rahega, bas compress
nahi hoga aur purana 2 MB ka hard cap lag jayega. PDF scan compress nahi hoti
(us par 2 MB cap hai).

Aage agar DB bahut badh jaye toh S3/Cloudflare R2 pe shift karna padega — tab
`image_data` ki jagah `image_url` column aayega, baaki logic waisa hi rahega.

---

## 6. Flow

1. **`/po/new`** — party choose karo, PO number daalo, scan upload karo, aur PO ki
   lines paste karo. Party ka size unit yahin set hota hai (ek baar set, yaad rehta hai).
2. Module har line se size + qty nikalta hai aur us party ke map se match karta hai.
3. **`/po/<id>`** — operator screen. Bayi taraf original scan, dayi taraf har line:
   - **auto-matched** — ek hi product fit hua, photo + naam dikh raha hai
   - **more than one product fits** — variants photo ke saath, operator ek chunta hai
   - **new size** — operator naam + photo daal ke ek baar map karta hai
   - **unit guessed** — flag, jab unit na explicit tha na party default se aaya
4. Jab tak saari lines map nahi ho jatin, Confirm button disabled rehta hai.
5. Confirm → PO `confirmed` ho jaata hai aur **`/po/dispatch`** me chala jaata hai.
6. Dispatch list se "Mark dispatched".

Har naya mapping agli baar khud match hota hai. Shuru me zyadatar lines "new size"
me girengi — 2-3 mahine baad zyadatar auto-match ho jayengi.

---

## 7. Parser kya samajhta hai

Tested aur pass:

```
12x18 - 500 pcs
18 X 24  qty 200
300x450 mm 150 nos
12" x 18" 100 pcs
12x18x2mm - 500 pcs      ← thickness ko qty nahi samjha jaata
Item 3 - 12x18 inch - 250 sheets
500 pcs of 12x18
12.5x18.5 - 40 box
```

Separators: `x`, `X`, `*`, `×`.

**Ek khaas cheez:** `2mm foam 12x18` me `mm` thickness ka hai, size ka nahi. Isliye
unit sirf size ke andar ya turant baad dekha jaata hai, poori line me nahi.

Jis line me size na ho, wo skip ho jaati hai (headers, "Thanks", delivery notes).

---

## 8. Test chalane ke liye

```bash
python3 test_po_parser.py    # parser — 25+ checks
python3 test_po_flow.py      # poora flow: new PO -> map -> confirm -> dispatch
```

`test_po_flow.py` asli `app.py` ke through chalta hai (Flask test client se), apni
alag `test_po.db` banata hai, aur ye bhi check karta hai ki upload hui badi JPEG DB
me chhoti hoke jaa rahi hai. Purani screens (dashboard, invoices, customers, items,
accounts) bhi isme regression ke liye hit hoti hain.

---

## 9. Jo abhi NAHI hai (honest list)

- **OCR / auto-extract** — abhi PO ki lines paste karni padti hain. Scan upload hota
  hai aur operator ko dikhta hai, par usse text khud nahi nikalta. Jab OCR lagega,
  wo bas `parse_po_text()` ko feed karega — baaki poora pipeline waisa hi chalega.
- **Operator ko message (WhatsApp/Telegram)** — abhi operator ko `/po/` list khud
  kholni padegi. Notification layer nahi bana.
- **Email/WhatsApp se auto-intake** — `source` field hai, par actual fetching nahi.
- **Stock check** — confirm hote hi PO seedha dispatch me jaata hai. Agar stock nahi
  hai toh production queue me bhejne wala step nahi hai.
- **Invoice/challan se link** — dispatch ke baad PO se apne aap challan nahi banta.
  Abhi manual banana padega.
- **Hindi translations** — templates me labels English me hardcoded hain,
  `translations.py` me keys add nahi kiye.
