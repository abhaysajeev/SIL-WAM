# DESIGN.md — SIL WhatsApp Manager (SIL-WAM)

> The design source of truth for this project. Humans and AI agents read it before building UI.
> Every value here is **extracted from the existing code**, with the file it lives in. Where the code
> is inconsistent, the inconsistency is listed under Open Questions instead of silently picking one.
> Format: Google Stitch DESIGN.md sections, plus Components inventory and Verification.
>
> Extracted 2026-10-31 from the live working copy (including uncommitted template changes).
> Paths are relative to the repo root. `CLAUDE.md` wins over this file wherever they disagree.

## 1. Visual Theme & Atmosphere

An internal admin panel for an ISV that hosts WhatsApp Business messaging for many client
companies. Two kinds of user: ISV admins (super_admin / admin) who see every company, and
company-scoped users who see only their own. People use it for operations: setting up companies
and their WhatsApp connection, managing users and roles, building phonebooks, sending broadcast
campaigns, reading conversations, and checking error logs and reports.

It should feel like a **dense, calm, precise operations console**: a white surface on a pale
green-grey page, a WhatsApp-adjacent brand green used as an accent, **square corners
everywhere**, hairline borders with very soft shadows, small uppercase monospace labels for
metadata, and short entrance animations. Clarity and scannability come before decoration.

## 2. Color Palette & Roles

All tokens are defined once in `static/css/theme.css` (`:root`). Templates should use only
`var(--token)`.

| Role | Token | Value | Notes / where used |
|---|---|---|---|
| Brand / primary action | `--green` | `#0FAF58` | `.btn`, `.dv-btn-primary`, active nav, focus border, links in breadcrumbs |
| Primary hover / dark text on green tint | `--green-dark` | `#089A4D` | button hover, green badge text |
| Light green (gradient start) | `--green-light` / `--g-start` | `#17B85C` | gradients only |
| Gradient mid | `--g-mid` | `#0FAF58` | |
| Gradients | `--green-gradient`, `--green-gradient-diagonal`, `--green-gradient-vertical` | linear-gradient `#17B85C → #089A4D` | sidebar logo bar (`base.css .sidebar-logo`), login brand panel |
| Green tint (selected / hover surface) | `--green-tint` | `#E6F9EE` | active nav row, table row hover, focused input fill, bulk-select bar |
| Page background | `--bg` | `#F0F4F0` | `body`, table header row, ghost hover |
| Surface | `--bg-card` | `#FFFFFF` | cards, sidebar, topbar, modals |
| Border (hairline) | `--line` | `rgba(0,0,0,.09)` | card borders, row dividers |
| Border (strong) | `--line-2` | `rgba(0,0,0,.15)` | inputs, outline buttons, dropdowns |
| Text: strongest | `--black` | `#0A0A0A` | page titles, modal titles, bold cells |
| Text: body | `--ink` | `#111827` | default body text |
| Text: secondary | `--ink-2` | `#374151` | table cells, ghost button text |
| Text: muted | `--muted` | `#6B7280` | table headers, stat labels |
| Text: subtle | `--subtle` | `#9CA3AF` | placeholders, nav section labels, counts, empty states |
| Danger | `--red` / `--red-dark` / `--red-tint` | `#DC2626` / `#B91C1C` / `#FEF2F2` | destructive buttons, failed/inactive badges, error banners |
| Warning-ish | `--orange` / `--orange-tint` | `#f97316` / `#fff7ed` | `.stat-icon.orange`, `.badge.orange` (text is hardcoded `#9a3412`) |
| Info | `--blue` / `--blue-tint` | `#3b82f6` / `#eff6ff` | in-progress / sending / role badges (text is hardcoded `#1d4ed8`) |
| Focus ring | `--ring` | `0 0 0 3px rgba(15,175,88,.15)` | every `:focus-visible` / input focus |

**Status colour mapping in use** (`layouts/list_view.html`, `broadcast/campaign_list.html`):
green = active / completed / ready / dispatched / settled; blue = in progress / sending / role tag;
gray = waiting / expired / draft / screening / "No role"; red = inactive / failed / cancelled.

**Rules**
- Green is the brand **and** the "success" colour. Use it for the one primary action and for
  positive states. Large green fills are limited to the sidebar logo bar and the login brand panel.
- Red is for destructive actions and failed states only, never decoration.
- WhatsApp's own colours (`#25D366`, chat wallpaper `#E5DDD5`/`#ECE5DD`, `#8696A0`) appear only
  inside WhatsApp message previews (`static/css/templates.css`, `broadcast/campaign_new.html`,
  `companies/detail.html`), where they mimic the WhatsApp client. Do not use them elsewhere.
- Light theme only. `base.css` sets `color-scheme: light`; there are no dark-mode rules anywhere.

## 3. Typography Rules

Loaded from Google Fonts in `app/templates/base.html` (and again, with slightly different weights,
in `login.html`). Tokens in `theme.css`:

| Token | Family | Role |
|---|---|---|
| `--f-ui` | Inter (300–700) | all body text, inputs, buttons. `base.css` turns on `cv05`, `cv11` |
| `--f-disp` | Space Grotesk (400–700) | page titles, modal titles, stat numbers, brand name |
| `--f-mono` | JetBrains Mono (400–600) | uppercase metadata labels, table headers, badges, IDs/codes, counts |

Base: `body` 14px / 1.5, `--ink`, antialiased (`base.css`). Scales up to 14.5px at ≥1920px and 15px at ≥2560px.

| Element | Class | Spec | File |
|---|---|---|---|
| Page title | `.dv-title` | Space Grotesk 22px / 700 / -.03em / `--black` | `docview.css` |
| Dashboard heading | `.dash-welcome-title` | Space Grotesk 22px / 700 | `dashboard/index.html` |
| Topbar title | `.page-title` | Space Grotesk 15px / 600 | `base.css` |
| Modal / form-doc title | `.modal-title`, `.dv-form-doc-title` | Space Grotesk 15px / 700 | `base.css`, `docview.css` |
| Card title | `.card-title` | Inter 13.5px / 600, green icon | `base.css` |
| Stat number | `.stat-value` / `.stat-info h3` | Space Grotesk 28–30px / 700, tabular-nums | `base.css`, `dashboard/index.html` |
| Body / table cell | `.dv-table td` | Inter 13px, `--ink-2`, tabular-nums | `docview.css` |
| Form label | `.dv-label` | Inter 11px / 600 | `docview.css` |
| Hint / subtitle | `.dv-hint`, `.dv-subtitle` | 11–12px, `--subtle` (subtitle is mono) | `docview.css` |
| Metadata label (eyebrow) | `.nav-label`, `.dv-section-label`, `.info-key`, `.stat-label`, table `th` | JetBrains Mono 9–9.5px, uppercase, letter-spacing .14–.26em | `base.css`, `docview.css` |
| Badge | `.dv-badge`, `.badge` | JetBrains Mono 9px, uppercase, .12em | `docview.css`, `base.css` |
| Code / ID cell | `.cell-mono` | JetBrains Mono 12px | `docview.css` |

Numbers in tables and stats use `font-variant-numeric: tabular-nums`.

## 4. Component Stylings

All shared components live in `static/css/base.css` (global, loaded by `base.html`) and
`static/css/docview.css` (list/form primitives, also loaded by `base.html`).

**Buttons** — square, 12.5px / 600, padding 9px 16px, lift 1px on hover.
- Primary: `.dv-btn.dv-btn-primary` (docview pages) or `.btn` (base) — green fill. One per screen.
- Secondary: `.dv-btn.dv-btn-ghost` (= `.dv-btn-secondary`) or `.btn.btn-outline` — white, `--line-2` border.
- Destructive: `.dv-btn.dv-btn-danger` — transparent, red text, red-tint hover.
- Sizes: `.dv-btn-xs` (5px 10px, 11.5px), `.btn-sm` (7px 12px, 12px).
- Loading: `.dv-btn` containing `.btn-label` + `.btn-spinner`; add `.loading`. Example: `layouts/form_view.html` Save.
- Icon-only: a `.dv-btn-xs` with one `<i>` and a `title` (form_view Delete).

**Inputs** — `.dv-input`, `.dv-select` (`docview.css`): 9px 12px, `--line-2` border, radius 0,
transparent fill; on focus green border + `--green-tint` fill + `--ring`. Modifiers: `.mono`,
`.error-border`, `[readonly]` (gray, not-allowed). Wrap each in `.dv-field` with a `.dv-label`
(`<span class="req">*</span>` for required, `.dv-label-opt` for optional) and `.dv-hint`.
Toggle switch: `.dv-toggle-wrap > label.dv-toggle > input + .dv-toggle-track` (36×20, the one
rounded control). Search box: `.dv-search > .dv-search-icon + .dv-search-input`.
Static (create-only) value on edit: `.dv-field-static`.

**Tables** — `.dv-list-card > .dv-list-meta + table.dv-table` (`docview.css`): mono uppercase
headers on `--bg`, 12px 20px cells, whole row clickable (`cursor:pointer`, green-tint hover with a
2px green inset on the first cell), staggered `rowIn` entrance, trailing `.dv-row-open` chevron.
A second, older table style `.table-wrap > table.table` exists in `base.css` with the same look.

**Cards** — `.card > .card-header(.card-title) + .card-body` (`base.css`); `.dv-section >
.dv-section-header(.dv-section-label) + .dv-section-body` for form sections (2-column grid,
gap 18px 24px). Stat tiles: `.stats-grid > .stat-card > .stat-info + .stat-icon.{green|blue|orange|red}`.
Accent card: `.welcome-card` (3px green left border).

**Badges / status chips** — `.dv-badge.dv-badge-{green|red|blue|gray}` (`docview.css`) is the
default; `.badge.{green|orange|red|blue|gray}` in `base.css` is the same design with an orange
variant. Many pages define their own (see Open Questions).

**Modals** — `.modal-backdrop.open > .modal > .modal-header(.modal-title, .modal-close) +
.modal-body + .modal-footer` (`base.css`), max-width 440px, `--shadow-pop`, scale-in. Reference:
Change Password modal in `base.html`. Close on backdrop click and on the × / Cancel button.

**Inline feedback (instead of toasts)** — there is **no toast component**. Feedback is:
`.dv-inline-error` / `.dv-inline-success` (inside modals), `.dv-form-error.visible` (form banner),
`.dv-save-status.success|error` (form toolbar), and `.dv-delete-confirm.visible` (inline
"Delete this X? Yes, delete / Cancel" in place of a dialog). Several pages still use `alert()` /
`confirm()` (see Open Questions).

**Empty states** — `.dv-empty > .dv-empty-icon + .dv-empty-text + .dv-empty-hint` (with a link to
create the first record when permitted). Search no-match uses the same block. Example:
`layouts/list_view.html`.

**Tabs** — `.cd-tabs > button.cd-tab(.active)` with `.cd-panel(.active)` panels, underline style,
defined in `static/css/whatsapp.css`. `form_view.html` reuses them for doctypes with `form_tabs`.

**Pagination** — `layouts/_pagination.html` (needs `page` and `page_url`), renders nothing for
one page. Its `.pg-*` styles are currently copied into each page that uses it.

**Bulk selection** — `.dv-bulkbar(.visible)` with `.dv-bulkbar-count`, hidden until a row is
ticked; opt-in per doctype via `bulk_delete` (`layouts/list_view.html`).

**Timestamps** — `{{ ts(value) }}`, `{{ ts(value, 'date') }}`, `{{ ts(value, 'time') }}` from
`app/core/templating.py`. It emits `<time datetime="…Z" data-fmt="…">server fallback</time>`;
`base.html` localises it to the viewer's timezone (24-hour, `02 Sep 2026, 12:02`) and puts full
precision in the `title`. Rows injected after load must call `window.renderTimestamps(root)`.

## 5. Layout Principles

**Skeleton** (`app/templates/base.html` + `base.css`):
```
.dash-layout
├── aside.sidebar (fixed, --sidebar-w, white, right hairline)
│     .sidebar-logo (green vertical gradient) · .sidebar-nav (.nav-label + .nav-item) · .sidebar-footer
├── .sidebar-overlay (mobile)
└── .dash-main (margin-left: --sidebar-w)
      header.dash-header (sticky, --topbar-h, title from `active` + user menu)
      .dash-content.page-enter  ← {% block content %}
```
Blocks a page fills: `title`, `extra_css`, `content`, `extra_js`.

| Measure | Value | Source |
|---|---|---|
| Sidebar width | 196px · 216px ≥1440 · 220px ≥1600 · 236px ≥1920 · 256px ≥2560 | `theme.css`, `base.css` |
| Topbar height | 46px · 52px ≥1920 · 56px ≥2560 | same |
| Content padding | 28px · 20px ≤1366 · 30px ≥1440 · 36px ≥1600 · 44px ≥1920 · 56px ≥2560 | `base.css` |
| Form max width | `.dv-form-wrap` 900px (1060 ≥1920, 1280 ≥2560); `.cd-wrap` 900px | `docview.css`, `whatsapp.css` |
| List pages | full width | |

**Spacing** — there are no spacing tokens; values are raw px on a loose 4px rhythm. The house values:

| Context | Value |
|---|---|
| Page header bottom margin (`.dv-page-header`) | 24px |
| Gap between stat cards / dashboard columns | 16px |
| `.grid-2` / `.grid-3` gap | 20px |
| Between form sections (`.dv-section`) | 16px |
| Form field grid gap | 18px row, 24px column |
| Label → input | 5px |
| Card / section / modal body padding | 20px |
| Card header / list meta padding | 14px 20px / 11px 20px |
| Toolbar → list card | 16px |
| Button group gap (`.dv-actions`, modal footer) | 8px |
| Icon → text inside button | 6–7px |

**Page header pattern** (list): `.dv-page-header` = left `.dv-title` + `.dv-subtitle` (record
count), right `.dv-actions` (primary "New X"). **Form/detail pattern**: `.dv-breadcrumb`
(Parent / Record) above a **sticky** `.dv-form-toolbar` (doc title left; status, Delete, Cancel,
Save right), sticking under the topbar at `top: var(--topbar-h)`.

## 6. Depth & Elevation

- **Radius: 0.** Square corners are the house style — buttons, inputs, cards, modals, badges
  (`border-radius: 0` is stated explicitly in `base.css`, `docview.css`, `login.css`,
  `templates.css`). Allowed exceptions: circles (`50%`: spinners, avatar dots), the toggle track
  (20px), and WhatsApp-style chat bubbles in previews/conversations.
- **Borders first, shadows second.** Every surface has a 1px `--line` border; shadows are faint.

| Token | Value | Use |
|---|---|---|
| `--shadow-card` | `0 1px 3px rgba(0,0,0,.05), 0 4px 16px rgba(0,0,0,.04)` | cards, tables, sections, toolbar |
| `--shadow-lift` | `0 2px 6px rgba(0,0,0,.07), 0 8px 24px rgba(0,0,0,.07)` | stat-card hover |
| `--shadow-pop` | `0 4px 16px rgba(0,0,0,.10), 0 24px 64px rgba(0,0,0,.18)` | modals |
| `--shadow-green` | `0 4px 24px rgba(8,154,77,.3), 0 1px 4px rgba(0,0,0,.1)` | login |

**z-index layers in use**

| Layer | z-index | Where |
|---|---|---|
| Sticky form toolbar | 40 | `docview.css` |
| Topbar | 50 | `base.css` |
| Campaign page sticky bars | 60 | `broadcast/campaign_detail.html`, `campaign_new.html` |
| Mobile sidebar overlay / sidebar | 99 / 100 | `base.css` |
| User dropdown, page-local modals (`.cl-`, `.bl-`, `.cn-`, `.ak-`) | 200 | `base.css`, broadcast + companies pages, `docview.css` |
| Global modal (`.modal-backdrop`) | 300 | `base.css` |
| Template / WhatsApp wizard modals | 9999 | `templates.css`, `whatsapp.css` |
| Session-expired banner | 99999 | inline in `base.html` |

**Motion** — `--ease: cubic-bezier(.2,.7,.2,1)`. Durations 120–350ms. Entrance animations:
`pageEnter` (content), `cardIn` (cards, tables, sections), `rowIn` (staggered table rows),
`modalIn`, `popoverIn`. Hover lift `translateY(-1px)` on buttons, `-2px` on stat cards.

## 7. Do's and Don'ts

**Do**
- Extend `base.html`; put page CSS in `{% block extra_css %}` and JS in `{% block extra_js %}`.
- Use the `theme.css` tokens for every colour, font, shadow and focus ring.
- Reuse `docview.css` classes (`dv-*`) before writing page-local CSS.
- Print every timestamp with `{{ ts(value) }}` / `ts(value, 'date')` / `ts(value, 'time')`; in a
  doctype list column use `"type": "datetime" | "date" | "time"` and pass the raw value.
- Build page routes with `build_templates()` from `app/core/templating.py`.
- Gate actions in the template on `perms[...]` (create / write / delete), the way `list_view.html` does.
- Keep one primary (green) button per screen; destructive actions are `.dv-btn-danger`.
- Use `—` (`EMPTY` in `templating.py`) for missing values.
- Prefer inline confirmation (`.dv-delete-confirm`) or a `.modal` over `confirm()`.

**Don't**
- Don't hardcode hex colours in templates — add or use a token in `theme.css`.
- Don't add `border-radius` to surfaces, inputs or buttons (square is the house style).
- Don't `.strftime()` a timestamp for display, or format dates server-side in a route.
- Don't construct `Jinja2Templates(...)` directly in a route file.
- Don't add new fonts, CSS frameworks or icon sets. Chart.js (`chart.js@4.4.0`, analytics only) is
  the only charting library.
- Don't add dark-mode rules.
- Don't let a table create page-level horizontal scroll; wrap wide tables in a scroll container.
- Don't use WhatsApp brand colours outside a WhatsApp message preview.

## 8. Responsive Behavior

Breakpoints used by the shared CSS:

| Width | What changes | File |
|---|---|---|
| ≤ 640px | form section grid → 1 column, `col-2` fields span 1 | `docview.css` |
| ≤ 768px | sidebar becomes an off-canvas drawer (`body.sidebar-open`), hamburger `.sidebar-toggle` appears, `.dash-main` margin 0 | `base.css`, `static/js/sidebar.js` |
| ≤ 1024px | `.grid-2`, `.grid-3`, `.dash-cols` → 1 column | `base.css`, `dashboard/index.html` |
| ≤ 1366px | content padding 20px, smaller stat cards | `base.css` |
| ≥ 1440 / 1600 / 1920 / 2560px | progressively wider sidebar, more padding, larger type and table cells | `base.css`, `docview.css` |

Page-local templates add their own breakpoints (560, 600, 700, 860, 900, 1000, 1100px) — 17
distinct `@media` widths in total.

- Stat grid uses `repeat(auto-fit, minmax(min(200px, calc(25% - 12px)), 1fr))` so it reflows on its own.
- `.table-wrap` scrolls horizontally inside itself. **`.dv-list-card` has no `overflow-x`**, so a
  wide `dv-table` can push the page sideways on a phone (see Open Questions).
- Touch targets: `.dv-btn` is about 38px tall (9px padding, 12.5px text at line-height 1.5, 1px
  borders); `.dv-btn-xs` is about 29px and `.dv-row-open` 24px, which is below a 44px touch target.

## 9. Agent Prompt Guide

When building or changing a screen in SIL-WAM:

1. **Read first**: `CLAUDE.md` (permission guards, company scoping, `build_templates()`, `ts()`),
   `PROJECT_MAP.md`, and this file.
2. **Route**: `templates = build_templates()` from `app/core/templating.py`. Page routes use
   `ctx=Depends(get_page_user)`, check `ctx["perms"][resource]["read"]`, apply
   `company_filter(user)`, and pass `user`, `perms` and `active` (the sidebar key) to the template.
3. **Simple CRUD resource** → don't write a template. Add a doctype dict to `app/core/doctypes.py`
   (`list_columns`, `form_sections`, optional `form_tabs`, `bulk_delete`) and render
   `layouts/list_view.html` and `layouts/form_view.html`. Copy the shape of `USERS_DOCTYPE`
   (tabs) or `ERPNEXT_CONFIG_DOCTYPE` (plain form). Route examples: `app/routes/users_pages.py`,
   `app/routes/erpnext_config_pages.py`, `app/routes/broadcast_settings_pages.py`.
4. **Custom list** (filters, per-status pills, bulk actions) → copy
   `app/templates/broadcast/campaign_list.html`: it reuses `dv-page-header`, `dv-toolbar`,
   `dv-list-card`, `dv-table`, `dv-bulkbar` and adds only a filter and a status pill.
5. **Detail view** → copy `app/templates/broadcast/campaign_detail.html` (header + stats + paged
   table) or `app/templates/conversations/detail.html` (info strip + two-column layout). For a
   tabbed detail use the `.cd-tabs` / `.cd-panel` pattern from `static/css/whatsapp.css`
   (as in `companies/detail.html` and `form_view.html`).
6. **Dashboard / KPIs** → `.stats-grid` + `.stat-card` from `base.css`, as in `dashboard/index.html`.
7. **Paged table** → `{% include "layouts/_pagination.html" %}` with `page` from
   `app/core/pagination.py`; copy the `.pg-*` rules from `broadcast/phonebook_detail.html` until
   they are moved into `docview.css`.
8. **Modal** → the global `.modal-backdrop > .modal` from `base.css` (see the Change Password
   modal in `base.html`). Do not invent another `xx-modal` prefix.
9. **Icons** → Tabler webfont: `<i class="ti ti-<name>"></i>` (the sidebar, layouts and newer
   screens use it). Font Awesome is also loaded but treat it as legacy (see Open Questions).
10. **Timestamps** → `{{ ts(x) }}`; JS-inserted rows call `window.renderTimestamps(container)`.
11. **API calls from page JS** → `fetchWithAuth(url, options)` (defined in `base.html`); it
    refreshes the token and shows the session-expired banner.
12. **New sidebar entry** → add it to `base.html` under the right `.nav-label`, gated by
    `{% if perms.<resource>.read %}`, with `active` matching the route.
13. Page-only CSS goes in `{% block extra_css %}<style>…</style>`, using tokens; if a second page
    needs it, move it to `docview.css` instead of copying it.

## 10. Components Inventory

| Need | Reuse this | File |
|---|---|---|
| App shell (sidebar, topbar, user menu) | `base.html` | `app/templates/base.html`, `static/css/base.css` |
| Design tokens | `:root` vars | `static/css/theme.css` |
| Generic list page | `layouts/list_view.html` + doctype | `app/templates/layouts/list_view.html`, `app/core/doctypes.py` |
| Generic create/edit form (with optional tabs) | `layouts/form_view.html` + doctype | `app/templates/layouts/form_view.html` |
| Page header with primary action | `.dv-page-header`, `.dv-title`, `.dv-actions` | `static/css/docview.css` |
| Breadcrumb | `.dv-breadcrumb` | `static/css/docview.css` |
| Sticky form toolbar | `.dv-form-toolbar` | `static/css/docview.css` |
| Search box | `.dv-search` | `static/css/docview.css` |
| Data table | `.dv-list-card` + `.dv-table` | `static/css/docview.css` |
| Bulk select bar | `.dv-bulkbar` | `layouts/list_view.html` (inline CSS) |
| Pager | `layouts/_pagination.html` | `app/templates/layouts/_pagination.html` |
| Status badge | `.dv-badge-{green,red,blue,gray}` | `static/css/docview.css` |
| Campaign status pill | `.cl-status.<state>` | `broadcast/campaign_list.html` |
| Buttons | `.dv-btn-{primary,ghost,danger}`, `.dv-btn-xs`; `.btn`, `.btn-outline`, `.btn-sm` | `docview.css`, `base.css` |
| Form field / input / select / toggle | `.dv-field`, `.dv-input`, `.dv-select`, `.dv-toggle` | `static/css/docview.css` |
| Masked secret field | `.dv-secret-*` | `static/css/docview.css` |
| Form section card | `.dv-section` | `static/css/docview.css` |
| Card | `.card`, `.card-header`, `.card-body` | `static/css/base.css` |
| KPI tiles | `.stats-grid`, `.stat-card`, `.stat-icon.<colour>` | `static/css/base.css`, `dashboard/index.html` |
| Key/value info grid | `.info-grid`, `.info-key`, `.info-val` | `dashboard/index.html` (inline) |
| Tabs | `.cd-tabs`, `.cd-tab`, `.cd-panel` | `static/css/whatsapp.css` |
| Modal | `.modal-backdrop`, `.modal` | `static/css/base.css` |
| Inline error / success | `.dv-inline-error`, `.dv-inline-success`, `.dv-form-error` | `base.css`, `docview.css` |
| Inline delete confirm | `.dv-delete-confirm` | `docview.css`, `form_view.html` |
| Empty state | `.dv-empty` | `docview.css`, `list_view.html` |
| Button spinner | `.btn-spinner` + `.loading` | `docview.css` |
| WhatsApp message preview | `.tpl-*` | `static/css/templates.css` |
| CSV import dialog | `.im-*` + `static/js/csv.js` | `static/css/import-dialog.css` |
| Chat thread | `.cd-msg`, `.cd-bubble-in/out` | `conversations/detail.html` (inline) |
| Charts | Chart.js 4.4.0 (CDN) | `analytics/index.html` |
| Timestamp | `ts()` | `app/core/templating.py`, script at end of `base.html` |
| Authenticated fetch | `fetchWithAuth()` | `base.html` |

## 11. Verification

- **Dev server**: `venv/bin/uvicorn app.main:app --reload` → http://127.0.0.1:8000. Check with
  `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/login` (expect 200; `curl -I`
  returns 405 because the route is GET-only).
- **Login**: every page except `/login` needs a session cookie. Super admin is created only via
  `venv/bin/python run.py create-superadmin <user> <pass>`. Sidebar items depend on permissions,
  so check a new page as an `admin` user too, not only as super_admin.
- **Reference screens** to compare new work against: `/users` (generic list),
  `/users/<id>` (generic form with tabs), `/broadcast/campaigns`
  (`broadcast/campaign_list.html`), `/dashboard`.
- **Widths**: 1440, 768, 390. At 390 check the sidebar drawer and that tables don't scroll the page.
- **View-only browsing — mandatory**: never send a WhatsApp message, never submit a form or any
  POST/PUT/DELETE, never click Save / Delete / Send / Launch / Approve / Confirm / Connect.
  **SHIRIN ASAL** is the company with live WhatsApp configuration — be extra careful on its pages.
  States that need a write (sending, failed, etc.) are designed from the code and reported as
  "not seen live".
- Tests: `venv/bin/pytest tests/ -v` must pass after any template change that touches routes.

## Open Questions

Inconsistencies found in the code that a human should decide. Counts are from the working copy
on 2026-10-31.

0. **Brand-token contrast fails WCAG AA** (computed from the `theme.css` values):
   white text on `--green` (primary buttons, `.dv-btn-primary`, `.btn`) is **2.88:1**;
   `--green` on `--green-tint` (active nav item) is 2.63:1; `--green-dark` on `--green-tint`
   (green badges, 9px text) is 3.34:1; `--subtle` `#9CA3AF` on white (counts, hints, subtitles,
   empty states) is 2.54:1; `--muted` on `--bg` (table header text, 9.5px) is 4.35:1. Small text
   needs 4.5:1. White on `--green-dark` is only 3.66:1, so darkening the hover state does not fix
   it either. A darker green for text and fills (with the current green kept for accents) is a
   brand decision, so it is listed here rather than changed.

1. **Two icon sets.** Tabler (`ti ti-*`, ~121 uses) and Font Awesome 6.5.1 (~170 uses) are both
   loaded on every page. 11 templates use only Tabler (sidebar, layouts, dashboard, broadcast,
   conversations, services, analytics); 8 use only Font Awesome (`companies/detail.html` alone has
   102, plus error logs, roles, users, login); 5 mix both. `doctypes.py` icons are split 5 FA / 5
   Tabler, so FA icons appear inside Tabler-based layouts. Which set is canonical — and can the
   other CDN stylesheet be dropped?
2. **Hardcoded colours.** 73 distinct hex values across 24 files, plus 43 distinct `rgba()`
   literals. 18 neutral greys are hardcoded outside `theme.css` across 7 files
   (`#111`, `#333`, `#555`, `#777`, `#e0e0e0`, `#e5e7eb`, `#f0f0f0`, `#f1f5f9`, `#f3f4f6`,
   `#f8f9fa`, `#f9fafb`, `#d1d5db`, `#475569`, `#94a3b8`, …) next to the 5 grey tokens.
   Worst offenders: `demo/messaging.html`, `webhook_config/index.html`, `companies/detail.html`,
   `static/css/templates.css`.
3. **A second green.** Tailwind green `#16a34a` (18 uses in 4 files) and its family (`#15803d`,
   `#166534`, `#bbf7d0`, `#dcfce7`, `#f0fdf4`) sit beside the brand `--green` `#0FAF58`.
4. **Missing tokens.** No warning/amber token (pages hardcode `#b45309`, `#92400e`, `#fffbeb`,
   `#f59e0b`, `#ca8a04`); the blue badge text `#1d4ed8` is hardcoded 12 times; the orange badge
   text `#9a3412` is hardcoded. Should `--amber*`, `--blue-dark`, `--orange-dark` be added?
5. **Undefined CSS variables.** `--border`, `--text`, `--text-muted`, `--bg-secondary`
   (`companies/detail.html`, `webhook_config/index.html`, `docview.css .dv-secret-saved`),
   `--surface`, `--accent` (`analytics/index.html`) are used but never defined, so those rules
   silently fall back.
6. **Radius drift.** House style is square, but 6px / 7px / 8px / 12px radii appear on
   surfaces in `webhook_config/index.html`, `analytics/index.html`, `demo/messaging.html`,
   `companies/detail.html`, and `.dv-secret-saved` (6px) in `docview.css` — 19 distinct
   `border-radius` values in total.
7. **Duplicated components.** 9 modal implementations (`.modal`, `.ak-`, `.bl-`, `.cl-`, `.cn-`,
   `.im-`, `.tpl-`, `.tpl-view-`, `.wa-`) at z-index 200, 300 or 9999; 6 button families (`.btn`,
   `.dv-btn`, `.tpl-btn`, `.wa-btn`, `.el-btn`, `.wc-btn`); ~10 badge/status families
   (`.dv-badge`, `.badge`, `.cl-status`, `.sd-status-*`, `.tpl-badge-*`, `.wa-status-*`,
   `.el-badge-*`, `.cd-badge-*`, `.wc-status-badge`, …); 10 table classes. Consolidate onto
   `.modal`, `.dv-btn`, `.dv-badge`, `.dv-table`?
8. **Page-local CSS volume.** 21 of 25 templates carry their own `<style>` block (~2,000 lines,
   vs ~3,600 lines of shared CSS), and there are 290 inline `style="…"` attributes across 21
   templates (84 in `companies/detail.html`). The `.pg-*` pager and `.dv-bulkbar` styles are
   copied between pages with small differences (pager padding 20px vs 18px).
9. **Prefix collision.** `cd-` means "company detail" in `whatsapp.css` and "conversation detail"
   in `conversations/detail.html`'s page CSS.
10. **No toast; native dialogs.** 20 `alert()` and 8 `confirm()` calls in routed templates (plus
    more in orphans) — including bulk delete in `list_view.html`. Should a shared toast and the
    `.dv-delete-confirm` / `.modal` pattern replace them?
11. **Font weights.** `login.html` loads different weights (Inter without 700, JetBrains Mono with
    700) than `base.html`. 31 distinct font-sizes are in use (6px–40px, including half-pixel
    steps 9.5 / 10.5 / 11.5 / 12.5 / 13.5 / 14.5) — no type scale tokens.
12. **No spacing tokens and 17 breakpoints.** Spacing is raw px; page-local breakpoints (560, 600,
    700, 860, 900, 1000, 1100) don't match the shared ones (640, 768, 1024, 1366).
13. **Narrow-screen tables.** `.dv-list-card` has no `overflow-x: auto`, unlike `.table-wrap`.
    Wide doctype lists likely overflow the page at 390px (not seen live).
14. **Accessibility.** No `prefers-reduced-motion` handling for the many entrance animations; the
    sidebar `active` page has no `aria-current`; small targets (24–26px) for row chevrons and
    `.dv-btn-xs`; whole-row `onclick` navigation is not keyboard-reachable except via the chevron
    link.
15. **Duplicate stylesheet and dead code.** `docview.css` is linked by `base.html` and again by
    9 page templates. `static/js/sidebar.js` still wires a `logoutBtn` / `logoutPopover` that no
    longer exists in `base.html`. The session-expired banner is styled inline in JS with a hex.
16. **Orphaned templates.** `companies/list.html`, `users/list.html` and
    `broadcast/coming_soon.html` are not rendered by any route (lists go through
    `layouts/list_view.html`). Delete, or keep as references?
17. **`company_tag` cell type** is documented in `doctypes.py` but has no branch in
    `list_view.html` (it renders as plain text).
