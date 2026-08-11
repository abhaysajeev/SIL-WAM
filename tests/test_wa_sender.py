"""
The template payload wa_sender builds for Meta.

Everything else mocks send_template out, so the shape of the JSON that actually
reaches the Graph API is otherwise untested. Media headers made that gap worth
closing: the header component nests the format name twice, which is easy to get
wrong and impossible to notice without looking at the request body.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services import wa_sender


@pytest.fixture()
def sent_payload():
    """Call send_template with a mocked Graph API and hand back the posted JSON."""
    def _send(**kwargs):
        account = MagicMock(phone_number_id="PNID", access_token_encrypted="enc")
        template = MagicMock(name="tpl", language="en_US")
        template.name = "order_confirm"

        res = MagicMock(status_code=200)
        res.json.return_value = {"messages": [{"id": "wamid.1"}]}

        with patch("app.services.wa_sender.httpx.Client") as client, \
             patch("app.services.wa_sender.decrypt_token", return_value="tok"):
            client.return_value.__enter__.return_value.post.return_value = res
            result = wa_sender.send_template(
                account, template, kwargs.pop("body_params", ["Ravi"]),
                "919876543210", **kwargs
            )
            posted = client.return_value.__enter__.return_value.post.call_args.kwargs["json"]
        assert result.ok
        return posted

    return _send


def _component(payload, ctype):
    return next((c for c in payload["template"]["components"] if c["type"] == ctype), None)


class TestNoHeader:
    def test_payload_is_unchanged_without_header_media(self, sent_payload):
        # The regression guard for every existing client — SFA, ERPNext, broadcasts.
        payload = sent_payload()
        assert _component(payload, "header") is None
        assert _component(payload, "body")["parameters"] == [{"type": "text", "text": "Ravi"}]

    def test_none_header_media_is_the_same_as_omitting_it(self, sent_payload):
        assert sent_payload(header_media=None) == sent_payload()


class TestMediaHeader:
    def test_image_header(self, sent_payload):
        payload = sent_payload(header_media={"format": "image", "link": "https://x/r.png"})
        assert _component(payload, "header")["parameters"] == [
            {"type": "image", "image": {"link": "https://x/r.png"}}
        ]

    def test_header_comes_before_body(self, sent_payload):
        payload = sent_payload(header_media={"format": "image", "link": "https://x/r.png"})
        assert [c["type"] for c in payload["template"]["components"]] == ["header", "body"]

    def test_document_carries_its_filename(self, sent_payload):
        # Without the filename the customer sees the URL's last path segment as the
        # document title, which is usually an opaque id.
        payload = sent_payload(header_media={
            "format": "document", "link": "https://x/inv.pdf", "filename": "Invoice 10234.pdf",
        })
        assert _component(payload, "header")["parameters"] == [
            {"type": "document",
             "document": {"link": "https://x/inv.pdf", "filename": "Invoice 10234.pdf"}}
        ]

    def test_filename_is_only_for_documents(self, sent_payload):
        payload = sent_payload(header_media={
            "format": "image", "link": "https://x/r.png", "filename": "ignored.png",
        })
        assert _component(payload, "header")["parameters"][0]["image"] == {"link": "https://x/r.png"}

    def test_video_header(self, sent_payload):
        payload = sent_payload(header_media={"format": "video", "link": "https://x/c.mp4"})
        assert _component(payload, "header")["parameters"] == [
            {"type": "video", "video": {"link": "https://x/c.mp4"}}
        ]

    def test_a_blank_link_sends_no_header_at_all(self, sent_payload):
        # Ingest rejects these, but a header component with an empty link would be
        # rejected by Meta for the whole message — dropping it is the safer failure.
        payload = sent_payload(header_media={"format": "image", "link": ""})
        assert _component(payload, "header") is None

    def test_header_and_cta_coexist(self, sent_payload):
        payload = sent_payload(
            header_media={"format": "image", "link": "https://x/r.png"},
            cta_urls={"0": "https://x/track"},
        )
        types = [c["type"] for c in payload["template"]["components"]]
        assert types == ["header", "body", "button"]
