"""
Lizo's order approval — tells SFA the customer accepted the order.

    POST http://.../api/Sfa/ApproveOrder
    {"Credentials": {...}, "RequestData": {CompanyID, UserID, SearchText, ...}}

Fired when the Confirm Order button on the template is tapped. Distinct from
app/lizo/notify.py, which reports *delivery status* to SaveWhatsAppOrderStatus: this
is the business action, the thing that actually marks the order approved in SFA's
own system. A confirmed order sends both — the approval, then the Confirmed status.

Keyed on `order_no`, not on our reference_id: SFA looks the order up by their own
number, through the generic SearchText field their list endpoints all share.

Why this is queued rather than called inline
--------------------------------------------
The tap arrives on Meta's inbound webhook, which must return promptly — Meta retries
a slow webhook, and a blocking POST to SFA would hold the request open for as long as
their server takes to answer, or time out and lose the approval entirely.

So this writes an OutboundNotification row, exactly as notify.py does.
notify_scheduler is payload- and URL-agnostic — it POSTs whatever JSONB is in the row
to whatever URL is in the row — so the approval inherits its 8 retries with backoff
to an hour, and survives a restart mid-flight. The only thing that differs from a
status callback is the URL, which comes from settings rather than the API key.

Import discipline matches notify.py: models and config only, no service-layer
imports, so conversation_engine and inbound can import this without a cycle.
"""
import logging

from sqlalchemy.orm import Session

from app.core.config import settings
from app.lizo import sfa
from app.lizo.notify import handles
from app.models.outbound_notification import OutboundNotification
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

# Keys Lizo sends inside `data`, carried through ingest untouched and read back here.
# Case-sensitive and exact: SFA's own field names for two of them, so they arrive
# spelled the way SFA spells them rather than being renamed on the way in.
ORDER_NO_KEY   = "order_no"
USER_ID_KEY    = "UserID"
COMPANY_ID_KEY = "CompanyID"

# Types are SFA's, and are not negotiable per field — CompanyID must be a JSON number
# and UserID a JSON string, even though Lizo's own `data` could hold either as either.
# Coercing here means a quoted "5" from the client still reaches SFA as 5.


def extract(data: dict | None) -> tuple[dict | None, list[str]]:
    """
    Pull and type-coerce the three fields ApproveOrder needs out of a `data` block.

    Returns (fields, problems). `fields` is None whenever `problems` is non-empty.
    Every offending field is reported, not just the first — a client fixing their
    payload should see the whole list in one response rather than one per attempt.

    Shared by two callers so the rule is declared once: app/lizo/validation.py runs
    it at ingest to turn a bad payload into a 422, and emit() below runs it at tap
    time as the safety net for anything that got in before, or around, that check.
    """
    data = data or {}
    problems: list[str] = []

    order_no = str(data.get(ORDER_NO_KEY, "") or "").strip()
    if not order_no:
        problems.append(f"'{ORDER_NO_KEY}' is missing or empty")

    user_id = str(data.get(USER_ID_KEY, "") or "").strip()
    if not user_id:
        problems.append(f"'{USER_ID_KEY}' is missing or empty")

    raw_company = data.get(COMPANY_ID_KEY)
    company_id = None
    if raw_company is None or (isinstance(raw_company, str) and not raw_company.strip()):
        problems.append(f"'{COMPANY_ID_KEY}' is missing or empty")
    elif isinstance(raw_company, bool):
        # int(True) is 1 — a silent, wrong company. Refuse rather than coerce.
        problems.append(f"'{COMPANY_ID_KEY}' must be a whole number, got a boolean")
    else:
        try:
            company_id = int(raw_company)
        except (TypeError, ValueError):
            problems.append(f"'{COMPANY_ID_KEY}' must be a whole number, got {raw_company!r}")

    if problems:
        return None, problems

    return {
        ORDER_NO_KEY:   order_no,
        USER_ID_KEY:    user_id,
        COMPANY_ID_KEY: company_id,
    }, []


def emit(db: Session, service) -> None:
    """
    Queue the ApproveOrder call for a confirmed service. Never raises.

    A problem here must not roll back the confirmation that triggered it: the
    customer has tapped the button, `lizo_confirmed_at` is stamped, and the Confirmed
    status callback still has to go out. So every failure path logs and returns.

    Called once per service — inbound._confirm's `lizo_confirmed_at` guard means a
    re-tap never reaches here, so SFA is never asked to approve the same order twice.
    """
    if not handles(service):
        return

    if not settings.LIZO_APPROVE_ORDER_URL:
        # Not configured — same inert-hook behaviour as a missing notify_url. Logged
        # at warning because for a Lizo service this is almost certainly a misconfigured
        # deployment rather than a deliberate opt-out.
        logger.warning(
            "Lizo approve: LIZO_APPROVE_ORDER_URL is not set — order not approved in SFA "
            "(service=%s)", service.service_id,
        )
        return

    fields, problems = extract(service.data)
    if problems:
        # Reachable for services created before these keys were required at ingest,
        # or if Lizo's payload shape drifts. The order simply is not approved — which
        # is why the ingest check exists, so this stays a safety net rather than the
        # first line of defence.
        log_error(
            "Lizo approve: order cannot be approved, required field(s) missing",
            "lizo.approve.emit",
            ValueError("; ".join(problems)),
            request_data={"service_id": service.service_id, "reference_id": str(service.id)},
        )
        logger.error(
            "Lizo approve: skipping ApproveOrder for service=%s — %s",
            service.service_id, "; ".join(problems),
        )
        return

    db.add(OutboundNotification(
        service_id      = service.id,
        service_attempt = service.attempt_no or 0,
        # Not tied to one Message: the approval is about the order, not about the
        # button-tap message that happened to trigger it.
        message_id      = None,
        notify_url      = settings.LIZO_APPROVE_ORDER_URL,
        payload         = _build_payload(fields),
    ))
    logger.info(
        "Lizo approve queued: service=%s order_no=%s user=%s company=%s",
        service.service_id, fields[ORDER_NO_KEY], fields[USER_ID_KEY], fields[COMPANY_ID_KEY],
    )


def _build_payload(fields: dict) -> dict:
    """
    SFA's ApproveOrder envelope, exactly as they specified it.

    Every key they listed is sent, including the ones that are always empty. Their
    RequestData is one shared shape across all their list/action endpoints — most of
    it is search and paging fields that mean nothing here — and sending the full block
    is what they asked for, so a missing key can never be the reason a call is refused.
    """
    company_id = fields[COMPANY_ID_KEY]
    user_id    = fields[USER_ID_KEY]

    return {
        "CheckSum": 0,
        "Operation": 0,
        "Credentials": sfa.credentials(
            sfa.SERVICE_APPROVE_ORDER,
            company_id    = company_id,
            login_user_id = user_id,
        ),
        "RequestData": {
            "CheckSum": 0,
            "Operation": 0,
            "CompanyID": company_id,               # int
            "UserID": user_id,                     # string
            "FactoryID": "",
            "CustomerID": "",
            "ColumnIndex": 0,
            "sortingOrder": 0,
            "pageNumber": 0,
            "pageSize": 0,
            "SearchType": 0,
            "StatusID": 0,
            # The order number. SFA finds the order to approve through this field.
            "SearchText": fields[ORDER_NO_KEY],    # string
            "SearchText1": "",
            "SearchText2": "",
            "SearchText3": "",
            "SearchText4": "",
            "SearchText5": "",
            "SearchText6": "",
            "SearchWord": "",
            "SearchId1": 0,
            "SearchId2": 0,
            "SearchId3": 0,
            "SearchId4": 0,
            "SearchId5": 0,
            "SearchApprovalDate": "",
            "SearchFromDate": "",
            "SearchToDate": "",
            "HierarchyID": "",
            "RouteID": "",
            "DeliveryRouteID": "",
            "IsDistributor": 0,
            "DistributorID": "",
        },
    }
