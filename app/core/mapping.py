"""
Resolving a template's dot-path mappings against a client's payload.

A template stores which field fills which slot as configuration rather than code —
param_mapping {"1": "data.customer_name"}, cta_mapping {"0": "data.invoice_url"},
header_mapping "data.ImageURL". These functions turn those paths into the values a
send actually carries.

Lives here rather than in app/api/client_services_api.py so any module can resolve a
mapping without importing a FastAPI router. That matters for app/lizo/, which is
imported by conversation_engine: pulling the ingest API into that import graph is
exactly what the note at conversation_engine.py:29-32 warns against.

Pure functions over dicts — no database, no Meta, no request context.
"""


def get_nested(data: dict, dot_path: str) -> str:
    """
    Resolve a dot-path like 'data.order.amount', or '' when any part is missing.

    Walks dicts only, so a path cannot index into a list — a variable-length
    collection has no single slot to land in. Returns a string because that is what
    a template parameter is; a dict reached by a too-short path stringifies to its
    Python repr, which is pinned by tests as a known gap rather than a feature.
    """
    parts = dot_path.split(".")
    val = data
    for part in parts:
        if not isinstance(val, dict):
            return ""
        val = val.get(part, "")
    return str(val) if val is not None else ""


def resolve_params(data: dict, param_mapping: dict) -> list[str]:
    """
    Build the ordered template_params list from a mapping. Keys are 1-indexed strings.

    Dense by design: a mapping of {"1": ..., "3": ...} still produces three entries,
    with "" in the gap, because Meta matches parameters by position and a short list
    would silently shift {{3}} into {{2}}.
    """
    if not param_mapping:
        return []
    max_idx = max(int(k) for k in param_mapping.keys())
    result = []
    for i in range(1, max_idx + 1):
        dot_path = param_mapping.get(str(i), "")
        result.append(get_nested(data, dot_path) if dot_path else "")
    return result


def resolve_cta_urls(data: dict, cta_mapping: dict) -> dict[str, str]:
    """Build the cta_urls dict from a mapping. Keys are 0-indexed button positions."""
    if not cta_mapping:
        return {}
    return {btn_idx: get_nested(data, dot_path) for btn_idx, dot_path in cta_mapping.items()}
