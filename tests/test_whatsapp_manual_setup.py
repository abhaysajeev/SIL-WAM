"""
manual_setup and refresh_whatsapp_status must subscribe the app to the WABA
(POST /{waba_id}/subscribed_apps), otherwise inbound webhook events (button
taps, replies) never arrive even though outbound sends work fine — the
bug that caused FACSRP110033's questionnaire to never fire.

Patches "httpx.Client" (the constructor reference), not "httpx.Client.get" /
".post" (methods on the shared class) — FastAPI's TestClient is itself built
on httpx.Client, so mutating the class in place would also hijack the test
client's own outer request to the app. Patching the constructor only affects
fresh httpx.Client(...) calls made after the patch (i.e. the ones inside
whatsapp_api.py), leaving the already-constructed TestClient instance alone.
"""
from unittest.mock import MagicMock, patch

from app.utils.whatsapp_crypto import encrypt_token
from tests.conftest import make_wa_account


def _mock_httpx_client(get_json=None, get_status=200, post_status=200, post_text='{"success":true}'):
    """A stand-in for `with httpx.Client(...) as client: client.get(...); client.post(...)`."""
    get_resp = MagicMock(status_code=get_status)
    get_resp.json.return_value = get_json or {"id": "test-id"}
    post_resp = MagicMock(status_code=post_status, text=post_text)

    instance = MagicMock()
    instance.__enter__.return_value = instance
    instance.__exit__.return_value = False
    instance.get.return_value = get_resp
    instance.post.return_value = post_resp
    return instance


def _subscribe_calls(instance):
    return [c for c in instance.post.call_args_list if "subscribed_apps" in c.args[0]]


class TestManualSetupSubscribes:
    def test_subscribes_waba_on_successful_setup(self, client, sa, company):
        mock_instance = _mock_httpx_client()
        with patch("httpx.Client", return_value=mock_instance):
            r = client.post(
                f"/api/whatsapp/{company['id']}/manual-setup",
                json={"waba_id": "test-waba-123", "access_token": "test-token-abc"},
                headers={"Authorization": f"Bearer {sa}"},
            )

        assert r.status_code == 200
        calls = _subscribe_calls(mock_instance)
        assert len(calls) == 1
        assert calls[0].args[0] == "https://graph.facebook.com/v22.0/test-waba-123/subscribed_apps"
        assert calls[0].kwargs["headers"]["Authorization"] == "Bearer test-token-abc"

    def test_setup_still_succeeds_if_subscription_call_fails(self, client, sa, company):
        mock_instance = _mock_httpx_client(post_status=400, post_text='{"error":"nope"}')
        with patch("httpx.Client", return_value=mock_instance):
            r = client.post(
                f"/api/whatsapp/{company['id']}/manual-setup",
                json={"waba_id": "test-waba-456", "access_token": "test-token-def"},
                headers={"Authorization": f"Bearer {sa}"},
            )

        # Subscription failing must not block saving otherwise-valid credentials.
        assert r.status_code == 200
        assert r.json()["success"] is True


class TestRefreshResubscribes:
    def test_refresh_resubscribes_when_account_becomes_active(self, client, sa, company, db):
        acc = make_wa_account(
            db, company["id"],
            phone_number_id="1234567890",
            access_token_encrypted=encrypt_token("real-token-xyz"),
        )
        acc.waba_id = "test-waba"
        db.commit()

        mock_instance = _mock_httpx_client(get_json={"id": "1234567890", "status": "CONNECTED"})
        with patch("httpx.Client", return_value=mock_instance):
            r = client.post(
                f"/api/whatsapp/{company['id']}/refresh",
                headers={"Authorization": f"Bearer {sa}"},
            )

        assert r.status_code == 200
        assert r.json()["account"]["connection_status"] == "active"
        calls = _subscribe_calls(mock_instance)
        assert len(calls) == 1
        assert calls[0].args[0] == "https://graph.facebook.com/v22.0/test-waba/subscribed_apps"

    def test_refresh_does_not_resubscribe_when_account_errors(self, client, sa, company, db):
        make_wa_account(
            db, company["id"],
            phone_number_id="1234567890",
            access_token_encrypted=encrypt_token("real-token-xyz"),
        )

        mock_instance = _mock_httpx_client(get_status=400, get_json={"error": {"message": "boom", "code": 1}})
        with patch("httpx.Client", return_value=mock_instance):
            r = client.post(
                f"/api/whatsapp/{company['id']}/refresh",
                headers={"Authorization": f"Bearer {sa}"},
            )

        assert r.status_code == 200
        assert r.json()["account"]["connection_status"] == "error"
        assert mock_instance.post.call_count == 0
