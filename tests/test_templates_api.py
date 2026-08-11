"""
Template creation, editing and mapping — with media headers.

The component builder had no coverage at all before media headers were added, and
it is the piece that talks to Meta about templates that are already live. The
regression these tests exist for: an edit that does not mention the header must
never strip an approved image off a template.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.api.whatsapp_api import _build_components
from app.core import template_body
from app.models.whatsapp import WhatsAppTemplate
from app.schemas.whatsapp import TemplateCreateRequest, TemplateUpdateRequest
from tests.conftest import (
    login, make_company, make_role, make_user, make_wa_account, make_wa_template,
)

_HANDLE = "4:sample:handle=="


def _create(**kw):
    base = {"name": "t_x", "category": "UTILITY", "body_text": "Hello {{1}}"}
    base.update(kw)
    return TemplateCreateRequest(**base)


def _update(**kw):
    base = {"body_text": "Hello {{1}}"}
    base.update(kw)
    return TemplateUpdateRequest(**base)


def _header_of(components):
    return next((c for c in components if c["type"] == "HEADER"), None)


class TestBuildComponents:
    """Pure component-array construction — no Meta, no DB."""

    def test_text_header_unchanged(self):
        comps = _build_components(_create(header_text="Hi there"))
        assert _header_of(comps) == {"type": "HEADER", "format": "TEXT", "text": "Hi there"}

    def test_no_header(self):
        assert _header_of(_build_components(_create())) is None

    def test_media_header_carries_the_sample_handle(self):
        comps = _build_components(_create(header_format="IMAGE", header_handle=_HANDLE))
        assert _header_of(comps) == {
            "type": "HEADER", "format": "IMAGE",
            "example": {"header_handle": [_HANDLE]},
        }

    def test_media_header_needs_a_sample_on_create(self):
        # Rejected by the schema, before any round trip to Meta.
        with pytest.raises(ValueError):
            _create(header_format="IMAGE")

    def test_header_precedes_body(self):
        # Not enforced by Meta, but the payload should read in message order.
        comps = _build_components(_create(header_format="IMAGE", header_handle=_HANDLE))
        assert [c["type"] for c in comps][:2] == ["HEADER", "BODY"]


class TestEditPreservesMediaHeader:
    """
    The data-loss regression. Before media headers existed the builder rebuilt
    components from the form alone, so any edit of a media template PATCHed a
    header-less array to Meta and destroyed the header on an approved template.
    """

    def setup_method(self):
        self.existing = _build_components(_create(header_format="IMAGE", header_handle=_HANDLE))

    def test_edit_without_a_new_sample_keeps_the_approved_one(self):
        comps = _build_components(_update(header_format="IMAGE", body_text="New text"), self.existing)
        assert _header_of(comps)["example"] == {"header_handle": [_HANDLE]}
        assert template_body.body_text(comps) == "New text"

    def test_edit_that_never_mentions_the_header_still_keeps_it(self):
        # An older cached build of the page, or any client that predates
        # header_format, sends no header field at all. Silence must mean
        # "unchanged", not "delete".
        comps = _build_components(_update(body_text="New text"), self.existing)
        assert _header_of(comps)["format"] == "IMAGE"

    def test_uploading_a_new_sample_replaces_it(self):
        comps = _build_components(
            _update(header_format="IMAGE", header_handle="4:replacement=="), self.existing
        )
        assert _header_of(comps)["example"] == {"header_handle": ["4:replacement=="]}

    def test_switching_format_without_a_sample_is_rejected(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            _build_components(_update(header_format="VIDEO"), self.existing)
        assert exc.value.status_code == 400

    def test_a_text_header_is_still_removable(self):
        # Media headers are sticky, text ones are not — removing a text header is
        # how the builder's header toggle has always worked.
        text_tpl = _build_components(_create(header_text="Hi"))
        assert _header_of(_build_components(_update(body_text="x"), text_tpl)) is None


class TestHeaderFormat:
    def test_reads_media_format(self):
        comps = _build_components(_create(header_format="DOCUMENT", header_handle=_HANDLE))
        assert template_body.header_format(comps) == "DOCUMENT"
        assert template_body.is_media_header(comps) is True

    def test_text_header_is_not_media(self):
        comps = _build_components(_create(header_text="Hi"))
        assert template_body.header_format(comps) == "TEXT"
        assert template_body.is_media_header(comps) is False

    def test_no_header(self):
        assert template_body.header_format([]) is None
        assert template_body.is_media_header(None) is False

    def test_lowercase_format_survives_normalisation(self):
        # Components travel through both our builder and Meta's sync; don't trust case.
        assert template_body.header_format([{"type": "header", "format": "image"}]) == "IMAGE"


# ── HTTP-level ────────────────────────────────────────────────────────────────

def _admin(db, client):
    make_role(db, name="admin", display_name="Admin",
              pages=["companies"], actions=["read", "write", "create", "delete"])
    make_user(db, username="tpladmin", role_name="admin")
    comp = make_company(db, code="TPL")
    acc = make_wa_account(db, comp.id)
    acc.connection_status = "active"
    acc.waba_id = "test-waba"
    db.commit()
    token = login(client, "tpladmin")["access_token"]
    return comp, {"Authorization": f"Bearer {token}"}


class TestMediaHandleUpload:
    def test_rejects_an_unsupported_type_before_calling_meta(self, client, db):
        comp, headers = _admin(db, client)
        with patch("app.services.meta_media.upload_resumable") as up:
            r = client.post(
                f"/api/whatsapp/{comp.id}/templates/media-handle",
                files={"file": ("notes.txt", b"hello", "text/plain")},
                headers=headers,
            )
        assert r.status_code == 400
        assert "Unsupported file type" in r.json()["detail"]
        up.assert_not_called()

    def test_returns_the_handle_and_inferred_format(self, client, db):
        comp, headers = _admin(db, client)
        with patch("app.services.meta_media.upload_resumable", return_value=_HANDLE):
            r = client.post(
                f"/api/whatsapp/{comp.id}/templates/media-handle",
                files={"file": ("receipt.png", b"\x89PNG fake bytes", "image/png")},
                headers=headers,
            )
        assert r.status_code == 200
        assert r.json() == {"handle": _HANDLE, "format": "IMAGE", "filename": "receipt.png"}

    def test_oversized_image_is_rejected_with_the_limit_named(self, client, db):
        comp, headers = _admin(db, client)
        r = client.post(
            f"/api/whatsapp/{comp.id}/templates/media-handle",
            files={"file": ("big.png", b"x" * (5 * 1024 * 1024 + 1), "image/png")},
            headers=headers,
        )
        assert r.status_code == 400
        assert "5 MB" in r.json()["detail"]


class TestMappingRoundTrip:
    def test_header_mapping_saves_and_is_returned(self, client, db):
        comp, headers = _admin(db, client)
        tpl = make_wa_template(db, comp.id, name="with_image")

        r = client.patch(
            f"/api/whatsapp/{comp.id}/templates/{tpl.id}/mapping",
            json={"header_mapping": "data.receipt_image_url"},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["header_mapping"] == "data.receipt_image_url"
        db.refresh(tpl)
        assert tpl.header_mapping == "data.receipt_image_url"

    def test_blank_clears_it(self, client, db):
        comp, headers = _admin(db, client)
        tpl = make_wa_template(db, comp.id, name="with_image")
        tpl.header_mapping = "data.old_path"
        db.commit()

        client.patch(
            f"/api/whatsapp/{comp.id}/templates/{tpl.id}/mapping",
            json={"header_mapping": ""},
            headers=headers,
        )
        db.refresh(tpl)
        assert tpl.header_mapping is None

    def test_omitting_it_leaves_it_alone(self, client, db):
        # The mapping PATCH is used for four independent mappings; saving one
        # must not blank the others.
        comp, headers = _admin(db, client)
        tpl = make_wa_template(db, comp.id, name="with_image")
        tpl.header_mapping = "data.keep_me"
        db.commit()

        client.patch(
            f"/api/whatsapp/{comp.id}/templates/{tpl.id}/mapping",
            json={"param_mapping": {"1": "data.name"}},
            headers=headers,
        )
        db.refresh(tpl)
        assert tpl.header_mapping == "data.keep_me"


class TestUpdateTemplateSendsPreservedHeader:
    def test_editing_body_text_does_not_strip_the_image_from_meta(self, client, db):
        """The end-to-end version of the regression, through the real route."""
        comp, headers = _admin(db, client)
        tpl = make_wa_template(db, comp.id, name="image_tpl")
        tpl.meta_template_id = "meta-123"
        tpl.components = _build_components(
            _create(header_format="IMAGE", header_handle=_HANDLE)
        )
        db.commit()

        meta_res = MagicMock(status_code=200)
        meta_res.json.return_value = {"status": "PENDING"}
        # Patch inside the module rather than httpx globally — TestClient is itself
        # an httpx client, and a global patch takes it down with Meta.
        with patch("app.api.whatsapp_api.httpx.Client") as mock_client, \
             patch("app.api.whatsapp_api.decrypt_token", return_value="tok"):
            mock_client.return_value.__enter__.return_value.post.return_value = meta_res
            r = client.put(
                f"/api/whatsapp/{comp.id}/templates/{tpl.id}",
                json={"body_text": "Rewritten body"},
                headers=headers,
            )
            assert r.status_code == 200
            sent = mock_client.return_value.__enter__.return_value.post.call_args.kwargs["json"]

        header = _header_of(sent["components"])
        assert header is not None, "the image header was dropped on edit"
        assert header["example"] == {"header_handle": [_HANDLE]}
