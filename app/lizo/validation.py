"""
Pre-flight checks Liso's ingest runs before delegating to the shared pipeline.

Meta rejects a template send whose parameters are blank (#132000), and it does so
at send time — which for us is on a background scheduler, seconds after the client
already got its 201. The failure then costs three retry attempts and reaches the
client, if at all, only through a delivery callback. Two orders died exactly this
way with params=['GIANT BAZAAR , Pattom', '', …, '', '', '', ''].

So the same thing is checked at the door instead, where it can be a 422 naming the
field the client needs to fix.

Deliberately Liso's own code rather than an addition to
client_services_api.ingest_service: that function is on Shirin Asal's live path and
this validation is not part of their contract. The resolution itself is shared —
_get_nested and template_body are the same ones the real send uses, so the check
cannot drift from what actually happens.
"""
from app.api.client_services_api import _get_nested
from app.core import template_body
from app.lizo.responses import (
    STATUS_MISSING_MEDIA_URL, STATUS_MISSING_PARAMETER, STATUS_TEMPLATE_NOT_CONFIGURED,
)


def check_resolvable(payload_data: dict, service_id: str, template) -> tuple[str, str] | None:
    """
    Return (status_code, message) for the first unusable field, or None if all good.

    The code separates the two owners of an otherwise identical 422:
    missing_parameter means the client's payload is incomplete;
    template_not_configured means we have not finished setting the template up.

    `payload_data` is the request's `data` block; the dot-paths in a mapping are
    resolved against the whole envelope, so the context is rebuilt here the same way
    ingest_service builds it — mapping paths start with "data.".
    """
    ctx = {"data": payload_data, "service_id": service_id}

    problem = _check_body_params(ctx, template)
    if problem:
        return problem
    return _check_header_media(ctx, template)


def _check_body_params(ctx: dict, template) -> tuple[str, str] | None:
    indices = template_body.param_indices(template.components)
    if not indices:
        return None

    if not template.param_mapping:
        return (STATUS_TEMPLATE_NOT_CONFIGURED, (
            f"Template '{template.name}' has {len(indices)} parameter(s) but no "
            "parameter mapping is configured — set one in the admin panel before sending."
        ))

    missing = []
    for n in indices:
        path = (template.param_mapping or {}).get(str(n))
        if not path:
            missing.append(f"{{{{{n}}}}} (no mapping configured)")
        elif not _get_nested(ctx, path).strip():
            missing.append(f"{{{{{n}}}}} ← '{path}'")

    if missing:
        # Meta refuses the whole message for one blank parameter, so report every
        # offender at once rather than making the client fix them one send at a time.
        return (STATUS_MISSING_PARAMETER, (
            f"Template '{template.name}' needs a value for every parameter, but "
            f"{', '.join(missing)} resolved to nothing."
        ))
    return None


def _check_header_media(ctx: dict, template) -> tuple[str, str] | None:
    fmt = template_body.header_format(template.components)
    if fmt not in template_body.MEDIA_HEADER_FORMATS:
        return None

    if not template.header_mapping:
        return (STATUS_TEMPLATE_NOT_CONFIGURED, (
            f"Template '{template.name}' has a {fmt} header but no header mapping is "
            "configured — set one in the admin panel before sending."
        ))
    if not _get_nested(ctx, template.header_mapping).strip():
        return (STATUS_MISSING_MEDIA_URL, (
            f"Template '{template.name}' needs a {fmt.lower()} URL at "
            f"'{template.header_mapping}', but that field is missing or empty."
        ))
    return None
