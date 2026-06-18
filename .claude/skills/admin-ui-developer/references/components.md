# UI components and helpers

All functions below are in `app/admin_ui.py`.

## Escape helpers

| Function | Use |
|---|---|
| `e(value)` | HTML-escape — use on all text from the database or user input |
| `u(value)` | URL-encode — use in URL segments |

## Layout

```python
render(title, body, *, user, request, settings, active_nav)
```
Generates the full HTML page with navbar, footer, i18n, and the user dropdown script.

## Pagination

```python
pagination_controls(
    page, page_size, total, path,
    query_params, page_param,
    size_param=None,        # e.g. "per_page"
    size_options=None,      # e.g. PAGE_SIZE_OPTIONS = [10, 25, 50]
)
```
Visual: `1 - 10 of 42` on the left · `Rows per page: 10 ▼  < 1/5 >` on the right.

Support helpers:
- `page_offset(page, page_size)` → integer offset
- `page_size_query_value(value, default)` → validates and normalizes page_size from query string
- `page_href(path, query_params, page_param, page)` → URL with page replaced

## Status badges

```python
nicky_status_badge(status)       # maps a status value to a styled badge
badge(label, ok: bool | str)     # ok=True (green), False (slate), "warn" (yellow)
```

## Primary button (Nicky standard)

```html
<a class="inline-flex h-10 shrink-0 items-center justify-center gap-2 rounded-lg bg-black px-4 text-sm font-semibold text-white hover:bg-zinc-800" href="...">
  <i class="ph ph-plus text-sm"></i>
  Label
</a>
```

## Edit button (table row)

```html
<a class="inline-flex h-9 items-center gap-2 rounded-lg bg-black px-4 text-sm font-semibold text-white hover:bg-zinc-800" href="...">
  <i class="ph ph-pencil text-sm"></i>
  Edit
</a>
```

## Role easter egg (navbar)

```python
user_easter_egg(user, settings)
```
Returns `👑` for Admin, `🛠️` for Support, empty string otherwise. Rendered inside the avatar button, between the initials and the chevron.

## Language switcher

```python
lang_switcher(current_path)
```
Dropdown with flags and names for all 12 locales. Saves the locale in the `lang` cookie.
