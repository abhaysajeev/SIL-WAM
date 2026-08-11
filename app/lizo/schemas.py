"""
Liso's request payloads, and their translation into the shared ingest envelope.

Two endpoints:

    LizoOrderRequest    POST /client-api/v1/lizo/orders     the order message
    LizoPaymentRequest  POST /client-api/v1/lizo/payments   payment confirmation

Both keep the same shape: a few fixed top-level fields, plus a `data` dictionary of
arbitrary shape. Nothing here inspects `data` beyond refusing reserved keys — which
field ends up in which template placeholder is decided later, per template, through
the mapping UI. Declaring Liso's current fields in this file would mean a code change
every time SFA adds a use case, which is exactly what the dynamic contract avoids.

The only reshaping done here is copying the fixed fields into `data`, because that is
where the shared pipeline and the mapping dot-paths look for them.
"""
from typing import Any

from pydantic import BaseModel, field_validator

from app.schemas.service import ServiceIngestRequest, _validate_mobile

# Keys the platform controls. Any other key in `data` is passed through untouched.
#
#   questions        the shared ingest lifts this key out of `data` and turns the
#                    service into a questionnaire (client_services_api.py:108).
#                    Lizo has no questionnaire, so the order would never
#                    auto-complete — and a non-list value raises AttributeError,
#                    surfacing as a 500.
#   customer_mobile  injected from the top-level field. Two sources of truth that
#                    disagree is ambiguous, so refuse rather than silently pick one.
#   _flow            our marker (see FLOW_MARKER). It is what tells the shared
#                    inbound handler that a button tap belongs to Lizo, so a client
#                    must not be able to set or spoof it.
#
# Refusing beats overwriting: Client X gets told at once instead of discovering it
# from a customer who never received a message.
_RESERVED_DATA_KEYS = ("questions", "customer_mobile", "_flow")

# Stamped onto Service.data at ingest, read back by app/lizo/inbound.handles().
#
# Service.data is already the per-row behaviour switch the conversation engine
# reads (completion_message, invoice_no, pdf_sent), so this needs no migration and
# no new column. It is never echoed to clients — notify_queue builds its payload
# from Service columns, not from data.
FLOW_MARKER_KEY   = "_flow"
FLOW_MARKER_VALUE = "lizo_order"

# The payment confirmation flow. A separate value on purpose: the order flow's marker
# routes button taps into the two-step confirm and, from there, SFA's ApproveOrder. A
# payment has nothing to approve, so it must never answer to that marker.
FLOW_PAYMENT_VALUE = "lizo_payment"

# Every flow this client owns. Used by the guards in inbound.py and notify.py, which
# must claim *all* Liso services — otherwise a payment falls through to the shared
# questionnaire path and to notify_queue, which would post Shirin Asal's payload shape
# to Liso's endpoint. Which flow it is decides what actually happens.
FLOW_VALUES = (FLOW_MARKER_VALUE, FLOW_PAYMENT_VALUE)

# Meta reads `to` as a full international number. A country code is 1–3 digits and
# no national number is shorter than 8, so anything under 11 digits is missing its
# country code — India's 91 + 10 digits makes 12.
_MIN_MOBILE_DIGITS = 11

# Deliberately the same sentence the shared validator uses, with the bounds
# corrected and the country code named, so every bad number — too short, too long,
# malformed, or missing its country code — reads identically to the client.
_MOBILE_ERROR = (
    "customer_mobile '{value}' is not a valid phone number "
    "(must be 11–15 digits including the country code, optional leading +, "
    "no spaces or dashes)"
)


def normalise_liso_mobile(v: str) -> str:
    """
    Validate at Liso's own boundary so a bad number is a 422.

    ServiceIngestRequest applies the shared rule to data.customer_mobile, but that
    runs inside the route rather than while parsing the request, and a pydantic error
    raised there surfaces as a 500. Failing first keeps the inner validator a no-op.

    Liso's rule is stricter than the shared one: it also requires a country code. The
    shared validator accepts 7–15 digits, so a bare 10-digit Indian mobile passes, we
    return 201, and the send then fails at Meta with 131026 — invisible to the client.
    Rejecting it here turns a silent delivery failure into an obvious rejection while
    their developer is still testing.

    A module function rather than a method on one request, so both endpoints share it
    and cannot drift on what a valid number is.
    """
    try:
        normalised = _validate_mobile(v)
    except ValueError:
        # Re-raise with Liso's wording rather than the shared "7–15 digits" one, so
        # every rejected number reads identically to this client.
        raise ValueError(_MOBILE_ERROR.format(value=v.strip())) from None

    if len(normalised) < _MIN_MOBILE_DIGITS:
        raise ValueError(_MOBILE_ERROR.format(value=v.strip()))
    return normalised


class LizoOrderRequest(BaseModel):
    service_id:            str
    template_name:         str
    template_expiry_hours: int = 24
    customer_mobile:       str
    # Opaque by design — any shape, any depth. Mapped to template parameters
    # through the UI, not through code.
    data:                  dict[str, Any]

    @field_validator("customer_mobile")
    @classmethod
    def normalise_mobile(cls, v: str) -> str:
        return normalise_liso_mobile(v)

    @field_validator("data")
    @classmethod
    def reject_reserved_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        clash = [k for k in _RESERVED_DATA_KEYS if k in v]
        if clash:
            raise ValueError(
                f"reserved key(s) not allowed inside data: {', '.join(clash)}. "
                "customer_mobile is sent as a top-level field; questions is not "
                "supported on this endpoint."
            )
        return v

    def to_ingest_request(self) -> ServiceIngestRequest:
        """Reshape into the envelope the shared pipeline understands."""
        # Copy so the caller's dict is not mutated — this object may be logged
        # or reused after the call.
        data = dict(self.data)
        data["customer_mobile"] = self.customer_mobile
        # Marks this service as Lizo's for the shared inbound handler. Services
        # from any other client never carry it, so that branch is dead code for them.
        data[FLOW_MARKER_KEY] = FLOW_MARKER_VALUE

        return ServiceIngestRequest(
            service_id            = self.service_id,
            template_name         = self.template_name,
            template_expiry_hours = self.template_expiry_hours,
            data                  = data,
        )


# The fields SFA sends on a payment confirmation. Top-level rather than buried in
# `data` so pydantic validates them: a missing one is an immediate 422 naming the
# field, instead of a template send that reaches Meta with a blank parameter.
#
# They are copied into `data` on the way through, exactly as customer_mobile is, so
# the mapping UI addresses them as data.customer_name / data.order_no /
# data.net_amount like every other field.
_PAYMENT_FIELD_KEYS = ("customer_name", "order_no", "net_amount")

# Reserved inside `data` for the payment endpoint: the four fixed fields plus the
# platform's own keys. Two sources of truth that could disagree is ambiguous, so
# refuse rather than silently pick one — same rule as _RESERVED_DATA_KEYS.
_PAYMENT_RESERVED_KEYS = _RESERVED_DATA_KEYS + _PAYMENT_FIELD_KEYS


class LizoPaymentRequest(BaseModel):
    """
    A payment confirmation. Deliberately a separate contract from LizoOrderRequest.

    An order carries the fields Confirm Order needs to approve it in SFA (UserID,
    CompanyID); a payment has nothing to approve and should not be asked for them.
    And its flow marker keeps button taps away from the approval path entirely — see
    FLOW_PAYMENT_VALUE.
    """
    service_id:            str
    template_name:         str
    template_expiry_hours: int = 24
    customer_mobile:       str
    customer_name:         str
    order_no:              str
    # Typed str on purpose. On the orders endpoint amounts live inside opaque `data`,
    # so 222.0 is accepted and reaches the customer as "₹222.0" with no error — the
    # trap documented in docs/LIZO_API.md. Here a numeric is rejected outright, and
    # the client sends the string exactly as it should be displayed.
    net_amount:            str
    # Anything else SFA wants to send now or later, mapped per template in the UI.
    data:                  dict[str, Any]

    @field_validator("customer_mobile")
    @classmethod
    def normalise_mobile(cls, v: str) -> str:
        return normalise_liso_mobile(v)

    @field_validator("customer_name", "order_no", "net_amount")
    @classmethod
    def reject_blank(cls, v: str, info) -> str:
        # Meta refuses the whole message if any template parameter is blank, so a
        # whitespace-only value would fail at send time with nobody watching.
        if not v.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return v.strip()

    @field_validator("data")
    @classmethod
    def reject_reserved_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        clash = [k for k in _PAYMENT_RESERVED_KEYS if k in v]
        if clash:
            raise ValueError(
                f"reserved key(s) not allowed inside data: {', '.join(clash)}. "
                "customer_mobile, customer_name, order_no and net_amount are sent as "
                "top-level fields; questions is not supported on this endpoint."
            )
        return v

    def to_ingest_request(self) -> ServiceIngestRequest:
        """Reshape into the envelope the shared pipeline understands."""
        # Copy so the caller's dict is not mutated — this object may be logged or
        # reused after the call.
        data = dict(self.data)
        data["customer_mobile"] = self.customer_mobile
        for key in _PAYMENT_FIELD_KEYS:
            data[key] = getattr(self, key)
        data[FLOW_MARKER_KEY] = FLOW_PAYMENT_VALUE

        return ServiceIngestRequest(
            service_id            = self.service_id,
            template_name         = self.template_name,
            template_expiry_hours = self.template_expiry_hours,
            data                  = data,
        )
