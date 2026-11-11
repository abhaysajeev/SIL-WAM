"""
The agent API's error taxonomy — every failure the app can be told about, in one place.

Two rules govern what a caller learns, and the line between them is authentication:

  BEFORE credentials are proven, the response must not reveal whether an account
  exists. `invalid_credentials` is returned for an unknown username and for a wrong
  password, with identical wording, and auth.py runs a dummy bcrypt verify on the
  unknown-username path so the timing matches too.

  AFTER credentials are proven, be specific. Someone holding a valid password for
  their own account is entitled to know why they still cannot get in, and every
  failure an agent actually hits in the field — wrong licence, wrong handset — is on
  this side of the line. A vague message there is a support call.

Errors are shaped `{"detail": {"code": ..., "message": ...}}`. The app branches on
`code`; `message` is written to be shown to the person holding the phone.
"""
from fastapi import HTTPException, status


def _err(http_status: int, code: str, message: str, **extra) -> HTTPException:
    detail = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=http_status, detail=detail)


# ── Before authentication ─────────────────────────────────────────────────────

def invalid_credentials() -> HTTPException:
    """Unknown username OR wrong password — deliberately indistinguishable."""
    return _err(
        status.HTTP_401_UNAUTHORIZED,
        "invalid_credentials",
        "Username or password is incorrect.",
    )


# ── After authentication ──────────────────────────────────────────────────────

def account_locked(minutes: int) -> HTTPException:
    return _err(
        status.HTTP_403_FORBIDDEN,
        "account_locked",
        f"Too many failed attempts. Try again in {minutes} minute(s).",
        retry_after_minutes=minutes,
    )


def account_disabled() -> HTTPException:
    return _err(
        status.HTTP_403_FORBIDDEN,
        "account_disabled",
        "This account has been disabled. Contact your administrator.",
    )


def not_an_agent() -> HTTPException:
    """Valid user, but no agent configuration — or it has been switched off."""
    return _err(
        status.HTTP_403_FORBIDDEN,
        "not_an_agent",
        "This login is not set up for the agent app. Contact your administrator.",
    )


def licence_mismatch() -> HTTPException:
    return _err(
        status.HTTP_403_FORBIDDEN,
        "licence_mismatch",
        "This customer ID does not match the one registered for your account.",
    )


def device_mismatch(bound_device_id: str) -> HTTPException:
    """The agent is bound to a different handset."""
    return _err(
        status.HTTP_403_FORBIDDEN,
        "device_mismatch",
        "Your account is registered to a different device. "
        "Ask your administrator to release it before signing in here.",
        registered_device_id=bound_device_id,
    )


def device_already_registered() -> HTTPException:
    """
    This handset belongs to somebody else.

    The other agent is deliberately not named: the caller has valid credentials for
    their own account, which is not a right to learn who else works here. The admin
    sees the detail in the panel.
    """
    return _err(
        status.HTTP_403_FORBIDDEN,
        "device_already_registered",
        "This device is already registered to another agent. "
        "Contact your administrator.",
    )


# ── Session-time (every authenticated request) ────────────────────────────────

def invalid_session() -> HTTPException:
    """
    The single outcome of every session failure — expired, revoked, unknown token,
    wrong or missing device header, agent switched off, user disabled.

    One code on purpose. Because there is no refresh to attempt, a 401 means exactly
    one thing to the app: drop the stored token and show the login screen. Splitting
    it would invite the client to branch on states it cannot act on differently.
    """
    return _err(
        status.HTTP_401_UNAUTHORIZED,
        "invalid_session",
        "Your session has ended. Please sign in again.",
    )


# ── Sending ───────────────────────────────────────────────────────────────────

def window_closed(last_inbound_at=None) -> HTTPException:
    """
    Free text was attempted outside WhatsApp's 24-hour service window.

    The single most important error in this file to get right, because the app will
    hit it legitimately every day. WhatsApp allows free text only within 24 hours of
    the customer's **last inbound message**; outside it, approved templates only.

    Sending never opens that window — only the customer writing to us does. An app
    that starts a 24-hour timer when a template goes out will offer a composer here
    and be refused, so the message says which event the clock actually runs from.
    """
    return _err(
        status.HTTP_409_CONFLICT,
        "window_closed",
        "You can only send a free message within 24 hours of the customer's last "
        "message. Send an approved template instead.",
        last_inbound_at=last_inbound_at.isoformat() if last_inbound_at else None,
    )


def recipient_opted_out() -> HTTPException:
    """
    A hard block, never a warning.

    Messaging someone who unsubscribed is a compliance breach and drives down the
    quality rating for the whole business number — which degrades delivery for every
    other agent on it, not just the one who sent it.
    """
    return _err(
        status.HTTP_403_FORBIDDEN,
        "recipient_opted_out",
        "This contact has unsubscribed and cannot be messaged.",
    )


def recipient_cap_exceeded(count: int, cap: int) -> HTTPException:
    return _err(
        status.HTTP_403_FORBIDDEN,
        "recipient_cap_exceeded",
        f"This would message {count} people, above the {cap} you may send to at once. "
        f"Choose a smaller phonebook or ask your administrator to raise the limit.",
        recipient_count=count,
        limit=cap,
    )


def daily_limit_reached(sent: int, limit: int) -> HTTPException:
    return _err(
        status.HTTP_403_FORBIDDEN,
        "daily_limit_reached",
        f"You have sent {sent} messages in the last 24 hours, reaching your limit of "
        f"{limit}. This resets gradually rather than at midnight.",
        sent_today=sent,
        limit=limit,
    )


def not_found(what: str) -> HTTPException:
    """A thread, phonebook or template that does not exist for this agent."""
    return _err(
        status.HTTP_404_NOT_FOUND,
        "not_found",
        f"That {what} was not found.",
    )


def send_failed(reason: str) -> HTTPException:
    """
    Meta rejected the send. Distinct from a 500: nothing is broken here, and the
    agent may well be able to act on it.
    """
    return _err(
        status.HTTP_502_BAD_GATEWAY,
        "send_failed",
        f"WhatsApp could not send this message. {reason}".strip(),
    )


def no_whatsapp_account() -> HTTPException:
    return _err(
        status.HTTP_409_CONFLICT,
        "no_whatsapp_account",
        "Your company has no WhatsApp number configured. Contact your administrator.",
    )


def param_count_mismatch(expected: int, supplied: int) -> HTTPException:
    """
    Almost always a stale `param_count` cached by the app.

    Worth its own code and its own wording: "invalid request" would send the agent to
    their administrator for something a refresh fixes, and a template's placeholder
    count genuinely does change when it is re-approved at Meta.
    """
    return _err(
        status.HTTP_400_BAD_REQUEST,
        "param_count_mismatch",
        f"This template needs {expected} value(s) and {supplied} were supplied. "
        f"Refresh your templates and try again.",
        expected=expected, supplied=supplied,
    )


def build_failed(message: str) -> HTTPException:
    """The recipient list could not be assembled — see broadcast_builder.BuildError."""
    return _err(status.HTTP_400_BAD_REQUEST, "build_failed", message)


def nothing_to_send() -> HTTPException:
    """
    Every contact was screened out. Not an error in the system — an answer about the
    list, and the agent needs to be told rather than shown a campaign that sends to
    nobody.
    """
    return _err(
        status.HTTP_409_CONFLICT,
        "nothing_to_send",
        "Every contact in this phonebook is unsubscribed or unreachable.",
    )


def nothing_selected() -> HTTPException:
    """
    An explicit but empty selection.

    Refused rather than read as "send to everyone". The app sends `contact_ids` only
    when the agent has been ticking rows, so an empty array means a bug in that screen
    — and the generous interpretation of a bug there is a broadcast to the whole
    phonebook. Omit the field entirely to mean "all".
    """
    return _err(
        status.HTTP_400_BAD_REQUEST,
        "nothing_selected",
        "No contacts were selected. Choose at least one, or send to the whole phonebook.",
    )


def unknown_contacts(missing: list[str]) -> HTTPException:
    """
    Some selected contacts are not in that phonebook any more.

    Almost always a stale list on the handset — an administrator edited the book after
    the app last synced. Refused as a whole rather than sent to the survivors: quietly
    dropping people would tell the agent they had messaged fifty when they had messaged
    forty-seven, and nothing on the screen would ever say otherwise.
    """
    return _err(
        status.HTTP_400_BAD_REQUEST,
        "unknown_contacts",
        f"{len(missing)} selected contact(s) are no longer in this phonebook. "
        f"Refresh your phonebooks and try again.",
        contact_ids=missing[:50],
        count=len(missing),
    )


# ── Phonebooks an agent builds ─────────────────────────────────────────────

def no_company() -> HTTPException:
    """A phonebook always belongs to a company, and this agent has none."""
    return _err(
        status.HTTP_403_FORBIDDEN,
        "no_company",
        "Your account is not linked to a company, so it cannot create phonebooks. "
        "Contact your administrator.",
    )


def phonebook_read_only() -> HTTPException:
    """Mapped to this agent, but made by an administrator — readable, not editable."""
    return _err(
        status.HTTP_403_FORBIDDEN,
        "phonebook_read_only",
        "This phonebook was set up by your administrator. You can use it, but only "
        "phonebooks you created can be changed from the app.",
    )


def phonebook_name_taken(name: str) -> HTTPException:
    return _err(
        status.HTTP_409_CONFLICT,
        "phonebook_name_taken",
        f"Your company already has a phonebook called '{name}'. Choose another name.",
    )


def phonebook_full(current: int, adding: int, limit: int) -> HTTPException:
    return _err(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "phonebook_full",
        f"A phonebook holds at most {limit} contacts. This one has {current}, and "
        f"adding {adding} would go over. Start a new phonebook instead.",
        contact_count=current,
        adding=adding,
        limit=limit,
    )


def contact_exists(mobile_no: str) -> HTTPException:
    return _err(
        status.HTTP_409_CONFLICT,
        "contact_exists",
        f"{mobile_no} is already in this phonebook.",
    )


def invalid_contact(reason: str) -> HTTPException:
    return _err(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "invalid_contact",
        reason,
    )
