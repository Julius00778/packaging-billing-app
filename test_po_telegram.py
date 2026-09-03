"""Telegram bot ke tests — asli Telegram ke bina.

Telegram ki jagah ek nakli API hai jo har call yaad rakh leti hai. Isse ye check
hota hai ki operator ko kya bheja gaya, kaun button daba sakta hai, status kis
kram me badalta hai, aur manager ko khabar kab jaati hai.

    python3 test_po_telegram.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
DB = os.path.join(os.path.dirname(__file__), "test_tg.db")
os.environ["DATABASE_URL"] = "sqlite:///" + DB
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
if os.path.exists(DB):
    os.remove(DB)

from app import app                                     # noqa: E402
from models import db, Customer, User, Settings, Category, Item  # noqa: E402
import telegram_bot                                     # noqa: E402
import po_module as M                                    # noqa: E402

fails = []


def check(label, got, want=True):
    if got != want:
        fails.append(f"{label}\n    got:  {got!r}\n    want: {want!r}")


def msg_id(packed):
    """Ab har msg "chat:msg" ke roop me yaad rehta hai (kai chat ho sakti hain).
    Test ko sirf pehla msg id chahiye."""
    return int(M.unpack_msg_ids(packed)[0][1])


class FakeTelegram:
    """Har call yaad rakhti hai. message_id badhta jaata hai, jaise asli me."""

    def __init__(self):
        self.calls = []
        self.next_id = 100

    def __call__(self, method, payload=None, files=None, token=None, timeout=20):
        self.calls.append((method, payload or {}, bool(files)))
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id}
        if method == "getMe":
            return {"username": "sambhav_orders_bot"}
        return True

    def of(self, method):
        return [c for c in self.calls if c[0] == method]

    def last_text(self, method):
        rows = self.of(method)
        if not rows:
            return ""
        p = rows[-1][1]
        return p.get("text") or p.get("caption") or ""


TG = FakeTelegram()
telegram_bot.call = TG

OPERATOR = {"id": 555, "first_name": "Ramesh"}
RANDOM_GUY = {"id": 999, "first_name": "Koi Aur"}
JULIUS = {"id": 111, "first_name": "Julius"}
OWNER_CHAT = {"id": 777, "title": "", "type": "private", "first_name": "Julius"}
OP_CHAT = {"id": -1001, "title": "Sambhav Operators", "type": "supergroup"}
MGR_CHAT = {"id": -1002, "title": "Sambhav Managers", "type": "supergroup"}
OP2_CHAT = {"id": -1003, "title": "Sambhav Operators 2", "type": "supergroup"}
STRAY_CHAT = {"id": -1009, "title": "Galti wala group", "type": "supergroup"}
OWNER2_CHAT = {"id": 888, "title": "", "type": "private", "first_name": "Partner"}
PARTNER = {"id": 222, "first_name": "Partner"}


def cb(data, user, chat=None):
    return {"callback_query": {"id": "c1", "from": user, "data": data,
                               "message": {"chat": chat or OP_CHAT, "message_id": 1}}}


# Screen ke raaste bhi isi test me jaanche jaate hain. Login user banne ke
# baad hota hai, isliye client yahan sirf banaya jaata hai.
client = app.test_client()

with app.app_context():
    db.create_all()
    s = Settings.get(); s.firm_name = "Sambhav"
    u = User(name="Julius", username="owner", role="owner"); u.set_password("secret123")
    cust = Customer(name="GM ENTERPREISES")
    db.session.add_all([u, cust])
    db.session.commit()

    # ---------------------------------------------- bot khud chats yaad rakhta hai
    M.handle_update({"message": {"chat": OP_CHAT, "from": OPERATOR, "text": "hi"}})
    M.handle_update({"message": {"chat": MGR_CHAT, "from": OPERATOR, "text": "hi"}})
    check("dono group yaad rahe", M.TelegramChat.query.count(), 2)
    check("bhejne wala bhi yaad raha", M.TelegramPerson.query.count(), 1)
    check("naya banda by default operator nahi hota",
          M.TelegramPerson.query.first().is_operator, False)

    op_chat = M.TelegramChat.query.filter_by(chat_id="-1001").first()
    mgr_chat = M.TelegramChat.query.filter_by(chat_id="-1002").first()
    M.set_chat_roles(op_chat, ["operator"])
    M.set_chat_roles(mgr_chat, ["manager"])
    ramesh = M.TelegramPerson.query.filter_by(tg_user_id="555").first()
    ramesh.is_operator = True
    db.session.commit()

    # ------------------------------------------------------------- ek order banao
    # Category isliye chahiye ki rate poochhte waqt wo saath jaani chahiye —
    # "size se rate nahi nikal sakte, category bhi pata honi chahiye".
    cat = Category(name="EPE Foam Sheet", units="pcs,box")
    db.session.add(cat)
    db.session.flush()
    item = Item(name="GME02 (23x14x4)", category_id=cat.id, track_stock=False)
    db.session.add(item)
    db.session.flush()
    product = M.PartyProductMap(customer_id=cust.id, item_code="GME02",
                                canonical_key="40.0x140.0x230.0",
                                raw_size_text="23x14x4", label="GME02 (23x14x4)",
                                item_id=item.id,
                                image_data=b"\xff\xd8\xff-nakli-photo", times_used=0)
    db.session.add(product)
    db.session.commit()

    po = M.PurchaseOrder(po_number="PO-77", customer_id=cust.id, status="pending",
                         po_date="2026-08-25", created_by=u.id)
    db.session.add(po)
    db.session.flush()
    db.session.add(M.POLine(po_id=po.id, line_no=1, item_code="GME02",
                            raw_size_text="23x14x4", canonical_key="40.0x140.0x230.0",
                            qty=500, qty_unit="pcs", match_status="code",
                            map_id=product.id))
    db.session.commit()
    po_id = po.id

    # -------------------------------------------------------- operator ko bhejna
    TG.calls.clear()
    M.send_order_to_operator(db.session.get(M.PurchaseOrder, po_id))
    po = db.session.get(M.PurchaseOrder, po_id)
    check("status operator ke paas chala gaya", po.status, "with_operator")
    check("har line ka photo card gaya", len(TG.of("sendPhoto")), 1)
    check("photo sach me attach hui", TG.of("sendPhoto")[0][2], True)
    check("card me item code hai", "GME02" in TG.of("sendPhoto")[0][1]["caption"], True)
    check("card me qty hai", "500 pcs" in TG.of("sendPhoto")[0][1]["caption"], True)
    check("aakhir me ek summary card", len(TG.of("sendMessage")), 1)
    check("button sirf summary card pe hai", "ok:" in str(TG.of("sendMessage")[-1][1]), True)
    check("line card pe koi button nahi", "callback_data" in str(TG.of("sendPhoto")[0][1]), False)
    check("message id yaad rahe", len(po.tg_message_ids.split(",")), 2)

    # ------------------------------------------- button kis chat me daba, wo maayne rakhta hai
    # Operator group hi apne aap me izaazat hai — us group me Julius ne khud
    # logon ko daala hai. Pehle har aadmi ko alag se "operator" banana padta
    # tha, aur naya aadmi group me OK dabata tha toh chup-chaap kuch nahi hota.
    TG.calls.clear()
    M.handle_update(cb(f"ok:{po_id}", RANDOM_GUY, chat=STRAY_CHAT))
    po = db.session.get(M.PurchaseOrder, po_id)
    check("bahar ki chat se status nahi badla", po.status, "with_operator")
    check("usko saaf jawab mila",
          "operator ki list me nahi" in TG.of("answerCallbackQuery")[-1][1]["text"], True)

    TG.calls.clear()
    M.handle_update(cb(f"ok:{po_id}", RANDOM_GUY))
    po = db.session.get(M.PurchaseOrder, po_id)
    check("operator group me daba toh chal gaya", po.status, "in_production")
    check("dabane wale ka naam laga", po.operator_name, "Koi Aur")

    # Wapas peeche laakar asli operator wala raasta bhi jaancha jaata hai
    ok_back, _ = M.move_status(po, "with_operator")
    po.operator_name = ""
    db.session.commit()
    check("test ke liye wapas peeche aaya", ok_back, True)

    # ------------------------------------------------------------- OK -> yellow
    TG.calls.clear()
    M.handle_update(cb(f"ok:{po_id}", OPERATOR))
    po = db.session.get(M.PurchaseOrder, po_id)
    check("operator ne OK kiya toh operation me", po.status, "in_production")
    check("operator ka naam laga", po.operator_name, "Ramesh")
    check("card update hua", len(TG.of("editMessageText")), 1)
    check("ab Done button dikhta hai", "done:" in str(TG.of("editMessageText")[-1][1]), True)
    check("manager ko abhi khabar nahi gayi", len(TG.of("sendMessage")), 0)

    # ------------------------------------- wahi button dobara — kuch nahi hona chahiye
    TG.calls.clear()
    M.handle_update(cb(f"ok:{po_id}", OPERATOR))
    po = db.session.get(M.PurchaseOrder, po_id)
    check("dobara OK se kuch nahi badla", po.status, "in_production")

    # ------------------------------------------------------------ Done -> green
    TG.calls.clear()
    M.handle_update(cb(f"done:{po_id}", OPERATOR))
    po = db.session.get(M.PurchaseOrder, po_id)
    check("Done ke baad ban gaya", po.status, "made")
    check("made_at set hua", bool(po.made_at), True)
    check("product ka times_used badha",
          db.session.get(M.PartyProductMap, product.id).times_used, 1)
    check("manager ko khabar gayi", len(TG.of("sendMessage")), 1)
    mgr = TG.of("sendMessage")[-1][1]
    check("khabar manager group me gayi", str(mgr["chat_id"]), "-1002")
    check("khabar me PO number hai", "PO-77" in mgr["text"], True)
    check("khabar me operator ka naam hai", "Ramesh" in mgr["text"], True)
    check("ban jaane ke baad koi button nahi",
          M.order_buttons(po), [])

    # ------------------------------------------- dispatch ka button manager pe
    # Julius ne kaha: "mark for dispatch bhi manager pe daal do". Pehle manager
    # ko sirf padhne wala msg jaata tha — dispatch karne ke liye screen kholni
    # padti thi.
    check("manager wale card pe dispatch ka button hai",
          "disp:" in str(mgr.get("reply_markup", "")), True)
    check("card ka pata yaad rakha gaya", bool(po.tg_manager_msg_ids), True)

    # Bahar ki chat se ye button nahi chalta
    TG.calls.clear()
    M.handle_update(cb(f"disp:{po_id}", RANDOM_GUY, chat=STRAY_CHAT))
    po = db.session.get(M.PurchaseOrder, po_id)
    check("bahar se dispatch nahi hua", po.status, "made")

    TG.calls.clear()
    M.handle_update(cb(f"disp:{po_id}", OPERATOR, chat=MGR_CHAT))
    po = db.session.get(M.PurchaseOrder, po_id)
    check("manager group se dispatch ho gaya", po.status, "dispatched")
    check("dispatched_at bhi laga", bool(po.dispatched_at), True)
    edits = TG.of("editMessageText")
    check("manager ka card update hua",
          any(str(e[1]["chat_id"]) == "-1002" for e in edits), True)
    mgr_edit = [e for e in edits if str(e[1]["chat_id"]) == "-1002"][-1][1]
    check("dispatch ke baad button hat gaya",
          "disp:" in str(mgr_edit.get("reply_markup", "")), False)
    check("operator ka card bhi update hua",
          any(str(e[1]["chat_id"]) == "-1001" for e in edits), True)

    # Wapas 'made' pe laakar aage ka test purane jaisa chalta rahe
    M.move_status(po, "made")

    # ============================================================ rate + bill
    # Aapka apna chat — rate yahin poochha jaata hai
    M.handle_update({"message": {"chat": OWNER_CHAT, "from": JULIUS, "text": "/start"}})
    own = M.TelegramChat.query.filter_by(chat_id="777").first()
    M.set_chat_roles(own, ["owner"])
    db.session.commit()

    po3 = M.PurchaseOrder(po_number="PO-79", customer_id=cust.id, status="pending",
                          created_by=u.id)
    db.session.add(po3)
    db.session.flush()
    db.session.add_all([
        M.POLine(po_id=po3.id, line_no=1, item_code="GME02", qty=200, qty_unit="pcs",
                 canonical_key="40.0x140.0x230.0", match_status="code", map_id=product.id),
        M.POLine(po_id=po3.id, line_no=2, item_code="GME02", qty=10, qty_unit="box",
                 canonical_key="40.0x140.0x230.0", match_status="code", map_id=product.id),
    ])
    db.session.commit()
    po3_id = po3.id

    TG.calls.clear()
    left = M.ask_rates(db.session.get(M.PurchaseOrder, po3_id))
    check("dono line ka rate poochha gaya", left, 2)
    sent = TG.of("sendMessage")
    check("summary + do sawaal", len(sent), 3)
    check("summary aapke chat me gaya", str(sent[0][1]["chat_id"]), "777")
    check("summary me 'rate chahiye' likha hai", "rate chahiye" in sent[0][1]["text"], True)
    # Rate sirf naap se tay nahi hota — 2mm aur 4mm foam ka bhav alag hota hai.
    check("summary me category likhi hai", "EPE Foam Sheet" in sent[0][1]["text"], True)
    check("summary me naap bhi likha hai", "23x14x4" in sent[0][1]["text"], True)
    check("har sawaal me bhi category hai",
          all("EPE Foam Sheet" in m[1]["text"] for m in sent[1:]), True)
    check("har sawaal me naap bhi hai",
          all("23x14x4" in m[1]["text"] for m in sent[1:]), True)
    check("jab tak rate baaki hai, bhejne ka button nahi",
          "rates:" in str(sent[0][1]), False)

    po3 = db.session.get(M.PurchaseOrder, po3_id)
    l_pcs, l_box = po3.lines[0], po3.lines[1]

    # pehli line ka rate — us sawaal ke reply me sirf number
    TG.calls.clear()
    M.handle_update({"message": {
        "chat": OWNER_CHAT, "from": JULIUS, "text": "25",
        "reply_to_message": {"message_id": msg_id(l_pcs.tg_rate_msg_id)}}})
    po3 = db.session.get(M.PurchaseOrder, po3_id)
    check("pcs ka rate lag gaya", po3.lines[0].rate, 25.0)
    check("ek line ka rate abhi baaki hai", po3.no_rate_count, 1)
    check("abhi bhi button nahi aaya",
          "rates:" in str(TG.of("editMessageText")[-1][1]), False)

    # doosri line — wahi product, par box me. Rate alag hi poochha jaata hai.
    TG.calls.clear()
    M.handle_update({"message": {
        "chat": OWNER_CHAT, "from": JULIUS, "text": "₹900",
        "reply_to_message": {"message_id": msg_id(l_box.tg_rate_msg_id)}}})
    po3 = db.session.get(M.PurchaseOrder, po3_id)
    check("rupee ka nishan aur comma hat jaate hain", po3.lines[1].rate, 900.0)
    check("ab koi rate baaki nahi", po3.no_rate_count, 0)
    check("total sahi", po3.total, 200 * 25 + 10 * 900)
    check("ab bhejne ka button aa gaya",
          "rates:" in str(TG.of("editMessageText")[-1][1]), True)

    check("pcs ka rate yaad raha",
          M.PartyRate.look_up(product.id, "pcs").rate, 25.0)
    check("box ka rate alag yaad raha",
          M.PartyRate.look_up(product.id, "box").rate, 900.0)

    # number ki jagah kachra
    TG.calls.clear()
    M.handle_update({"message": {
        "chat": OWNER_CHAT, "from": JULIUS, "text": "pata nahi",
        "reply_to_message": {"message_id": msg_id(l_pcs.tg_rate_msg_id)}}})
    check("kachre pe saaf jawab",
          "Sirf number" in TG.of("sendMessage")[-1][1]["text"], True)
    check("rate waisa hi raha",
          db.session.get(M.PurchaseOrder, po3_id).lines[0].rate, 25.0)

    # rate ka reply operator group se — nahi chalna chahiye
    TG.calls.clear()
    M.handle_update({"message": {
        "chat": OP_CHAT, "from": OPERATOR, "text": "1",
        "reply_to_message": {"message_id": msg_id(l_pcs.tg_rate_msg_id)}}})
    check("operator group se rate nahi badalta",
          db.session.get(M.PurchaseOrder, po3_id).lines[0].rate, 25.0)

    # bhejne wala button operator group se — nahi chalna chahiye
    TG.calls.clear()
    M.handle_update({"callback_query": {"id": "c9", "from": OPERATOR,
                                        "data": f"rates:{po3_id}",
                                        "message": {"chat": OP_CHAT, "message_id": 9}}})
    check("galat jagah se rates button nahi chalta",
          db.session.get(M.PurchaseOrder, po3_id).status, "pending")

    # ---------------------------------------- screen se bina mohar ke nahi bhej sakte
    # Bill isi rate se banta hai, isliye rate pe Telegram wali haan zaroori hai.
    client.post("/login", data={"username": "owner", "password": "secret123"},
                follow_redirects=True)
    check("abhi rate pe mohar nahi lagi",
          db.session.get(M.PurchaseOrder, po3_id).rates_ok_at, None)
    r = client.post(f"/po/{po3_id}/confirm", follow_redirects=True)
    check("screen se bhejna ruk gaya",
          db.session.get(M.PurchaseOrder, po3_id).status, "pending")
    check("aur wajah bhi batayi", "not confirmed yet" in r.data.decode("utf8", "ignore"))

    # sahi jagah se
    TG.calls.clear()
    M.handle_update({"callback_query": {"id": "c10", "from": JULIUS,
                                        "data": f"rates:{po3_id}",
                                        "message": {"chat": OWNER_CHAT, "message_id": 9}}})
    po3 = db.session.get(M.PurchaseOrder, po3_id)
    check("rate confirm karte hi operator ko chala gaya", po3.status, "with_operator")
    check("aur mohar lag gayi", bool(po3.rates_ok_at), True)

    # operator ne banaya -> bill
    TG.calls.clear()
    M.handle_update(cb(f"ok:{po3_id}", OPERATOR))
    M.handle_update(cb(f"done:{po3_id}", OPERATOR))
    po3 = db.session.get(M.PurchaseOrder, po3_id)
    check("ban gaya", po3.status, "made")
    check("bill khud ban gaya", bool(po3.invoice_id), True)

    from models import Invoice
    inv = db.session.get(Invoice, po3.invoice_id)
    check("bill ka total order jitna", inv.grand_total, float(200 * 25 + 10 * 900))
    check("bill me do line", len(inv.items), 2)
    check("pcs wali line ka rate", inv.items[0].rate, 25.0)
    check("box wali line ka unit", inv.items[1].unit, "box")
    bill_msgs = [c for c in TG.of("sendMessage") if "Bill ban gaya" in c[1].get("text", "")]
    check("bill ki khabar gayi", len(bill_msgs), 2)   # aapko aur manager ko
    check("khabar me invoice number", inv.invoice_no in bill_msgs[0][1]["text"], True)

    # ------------------------------- rate bina reply ke bhi, jab saaf ho ki kiska hai
    po4 = M.PurchaseOrder(po_number="PO-80", customer_id=cust.id, status="pending",
                          created_by=u.id)
    db.session.add(po4)
    db.session.flush()
    # 'sheet' unit ka rate abhi tak nahi diya gaya, isliye sawaal banega
    db.session.add(M.POLine(po_id=po4.id, line_no=1, item_code="GME02", qty=5,
                            qty_unit="sheet", canonical_key="40.0x140.0x230.0",
                            match_status="code", map_id=product.id))
    db.session.commit()
    po4_id = po4.id
    M.ask_rates(db.session.get(M.PurchaseOrder, po4_id))
    check("yaad na hone par sawaal banta hai",
          db.session.get(M.PurchaseOrder, po4_id).no_rate_count, 1)

    TG.calls.clear()
    M.handle_update({"message": {"chat": OWNER_CHAT, "from": JULIUS, "text": "40"}})
    check("ek hi line baaki thi toh bina reply ke bhi rate lag gaya",
          db.session.get(M.PurchaseOrder, po4_id).lines[0].rate, 40.0)

    # ab do order rate ka intezaar kar rahe hain — andaza nahi lagana chahiye
    po5 = M.PurchaseOrder(po_number="PO-81", customer_id=cust.id, status="pending",
                          created_by=u.id)
    po6 = M.PurchaseOrder(po_number="PO-82", customer_id=cust.id, status="pending",
                          created_by=u.id)
    db.session.add_all([po5, po6])
    db.session.flush()
    db.session.add_all([
        M.POLine(po_id=po5.id, line_no=1, item_code="GME02", qty=5, qty_unit="kg",
                 match_status="code", map_id=product.id),
        M.POLine(po_id=po6.id, line_no=1, item_code="GME02", qty=7, qty_unit="roll",
                 match_status="code", map_id=product.id),
    ])
    db.session.commit()
    po5_id, po6_id = po5.id, po6.id

    TG.calls.clear()
    M.handle_update({"message": {"chat": OWNER_CHAT, "from": JULIUS, "text": "60"}})
    check("do line baaki hon toh andaza nahi lagta",
          db.session.get(M.PurchaseOrder, po5_id).lines[0].rate, 0.0)
    check("aur wajah bata di jaati hai",
          "reply karo" in TG.of("sendMessage")[-1][1]["text"], True)

    # code ke saath bhejo toh chal jaata hai — par yahan dono ka code ek hi hai
    TG.calls.clear()
    M.handle_update({"message": {"chat": OWNER_CHAT, "from": JULIUS, "text": "GME02 60"}})
    check("ek hi code ki do line hon toh bhi andaza nahi lagta",
          db.session.get(M.PurchaseOrder, po5_id).lines[0].rate, 0.0)

    # reply se hamesha chalta hai
    TG.calls.clear()
    l5 = db.session.get(M.PurchaseOrder, po5_id).lines[0]
    M.ask_rates(db.session.get(M.PurchaseOrder, po5_id))
    l5 = db.session.get(M.PurchaseOrder, po5_id).lines[0]
    M.handle_update({"message": {
        "chat": OWNER_CHAT, "from": JULIUS, "text": "60",
        "reply_to_message": {"message_id": msg_id(l5.tg_rate_msg_id)}}})
    check("reply se saaf hai ki kis line ka hai",
          db.session.get(M.PurchaseOrder, po5_id).lines[0].rate, 60.0)
    check("doosra order waisa hi pada hai",
          db.session.get(M.PurchaseOrder, po6_id).lines[0].rate, 0.0)

    # ------------------------- pehle se laga hua rate badalna (Julius ki shikayat)
    # Pichli baar wala rate apne aap bhar jaata hai. Pehle uske baad card sirf
    # "haan" kehne deta tha — badalne ka koi rasta hi nahi tha. Ab code ke saath
    # number bhejo aur wo badal jaata hai.
    for stale in (po4_id, po5_id, po6_id):
        M.move_status(db.session.get(M.PurchaseOrder, stale), "rejected")
    db.session.commit()

    po7 = M.PurchaseOrder(po_number="PO-83", customer_id=cust.id, status="pending",
                          created_by=u.id)
    db.session.add(po7)
    db.session.flush()
    db.session.add(M.POLine(po_id=po7.id, line_no=1, item_code="GME02", qty=10,
                            qty_unit="pcs", canonical_key="40.0x140.0x230.0",
                            match_status="code", map_id=product.id))
    db.session.commit()
    po7_id = po7.id

    TG.calls.clear()
    left = M.ask_rates(db.session.get(M.PurchaseOrder, po7_id))
    po7 = db.session.get(M.PurchaseOrder, po7_id)
    check("pichli baar ka rate apne aap bhar gaya", po7.lines[0].rate, 25.0)
    check("isliye poochhne ko kuch nahi bacha", left, 0)
    card = TG.of("sendMessage")[0][1]["text"]
    check("phir bhi card batata hai ki rate badla ja sakta hai",
          "badalna" in card and "GME02" in card, True)
    check("aur bhejne ka button maujood hai",
          "rates:" in str(TG.of("sendMessage")[0][1]), True)

    # Aur screen pe wo raasta maujood hona chahiye. Pehle ye button sirf tab
    # dikhta tha jab kisi line ka rate khaali ho — yaani jab saare rate yaad se
    # bhar jaate the, tab bhejne ka button band aur poochhne ka button gayab.
    # Order wahin phans jaata tha.
    page = client.get(f"/po/{po7_id}").data.decode("utf8", "ignore")
    check("sab rate bhare hon tab bhi Telegram wala rasta khula hai",
          f"/po/{po7_id}/ask-rates" in page, True)
    check("aur wajah likhi hai ki mohar wahin lagegi",
          "GME01 30" in page, True)

    # Koi sawaal wala msg bana hi nahi, isliye reply karne ko kuch nahi — code se
    TG.calls.clear()
    M.handle_update({"message": {"chat": OWNER_CHAT, "from": JULIUS, "text": "GME02 30"}})
    po7 = db.session.get(M.PurchaseOrder, po7_id)
    check("laga hua rate code ke saath badal gaya", po7.lines[0].rate, 30.0)
    check("naya rate yaad bhi rakha gaya",
          M.PartyRate.look_up(product.id, "pcs").rate, 30.0)
    check("purana rate kya tha, ye bhi bataya",
          "pehle" in TG.of("sendMessage")[-1][1]["text"], True)

    # Rate badla toh mohar hat jaani chahiye — purani haan naye rate pe nahi chalti
    po7.rates_ok_at = M.datetime.utcnow()
    db.session.commit()
    M.handle_update({"message": {"chat": OWNER_CHAT, "from": JULIUS, "text": "GME02 32"}})
    po7 = db.session.get(M.PurchaseOrder, po7_id)
    check("rate badalte hi mohar hat gayi",
          (po7.lines[0].rate, po7.rates_ok_at), (32.0, None))

    # Sab bhare hon aur code na ho toh andaza bilkul nahi — galti seedhi bill me
    TG.calls.clear()
    M.handle_update({"message": {"chat": OWNER_CHAT, "from": JULIUS, "text": "99"}})
    check("khaali number pe andaza nahi lagta",
          db.session.get(M.PurchaseOrder, po7_id).lines[0].rate, 32.0)
    check("balki code maanga jaata hai",
          "code ke saath" in TG.of("sendMessage")[-1][1]["text"], True)

    # Do order me ek hi code pada ho toh khula code kaafi nahi — par card ka
    # reply order tay kar deta hai, aur wahi aadmi ka seedha raasta bhi hai.
    po8 = M.PurchaseOrder(po_number="PO-84", customer_id=cust.id, status="pending",
                          created_by=u.id)
    db.session.add(po8)
    db.session.flush()
    db.session.add(M.POLine(po_id=po8.id, line_no=1, item_code="GME02", qty=4,
                            qty_unit="pcs", canonical_key="40.0x140.0x230.0",
                            match_status="code", map_id=product.id))
    db.session.commit()
    po8_id = po8.id
    M.ask_rates(db.session.get(M.PurchaseOrder, po8_id))

    TG.calls.clear()
    M.handle_update({"message": {"chat": OWNER_CHAT, "from": JULIUS, "text": "GME02 44"}})
    check("ek hi code do order me ho toh andaza nahi lagta",
          (db.session.get(M.PurchaseOrder, po7_id).lines[0].rate,
           db.session.get(M.PurchaseOrder, po8_id).lines[0].rate), (32.0, 32.0))

    TG.calls.clear()
    card_id = msg_id(db.session.get(M.PurchaseOrder, po8_id).tg_rate_summary_id)
    M.handle_update({"message": {
        "chat": OWNER_CHAT, "from": JULIUS, "text": "GME02 44",
        "reply_to_message": {"message_id": card_id}}})
    check("card ke reply se sahi order ka rate badla",
          db.session.get(M.PurchaseOrder, po8_id).lines[0].rate, 44.0)
    check("doosra order chhua tak nahi gaya",
          db.session.get(M.PurchaseOrder, po7_id).lines[0].rate, 32.0)

    # Us order me ek hi line hai — card ke reply me sirf number bhi kaafi hai
    TG.calls.clear()
    M.handle_update({"message": {
        "chat": OWNER_CHAT, "from": JULIUS, "text": "46",
        "reply_to_message": {"message_id": card_id}}})
    check("ek line wale card pe khaali number bhi chal jaata hai",
          db.session.get(M.PurchaseOrder, po8_id).lines[0].rate, 46.0)

    for done in (po7_id, po8_id):
        M.move_status(db.session.get(M.PurchaseOrder, done), "rejected")
    db.session.commit()

    # aam baatcheet ko rate nahi samajhna chahiye
    TG.calls.clear()
    M.handle_update({"message": {"chat": OWNER_CHAT, "from": JULIUS, "text": "theek hai bhai"}})
    check("aam msg pe kuch nahi hota", len(TG.calls), 0)
    M.handle_update({"message": {"chat": OWNER_CHAT, "from": JULIUS, "text": "/start"}})
    check("command pe bhi kuch nahi hota", len(TG.calls), 0)

    # --------------------------------------------------- galat chhalang nahi chalti
    po2 = M.PurchaseOrder(po_number="PO-78", customer_id=cust.id, status="pending",
                          created_by=u.id)
    db.session.add(po2); db.session.commit()
    ok, msg = M.move_status(po2, "dispatched")
    check("pending se seedha dispatched nahi", ok, False)
    check("wajah bhi batayi", "nahi ho sakta" in msg, True)
    check("status waisa hi raha", po2.status, "pending")

    # ------------------------------------------------- office peeche la sakta hai
    po = db.session.get(M.PurchaseOrder, po_id)
    ok, _ = M.move_status(po, "in_production")
    check("ban gaya se wapas operation me ja sakta hai", ok, True)
    ok, _ = M.move_status(po, "made")
    check("phir aage bhi", ok, True)
    ok, _ = M.move_status(po, "dispatched")
    check("aur dispatch bhi", (ok, po.status), (True, "dispatched"))

    # ---------------------------------------------------- cancel kahin se bhi ho
    ok, _ = M.move_status(po2, "rejected")
    check("pending order cancel ho gaya", (ok, po2.status), (True, "rejected"))

    # ============================ ek role ki kai chat, ek chat ke kai role
    # Asli dafter: do operator group, do co-owner. Pehle yahan "ek role sirf
    # ek chat ka" wala niyam tha — naye group ko operator banate hi purane ka
    # role chup-chaap hat jaata tha, aur order sirf ek jagah jaata tha.
    M.handle_update({"message": {"chat": OP2_CHAT, "from": OPERATOR, "text": "hi"}})
    M.handle_update({"message": {"chat": OWNER2_CHAT, "from": PARTNER, "text": "/start"}})
    op2 = M.TelegramChat.query.filter_by(chat_id="-1003").first()
    own2 = M.TelegramChat.query.filter_by(chat_id="888").first()
    M.set_chat_roles(op2, ["operator"])
    M.set_chat_roles(own2, ["owner"])
    db.session.commit()

    check("ab do operator group hain", len(M.chats_for_role("operator")), 2)
    check("aur do owner chat", len(M.chats_for_role("owner")), 2)
    check("purane group ka role nahi hata",
          M.TelegramChat.query.filter_by(chat_id="-1001").first().has_role("operator"), True)

    # ek chat ke do role — manager group ko owner bhi bana do
    M.set_chat_roles(mgr_chat, ["manager", "owner"])
    db.session.commit()
    check("ek chat ke do role chalte hain",
          sorted(M.TelegramChat.query.filter_by(chat_id="-1002").first().role_list()),
          ["manager", "owner"])
    M.set_chat_roles(mgr_chat, ["manager"])
    db.session.commit()

    # ---- order dono operator group me jaana chahiye
    po9 = M.PurchaseOrder(po_number="PO-85", customer_id=cust.id, status="pending",
                          created_by=u.id)
    db.session.add(po9)
    db.session.flush()
    db.session.add(M.POLine(po_id=po9.id, line_no=1, item_code="GME02", qty=20,
                            qty_unit="pcs", canonical_key="40.0x140.0x230.0",
                            match_status="code", map_id=product.id, rate=25))
    db.session.commit()
    po9_id = po9.id

    TG.calls.clear()
    M.send_order_to_operator(db.session.get(M.PurchaseOrder, po9_id))
    sent_to = {str(c[1]["chat_id"]) for c in TG.of("sendMessage") + TG.of("sendPhoto")}
    check("order dono operator group me gaya", sent_to, {"-1001", "-1003"})
    po9 = db.session.get(M.PurchaseOrder, po9_id)
    check("dono group ke msg yaad rahe",
          {c for c, _ in M.unpack_msg_ids(po9.tg_message_ids)}, {"-1001", "-1003"})

    # ek group me OK dabao — dono group ke card badalne chahiye
    TG.calls.clear()
    M.handle_update(cb(f"ok:{po9_id}", OPERATOR))
    check("ek jagah OK se order aage badha",
          db.session.get(M.PurchaseOrder, po9_id).status, "in_production")
    check("dono group ke card update hue",
          {str(c[1]["chat_id"]) for c in TG.of("editMessageText")}, {"-1001", "-1003"})

    # ---- rate ka card dono co-owner ko jaye, aur mohar koi bhi laga sake
    po10 = M.PurchaseOrder(po_number="PO-86", customer_id=cust.id, status="pending",
                           created_by=u.id)
    db.session.add(po10)
    db.session.flush()
    db.session.add(M.POLine(po_id=po10.id, line_no=1, item_code="GME02", qty=7,
                            qty_unit="pcs", canonical_key="40.0x140.0x230.0",
                            match_status="code", map_id=product.id))
    db.session.commit()
    po10_id = po10.id

    TG.calls.clear()
    M.ask_rates(db.session.get(M.PurchaseOrder, po10_id))
    check("rate ka card dono owner chat me gaya",
          {str(c[1]["chat_id"]) for c in TG.of("sendMessage")}, {"777", "888"})

    # doosre co-owner ke chat se rate bhejo
    TG.calls.clear()
    M.handle_update({"message": {"chat": OWNER2_CHAT, "from": PARTNER, "text": "GME02 40"}})
    check("doosre co-owner ka rate bhi lagta hai",
          db.session.get(M.PurchaseOrder, po10_id).lines[0].rate, 40.0)
    check("jawab usi chat me gaya jahan se aaya",
          str(TG.of("sendMessage")[-1][1]["chat_id"]), "888")

    # aur mohar bhi wahi laga sake
    TG.calls.clear()
    M.handle_update({"callback_query": {"id": "c9", "from": PARTNER,
                                        "data": f"rates:{po10_id}",
                                        "message": {"chat": OWNER2_CHAT, "message_id": 1}}})
    po10 = db.session.get(M.PurchaseOrder, po10_id)
    check("doosre co-owner ki mohar bhi chalti hai", bool(po10.rates_ok_at), True)
    check("aur order operator ko chala gaya", po10.status, "with_operator")

    # ---- galti se bani chat list se hatt jaye
    M.handle_update({"message": {"chat": STRAY_CHAT, "from": OPERATOR, "text": "hi"}})
    stray = M.TelegramChat.query.filter_by(chat_id="-1009").first()
    check("galti wala group list me aa gaya", bool(stray), True)
    stray_id = stray.id
    client.post("/login", data={"username": "owner", "password": "secret123"},
                follow_redirects=True)
    r = client.post(f"/po/telegram/chat/{stray_id}/delete", follow_redirects=True)
    check("hatane ka rasta chalta hai -> HTTP", r.status_code, 200)
    check("group list se hat gaya",
          M.TelegramChat.query.filter_by(chat_id="-1009").first(), None)
    check("baaki chats waisi hi hain", len(M.chats_for_role("operator")), 2)

# ------------------------------------------------------------------ webhook

r = client.get("/po/telegram")
check("telegram page khulta hai", r.status_code, 200)
check("bot ka naam dikhta hai", b"sambhav_orders_bot" in r.data, True)

# Naya aadmi jodne ka rasta screen pe hona chahiye. Bot kisi ko khud se msg
# nahi kar sakta jab tak wo Start na dabaye — ye Telegram ka niyam hai, aur
# screen pe likha hona chahiye warna aadmi group bana ke kaam chalata rahega.
page = r.data.decode("utf8", "ignore")
check("bot ka link diya hai", "https://t.me/sambhav_orders_bot" in page)
check("Start dabane wali baat likhi hai", "Start" in page)
# Role ke naam nahi, unka kaam likha ho — "manager" se ye pata nahi chalta
# ki us chat ko kya msg jayega.
check("tick pe kaam likha hai — rate", "Rate questions" in page)
check("tick pe kaam likha hai — naya kaam", "New work to make" in page)
check("tick pe kaam likha hai — dispatch", "Ready to dispatch" in page)
check("private chat aur group alag dikhte hain",
      "one person" in page and "the whole group" in page)

check("bina group wale rate ke koi chetavni nahi",
      "Everyone in this group can see the rates" in page, False)

# Group pe rate ka tick lagana matlab poore group ko bhav dikhega — ye baat
# tick ke waqt saamne honi chahiye, baad me nahi.
with app.app_context():
    grp = M.TelegramChat.query.filter_by(chat_id="-1002").first()
    M.set_chat_roles(grp, ["manager", "owner"])
    db.session.commit()
page2 = client.get("/po/telegram").data.decode("utf8", "ignore")
check("group pe rate ka tick ho toh chetavni dikhti hai",
      "Everyone in this group can see the rates" in page2)
with app.app_context():
    grp = M.TelegramChat.query.filter_by(chat_id="-1002").first()
    M.set_chat_roles(grp, ["manager"])
    db.session.commit()

with app.app_context():
    op = M.TelegramChat.query.filter_by(chat_id="-1001").first()
    op_id = op.id
TG.calls.clear()
r = client.post(f"/po/telegram/chat/{op_id}/test", follow_redirects=True)
check("test msg bhejne ka rasta chalta hai", r.status_code, 200)
check("test msg sach me gaya", len(TG.of("sendMessage")), 1)
test_msg = TG.of("sendMessage")[-1][1]
check("test msg usi chat me gaya", str(test_msg["chat_id"]), "-1001")
check("test msg batata hai ki kya milega",
      "New work to make" in test_msg["text"], True)
check("aur wo cheez nahi jo nahi milegi",
      "Rate questions" in test_msg["text"], False)

r = client.post("/po/telegram/hook", follow_redirects=True)
check("webhook set ho gaya", r.status_code, 200)
check("Telegram ko setWebhook gaya", len(TG.of("setWebhook")) >= 1, True)
hook_url = TG.of("setWebhook")[-1][1]["url"]
check("webhook https pe hai", hook_url.startswith("https://"), True)
check("webhook me secret hai", bool(TG.of("setWebhook")[-1][1].get("secret_token")), True)

with app.app_context():
    secret = M.POSetting.get(M.TG_SECRET_KEY)

check("galat secret pe 404", client.post("/po/hook/galat-secret", json={}).status_code, 404)
check("sahi secret pe 200",
      client.post(f"/po/telegram/hook/{secret}", json={
          "message": {"chat": OP_CHAT, "from": OPERATOR, "text": "test"}}).status_code, 200)
check("galat secret header pe 403",
      client.post(f"/po/telegram/hook/{secret}", json={},
                  headers={"X-Telegram-Bot-Api-Secret-Token": "kuch aur"}).status_code, 403)
check("kachra bheja jaye toh bhi 200 (warna Telegram baar baar bhejega)",
      client.post(f"/po/telegram/hook/{secret}", data="ye json nahi hai").status_code, 200)

# purani screens bhi chalti rahni chahiye
for path in ("/", "/invoices", "/customers", "/items", "/po/", "/po/dispatch",
             "/po/mappings", "/po/drive"):
    check(f"GET {path}", client.get(path).status_code, 200)

if fails:
    print(f"FAILED {len(fails)} check(s):\n")
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("telegram: all checks passed")
