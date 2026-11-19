"""
Read-only health check of a Meta access token (e.g. Softland's system-user token).

Answers, per WABA the token can reach:
  - does the token have the right permissions, and which WABAs is it granted on?
  - can it read the WABA, its phone numbers and templates?
  - is a payment method / credit line attached (error 131042)?
  - which apps are subscribed to the WABA's webhooks (403s / no inbound)?
  - is the app's webhook subscribed to the `messages` field?

ONLY GET requests are made. Nothing is changed at Meta or in our DB.

Usage (token is read from the env or prompted — never pass it as an argument,
it would land in your shell history):

    export META_TOKEN='EAAG...'
    python -m scripts.check_meta_access                 # WABAs the token is granted on
    python -m scripts.check_meta_access --waba 123 456  # specific WABAs
    python -m scripts.check_meta_access --from-db       # also compare with our DB rows

Inside the prod container:
    docker exec -it -e META_TOKEN='EAAG...' sil-wam-app python -m scripts.check_meta_access --from-db
"""
import argparse
import getpass
import os
import sys

import httpx

GRAPH = "https://graph.facebook.com/v21.0"
OUR_APP_ID = os.environ.get("FB_APP_ID") or "981233374512490"

OK, WARN, BAD = "  [OK]  ", "  [WARN]", "  [FAIL]"
problems: list[str] = []


def get(client: httpx.Client, path: str, token: str, **params):
    """GET a Graph path. Returns (json | None, error message | None)."""
    r = client.get(f"{GRAPH}/{path}", params={"access_token": token, **params})
    try:
        body = r.json()
    except ValueError:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    if r.status_code != 200 or "error" in body:
        e = body.get("error", {})
        return None, f"HTTP {r.status_code} code={e.get('code')} {e.get('message')}"
    return body, None


def fail(label: str, msg: str):
    print(f"{BAD} {label}: {msg}")
    problems.append(f"{label}: {msg}")


# ── 1. The token itself ──────────────────────────────────────────────────────

def check_token(client, token) -> list[str]:
    print("\n=== Token ===")
    me, err = get(client, "me", token, fields="id,name")
    if err:
        fail("token", err)
        return []
    print(f"{OK} belongs to: {me.get('name')} (id {me.get('id')})")

    dbg, err = get(client, "debug_token", token, input_token=token)
    if err:
        print(f"{WARN} debug_token unavailable ({err}) — pass --waba explicitly")
        return []
    d = dbg.get("data", {})
    print(f"{OK} app: {d.get('application')} (id {d.get('app_id')})")
    if str(d.get("app_id")) != OUR_APP_ID:
        print(f"{WARN} token was issued for app {d.get('app_id')}, not {OUR_APP_ID}")
    if d.get("is_valid"):
        exp = d.get("expires_at")
        print(f"{OK} expires: {'never' if not exp else exp}")
    else:
        fail("token", "Meta says this token is NOT valid")

    scopes = set(d.get("scopes", []))
    for need in ("whatsapp_business_messaging", "whatsapp_business_management"):
        if need in scopes:
            print(f"{OK} permission {need}")
        else:
            fail("token", f"missing permission {need}")
    if "business_management" not in scopes:
        print(f"{WARN} no business_management (only needed for billing/credit-line reads)")

    wabas: set[str] = set()
    for g in d.get("granular_scopes", []):
        if g.get("scope", "").startswith("whatsapp_business"):
            wabas.update(g.get("target_ids") or [])
    assigned, err = get(client, "me/assigned_whatsapp_business_accounts", token, fields="id,name")
    for a in (assigned or {}).get("data", []):
        wabas.add(a["id"])
        print(f"{OK} WABA assigned to this system user: {a.get('name')} ({a['id']})")
    if wabas:
        print(f"{OK} WABAs reachable: {', '.join(sorted(wabas))}")
    else:
        fail("token", "NO WhatsApp account is assigned to this system user — Business "
                      "Settings → System users → Assign assets → WhatsApp accounts")
    return sorted(wabas)


# ── 2. One WABA ──────────────────────────────────────────────────────────────

def check_waba(client, token, waba: str):
    print(f"\n=== WABA {waba} ===")
    info, err = get(client, waba, token,
                    fields="id,name,currency,timezone_id,account_review_status,"
                           "business_verification_status,owner_business_info")
    if err:
        fail(f"WABA {waba}", f"cannot read ({err}) — partner access or system-user "
                             "asset assignment missing")
        return
    owner = (info.get("owner_business_info") or {}).get("name")
    print(f"{OK} name: {info.get('name')} · owner: {owner} · currency: {info.get('currency')}")
    review = info.get("account_review_status")
    if review == "APPROVED":
        print(f"{OK} account review: {review}")
    else:
        fail(f"WABA {waba}", f"account review status is {review}")
    print(f"{OK} business verification: {info.get('business_verification_status')}")

    # Billing — the 131042 cause
    fund, err = get(client, waba, token, fields="primary_funding_id")
    if err:
        print(f"{WARN} billing not readable with this token ({err})")
    elif fund.get("primary_funding_id"):
        print(f"{OK} payment attached (funding id {fund['primary_funding_id']})")
    else:
        fail(f"WABA {waba}", "NO payment method / credit line attached → sends fail 131042")

    # Phone numbers
    phones, err = get(client, f"{waba}/phone_numbers", token,
                      fields="id,display_phone_number,verified_name,status,quality_rating,"
                             "messaging_limit_tier,name_status,code_verification_status")
    if err:
        fail(f"WABA {waba}", f"cannot list phone numbers ({err})")
    else:
        for p in phones.get("data", []):
            line = (f"phone {p.get('display_phone_number')} id={p.get('id')} "
                    f"'{p.get('verified_name')}' status={p.get('status')} "
                    f"quality={p.get('quality_rating')} tier={p.get('messaging_limit_tier')} "
                    f"name={p.get('name_status')}")
            if p.get("status") == "CONNECTED" and p.get("quality_rating") != "RED":
                print(f"{OK} {line}")
            else:
                fail(f"WABA {waba}", line)
        if not phones.get("data"):
            fail(f"WABA {waba}", "no phone numbers")

    # Templates
    tpls, err = get(client, f"{waba}/message_templates", token, fields="name,status", limit=100)
    if err:
        fail(f"WABA {waba}", f"cannot read templates ({err})")
    else:
        rows = tpls.get("data", [])
        approved = sum(1 for t in rows if t.get("status") == "APPROVED")
        print(f"{OK} templates: {approved} approved of {len(rows)} (first page)")

    # Webhook subscription
    subs, err = get(client, f"{waba}/subscribed_apps", token)
    if err:
        fail(f"WABA {waba}", f"cannot read subscribed_apps ({err})")
    else:
        apps = [(a.get("whatsapp_business_api_data") or {}) for a in subs.get("data", [])]
        ids = [str(a.get("id")) for a in apps]
        for a in apps:
            print(f"{OK} subscribed app: {a.get('name')} (id {a.get('id')})")
        if OUR_APP_ID not in ids:
            fail(f"WABA {waba}", f"our app {OUR_APP_ID} is NOT subscribed → no inbound "
                                 f"webhooks (fix: POST /{waba}/subscribed_apps)")
        others = [i for i in ids if i != OUR_APP_ID]
        if others:
            print(f"{WARN} other apps also subscribed: {', '.join(others)} — their webhooks "
                  "reach us only if their secret is in META_APP_SECRETS, else 403")


# ── 3. The app's webhook config (needs the app secret) ───────────────────────

def check_app(client):
    secret = os.environ.get("META_APP_SECRET")
    print(f"\n=== App {OUR_APP_ID} webhook ===")
    if not secret:
        print(f"{WARN} META_APP_SECRET not set — skipped (check in the developer console)")
        return
    subs, err = get(client, f"{OUR_APP_ID}/subscriptions", f"{OUR_APP_ID}|{secret}")
    if err:
        fail("app", err)
        return
    wa = [s for s in subs.get("data", []) if s.get("object") == "whatsapp_business_account"]
    if not wa:
        fail("app", "no whatsapp_business_account webhook subscription")
        return
    for s in wa:
        fields = {f.get("name") for f in s.get("fields", [])}
        print(f"{OK} callback: {s.get('callback_url')} active={s.get('active')}")
        for need in ("messages", "message_template_status_update", "phone_number_quality_update"):
            if need in fields:
                print(f"{OK} field {need}")
            elif need == "messages":
                fail("app", "webhook field `messages` NOT subscribed → no inbound at all")
            else:
                print(f"{WARN} field {need} not subscribed (account-health updates)")


# ── 4. Compare with our DB ───────────────────────────────────────────────────

def db_wabas() -> list[str]:
    from app.core.database import SessionLocal
    from app.models.company import Company
    from app.models.whatsapp import WhatsAppAccount

    db = SessionLocal()
    try:
        rows = (db.query(Company.name, WhatsAppAccount.waba_id,
                         WhatsAppAccount.phone_number_id, WhatsAppAccount.connection_status)
                .join(WhatsAppAccount, WhatsAppAccount.company_id == Company.id).all())
    finally:
        db.close()
    print("\n=== Our DB ===")
    for name, waba, pnid, status in rows:
        print(f"  {name}: waba={waba} phone_number_id={pnid} status={status}")
    return [w for _, w, _, _ in rows if w]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--waba", nargs="*", default=[], help="WABA ids to check")
    ap.add_argument("--from-db", action="store_true", help="also check WABAs stored in our DB")
    args = ap.parse_args()

    token = os.environ.get("META_TOKEN") or getpass.getpass("Meta access token: ").strip()
    if not token:
        sys.exit("no token")

    with httpx.Client(timeout=20) as client:
        granted = check_token(client, token)
        wabas = list(dict.fromkeys(args.waba or granted))
        if args.from_db:
            stored = db_wabas()
            for w in stored:
                if granted and w not in granted:
                    fail(f"WABA {w}", "in our DB but NOT granted to this token")
            wabas = list(dict.fromkeys(wabas + stored))
        for w in wabas:
            check_waba(client, token, w)
        check_app(client)

    print("\n=== Summary ===")
    if problems:
        for p in problems:
            print(f"{BAD} {p}")
        sys.exit(1)
    print(f"{OK} everything checked looks good")


if __name__ == "__main__":
    main()
