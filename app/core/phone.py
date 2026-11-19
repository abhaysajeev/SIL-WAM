"""
One canonical form for a customer's WhatsApp number.

**Canonical = E.164 digits without the '+'**, e.g. `918075571893`. That is exactly what
Meta sends as `from` / `wa_id` / `recipient_id`, so a number we stored and a number Meta
reports compare equal as plain strings — which is what keys a customer's chat thread.

Why this exists: numbers used to be checked only for being 7–15 digits. A phonebook row
typed as `8075571893` and Meta's reply from `918075571893` were two different strings, so
the broadcast and the customer's answer landed in two different threads and the reply
never reached the agent who sent the broadcast.

A number without a country code is read in the company's default country
(`broadcast_settings.default_country`, ISO 3166 alpha-2, default IN).

Deliberately NOT used by the Lizo/Shirin client APIs or the Conversation service flows:
those have their own, working normalisers and live order traffic.
"""
import re

import phonenumbers
from phonenumbers import NumberParseException

DEFAULT_REGION = "IN"

_SEPARATORS = re.compile(r"[\s\-(). /]")
_SCIENTIFIC = re.compile(r"^\d(\.\d+)?[eE]\+?\d+$")


class InvalidPhone(ValueError):
    """The value cannot be read as a WhatsApp number. The message is written for the
    person who typed it."""


def _valid(num) -> bool:
    return num is not None and phonenumbers.is_valid_number(num)


def _try(text: str, region: str | None):
    try:
        return phonenumbers.parse(text, region)
    except NumberParseException:
        return None


def normalize(raw, default_region: str | None = DEFAULT_REGION) -> str:
    """
    Return the canonical form of `raw`, or raise InvalidPhone.

    Rules, in order:
      1. '+…' or '00…' is international — the country code is already there.
      2. Bare digits that start with the default country's code and are valid read that
         way are already international (`918075571893` in IN).
      3. Otherwise bare digits valid in the default country are national
         (`8075571893` → `918075571893`; `0501234567` in AE → `971501234567`).
      4. Otherwise bare digits valid as international are taken as such
         (`971501234567` typed for an IN company).
    """
    region = (default_region or DEFAULT_REGION).upper()
    if raw is None:
        raise InvalidPhone("WhatsApp number is required")
    text = str(raw).strip()
    if not text:
        raise InvalidPhone("WhatsApp number is required")

    if _SCIENTIFIC.match(text.replace(",", "")):
        raise InvalidPhone(
            f"'{text}' was changed by Excel into a rounded number — format the WhatsApp "
            f"Number column as Text and type the number again")

    compact = _SEPARATORS.sub("", text)
    if compact.startswith("00"):
        compact = "+" + compact[2:]

    if compact.startswith("+"):
        num = _try(compact, None)
        if _valid(num):
            return _canonical(num)
        raise InvalidPhone(f"'{text}' is not a valid WhatsApp number")

    if not compact.isdigit():
        raise InvalidPhone(f"'{text}' is not a valid WhatsApp number — use digits only")

    as_intl = _try("+" + compact, None)
    country_code = str(phonenumbers.country_code_for_region(region) or "")
    if country_code and compact.startswith(country_code) and _valid(as_intl):
        return _canonical(as_intl)

    as_national = _try(compact, region)
    if _valid(as_national):
        return _canonical(as_national)

    if _valid(as_intl):
        return _canonical(as_intl)

    raise InvalidPhone(
        f"'{text}' is not a valid WhatsApp number — include the country code, "
        f"e.g. 91 for India or 971 for the UAE")


def _canonical(num) -> str:
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164).lstrip("+")


def try_normalize(raw, default_region: str | None = DEFAULT_REGION) -> str | None:
    """normalize(), but None instead of raising — for paths that skip bad rows."""
    try:
        return normalize(raw, default_region)
    except InvalidPhone:
        return None


def is_supported_region(code: str | None) -> bool:
    return bool(code) and code.upper() in phonenumbers.SUPPORTED_REGIONS


def company_region(db, company_id) -> str:
    """The default country configured for this company (Broadcast settings)."""
    from app.core import broadcast_config     # local: broadcast_config imports this module
    return broadcast_config.get(db, company_id).default_country


def normalize_for_company(db, company_id, raw) -> str:
    """normalize() in the company's default country. Raises InvalidPhone."""
    return normalize(raw, company_region(db, company_id))
