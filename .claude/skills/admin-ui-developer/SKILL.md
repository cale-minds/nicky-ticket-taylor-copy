---
name: admin-ui-developer
description: Generates and edits the server-rendered Admin UI for this service. Trigger when working on HTML pages, UI components, translations (i18n), pagination, forms, styles, or any part of app/admin_ui.py.
license: MIT
metadata:
  author: Nicky
  version: '1.0'
---

# Admin UI Developer Guidelines (nicky-ticket-tailor-service)

The Admin UI is a FastAPI router that returns `HTMLResponse` with HTML generated in Python f-strings. **There is no frontend framework** — all markup lives in `app/admin_ui.py`.

## Non-negotiable rules

1. **HTML in Python f-strings** — all markup stays in `app/admin_ui.py`. Do not create separate `.html` files or Jinja2 templates.

2. **Tailwind CSS via CDN** — use Tailwind classes directly. Do not write custom CSS. Main palette: `slate-*` for neutrals, `black`/`zinc-800` for primary actions.

3. **Phosphor icons** — use `<i class="ph ph-{name}">` (same icon set as the Nicky frontend). Never use other icon sets.

4. **Buttons**: primary standard = `inline-flex h-10 items-center gap-2 rounded-lg bg-black px-4 text-sm font-semibold text-white hover:bg-zinc-800` with `<i class="ph ph-plus text-sm">` — no colored background on the icon. Follow the pattern from `nicky-button.component.ts` in the main frontend.

5. **i18n is mandatory** — every visible string uses `t("NAMESPACE.KEY")`. Never hardcode strings in any language directly in the HTML. When adding a new key, add it to **all** 12 translation JSON files (`app/translations/`). The server uses `lru_cache` — **restart after editing any JSON**.

6. **Escape output** — always use `e(value)` to HTML-escape and `u(value)` for URLs. Never interpolate database or user-supplied values directly into the HTML.

7. **Pagination** — use the existing `pagination_controls()` function. Visual pattern: `X - Y of Z` on the left, `Rows per page: N ▼  < N/M >` on the right.

8. **Logout** — use `window.location.replace()`, never `window.location.assign()` (prevents back-navigation).

9. **Test in the browser** — after any visual change, restart the server via `start-local-auth0-compat.bat` and verify at `http://localhost:4200/admin-ui/`.

## i18n

How translations work and the supported locales: [i18n.md](references/i18n.md)

## UI components

Helper functions and reusable component patterns: [components.md](references/components.md)
