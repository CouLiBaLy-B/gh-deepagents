---
name: i18n-conventions
description: "Internationalization catalogue conventions: key naming, placeholder syntax, locale parity, never auto-translating. Use when touching translation files or user-facing strings."
license: MIT
---

# Skill: i18n-conventions

## Universal rules

- The **reference locale** holds the source of truth (usually English).
- Every secondary locale MUST have the same key set as the reference.
- Translations not yet done = English value with `TRANSLATE_ME:` prefix.
- Never auto-translate. Translators do that.
- Keep keys **sorted alphabetically** in JSON catalogues for clean diffs.

## Per-stack conventions

### Python — gettext / Babel
- Catalogues: `locale/<lang>/LC_MESSAGES/messages.po`.
- Wrap strings as `_("...")` or `gettext("...")`.
- Always use full sentences as keys; never concatenate.
- Run `pybabel extract` then `pybabel update` to refresh `.po` files.

### Python — Django
- `gettext_lazy` in models / class-level (not `gettext`, which evaluates at import).
- `{% trans "..." %}` in templates.
- `python manage.py makemessages -l <lang>` to refresh.

### JS / TS — i18next
- Use namespaces (`t("common:save")`) to keep files small.
- Catalogues under `public/locales/<lang>/<namespace>.json`.
- `i18next-parser` config drives extraction; run it on every UI change.

### JS / TS — react-intl / formatjs
- Use `defineMessages` so the extractor picks up `id` + `defaultMessage`.
- Keys are explicit IDs (`app.button.save`), not raw text.

### Go — go-i18n
- TOML files under `i18n/<lang>.toml`.
- Use `i18n.Localize(&i18n.LocalizeConfig{...})`, never `fmt.Sprintf` user-facing.

## What counts as user-facing

- UI labels, button text, error messages shown to users
- Email subjects/bodies, notification text
- CLI `--help` strings (often skipped — confirm with the project)

What does NOT need i18n:
- Log messages (developer-facing)
- API error codes (machine-readable)
- Test fixture strings

## Quality checks before PR

- [ ] `i18n_check_parity` returns OK for every locale (or only `TRANSLATE_ME:` placeholders for new keys)
- [ ] No raw string introduced in the diff for a user-facing element
- [ ] Plural forms use the framework's plural API, not `if count == 1` branches
- [ ] Numbers/dates formatted with the framework's intl tool (not hardcoded)

## Failure modes

- "Added a new string but forgot to extract" → catch with `i18n_extract` + parity check
- "Translated key removed by accident" → parity check flags it as `missing`
- "Same string with two keys" → consolidate manually; ast-grep can help find call sites
