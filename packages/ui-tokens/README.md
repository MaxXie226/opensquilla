# OpenSquilla UI Tokens

Public, product-neutral design token and theme contracts for OpenSquilla
clients. The package owns the semantic token names, the built-in value-theme
palettes, and a diagnostic resolver for product-supplied theme extensions.

Public exports:

- `@opensquilla/ui-tokens` — typed token names and theme resolution helpers.
- `@opensquilla/ui-tokens/foundation.css` — typography, spacing, radius,
  motion, focus, and derived semantic defaults.
- `@opensquilla/ui-tokens/themes.css` — the public built-in value themes.
- `@opensquilla/ui-tokens/theme-contract.json` — machine-readable theme roles.
- `@opensquilla/ui-tokens/themes/<id>` — one built-in palette.

Imports from `src/` or any other undeclared path are unsupported.
