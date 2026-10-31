# DESIGN.md — SIL WhatsApp Manager (SIL-WAM)

> The design source of truth for this project. Humans and AI agents read it before building UI.
> `CLAUDE.md` wins over this file wherever they disagree (permissions, `build_templates()`, `ts()`).
>
> **Status (2026-10-31): redesign phase 1 of 3 is in.** Phase 1 replaced the design direction,
> the tokens, the shared components, the app shell, the generic list/form layouts and the sign-in
> page. Feature screens still carry page-local CSS from the old system; they keep working through
> the legacy aliases listed in §2 and §4 and are migrated in phases 2–3 (§12).
> Paths are relative to the repo root.

## 1. Design direction

SIL-WAM is a tool people keep open all day: ISV admins set up companies and their WhatsApp
numbers, manage users and roles, send broadcasts, read conversations and chase failures. It should
read like a **calm, dense, production SaaS console** in the family of Linear, Stripe's dashboard and
Intercom — quiet chrome, the data at full contrast, one obvious action per screen, and nothing on
the page that does not carry information. Personality comes from precision (alignment, real
numbers, honest empty states, good copy), not from decoration.

The previous look leaned on devices that read as generated: tiny uppercase monospace labels on
every heading and table header, a gradient logo bar, a tinted icon tile on every KPI, a green
inset bar and green tint on every hovered row, staggered fade-in on every row and card, and lifts on
hover. All of those are gone. Structure now comes from one tight radius, hairline borders on white
surfaces over a near-white ground, sentence-case labels, and spacing on a 4px scale. Motion is
reserved for things that appear (menus, dialogs, toasts) and respects `prefers-reduced-motion`.

The brand stays green — this is a WhatsApp messaging product — but the green had to change. The old
`#0FAF58` gave white text only 2.9:1. The primary is now **`#0B7340`** (white text 5.9:1), used for
the one primary button per screen, the active nav icon, focus rings, switched-on toggles, checkboxes
and success states. The bright green survives only as `--brand-bright` for the logo and non-text
decoration. Everything else is neutral: the accent is never used as a large surface or on chrome.
**One typeface (Inter)**; JetBrains Mono only for IDs, usernames, keys and codes. **Radius is tight
and consistent**: 4px chips, 6px controls, 8px surfaces. Light theme only.

## 2. Colour

All tokens live once in `static/css/theme.css` (`:root`). Templates and page CSS use only
`var(--token)`; no hex in templates. Every text token reaches **4.5:1 on white and on `--bg`**.

| Role | Token | Value | Contrast / use |
|---|---|---|---|
| Primary fill, primary text-link | `--primary` | `#0B7340` | white on it 5.9:1 |
| Primary hover / active | `--primary-hover` / `--primary-active` | `#095F35` / `#084F2C` | |
| Selected / success surface | `--primary-tint` / `--primary-tint-2` (border) | `#E8F5EE` / `#D3EDDF` | |
| Text on primary tint | `--primary-ink` | `#085C33` | 7.1:1 on tint |
| Logo / decoration only | `--brand-bright` | `#0FAF58` | never text, never under text |
| Page ground, sidebar, topbar | `--bg` | `#F6F7F7` | |
| Surfaces (cards, tables, modals) | `--bg-card` | `#FFFFFF` | |
| Inset well, read-only field, modal footer | `--bg-subtle` | `#FAFBFB` | |
| Hover / active-nav | `--bg-hover` / `--bg-active` | `#F0F2F2` / `#E7EAEA` | |
| Surface edge | `--border` | `#E3E6E6` | |
| Row rule, inner divider | `--rule` | `#ECEEEE` | |
| Control border (input, secondary button) | `--control` / `--control-hover` | `#D0D5D5` / `#B4BBBB` | see Open Question 18 |
| Text: primary | `--text` | `#141717` | 18:1 |
| Text: body / secondary cells | `--text-2` | `#3D4444` | 10:1 |
| Text: labels, meta, table headers | `--text-3` | `#5C6363` | 6.1:1 |
| Text: placeholder, hint, count | `--text-4` | `#646B6B` | 5.4:1 |
| Decorative icon (not text) | `--icon` | `#7A8282` | |
| Danger | `--danger` / `-hover` / `-tint` / `-border` / `-ink` | `#B42318` / `#912018` / `#FEF3F2` / `#FECDCA` / `#912018` | 6.6:1 |
| Warning (new) | `--warning` / `-tint` / `-border` / `-ink` | `#DC6803` / `#FFF8EB` / `#FEDF89` / `#93370D` | ink 7:1 on tint |
| Info | `--info` / `-tint` / `-border` / `-ink` | `#2E6BD6` / `#EFF5FF` / `#C7DAFE` / `#1849A9` | ink 7.5:1 on tint |
| Success | `--success*` | aliases of the primary family | |
| Neutral chip | `--neutral-tint` / `-border` / `-ink` | `#EEF0F0` / `#E0E3E3` / `#3D4444` | |

**Status mapping** (badges, `layouts/list_view.html`): success = active / completed / ready /
dispatched / settled; info = in progress / sending; neutral = inactive / waiting / expired / draft /
role names; warning = needs attention but not failed; danger = failed / cancelled / rejected only.
"Inactive" is neutral, not red: red is a signal, not a texture.

**Rules**
- One filled primary button per screen. Destructive actions use the danger styles, never primary.
- The accent goes on actions and on data, never on chrome: the active nav row is a neutral grey
  pill (only its icon is green), tabs underline in green but their label is ink, hover is grey.
- WhatsApp's own colours (`#25D366`, wallpaper `#E5DDD5`/`#ECE5DD`) only inside WhatsApp message
  previews (`static/css/templates.css`, `broadcast/campaign_new.html`, `companies/detail.html`).
- Light theme only. No dark-mode rules.

**Legacy aliases** (defined at the bottom of `theme.css`, for pages not yet migrated — never in new
code): `--green`→`--primary`, `--green-dark`→`--primary-hover`, `--green-tint`→`--primary-tint`,
`--green-gradient*`→ a flat primary fill, `--line`→`--border`, `--line-2`→`--control`,
`--black`/`--ink`→`--text`, `--ink-2`→`--text-2`, `--muted`→`--text-3`, `--subtle`→`--text-4`,
`--red*`→`--danger*`, `--orange*`→`--warning*`, `--blue*`→`--info*`, `--f-disp`→`--f-ui`,
`--shadow-card`/`--shadow-lift`/`--shadow-green`→`none`, and the five that were used but never
defined: `--text-muted`, `--bg-secondary`, `--surface`, `--accent` (plus `--border`, `--text`,
which are now real tokens). Because `--green` now points at the accessible primary, every legacy
green button and link became AA-compliant without touching its page.

## 3. Typography

Loaded from Google Fonts in `app/templates/base.html` (Inter 400/500/600/700, JetBrains Mono
400/500) and `login.html` (Inter only). Space Grotesk is retired.

| Token | Family | Use |
|---|---|---|
| `--f-ui` | Inter | everything |
| `--f-mono` | JetBrains Mono | IDs, usernames, API keys, codes, E.164 numbers in code contexts. Never for labels or headers. |

**Scale** — six sizes, each with a job:

| Token | Size | Role |
|---|---|---|
| `--fs-xs` | 12px | badges, table headers, meta, hints, counts |
| `--fs-sm` | 13px | table cells, labels, buttons, breadcrumbs, menu items |
| `--fs-base` | 14px | body, inputs, nav items, card titles |
| `--fs-md` | 16px | modal title, toolbar title on legacy pages |
| `--fs-lg` | 20px | KPI values on phones, page title on phones |
| `--fs-xl` | 24px | page title (h1), KPI values |

Weights: 400 body, 500 labels/buttons/nav, 600 titles and the primary cell. 700 is not used by
shared components. Letter-spacing 0 except titles (−0.015em). **Sentence case everywhere**; no
uppercase tracked labels. `font-variant-numeric: tabular-nums` on table cells and KPI values
(`.num` utility for anything else). Nothing below 12px.

## 4. Components

`static/css/base.css` holds the reset, the shell and the global primitives; `static/css/docview.css`
holds page patterns. Both are loaded once by `base.html` — pages must not link them again.

**Buttons** — `.dv-btn` + one variant (+ optional size):
- `.dv-btn-primary` — filled green. One per screen.
- `.dv-btn-secondary` — white, `--control` border. Cancel, Back, secondary actions.
- `.dv-btn-tertiary` — text only, grey hover. Low-weight actions ("Clear selection", "Change").
- `.dv-btn-danger` — white with red text and border: a destructive *trigger*.
- `.dv-btn-danger-solid` — red fill: the final confirm of a destructive action.
- Sizes: default 34px, `.dv-btn-sm` 28px, `.dv-btn-lg` 40px; `.dv-btn-icon` makes it square
  (give it `aria-label`); `.dv-btn-block` full width. On touch screens buttons grow to ≥40px.
- Loading: put `<span class="btn-label">…</span><span class="btn-spinner"></span>` inside and toggle
  `.loading` (example: Save in `layouts/form_view.html`).
- Legacy names that still render correctly: `.dv-btn-ghost` (= secondary), `.dv-btn-xs` (= sm),
  `.btn` (= primary), `.btn.btn-outline` (= secondary), `.btn-sm`.

**Form controls** (`base.css`, `docview.css`)
- `.dv-field` (column, 6px gap) > `label.dv-label` (13/500, `for=` the input) + control + `.dv-hint`.
  Required: `<span class="req" aria-hidden="true">*</span><span class="sr-only">(required)</span>`.
- `.dv-input`, `.dv-select` (custom chevron), `textarea.dv-input`: 34px, 6px radius, white,
  `--control` border, green border + `--ring` on focus, `aria-invalid="true"` or `.error-border`
  for errors, `[readonly]`/`:disabled` render on `--bg-subtle`. 40px and 16px text on phones.
- `.dv-toggle-wrap > label.dv-toggle > input + .dv-toggle-track` + `.dv-toggle-label` — 34×20 switch.
- Checkboxes: native, `accent-color: var(--primary)`, 16px (`.dv-check`, `.dv-row-check`).
- `.dv-field-static` — a create-only value shown on an existing record.
- `.dv-secret-wrap` / `.dv-secret-saved` — masked secret with a Change button.

**Badges** — `.dv-badge` + `-success | -danger | -warning | -info | -neutral | -outline`, optional
`.dv-badge-dot` (6px dot in the text colour). 22px, 4px radius, 12/500, sentence case, pale tint +
dark ink of the same hue. One status mark per row. Legacy: `-green/-red/-blue/-gray/-amber` and
`.badge.green|red|blue|gray|orange`.

**Cards** — `.card` (white, `--border`, 8px radius, no shadow) > `.card-header` (`.card-title`,
`.card-sub`) + `.card-body` (`.p-0` for a flush table) + optional `.card-footer`. KPI tiles:
`.stats-grid > .stat-card > .stat-info(h3 value + p label) + .stat-icon` — label under value,
icon plain grey, no tinted tile, no hover lift.

**Tables** — `.dv-table-wrap > table.dv-table` (`.table-wrap > table.table` is the legacy twin).
12px grey sentence-case headers on the surface (no band), 44px rows, `--rule` row dividers, grey
hover, `td.num`/`th.num` right-aligned with tabular figures. Inside `.dv-list-card`, cells don't
wrap and the wrapper scrolls sideways — the page never does. Cell helpers: `.cell-bold`,
`.cell-mono`, `.cell-muted` (missing value `—`).
- **Clickable rows**: the row keeps its `onclick` for the pointer, and the record's name cell is a
  real link (`a.dv-row-link`) — that is the keyboard path. The trailing `.dv-row-open` chevron is
  `aria-hidden` and `tabindex="-1"`.

**Page header** — `.dv-page-header` > (`h1.dv-title` 24/600 + `p.dv-subtitle` — the count with its
noun, "12 users") + `.dv-actions`.

**Breadcrumb** — the topbar is the breadcrumb (`base.html` block `topbar_crumbs`): on a list it
shows "Group / Page"; a page with a parent overrides the block with a link back
("Users / rahul.menon", see `form_view.html`). `.dv-breadcrumb` remains for in-page use on legacy
pages.

**Toolbar + search** — `.dv-toolbar` > `.dv-search` (`role="search"`) > `.dv-search-icon` +
`input[type=search].dv-search-input` with an `aria-label`.

**List card** — `.dv-list-card` holds the bulk bar, the table wrap, the no-match state and the empty
state. `.dv-list-meta` (title + count strip) is legacy; new lists put the count in the subtitle.

**Bulk bar** — `.dv-bulkbar(.visible)` > `.dv-bulkbar-count` + `.dv-btn-tertiary` "Clear selection"
+ `.dv-btn-danger` "Delete". Hidden until a row is ticked. Delete goes through `confirmDialog()`.

**Empty state** — `.dv-empty` > `i.dv-empty-icon` (40px neutral disc) + `.dv-empty-text`
("No users yet") + `.dv-empty-hint` (one sentence or one link). Search no-match uses the same block.

**Pager** — `{% include "layouts/_pagination.html" %}` (needs `page`, `page_url`); styles `.pg-*`
are shared in `docview.css`. `nav[aria-label=Pagination]`, current page `aria-current="page"`,
prev/next labelled. Renders nothing for one page.

**Tabs** — `.dv-tabs > button.dv-tab[role=tab]` + `.dv-panel[role=tabpanel]` (`.active` on both).
Underline style: ink label, green 2px underline. Arrow keys move between tabs (see the switcher in
`form_view.html`). `.cd-tabs / .cd-tab / .cd-panel` are the same component under the old name and
render identically.

**Form view** — `.dv-form-toolbar.dv-form-toolbar--bleed > .dv-form-toolbar-inner` (sticky under the
topbar, title left, actions right, aligned to the 880px form column; gains a bottom rule only when
stuck via `.is-stuck`), then `.dv-form-wrap` > `.dv-form-error` banner > sections. A plain
`.dv-form-toolbar` (no modifier) is the white bar-card used by legacy pages.
Sections: `.dv-section` > `.dv-section-header > h2.dv-section-label` + `.dv-section-body`
(2-column grid, 20px/24px gaps; one column ≤640px; `.dv-field.col-2` spans both).
Save feedback: `.dv-save-status.success|error` beside the buttons ("Saved").
Delete: `.dv-btn-danger` "Delete" swaps in place for `.dv-delete-confirm` ("Delete this user?
Cancel / **Delete**"), focus moves to Cancel.

**Alerts / inline feedback** — `.dv-alert` + `-danger | -success | -warning` (info by default):
6px radius, tint + border + ink. Legacy names `.dv-inline-error` / `.dv-inline-success` render
identically. A failure is reported inside the block that failed.

**Modal** — the one implementation, `z-index: var(--z-modal)`:
```html
<div class="modal-backdrop" id="xBackdrop">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="xTitle">   <!-- .modal-lg = 640px -->
    <div class="modal-header"><h2 class="modal-title" id="xTitle">…</h2>
      <button type="button" class="modal-close" aria-label="Close"><i class="ti ti-x"></i></button></div>
    <div class="modal-body"> .modal-field > .dv-label + .dv-input + .dv-hint … </div>
    <div class="modal-footer"> secondary Cancel, then the primary </div>
  </div>
</div>
```
Open/close with `wamModal.open(el, {onClose})` / `wamModal.close(el)` from `base.html`: Esc, focus
into the first field, Tab trapped inside, focus returned on close. On phones it becomes a bottom
sheet with full-width stacked buttons. Reference: Change password in `base.html`.

**Confirm dialog** — `await confirmDialog({ title, message, items, moreIndex, confirmLabel,
cancelLabel, danger })` → `true|false`. The replacement for `confirm()`. Name the object and the
consequence ("Delete 7 users?" / "Everything inside them is deleted too. This cannot be undone." /
button "Delete 7 users"). Reference: bulk delete in `layouts/list_view.html`.

**Toast** — `toast(message, type?, { timeout })`, type `success | error | warning | info`. Bottom
right (full width on phones), 4s (errors 7s), pauses on hover, dismiss button, errors are
`role=alert`. The replacement for `alert()`. For a message that must survive a reload, set
`sessionStorage.wam_flash = JSON.stringify({msg, type})` before reloading; `base.html` shows it on
the next page.

**Session-expired banner** — `showSessionBanner()` in `base.html`, styled by `.session-banner`
(`--z-banner`).

**Timestamps** — `{{ ts(value) }}` / `ts(value, 'date')` / `ts(value, 'time')` from
`app/core/templating.py` (see CLAUDE.md). Rows injected after load call `renderTimestamps(root)`.

## 5. Layout

**Skeleton** (`app/templates/base.html`, `base.css`):
```
a.skip-link → #main
.dash-layout
├── aside.sidebar#sidebar (fixed, --sidebar-w, on --bg, right hairline)
│     a.sidebar-logo (mark + "SIL WhatsApp" + "Business messaging")
│     nav.sidebar-nav: Dashboard, then sentence-case .nav-label groups of .nav-item
├── .sidebar-overlay (phone/tablet scrim)
└── .dash-main
      header.dash-header (sticky, --topbar-h 52px, on --bg): menu button · nav.topbar-crumbs · user menu
      main.dash-content#main  ← {% block content %}
```
Blocks a page fills: `title`, `extra_css`, `topbar_crumbs` (optional), `content`, `extra_js`.
Sidebar items come from the `nav()` macro in `base.html`, which sets `.active` and
`aria-current="page"`.

| Measure | Value |
|---|---|
| Sidebar | 232px (248px ≥1920); drawer ≤900px |
| Topbar | 52px |
| Page padding `--page-pad` | 32px · 24px ≤1024 · 16px ≤640 · 40px ≥1920 |
| Form column `--form-max` | 880px |
| Lists | fluid, full width |

**Spacing** — 4px base: `--s-0_5` 2 · `--s-1` 4 · `--s-1_5` 6 · `--s-2` 8 · `--s-2_5` 10 ·
`--s-3` 12 · `--s-4` 16 · `--s-5` 20 · `--s-6` 24 · `--s-8` 32 · `--s-10` 40 · `--s-12` 48.

| Context | Token |
|---|---|
| Page header → content | `--s-6` |
| Toolbar → list card | `--s-3` |
| Between cards / sections / grid gap | `--s-4` |
| Card / section / modal padding | `--s-5` |
| Field gap: row / column | `--s-5` / `--s-6` |
| Label → control | `--s-1_5` |
| Button group gap | `--s-2` |
| Icon → text in a button | `--s-1_5` |

**Control heights** — `--h-sm` 28 · `--h-md` 34 (default) · `--h-lg` 40 (sign-in, phones).

## 6. Depth, radius, layers, motion

- **Radius**: `--r-sm` 4px (badges, checkbox), `--r-md` 6px (buttons, inputs, menu items, pager),
  `--r-lg` 8px (cards, tables, sections, modals, toasts), `--r-full` (toggle, avatar, dots).
- **A border or a shadow, never both at rest.** Surfaces are white with a `--border` edge and no
  shadow. Controls get `--shadow-xs`. Shadows belong to what floats: `--shadow-menu` (dropdown,
  toast), `--shadow-pop` (modal, mobile drawer). No hover lifts.

**z-index layers** (tokens in `theme.css`)

| Layer | Token | Value |
|---|---|---|
| Sticky form toolbar / sticky headers | `--z-sticky` | 40 |
| Topbar | `--z-topbar` | 50 |
| Drawer scrim | `--z-overlay` | 90 |
| Sidebar | `--z-sidebar` | 100 |
| Dropdowns, popovers | `--z-dropdown` | 200 |
| Modal (`.modal-backdrop`) | `--z-modal` | 300 |
| Toasts | `--z-toast` | 400 |
| Session banner | `--z-banner` | 500 |

Legacy page modals still use 200 / 9999 until migrated (§12).

**Motion** — `--t-fast` 120ms, `--t-base` 180ms, `--ease`, `--ease-out`. Only: menu/popover in
(fade + 4px), modal in (fade + 8px + scale .98), toast in, drawer slide, colour transitions on
hover. No entrance animation on pages, cards or rows (`pageEnter`, `cardIn`, `rowIn` are kept as
no-op keyframes so page CSS that names them does nothing). `prefers-reduced-motion: reduce` turns
all of it off except spinners, which keep turning slowly.

## 7. Do's and don'ts

**Do**
- Extend `base.html`; page CSS in `{% block extra_css %}`, JS in `{% block extra_js %}`.
- Use tokens for every colour, size, space, radius, shadow and layer.
- Reuse the components in §4 before writing page CSS; if two pages need the same thing, it goes
  into `docview.css`.
- Use `toast()` and `confirmDialog()` — never `alert()` / `confirm()`.
- Give every icon-only control an `aria-label`, every input a `<label for>`, every icon
  `aria-hidden="true"`.
- Write sentence case, name the object in confirmations, use `—` for missing values.
- Print timestamps with `ts()`; build routes with `build_templates()` (CLAUDE.md).
- Gate actions on `perms[...]` the way `list_view.html` does.

**Don't**
- Hardcode hex in templates; add `border-radius: 0` or any other radius outside the scale.
- Use uppercase tracked labels, monospace for labels/headers, gradients, glows, tinted icon tiles,
  coloured left-border accents, hover lifts, or staggered entrance animations.
- Use the green on chrome (nav rows, tab labels, header bars) or as a large fill.
- Use Font Awesome in new code — Tabler only (`<i class="ti ti-name" aria-hidden="true"></i>`).
- Link `docview.css` again from a page, or invent another `xx-modal` / `xx-btn` / `xx-badge`.
- Let a table scroll the page sideways.
- Add fonts, CSS frameworks, icon sets or dark-mode rules. Chart.js 4.4.0 (analytics) is the
  only charting library.

## 8. Responsive

| Width | What changes |
|---|---|
| ≤ 640px | page padding 16px; one-column form sections; inputs 40px/16px text; modal becomes a bottom sheet; toasts full width; topbar shows only the page (and the parent link) |
| ≤ 900px | sidebar becomes a drawer (`body.sidebar-open`, scrim, Esc closes, focus moves in and back); menu button appears; nav rows 44px |
| ≤ 1024px | page padding 24px; `.grid-2`/`.grid-3` stack |
| ≥ 1920px | sidebar 248px, page padding 40px (type does not scale up) |
| `pointer: coarse` | buttons ≥40px, pager links 40px |

Tables keep one line per cell and scroll inside `.dv-table-wrap`. Page-local breakpoints on feature
screens (560/600/700/860/1000/1100) are migrated to 640/900/1024 in phases 2–3.

## 9. Agent prompt guide

1. **Read first**: `CLAUDE.md`, `PROJECT_MAP.md`, this file.
2. **Route**: `templates = build_templates()`; `ctx=Depends(get_page_user)`; check
   `ctx["perms"][resource]["read"]`; apply `company_filter(user)`; pass `user`, `perms`, `active`.
3. **Simple CRUD** → no template: add a doctype to `app/core/doctypes.py` and render
   `layouts/list_view.html` / `layouts/form_view.html` (copy `USERS_DOCTYPE`). Column types:
   `text`, `mono`, `status`, `service_status`, `role_tag`, `datetime`/`date`/`time`.
4. **Custom list** → copy the structure of `layouts/list_view.html`: `.dv-page-header` (h1 + count),
   `.dv-toolbar`, `.dv-list-card` > `.dv-bulkbar` + `.dv-table-wrap > .dv-table` + `.dv-empty`.
5. **Detail / settings page** → topbar crumbs block with a parent link, a bleed
   `.dv-form-toolbar`, `.dv-form-wrap`, `.dv-section` cards, `.dv-tabs` for areas.
6. **Dashboard / KPIs** → `.stats-grid` + `.stat-card` (label + value, no icon tiles).
7. **Paged table** → `{% include "layouts/_pagination.html" %}`.
8. **Dialogs** → the `.modal` markup in §4 + `wamModal`; for yes/no, `confirmDialog()`.
9. **Feedback** → `toast()`; inline `.dv-alert-*` inside the failing block; `.dv-save-status` for "Saved".
10. **Icons** → Tabler webfont only.
11. **API calls** → `fetchWithAuth(url, options)`.
12. **New sidebar entry** → one `{{ nav(href, key, 'ti-icon', 'Label', active) }}` line in
    `base.html` under the right `.nav-label`, gated by `{% if perms.<resource>.read %}`; add its
    group and display name to `nav_groups` / `nav_names` at the top of `base.html`.

## 10. Components inventory

| Need | Use | File |
|---|---|---|
| Tokens | `:root` | `static/css/theme.css` |
| Shell, user menu, change-password modal | `base.html` | `app/templates/base.html`, `static/css/base.css` |
| Mobile drawer | `sidebar.js` | `static/js/sidebar.js` |
| Generic list / form | `layouts/list_view.html`, `layouts/form_view.html` | + `app/core/doctypes.py` |
| Pager | `layouts/_pagination.html`, `.pg-*` | `static/css/docview.css` |
| Buttons | `.dv-btn-*` | `base.css` |
| Inputs, select, textarea, checkbox | `.dv-input`, `.dv-select`, `.dv-check` | `base.css` |
| Toggle, static value, secret | `.dv-toggle*`, `.dv-field-static`, `.dv-secret-*` | `docview.css` |
| Badges | `.dv-badge-*` | `base.css` |
| Card, KPI tile | `.card*`, `.stat-card` | `base.css` |
| Table | `.dv-table-wrap > .dv-table` | `base.css`, `docview.css` |
| Page header, toolbar, search | `.dv-page-header`, `.dv-toolbar`, `.dv-search` | `docview.css` |
| Bulk bar, empty state | `.dv-bulkbar`, `.dv-empty` | `docview.css` |
| Tabs | `.dv-tabs` (= `.cd-tabs`) | `docview.css` |
| Form toolbar, sections | `.dv-form-toolbar--bleed`, `.dv-section` | `docview.css` |
| Alerts | `.dv-alert-*` | `base.css` |
| Modal / confirm / toast | `.modal`, `wamModal`, `confirmDialog()`, `toast()` | `base.css`, `base.html` |
| API keys panel (company detail) | `.ak-*` | `docview.css` (legacy, migrate in phase 2) |
| WhatsApp message preview | `.tpl-*` | `static/css/templates.css` |
| CSV import dialog | `.im-*` + `static/js/csv.js` | `static/css/import-dialog.css` |
| Charts | Chart.js 4.4.0 (CDN) | `analytics/index.html` |
| Timestamp | `ts()` / `renderTimestamps()` | `app/core/templating.py`, `base.html` |
| Authenticated fetch | `fetchWithAuth()` | `base.html` |

## 11. Verification

- **Dev server**: `venv/bin/uvicorn app.main:app --reload` → http://127.0.0.1:8000 (a worktree
  preview may run on :8001). `curl -s -o /dev/null -w '%{http_code}' …/login` → 200.
- **Login** needed for every page except `/login`. Check permission-gated pages as an `admin`, not
  only as super_admin.
- **Reference screens**: `/users` (list), `/users/<id>` (form with tabs), `/users/new`, `/login`.
- **Widths**: 1440, 768, 390. At 390: drawer opens and closes, tables scroll inside their frame,
  no page-level horizontal scroll, modals are bottom sheets.
- **Keyboard**: Tab reaches the skip link, nav, the row name links, tabs (arrow keys), the user menu
  (arrow keys, Esc), dialogs (Esc, trapped Tab).
- **View-only browsing**: never send a WhatsApp message or submit a write; never click Save /
  Delete / Send / Launch / Approve / Confirm / Connect. SHIRIN ASAL has live WhatsApp config.
- Without a browser session, templates can be rendered offline with sample context through
  `build_templates()` and screenshotted (phase 1 did this; see its report).
- Tests: `venv/bin/pytest tests/ -q` must pass.

## Open questions — status after phase 1

| # | Question | Status |
|---|---|---|
| 0 | Brand contrast fails AA | **Resolved.** Primary `#0B7340` (white 5.9:1); all text tokens ≥4.5:1 on white and `--bg`; bright green kept as `--brand-bright` for decoration only. Legacy `--green` aliases the new primary. |
| 1 | Two icon sets | **Decided: Tabler.** Shell, list, form, pager, login converted; login no longer loads FA. FA stays linked in `base.html` until phases 2–3 finish (§12), then the link is deleted. |
| 2 | 73 hardcoded hex values | **Rule set**, tokens available (neutral, warning, info). Page hexes are phase 2–3 work. |
| 3 | A second (Tailwind) green | **Decided:** one green family (`--primary*`). Pages using `#16a34a` etc. migrate in phase 2. |
| 4 | Missing warning / blue-dark / orange-dark | **Resolved:** `--warning*` (amber), `--info-ink`, status `-ink` tokens. |
| 5 | Undefined CSS variables | **Resolved:** all six now resolve (`theme.css` legacy block). |
| 6 | Radius drift | **Decided:** 4 / 6 / 8 / full. Square corners are retired; pages that set `border-radius: 0` or 7/12px migrate in phases 2–3. |
| 7 | Duplicated components | **Decided:** `.modal`, `.dv-btn`, `.dv-badge`, `.dv-table`, `.dv-tabs` are canonical; legacy families listed in §12. |
| 8 | Page-local CSS volume | Open — phases 2–3 (20 templates with `<style>`, ~290 inline styles). |
| 9 | `cd-` prefix collision | **Decided:** shared tabs are `.dv-tabs`; `.cd-tabs` kept as an alias. Conversation detail's `cd-*` page classes get renamed (`cv-`) in phase 3. |
| 10 | No toast; native dialogs | **Resolved for shared layouts:** `toast()`, `confirmDialog()`, `wamModal`. Remaining `alert()`/`confirm()` in pages listed in §12. |
| 11 | Font weights / 31 sizes | **Resolved for shared CSS:** one family, six sizes. Pages migrate. |
| 12 | Spacing tokens, 17 breakpoints | **Resolved for shared CSS:** spacing scale + 640/900/1024/1920. Pages migrate. |
| 13 | Narrow-screen tables | **Resolved:** `.dv-table-wrap` scrolls inside the card; cells don't wrap. |
| 14 | Accessibility | **Resolved for the shell and layouts:** reduced motion, `aria-current`, skip link, labelled controls, keyboard row links, ARIA tabs, focus-managed dialogs and drawer, ≥40px touch targets on coarse pointers. |
| 15 | Duplicate stylesheet, dead code, inline banner | **Resolved in shared files:** dead `logoutBtn` wiring removed from `sidebar.js`; banner uses `.session-banner`; `list_view`/`form_view` no longer re-link `docview.css` or `whatsapp.css`. 8 page templates still re-link `docview.css` (§12). |
| 16 | Orphaned templates | Open — `companies/list.html`, `users/list.html`, `broadcast/coming_soon.html` are unrouted; recommend deleting in phase 3. |
| 17 | `company_tag` cell type | Open — still renders as text; add a branch when a doctype uses it. |
| 18 | Input border contrast | New, accepted: `--control` `#D0D5D5` is 1.5:1 against white, below WCAG 1.4.11's 3:1 for a component boundary. Kept deliberately (as GitHub Primer and Stripe do): every field has a visible label above it, a 1px shadow, and a 3:1+ focus state. Revisit if an audit requires strict 1.4.11. |
| 19 | Doctype icons | New: `doctypes.py` still names Font Awesome icons for 5 doctypes and its tab icons. The generic layouts no longer draw them (empty states use a Tabler icon; tabs are text). Phase 3: switch the strings to Tabler or drop the key. Placeholder copy there ("John Doe", "e.g. john_doe") should become neutral examples. |

## 12. Migration checklist — phases 2 and 3

Counts are from the working copy after phase 1. "Migrate" means: delete page-local CSS that a §4
component covers, swap classes to the canonical ones, replace Font Awesome with Tabler, replace
`alert()`/`confirm()` with `toast()`/`confirmDialog()`, replace hex with tokens, drop
`border-radius: 0`, uppercase/mono labels and entrance animations, remove the extra
`docview.css` link, and check 1440/768/390.

**Phase 2 — the heavy, most-used screens**
- [ ] `companies/detail.html` — 109 FA icons, 7 `alert/confirm`, 84 inline styles, `.cd-*` tabs,
      `.ak-*` API-key panel + modal (move to `.modal`), undefined-var usage, radius drift,
      re-links `docview.css`.
- [ ] `broadcast/campaign_new.html` — 14 FA, 42 inline styles, `.cn-modal` → `.modal`, its own
      sticky bar (z 60), re-links `docview.css`.
- [ ] `broadcast/campaign_list.html` — 6 `alert/confirm` (bulk delete → `confirmDialog`),
      `.cl-modal`, `.cl-status` → `.dv-badge-*`, page bulk-bar CSS → shared `.dv-bulkbar`.
- [ ] `broadcast/campaign_detail.html` — its own pager markup → include `_pagination.html`;
      2 `alert/confirm`; sticky bar z 60.
- [ ] `broadcast/phonebook_detail.html` — 6 `alert/confirm`, `.bl-modal`, 1 FA.
- [ ] `broadcast/index.html` — page CSS, uppercase labels.
- [ ] `dashboard/index.html` — welcome copy, KPI row on the new `.stat-card`, `info-grid` mono
      labels, recent-activity table → `.dv-table`.
- [ ] `users/_agent_config.html` — 2 FA, 1 `confirm`, 18 inline styles, re-links `docview.css`,
      `.ak-delete-confirm`.
- [ ] `static/css/whatsapp.css` (911 lines) and `static/css/templates.css` (686) — tokens,
      radius, `.wa-`/`.tpl-` modals (z 9999) → `.modal`, `.wa-btn`/`.tpl-btn` → `.dv-btn`.

**Phase 3 — the rest**
- [ ] `conversations/detail.html` — rename page `cd-*` → `cv-*`; chat bubbles keep their shape.
- [ ] `services/detail.html` — uppercase/mono labels.
- [ ] `analytics/index.html` — 1 `alert`, `--surface`/`--accent` usage, radius 8–12px, chart
      colours from tokens (one hue per chart).
- [ ] `error_logs/list.html`, `error_logs/detail.html` — 13 FA, `.el-*` badges/buttons.
- [ ] `roles/list.html`, `roles/permissions.html` — 10 FA, 21 inline styles.
- [ ] `webhook_config/index.html` — 7 FA, hex greys, radius 6–8px, `.wc-*` badges/buttons.
- [ ] `demo/messaging.html` — 8 FA, worst hex offender.
- [ ] `static/css/import-dialog.css` — `.im-*` modal → `.modal`.
- [ ] `app/core/doctypes.py` icons → Tabler (Open Question 19).
- [ ] Delete orphans `companies/list.html`, `users/list.html`, `broadcast/coming_soon.html`.
- [ ] Remove the `.ak-*` block from `docview.css`, the legacy aliases from `theme.css` and the
      legacy class aliases from `base.css` once `grep` finds no users.
- [ ] **Last:** delete the Font Awesome `<link>` from `base.html` when
      `grep -rE 'fa-|"fas |fab |far ' app/templates static` returns nothing.
