# Aabroo Operations Portal

Full Aabroo order-operations portal, adapted from the AghaJe workflow and linked to Al Karamat's Aabroo ledger.

## Included workflows

- Shopify order dashboard and protected customer/order data
- Barcode/QR scanner and pending-order processing
- Employee and admin portals
- Product lookup and draft-order creation
- Courier tracking, undelivered follow-up, and PostEx payment views
- Accounts and owner ledger
- Paid-order synchronization with the Al Karamat dashboard

## Deployment

The app runs with `gunicorn main:app` and exposes `/health` for Railway. Copy `.env.example` into your deployment environment and provide the Shopify custom-app credentials, database URL, portal passwords, and any courier credentials in use.

Production URL: <https://admin.aabroo.store>

Al Karamat ledger: <https://dashboard.alkaramat.com/aabroo-portal>
