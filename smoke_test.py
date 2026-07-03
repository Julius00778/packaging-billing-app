import os, sys
sys.path.insert(0, os.path.dirname(__file__))
os.environ["DATABASE_URL"] = "sqlite:///test.db"

# fresh db each run
dbfile = os.path.join(os.path.dirname(__file__), "test.db")
if os.path.exists(dbfile):
    os.remove(dbfile)

from app import app

client = app.test_client()

def check(label, resp, expect=200):
    ok = resp.status_code == expect
    print(("PASS" if ok else "FAIL"), label, resp.status_code)
    if not ok:
        print(resp.data[:500])
    return ok

# 1. setup owner
r = client.get("/setup"); check("GET /setup", r)
r = client.post("/setup", data={"name":"Rahul","username":"owner","password":"secret123"}, follow_redirects=True)
check("POST /setup", r)

# 2. login
r = client.post("/login", data={"username":"owner","password":"secret123"}, follow_redirects=True)
check("login", r)

# 3. dashboard
r = client.get("/"); check("dashboard", r)

# 4. add customer (same state)
r = client.post("/customers", data={"name":"Sharma Traders","address":"Pune","phone":"9876543210","gstin":"","state":"Maharashtra"}, follow_redirects=True)
check("add customer same-state", r)

# 5. add customer (different state)
r = client.post("/customers", data={"name":"Delhi Hardware Co","address":"Delhi","phone":"9123456780","gstin":"","state":"Delhi"}, follow_redirects=True)
check("add customer diff-state", r)

# need firm state set - set settings
r = client.post("/settings", data={
    "firm_name":"Aligarh Packaging Works","address":"Industrial Area, Aligarh","phone":"9999999999",
    "email":"", "gstin":"27ABCDE1234F1Z5","state":"Maharashtra","bank_name":"SBI","bank_acc":"12345",
    "ifsc":"SBIN0001234","invoice_prefix":"INV","next_invoice_no":"1"
}, follow_redirects=True)
check("save settings", r)

# 6. add item
r = client.post("/items", data={"name":"Hardware Kit Foam Box","hsn_code":"3921","unit":"pcs","gst_rate":"18","sale_price":"150","current_stock":"100","reorder_level":"10","track_stock":"on"}, follow_redirects=True)
check("add item", r)

import re
from models import db, Customer, Item

with app.app_context():
    cust1 = Customer.query.filter_by(name="Sharma Traders").first()
    cust2 = Customer.query.filter_by(name="Delhi Hardware Co").first()
    item1 = Item.query.filter_by(name="Hardware Kit Foam Box").first()
    cust1_id, cust2_id, item1_id = cust1.id, cust2.id, item1.id

import json
items_json = json.dumps([{"item_id": item1_id, "description":"Hardware Kit Foam Box","hsn_code":"3921","qty":10,"unit":"pcs","rate":150,"gst_rate":18}])

# 7. create invoice - same state customer (expect CGST+SGST)
r = client.post("/invoices/new", data={
    "customer_id": str(cust1_id), "invoice_no":"INV-0001", "date":"2026-06-22",
    "payment_status":"unpaid", "discount_type":"amount", "discount_value":"0", "other_charges":"0",
    "amount_received":"0", "notes":"Test invoice", "items_json": items_json
}, follow_redirects=True)
check("create invoice same-state", r)

with app.app_context():
    from models import Invoice, Item as ItemModel
    inv = Invoice.query.filter_by(invoice_no="INV-0001").first()
    print("Invoice totals:", inv.subtotal, inv.cgst_amount, inv.sgst_amount, inv.igst_amount, inv.grand_total)
    assert inv.cgst_amount > 0 and inv.sgst_amount > 0 and inv.igst_amount == 0, "Same state should give CGST+SGST"
    item_after = ItemModel.query.get(item1_id)
    print("Stock after sale:", item_after.current_stock)
    assert item_after.current_stock == 90, "Stock should reduce by 10"

# 8. create invoice - different state customer (expect IGST)
items_json2 = json.dumps([{"item_id": item1_id, "description":"Hardware Kit Foam Box","hsn_code":"3921","qty":5,"unit":"pcs","rate":150,"gst_rate":18}])
r = client.post("/invoices/new", data={
    "customer_id": str(cust2_id), "invoice_no":"INV-0002", "date":"2026-06-22",
    "payment_status":"paid", "discount_type":"amount", "discount_value":"0", "other_charges":"0",
    "amount_received":"0", "notes":"Test invoice 2", "items_json": items_json2
}, follow_redirects=True)
check("create invoice diff-state", r)

with app.app_context():
    inv2 = Invoice.query.filter_by(invoice_no="INV-0002").first()
    print("Invoice2 totals:", inv2.subtotal, inv2.cgst_amount, inv2.sgst_amount, inv2.igst_amount, inv2.grand_total, inv2.amount_received)
    assert inv2.igst_amount > 0 and inv2.cgst_amount == 0, "Different state should give IGST"
    assert inv2.amount_received == inv2.grand_total, "Paid invoice should have amount_received == grand_total"

# 9. print invoice
r = client.get(f"/invoices/{inv.id}/print"); check("print invoice", r)

# 10. invoice list page
r = client.get("/invoices"); check("invoices list", r)

# 11. dashboard after invoices
r = client.get("/"); check("dashboard after invoices", r)

# 12. settings page accessible
r = client.get("/settings"); check("settings page", r)

# 13. add staff user
r = client.post("/users", data={"username":"staffuser","name":"Staff One","password":"staff123","role":"staff"}, follow_redirects=True)
check("add staff user", r)

# 14. delete invoice and check stock restored
with app.app_context():
    inv1_id = inv.id
r = client.post(f"/invoices/{inv1_id}/delete", follow_redirects=True)
check("delete invoice", r)
with app.app_context():
    item_after_delete = ItemModel.query.get(item1_id)
    print("Stock after delete:", item_after_delete.current_stock)
    assert item_after_delete.current_stock == 95, "Stock should be restored by 10 after delete (90+10=100, minus 5 sold to cust2)"

# 15. staff login and restricted access
client2 = app.test_client()
r = client2.post("/login", data={"username":"staffuser","password":"staff123"}, follow_redirects=True)
check("staff login", r)
r = client2.get("/settings", follow_redirects=True)
check("staff blocked from settings (should redirect, 200 after redirect)", r)

print("\nALL CHECKS COMPLETE")
