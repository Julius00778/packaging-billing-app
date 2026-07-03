# Packaging Firm — Invoice & Billing System (Phase 1, no GST yet)

Simple invoicing + finished-goods stock + multi-user (Owner/Staff) web app, built for
a foam/thermocol packaging manufacturer. Works on any computer/phone via a web
browser once deployed — see `DEPLOY_GUIDE.md`.

GST is intentionally switched off in this phase — invoices show Subtotal, Discount,
Other Charges, and Grand Total only. The tax engine already exists in the codebase
(tested and working) and will be switched back on in Phase 2 without rebuilding
anything — just re-adding the GST fields to the forms.

## What's included (Phase 1)

- **Login system** with two roles: **Owner** (full access) and **Staff** (create
  invoices/customers/items, no access to Settings/Users/delete).
- **Simple invoicing**: Subtotal, Discount (₹ or %), Other Charges (freight/packing),
  Grand Total. No tax calculation.
- **Customers** master (name, address, phone — GSTIN/state fields exist for Phase 2,
  harmless to leave blank for now).
- **Items** master (finished goods) with price and stock tracking + low-stock alerts.
- **Invoices**: create, edit, search, delete (delete restores stock), printable
  invoice (browser print → "Save as PDF" works on phone and desktop).
- **Dashboard**: total billed/received/pending, low stock list, recent invoices.
- **Settings** (Owner only): firm details, bank details, invoice numbering.
- **Users** (Owner only): add staff logins, activate/deactivate.

## Coming in Phase 2

- **GST**: CGST/SGST/IGST auto-calculated from firm vs customer state (the
  calculation function `calc_invoice_totals` in `app.py` already does this — just
  needs the GST-rate fields turned back on in the Item and Invoice forms).
- Raw material (foam/thermocol sheet & block) inventory with cutting/yield tracking
- Cash flow / ledger (khata) beyond what's shown on the dashboard

## Local testing (optional, before deploying)

```bash
pip install -r requirements.txt
python app.py
```
Open `http://localhost:5000` → it will ask you to create the first Owner account.

## Important notes

- Change `SECRET_KEY` to a random value in production (see deploy guide).
- The default SQLite database is fine to start, but for multi-location use, connect
  a proper Postgres database (the app already supports this via `DATABASE_URL`).

