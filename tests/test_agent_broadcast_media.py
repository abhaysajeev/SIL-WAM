"""
Phase 2 — standard broadcasts from the device, at parity with the web panel.

  2a  several phonebooks in one broadcast; a number in two books is messaged once
  2b  a media-header template takes ONE uploaded file, used for every recipient
  2d  the agent phonebook ceiling is the company's Broadcast setting
  2e  GET /limits reports every ceiling before the agent hits it
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.agent import AgentPhonebook
from app.models.agent_timeline import AgentMessage, AgentThread
from app.models.broadcast_campaign import BroadcastCampaign, BroadcastRecipient
from app.models.media_asset import MediaAsset
from app.models.phonebook import Phonebook, PhonebookContact
from app.services import agent_timeline, media_assets, media_store
from tests.test_agent_send import BROADCAST, _agent_headers, _settings, _setup

MEDIA = "/agent-api/v1/media"
LIMITS = "/agent-api/v1/limits"

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 2048
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048
PDF = b"%PDF-1.4\n" + b"\x00" * 2048


@pytest.fixture(autouse=True)
def local_media(tmp_path):
    media_store.set_store(media_store.local_store(str(tmp_path)))
    yield tmp_path
    media_store.set_store(None)


@pytest.fixture()
def meta_upload():
    with patch("app.services.media_assets.meta_media.upload_media",
               return_value="meta-media-1") as up:
        yield up


def _second_book(client, db, comp, user, hdr, pb, numbers):
    pb2 = Phonebook(company_id=comp.id, name="Second")
    db.add(pb2)
    db.flush()
    for i, n in enumerate(numbers):
        db.add(PhonebookContact(phonebook_id=pb2.id, mobile_no=n, customer_name=f"B{i}"))
    db.commit()
    db.add(AgentPhonebook(agent_id=_agent_row(db, user), phonebook_id=pb2.id))
    db.commit()
    return pb2


def _agent_row(db, user):
    from app.models.agent import Agent
    return db.query(Agent).filter(Agent.user_id == user.id).one().id


def _image_template(db, tpl):
    tpl.components = [{"type": "HEADER", "format": "IMAGE"},
                      {"type": "BODY", "text": "Hi {{1}}, offer on {{2}}."}]
    db.commit()


def _upload(client, h, data=JPEG, name="offer.jpg", mime="image/jpeg"):
    return client.post(MEDIA, headers=h, files={"file": (name, data, mime)})


# ── 2a: several phonebooks ───────────────────────────────────────────────────

class TestSeveralPhonebooks:
    def test_two_books_deduplicated_into_one_campaign(self, client, db):
        comp, _a, tpl, pb, user, hdr = _setup(client, db, "MB1", contacts=3)
        # The first book holds 916666000000..002; the second repeats one of them in
        # a different spelling, plus one new number.
        pb2 = _second_book(client, db, comp, user, hdr, pb,
                           ["+91 66660 00000", "917025985366"])
        h = _agent_headers(client)
        r = client.post(BROADCAST, headers=h, json={
            "phonebook_ids": [str(pb.id), str(pb2.id)], "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"]})
        assert r.status_code == 201, r.text
        assert r.json()["total"] == 4
        camp = db.query(BroadcastCampaign).filter_by(id=r.json()["campaign_id"]).one()
        assert camp.name.startswith("List MB1 +1")

    def test_old_single_phonebook_field_still_works(self, client, db):
        comp, _a, tpl, pb, *_ = _setup(client, db, "MB2", contacts=2)
        r = client.post(BROADCAST, headers=_agent_headers(client), json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id), "params": ["a", "b"]})
        assert r.status_code == 201 and r.json()["total"] == 2

    def test_one_unmapped_book_refuses_the_whole_request(self, client, db):
        comp, _a, tpl, pb, *_ = _setup(client, db, "MB3")
        other = Phonebook(company_id=comp.id, name="Not mapped")
        db.add(other)
        db.commit()
        r = client.post(BROADCAST, headers=_agent_headers(client), json={
            "phonebook_ids": [str(pb.id), str(other.id)], "template_id": str(tpl.id),
            "params": ["a", "b"]})
        assert r.status_code == 404
        assert db.query(BroadcastCampaign).filter_by(company_id=comp.id).count() == 0

    def test_cap_applies_to_the_deduplicated_total(self, client, db):
        comp, _a, tpl, pb, user, hdr = _setup(client, db, "MB4", contacts=2)
        pb2 = _second_book(client, db, comp, user, hdr, pb, ["917025985366"])
        _settings(db, comp.id, agent_max_recipients_per_send=2)
        r = client.post(BROADCAST, headers=_agent_headers(client), json={
            "phonebook_ids": [str(pb.id), str(pb2.id)], "template_id": str(tpl.id),
            "params": ["a", "b"]})
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "recipient_cap_exceeded"

    def test_selected_contacts_across_books(self, client, db):
        comp, _a, tpl, pb, user, hdr = _setup(client, db, "MB5", contacts=3)
        pb2 = _second_book(client, db, comp, user, hdr, pb, ["917025985366"])
        c1 = db.query(PhonebookContact).filter_by(phonebook_id=pb.id).first()
        c2 = db.query(PhonebookContact).filter_by(phonebook_id=pb2.id).first()
        r = client.post(BROADCAST, headers=_agent_headers(client), json={
            "phonebook_ids": [str(pb.id), str(pb2.id)], "contact_ids": [str(c1.id), str(c2.id)],
            "template_id": str(tpl.id), "params": ["a", "b"]})
        assert r.status_code == 201 and r.json()["total"] == 2

    def test_too_many_books(self, client, db):
        comp, _a, tpl, pb, *_ = _setup(client, db, "MB6")
        ids = [str(pb.id)] + [f"00000000-0000-0000-0000-{i:012d}" for i in range(25)]
        r = client.post(BROADCAST, headers=_agent_headers(client), json={
            "phonebook_ids": ids, "template_id": str(tpl.id), "params": ["a", "b"]})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "too_many_phonebooks"


# ── 2b: media ────────────────────────────────────────────────────────────────

class TestMediaUpload:
    def test_upload_stores_once_and_uploads_once(self, client, db, meta_upload, local_media):
        _setup(client, db, "MU1")
        r = _upload(client, _agent_headers(client))
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["format"] == "IMAGE" and body["mime"] == "image/jpeg"
        meta_upload.assert_called_once()
        asset = db.query(MediaAsset).one()
        assert asset.meta_media_id == "meta-media-1"
        assert list(local_media.rglob("*.jpg"))

    def test_type_is_read_from_the_bytes_not_the_label(self, client, db, meta_upload):
        _setup(client, db, "MU2")
        r = _upload(client, _agent_headers(client), data=b"not really an image" * 50,
                    name="x.jpg", mime="image/jpeg")
        assert r.status_code == 415
        assert r.json()["detail"]["code"] == "unsupported_media_type"
        meta_upload.assert_not_called()

    def test_image_over_5mb_refused(self, client, db, meta_upload):
        _setup(client, db, "MU3")
        big = b"\xff\xd8\xff\xe0" + b"\x00" * (6 * 1024 * 1024)
        r = _upload(client, _agent_headers(client), data=big)
        assert r.status_code == 413
        meta_upload.assert_not_called()

    def test_meta_failure_leaves_nothing_behind(self, client, db, local_media):
        from app.services import meta_media
        _setup(client, db, "MU4")
        with patch("app.services.media_assets.meta_media.upload_media",
                   side_effect=meta_media.MediaUploadError("Meta said no")):
            r = _upload(client, _agent_headers(client))
        assert r.status_code == 502
        assert db.query(MediaAsset).count() == 0
        assert not list(local_media.rglob("*.jpg"))


class TestMediaTemplateBroadcast:
    def test_one_asset_serves_every_recipient(self, client, db, meta_upload):
        comp, _a, tpl, pb, *_ = _setup(client, db, "MT1", contacts=3)
        _image_template(db, tpl)
        h = _agent_headers(client)
        mid = _upload(client, h).json()["media_id"]
        r = client.post(BROADCAST, headers=h, json={
            "phonebook_ids": [str(pb.id)], "template_id": str(tpl.id),
            "params": ["a", "b"], "header_media": {"media_id": mid}})
        assert r.status_code == 201, r.text
        camp = db.query(BroadcastCampaign).filter_by(id=r.json()["campaign_id"]).one()
        assert camp.header_media_mode == "shared"
        assert camp.header_media["media_id"] == "meta-media-1"
        assert camp.header_media["asset_id"] == mid
        assert meta_upload.call_count == 1          # uploaded once, at /media

    def test_media_template_without_a_file_is_refused(self, client, db):
        comp, _a, tpl, pb, *_ = _setup(client, db, "MT2")
        _image_template(db, tpl)
        r = client.post(BROADCAST, headers=_agent_headers(client), json={
            "phonebook_ids": [str(pb.id)], "template_id": str(tpl.id), "params": ["a", "b"]})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "header_media_required"
        assert r.json()["detail"]["format"] == "IMAGE"

    def test_wrong_kind_of_file_is_refused(self, client, db, meta_upload):
        comp, _a, tpl, pb, *_ = _setup(client, db, "MT3")
        _image_template(db, tpl)
        h = _agent_headers(client)
        mid = _upload(client, h, data=PDF, name="a.pdf", mime="application/pdf").json()["media_id"]
        r = client.post(BROADCAST, headers=h, json={
            "phonebook_ids": [str(pb.id)], "template_id": str(tpl.id),
            "params": ["a", "b"], "header_media": {"media_id": mid}})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "header_media_mismatch"

    def test_file_for_a_text_template_is_refused(self, client, db, meta_upload):
        comp, _a, tpl, pb, *_ = _setup(client, db, "MT4")
        h = _agent_headers(client)
        mid = _upload(client, h).json()["media_id"]
        r = client.post(BROADCAST, headers=h, json={
            "phonebook_ids": [str(pb.id)], "template_id": str(tpl.id),
            "params": ["a", "b"], "header_media": {"media_id": mid}})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "header_media_not_allowed"

    def test_expired_file_is_refused(self, client, db, meta_upload):
        comp, _a, tpl, pb, *_ = _setup(client, db, "MT5")
        _image_template(db, tpl)
        h = _agent_headers(client)
        mid = _upload(client, h).json()["media_id"]
        asset = db.query(MediaAsset).one()
        asset.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        r = client.post(BROADCAST, headers=h, json={
            "phonebook_ids": [str(pb.id)], "template_id": str(tpl.id),
            "params": ["a", "b"], "header_media": {"media_id": mid}})
        assert r.status_code == 410
        assert r.json()["detail"]["code"] == "media_expired"

    def test_another_companys_file_is_not_found(self, client, db, meta_upload):
        comp, _a, tpl, pb, *_ = _setup(client, db, "MT6")
        _image_template(db, tpl)
        foreign = MediaAsset(company_id=_setup_other_company(db), format="IMAGE",
                             mime="image/jpeg", size_bytes=1, sha256="0" * 64,
                             storage_key="x", meta_media_id="m",
                             expires_at=datetime.now(timezone.utc) + timedelta(days=1))
        db.add(foreign)
        db.commit()
        r = client.post(BROADCAST, headers=_agent_headers(client), json={
            "phonebook_ids": [str(pb.id)], "template_id": str(tpl.id),
            "params": ["a", "b"], "header_media": {"media_id": str(foreign.id)}})
        assert r.status_code == 404

    def test_meta_id_near_expiry_is_refreshed_from_our_copy(self, client, db, meta_upload):
        comp, _a, tpl, pb, *_ = _setup(client, db, "MT7")
        _image_template(db, tpl)
        h = _agent_headers(client)
        mid = _upload(client, h).json()["media_id"]
        asset = db.query(MediaAsset).one()
        asset.meta_media_expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
        db.commit()
        meta_upload.return_value = "meta-media-2"
        r = client.post(BROADCAST, headers=h, json={
            "phonebook_ids": [str(pb.id)], "template_id": str(tpl.id),
            "params": ["a", "b"], "header_media": {"media_id": mid}})
        assert r.status_code == 201, r.text
        camp = db.query(BroadcastCampaign).filter_by(id=r.json()["campaign_id"]).one()
        assert camp.header_media["media_id"] == "meta-media-2"


def _setup_other_company(db):
    from tests.conftest import make_company
    return make_company(db, code="OTHERCO").id


class TestMediaDownload:
    def test_uploader_can_download(self, client, db, meta_upload):
        _setup(client, db, "MD1")
        h = _agent_headers(client)
        mid = _upload(client, h).json()["media_id"]
        r = client.get(f"{MEDIA}/{mid}", headers=h)
        assert r.status_code == 200
        assert r.content == JPEG
        assert r.headers["content-type"] == "image/jpeg"
        assert "private" in r.headers["cache-control"]

    def test_unknown_and_foreign_ids_are_not_found(self, client, db, meta_upload):
        _setup(client, db, "MD2")
        h = _agent_headers(client)
        assert client.get(f"{MEDIA}/not-a-uuid", headers=h).status_code == 404
        assert client.get(f"{MEDIA}/00000000-0000-0000-0000-000000000999",
                          headers=h).status_code == 404

    def test_deleted_file_is_gone(self, client, db, meta_upload, local_media):
        _setup(client, db, "MD3")
        h = _agent_headers(client)
        mid = _upload(client, h).json()["media_id"]
        asset = db.query(MediaAsset).one()
        asset.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        assert media_assets.expire_due(db) == 1
        assert not list(local_media.rglob("*.jpg"))
        r = client.get(f"{MEDIA}/{mid}", headers=h)
        assert r.status_code == 410

    def test_file_shown_in_a_company_chat_is_viewable(self, client, db, meta_upload):
        """The broadcast bubble carries asset_id; an agent opening that chat can see it."""
        comp, *_ = _setup(client, db, "MD4")
        h = _agent_headers(client)
        mid = _upload(client, h).json()["media_id"]
        db.query(MediaAsset).update({"uploaded_by_agent_id": None})
        db.commit()
        assert client.get(f"{MEDIA}/{mid}", headers=h).status_code == 404
        agent_timeline.record_outbound(db, comp.id, "918075571893", kind="broadcast",
                                       media={"format": "image", "asset_id": mid})
        db.commit()
        assert client.get(f"{MEDIA}/{mid}", headers=h).status_code == 200


def test_scheduler_carries_asset_id_to_the_timeline():
    from app.services.broadcast_scheduler import _header_media_for

    class C:
        header_media_mode = "shared"
        header_media = {"format": "IMAGE", "media_id": "m1", "asset_id": "a1",
                        "filename": "x.jpg"}
    out = _header_media_for(C(), None, None)
    assert out == {"format": "image", "media_id": "m1", "filename": "x.jpg", "asset_id": "a1"}


# ── 2d / 2e ──────────────────────────────────────────────────────────────────

class TestLimits:
    def test_defaults(self, client, db):
        _setup(client, db, "LM1")
        r = client.get(LIMITS, headers=_agent_headers(client))
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["max_recipients_per_send"] == 100
        assert b["max_contacts_per_phonebook"] == 2000
        assert b["max_phonebooks_per_broadcast"] == 20
        assert b["default_country"] == "IN"
        assert {m["format"] for m in b["media"]} == {"IMAGE", "VIDEO", "DOCUMENT"}
        assert set(b["import_formats"]) == {"xlsx", "csv"}

    def test_reflects_company_settings(self, client, db):
        comp, *_ = _setup(client, db, "LM2")
        _settings(db, comp.id, agent_max_recipients_per_send=7,
                  agent_max_contacts_per_phonebook=300, default_country="AE")
        b = client.get(LIMITS, headers=_agent_headers(client)).json()
        assert (b["max_recipients_per_send"], b["max_contacts_per_phonebook"],
                b["default_country"]) == (7, 300, "AE")


class TestBroadcastSettingsApi:
    def test_new_fields_round_trip_and_validate(self, client, db):
        from tests.test_agent_send import _admin
        from tests.conftest import make_company
        hdr = _admin(client, db, "bs_admin")
        comp = make_company(db, code="BSAPI")
        r = client.post("/api/broadcast-settings/", headers=hdr, json={
            "company_id": str(comp.id), "agent_max_contacts_per_phonebook": 500,
            "default_country": "ae"})
        assert r.status_code in (200, 201), r.text
        assert r.json()["default_country"] == "AE"
        assert r.json()["agent_max_contacts_per_phonebook"] == 500
        sid = r.json()["id"]
        bad = client.put(f"/api/broadcast-settings/{sid}", headers=hdr,
                         json={"agent_max_contacts_per_phonebook": 50000})
        assert bad.status_code == 422
        bad = client.put(f"/api/broadcast-settings/{sid}", headers=hdr,
                         json={"default_country": "XX"})
        assert bad.status_code == 422
