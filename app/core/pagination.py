"""
Server-side pagination for SSR list pages.

Every list route before this rendered every row it found. That is survivable at
55 services but not at 2,000 phonebook contacts, and certainly not at a campaign
recipient list — the page weight grows linearly and the browser has to hold all
of it before the client-side search filter can run.

Usage:

    page = paginate(query.order_by(Model.created_at.desc()), request, per_page=50)
    ...
    {"rows": [_row(x) for x in page.items], "page": page}

and in the template:

    {% include "layouts/_pagination.html" %}
"""
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Query

DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 200

# How many numbered links to show either side of the current page.
_WINDOW = 2


@dataclass
class Page:
    items: list[Any]
    page: int
    per_page: int
    total: int

    pages: int = field(init=False)
    has_prev: bool = field(init=False)
    has_next: bool = field(init=False)
    prev_page: int = field(init=False)
    next_page: int = field(init=False)
    # Page numbers to render; 0 is a gap marker rendered as an ellipsis.
    window: list[int] = field(init=False)
    # 1-based index of the first and last row on this page, for "showing X–Y of Z".
    first_index: int = field(init=False)
    last_index: int = field(init=False)

    def __post_init__(self) -> None:
        self.pages = max(1, -(-self.total // self.per_page))  # ceil
        self.has_prev = self.page > 1
        self.has_next = self.page < self.pages
        self.prev_page = max(1, self.page - 1)
        self.next_page = min(self.pages, self.page + 1)
        self.first_index = 0 if self.total == 0 else (self.page - 1) * self.per_page + 1
        self.last_index = min(self.page * self.per_page, self.total)
        self.window = _build_window(self.page, self.pages)


def _build_window(current: int, pages: int) -> list[int]:
    """
    Page numbers to render, with 0 marking an elided run.

    Always shows the first and last page so the ends stay reachable no matter
    how many pages there are: 1 … 7 8 [9] 10 11 … 40
    """
    if pages <= 7:
        return list(range(1, pages + 1))

    nums = {1, pages}
    nums.update(range(max(1, current - _WINDOW), min(pages, current + _WINDOW) + 1))

    out: list[int] = []
    prev = 0
    for n in sorted(nums):
        if prev and n - prev > 1:
            out.append(0)
        out.append(n)
        prev = n
    return out


def _read_int(request: Request, key: str, default: int, lo: int, hi: int) -> int:
    """Query params come from the URL bar — clamp rather than trust or 500."""
    raw = request.query_params.get(key)
    if raw is None:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


def paginate(query: Query, request: Request, per_page: int = DEFAULT_PER_PAGE) -> Page:
    """
    Slice `query` according to ?page= and ?per_page= on the request.

    `query` should already be filtered, company-scoped and ordered — an unordered
    query gives Postgres licence to return rows in a different order per page,
    which silently duplicates and drops rows across page boundaries.
    """
    per_page = _read_int(request, "per_page", per_page, 1, MAX_PER_PAGE)
    total = query.order_by(None).count()

    pages = max(1, -(-total // per_page))
    page = _read_int(request, "page", 1, 1, pages)

    items = query.limit(per_page).offset((page - 1) * per_page).all()
    return Page(items=items, page=page, per_page=per_page, total=total)


def page_url(request: Request, page: int) -> str:
    """Current URL with ?page= replaced, preserving every other query param."""
    params = dict(request.query_params)
    params["page"] = str(page)
    return f"{request.url.path}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
