"""
Fuzzy mapping of spreadsheet column headers onto our own fields.

Same idea as Frappe's data-import tool: the user uploads a CSV whose headers are
whatever their system happens to call things ("Cust Name", "WA No.", "Mobile"),
and we guess which of our fields each one belongs to so they only have to
correct the guesses rather than map everything by hand.

Matching runs in four passes, strongest first, so a confident hit is never
displaced by a weaker one:

  1. exact match on the normalised field name        "mobile_no"   -> mobile_no
  2. exact match on a known alias                    "whatsapp no" -> mobile_no
  3. containment either way                          "customer mobile number" -> mobile_no
  4. difflib similarity above a threshold            "custmer nam" -> customer_name

Each header is consumed once, so two columns cannot both claim the same field.
"""
from __future__ import annotations

import difflib
import re

# Similarity floor for pass 4. Below this the guess is worse than no guess —
# a wrong pre-selection is more dangerous than an empty dropdown, because the
# user may not notice it.
_FUZZY_THRESHOLD = 0.72

# Minimum characters on BOTH sides of a containment test. Short headers fall
# through to the fuzzy pass, which has its own threshold.
_MIN_SUBSTR = 4


class FieldSpec:
    __slots__ = ("name", "label", "required", "aliases", "help")

    def __init__(self, name: str, label: str, required: bool, aliases: list[str], help: str = ""):
        self.name = name
        self.label = label
        self.required = required
        self.aliases = aliases
        self.help = help


# Import targets for a PhoneBook contact, in the order shown in the dialog.
CONTACT_FIELDS: list[FieldSpec] = [
    FieldSpec(
        "customer_name", "Customer name", True,
        ["name", "customer", "customer name", "contact name", "full name",
         "client name", "party name", "recipient"],
        "Required. Shown in the contact list.",
    ),
    FieldSpec(
        "mobile_no", "WhatsApp number", True,
        # Short forms ("wa", "mob") are safe here because pass 2 is an exact
        # match — they can never be reached by the fuzzier passes below.
        ["mobile", "mobile no", "mobile number", "mob", "mob no",
         "phone", "phone no", "phone number", "ph", "ph no",
         "whatsapp", "whatsapp no", "whatsapp number",
         "wa", "wa no", "wa number", "wa phone",
         "contact no", "contact number", "number", "msisdn", "cell", "tel"],
        "Required. Include the country code.",
    ),
    FieldSpec(
        "email", "Email", False,
        ["email", "email id", "email address", "e mail", "mail", "mail id"],
        "Optional.",
    ),
    FieldSpec(
        "agent_id", "Agent ID", False,
        ["agent", "agent id", "agent code", "assigned to", "assignee", "owner"],
        "Optional. Replies route to this agent.",
    ),
]


# Reused verbatim by campaign_fields — the number column means the same thing
# whether the CSV is filling a PhoneBook or a personalised broadcast.
_MOBILE_ALIASES = CONTACT_FIELDS[1].aliases


def campaign_fields(param_count: int) -> list[FieldSpec]:
    """
    Import targets for a personalised broadcast, for a template taking
    `param_count` parameters.

    Unlike CONTACT_FIELDS this is built per template, because the number of
    parameter columns is whatever the chosen template declares.

    Every parameter is required: a blank leaves a hole in the message that Meta
    will happily deliver, and a customer reading "Dear ," is worse than a row
    the import told you it skipped.
    """
    fields = [
        FieldSpec(
            "mobile_no", "WhatsApp number", True,
            list(_MOBILE_ALIASES),
            "Required. Include the country code.",
        ),
        FieldSpec(
            "customer_name", "Customer name", False,
            ["name", "customer", "customer name", "contact name", "full name",
             "client name", "party name", "recipient"],
            "Optional. Shown in the review table, not sent.",
        ),
    ]
    for n in range(1, param_count + 1):
        fields.append(FieldSpec(
            f"param_{n}", f"Param {n}", True,
            # "{{n}}" and a bare number are matched exactly (pass 2), so they
            # cannot be reached by the fuzzy passes and mis-claim a column.
            [f"param {n}", f"parameter {n}", f"param{n}", f"{{{{{n}}}}}", str(n)],
            f"Required. Fills placeholder {{{{{n}}}}} in the template.",
        ))
    return fields


def _norm(s: str) -> str:
    """Lowercase and strip everything that is not a letter or digit."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def suggest_mapping(headers: list[str], fields: list[FieldSpec] | None = None) -> dict[str, str | None]:
    """
    Guess which CSV header belongs to each field.

    Returns {field_name: header or None}. A header is used at most once.
    """
    fields = fields or CONTACT_FIELDS
    norm_headers = {h: _norm(h) for h in headers if h and h.strip()}

    mapping: dict[str, str | None] = {f.name: None for f in fields}
    taken: set[str] = set()

    def claim(field: str, header: str) -> None:
        mapping[field] = header
        taken.add(header)

    def available() -> dict[str, str]:
        return {h: n for h, n in norm_headers.items() if h not in taken}

    # 1 — exact match on the field's own name
    for f in fields:
        if mapping[f.name]:
            continue
        target = _norm(f.name)
        for h, n in available().items():
            if n == target:
                claim(f.name, h)
                break

    # 2 — exact match on an alias
    for f in fields:
        if mapping[f.name]:
            continue
        alias_norms = {_norm(a) for a in f.aliases}
        for h, n in available().items():
            if n in alias_norms:
                claim(f.name, h)
                break

    # 3 — containment, longest header first so "whatsapp number" beats "number".
    #     Both sides need >= _MIN_SUBSTR characters: without a floor on the
    #     header, a column literally called "A" matches "name" because "a" is a
    #     substring of it, and the user gets a confidently wrong pre-selection
    #     they may not think to check.
    for f in fields:
        if mapping[f.name]:
            continue
        candidates = {_norm(f.name)} | {_norm(a) for a in f.aliases}
        best, best_len = None, -1
        for h, n in available().items():
            if len(n) < _MIN_SUBSTR:
                continue
            for c in candidates:
                if len(c) >= _MIN_SUBSTR and (c in n or n in c) and len(n) > best_len:
                    best, best_len = h, len(n)
        if best:
            claim(f.name, best)

    # 4 — fuzzy, as a last resort
    for f in fields:
        if mapping[f.name]:
            continue
        candidates = [_norm(f.name)] + [_norm(a) for a in f.aliases]
        best, best_score = None, 0.0
        for h, n in available().items():
            if len(n) < 3:
                continue
            score = max(difflib.SequenceMatcher(None, n, c).ratio() for c in candidates)
            if score > best_score:
                best, best_score = h, score
        if best and best_score >= _FUZZY_THRESHOLD:
            claim(f.name, best)

    return mapping


def field_catalogue(fields: list[FieldSpec] | None = None) -> list[dict]:
    """Field metadata for the mapping dialog."""
    return [
        {"name": f.name, "label": f.label, "required": f.required, "help": f.help}
        for f in (fields or CONTACT_FIELDS)
    ]
