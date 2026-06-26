"""
Module 5 — Condition A expiry scheduler.

Tests call _expire_timed_out_services(db) directly — no scheduler thread needed.
All DB operations use the test session so results are immediately verifiable.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.models.conversation import MobileQueue, Service
from app.services.expiry_scheduler import _expire_timed_out_services
from tests.conftest import (
    make_company, make_conversation, make_queue_entry, make_service, make_wa_account,
)

_PAST = datetime.now(timezone.utc) - timedelta(hours=48)


def _expired_service(db, company_id, conv_id, questions=None, service_id=None, mobile="91TEST"):
    svc = make_service(
        db, conv_id, company_id,
        service_id=service_id,
        status="in_progress",
        questions=questions,
        created_at=_PAST,
        template_expiry_hours=24,
        mobile_no=mobile,
    )
    svc.data = {"customer_mobile": mobile}
    db.commit()
    make_queue_entry(db, svc, mobile_no=mobile, status="in_progress")
    return svc


class TestExpireTimedOutServices:
    def test_past_deadline_no_answers_gets_expired(self, db):
        comp = make_company(db, code="EXP01")
        conv = make_conversation(db, comp.id, "91EXPIRED")
        make_wa_account(db, comp.id)
        questions = [{"sequence": 1, "sent": 0, "field_key": "q1",
                      "question": "Rate?", "answer_type": 1}]
        svc = _expired_service(db, comp.id, conv.id, questions=questions, mobile="91EXPIRED")

        with patch("app.services.queue_manager.advance_queue"):
            count = _expire_timed_out_services(db)

        assert count == 1
        db.refresh(svc)
        assert svc.status == "expired"
        assert svc.expired_reason == "timeout"

    def test_queue_entry_completed_on_expiry(self, db):
        comp = make_company(db, code="EXP02")
        conv = make_conversation(db, comp.id, "91QCOMP")
        make_wa_account(db, comp.id)
        questions = [{"sequence": 1, "sent": 0, "field_key": "q1",
                      "question": "Q?", "answer_type": 2}]
        svc = _expired_service(db, comp.id, conv.id, questions=questions, mobile="91QCOMP")

        with patch("app.services.queue_manager.advance_queue"):
            _expire_timed_out_services(db)

        queue_entry = db.query(MobileQueue).filter(MobileQueue.service_id == svc.id).first()
        assert queue_entry.status == "completed"

    def test_past_deadline_with_answered_question_not_expired(self, db):
        comp = make_company(db, code="EXP03")
        conv = make_conversation(db, comp.id, "91ANSWERED")
        make_wa_account(db, comp.id)
        # sent=1 means button was tapped, flow started
        questions = [{"sequence": 1, "sent": 1, "field_key": "q1",
                      "question": "Rate?", "answer_type": 1}]
        svc = _expired_service(db, comp.id, conv.id, questions=questions, mobile="91ANSWERED")

        count = _expire_timed_out_services(db)

        assert count == 0
        db.refresh(svc)
        assert svc.status == "in_progress"  # not expired

    def test_within_deadline_not_expired(self, db):
        comp = make_company(db, code="EXP04")
        conv = make_conversation(db, comp.id, "91FRESH")
        make_wa_account(db, comp.id)
        questions = [{"sequence": 1, "sent": 0, "field_key": "q1",
                      "question": "Rate?", "answer_type": 2}]
        # Created just now — not past deadline
        svc = make_service(
            db, conv.id, comp.id,
            status="in_progress",
            questions=questions,
            template_expiry_hours=24,
            mobile_no="91FRESH",
        )
        make_queue_entry(db, svc, mobile_no="91FRESH", status="in_progress")

        count = _expire_timed_out_services(db)

        assert count == 0
        db.refresh(svc)
        assert svc.status == "in_progress"

    def test_template_only_service_skipped(self, db):
        comp = make_company(db, code="EXP05")
        conv = make_conversation(db, comp.id, "91TMPLONLY")
        make_wa_account(db, comp.id)
        # questions=None → template-only, should be skipped
        svc = _expired_service(db, comp.id, conv.id, questions=None, mobile="91TMPLONLY")

        count = _expire_timed_out_services(db)

        assert count == 0
        db.refresh(svc)
        assert svc.status == "in_progress"  # not touched

    def test_empty_questions_list_skipped(self, db):
        comp = make_company(db, code="EXP06")
        conv = make_conversation(db, comp.id, "91EMPTYQ")
        make_wa_account(db, comp.id)
        svc = _expired_service(db, comp.id, conv.id, questions=[], mobile="91EMPTYQ")

        count = _expire_timed_out_services(db)

        assert count == 0

    def test_multiple_services_only_eligible_expired(self, db):
        comp = make_company(db, code="EXP07")
        conv = make_conversation(db, comp.id, "91MULTI")
        make_wa_account(db, comp.id)
        questions = [{"sequence": 1, "sent": 0, "field_key": "q1",
                      "question": "Q?", "answer_type": 2}]

        # One expired, one fresh
        svc_old = _expired_service(db, comp.id, conv.id, questions=questions,
                                   service_id="OLD-SVC", mobile="91MULTI")
        # Need a different conversation for second service (different mobile to avoid queue conflict)
        conv2 = make_conversation(db, comp.id, "91MULTIFRESH")
        svc_new = make_service(
            db, conv2.id, comp.id,
            service_id="NEW-SVC",
            status="in_progress",
            questions=questions,
            template_expiry_hours=24,
            mobile_no="91MULTIFRESH",
        )
        make_queue_entry(db, svc_new, mobile_no="91MULTIFRESH", status="in_progress")

        with patch("app.services.queue_manager.advance_queue"):
            count = _expire_timed_out_services(db)

        assert count == 1
        db.refresh(svc_old)
        db.refresh(svc_new)
        assert svc_old.status == "expired"
        assert svc_new.status == "in_progress"

    def test_advance_queue_called_after_expiry(self, db):
        comp = make_company(db, code="EXP08")
        conv = make_conversation(db, comp.id, "91ADVANCE")
        make_wa_account(db, comp.id)
        questions = [{"sequence": 1, "sent": 0, "field_key": "q1",
                      "question": "Q?", "answer_type": 2}]
        _expired_service(db, comp.id, conv.id, questions=questions, mobile="91ADVANCE")

        with patch("app.services.queue_manager.advance_queue") as mock_adv:
            _expire_timed_out_services(db)

        mock_adv.assert_called_once()
