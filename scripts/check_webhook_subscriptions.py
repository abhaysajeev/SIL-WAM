"""
Check, per company, whether the Meta app is actually subscribed to webhooks
for that company's WABA — the real source of truth for "is the webhook connected",
as opposed to connection_status which only reflects token/phone validity.

Run from the app's venv/container (needs DB access + SECRET_KEY from settings):

    python scripts/check_webhook_subscriptions.py
"""
import httpx

from app.core.database import SessionLocal
from app.models.company import Company
from app.models.whatsapp import WhatsAppAccount
from app.utils.whatsapp_crypto import decrypt_token

GRAPH_BASE = "https://graph.facebook.com/v21.0"


def main():
    db = SessionLocal()
    try:
        rows = (
            db.query(Company, WhatsAppAccount)
            .join(WhatsAppAccount, WhatsAppAccount.company_id == Company.id)
            .all()
        )
        if not rows:
            print("No companies with a WhatsApp account row found.")
            return

        with httpx.Client(timeout=15) as client:
            for company, acc in rows:
                print(f"\n=== {company.name} ({company.company_code}) ===")
                if not acc.waba_id or not acc.access_token_encrypted:
                    print("  SKIP — missing waba_id or access token")
                    continue

                token = decrypt_token(acc.access_token_encrypted)
                resp = client.get(
                    f"{GRAPH_BASE}/{acc.waba_id}/subscribed_apps",
                    params={"access_token": token},
                )
                if resp.status_code != 200:
                    print(f"  ERROR {resp.status_code}: {resp.text}")
                    continue

                data = resp.json().get("data", [])
                if not data:
                    print("  NOT SUBSCRIBED — no apps subscribed to this WABA's webhooks")
                else:
                    for app in data:
                        print(f"  SUBSCRIBED — app_id={app.get('whatsapp_business_api_data', {}).get('id') or app.get('id')} name={app.get('whatsapp_business_api_data', {}).get('name')}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
