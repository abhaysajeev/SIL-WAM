"""
Reading the body of an approved WhatsApp template.

Meta stores a template as a list of components; the BODY one carries the text and its
`{{n}}` placeholders. Several features need to reason about those placeholders — the
demo sender, the broadcast campaign builder, the parameter-count check — so the parsing
lives here rather than being re-derived each time with a slightly different regex.

Nothing here talks to Meta or the database; it is pure functions over `components`.
"""
import re

_PLACEHOLDER = re.compile(r"\{\{(\d+)\}\}")

# Header formats whose value is a media URL supplied per send, not text.
MEDIA_HEADER_FORMATS = ("IMAGE", "DOCUMENT", "VIDEO")


def body_text(components: list | None) -> str:
    """The BODY component's text, or '' if the template has none."""
    body = next((c for c in (components or []) if c.get("type") == "BODY"), None)
    return body.get("text", "") if body else ""


def header_format(components: list | None) -> str | None:
    """
    The HEADER component's format — 'TEXT', 'IMAGE', 'DOCUMENT', 'VIDEO' — or None.

    Meta upper-cases these but templates reaching us have travelled through both our
    own builder and Meta's sync, so normalise rather than trusting the case.
    """
    for c in components or []:
        if str(c.get("type", "")).upper() == "HEADER":
            fmt = str(c.get("format", "")).upper()
            return fmt or None
    return None


def is_media_header(components: list | None) -> bool:
    """True when the template's header carries media rather than text."""
    return header_format(components) in MEDIA_HEADER_FORMATS


def param_indices(components: list | None) -> list[int]:
    """
    Placeholder numbers in the body, ascending and de-duplicated.

    `{{2}}` may legitimately appear twice; it is still one parameter. Sorted numerically
    rather than as strings so `{{10}}` follows `{{9}}`.
    """
    return sorted({int(n) for n in _PLACEHOLDER.findall(body_text(components))})


def param_count(components: list | None) -> int:
    """How many values a send must supply."""
    return len(param_indices(components))


def example_values(components: list | None) -> dict[int, str]:
    """
    Meta's own sample values, keyed by placeholder number.

    Templates approved through this platform carry `example.body_text` — a single-element
    list of the values shown to the reviewer. Useful for previewing a template before the
    user has typed anything.
    """
    body = next((c for c in (components or []) if c.get("type") == "BODY"), None)
    rows = ((body or {}).get("example") or {}).get("body_text") or []
    if not rows or not isinstance(rows[0], list):
        return {}
    return {i: str(v) for i, v in enumerate(rows[0], start=1)}


def render(components: list | None, params: list[str] | dict[int, str] | None) -> str:
    """
    Substitute `params` into the body for preview.

    Accepts either an ordered list (position 0 → `{{1}}`) or a dict keyed by placeholder
    number. A placeholder with no value is left as-is rather than blanked, so the preview
    shows which slots are still empty instead of silently collapsing them — the same
    silent-blank failure that makes a mis-mapped parameter hard to notice.
    """
    if isinstance(params, dict):
        values = {int(k): "" if v is None else str(v) for k, v in params.items()}
    else:
        values = {i: "" if v is None else str(v) for i, v in enumerate(params or [], start=1)}

    def sub(match: re.Match) -> str:
        idx = int(match.group(1))
        val = values.get(idx, "")
        return val if val != "" else match.group(0)

    return _PLACEHOLDER.sub(sub, body_text(components))


def describe_params(components: list | None, param_mapping: dict | None = None) -> list[dict]:
    """
    Ordered descriptors for a parameter-entry or mapping UI.

    `label` prefers the mapped dot-path's last segment ("data.customer_name" →
    "Customer Name"), then Meta's example value, then a bare "Param n". Without that
    fallback chain a user filling in eight boxes has nothing to distinguish them.
    """
    mapping  = param_mapping or {}
    examples = example_values(components)
    out = []
    for idx in param_indices(components):
        dot_path = mapping.get(str(idx), "")
        if dot_path:
            label = dot_path.split(".")[-1].replace("_", " ").title()
        else:
            label = f"Param {idx}"
        out.append({
            "index":   idx,
            "label":   label,
            "hint":    dot_path,
            "example": examples.get(idx, ""),
        })
    return out
