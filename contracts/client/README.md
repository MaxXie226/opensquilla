# Gateway client contract

`v3/` is the generated, machine-comparable baseline of the current Gateway
surface. It records transport frames, the locked RPC name/scope inventory,
advertised and observed events, final ASGI routes, and synthetic golden frames.

Do not edit generated JSON by hand. Regenerate it with:

```bash
uv run python scripts/export_gateway_client_contract.py
```

CI-style verification is read-only:

```bash
uv run python scripts/export_gateway_client_contract.py --check
```

The baseline is descriptive. It explicitly records known differences between
declared Pydantic models and the current hand-written wire parser; it does not
change protocol v3 behavior or make every RPC payload strongly typed.
