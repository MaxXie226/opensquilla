# OpenSquilla UI Primitives

Public, browser-safe Vue primitives for OpenSquilla clients. The initial stable
surface includes Button, Input, Dialog, Stack, Card, and Switch. Components use
semantic tokens from `@opensquilla/ui-tokens` and contain no product routing,
state, Gateway, or native-bridge behavior.

Import components from `@opensquilla/ui-primitives` and load
`@opensquilla/ui-primitives/styles.css` once in the application composition
root. Imports from `src/` or other undeclared paths are unsupported.
