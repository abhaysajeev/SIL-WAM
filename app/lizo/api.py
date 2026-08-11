"""
Lizo's client-facing ingest endpoint.

Thin on behaviour, opinionated on contract. Auth, template lookup, phone
normalisation, param resolution, queueing, sending, delivery tracking and retry are
all the shared pipeline's job — this only reshapes the body and hands it over, so a
fix there reaches Lizo without being copied.

What is *not* shared is the response format. Every reply from this endpoint uses
Lizo's four-key envelope (see responses.py), rendered by LizoRoute. Shirin Asal's
/client-api/v1/services is untouched and keeps FastAPI's default `{"detail": …}`.

    POST /client-api/v1/lizo/orders
    X-API-Key: <lizo's key>
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.client_services_api import ingest_service
from app.core.database import get_db
from app.core.deps import get_api_key_and_company
from app.lizo.responses import (
    STATUS_DUPLICATE_SERVICE_ID, LizoOrderResponse, failure, success,
)
from app.lizo.route import LizoRoute
from app.lizo.schemas import LizoOrderRequest
from app.lizo.validation import check_approval_fields, check_resolvable
from app.models.conversation import Service
from app.models.whatsapp import WhatsAppTemplate

router = APIRouter(prefix="/client-api/v1/lizo", tags=["Lizo"], route_class=LizoRoute)


@router.post("/orders", response_model=LizoOrderResponse, status_code=201)
def ingest_lizo_order(
    payload:         LizoOrderRequest,
    api_key_company: tuple   = Depends(get_api_key_and_company),
    db:              Session = Depends(get_db),
):
    """
    Accept a Lizo order and hand it to the shared ingest path.

    ingest_service is a plain function — its Depends(...) arguments are only
    resolved when FastAPI itself calls it, so passing the already-resolved key,
    company and session here bypasses them. The same dependencies are declared
    on this route, which is what actually performs the auth.
    """
    _api_key, company = api_key_company

    # Duplicate check runs here rather than being left to the shared pipeline, which
    # fetches the clashing row and discards it. Lizo gets the existing reference_id
    # back, so a client whose POST succeeded but whose response never arrived —
    # timeout, reset, restart — can reconcile on retry instead of being stuck with
    # an order it cannot identify. Doing it here also means client_services_api's
    # own 409 message stays exactly as Shirin Asal receives it today.
    existing = db.query(Service).filter(
        Service.service_id == payload.service_id,
        Service.company_id == company.id,
    ).first()
    if existing:
        return failure(
            409,
            f"service_id '{payload.service_id}' already exists for this company",
            status       = STATUS_DUPLICATE_SERVICE_ID,
            service_id   = payload.service_id,
            reference_id = existing.id,
        )

    # Build the reshaped envelope once — checked against *that*, not payload.data,
    # because to_ingest_request injects customer_mobile, so a mapping pointing at it
    # would otherwise look unresolvable here and resolve fine at send time.
    ingest_payload = payload.to_ingest_request()

    # The fields Confirm Order will need to approve the order in SFA. Checked before
    # the template lookup so a payload missing them is rejected even when the template
    # is the other thing that is wrong — and checked at all because these are not read
    # until the customer taps, long after this response was accepted as a success.
    problem = check_approval_fields(ingest_payload.data)
    if problem:
        status, message = problem
        return failure(422, message, status=status, service_id=payload.service_id)

    # Meta refuses a template send with any blank parameter, and does so on the
    # background scheduler long after the client has its response. Check it here so
    # a missing field is a 422 the client can act on rather than three silent retry
    # attempts and a delivery that never happens. The template is looked up again
    # inside ingest_service — a cheap indexed query, and the price of leaving
    # client_services_api untouched.
    template = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.name       == payload.template_name,
        WhatsAppTemplate.company_id == company.id,
        WhatsAppTemplate.status     == "APPROVED",
    ).first()

    if template:
        problem = check_resolvable(ingest_payload.data, payload.service_id, template)
        if problem:
            status, message = problem
            return failure(422, message, status=status, service_id=payload.service_id)
    # A missing template is left to ingest_service, which owns the 404.

    result = ingest_service(
        payload         = ingest_payload,
        api_key_company = api_key_company,
        db              = db,
    )
    return success(result.service_id, result.reference_id)
