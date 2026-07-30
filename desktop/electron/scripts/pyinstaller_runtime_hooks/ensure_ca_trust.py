"""Compatibility import for the public Gateway Runtime CA hook."""

from scripts.gateway_runtime.ensure_ca_trust import ensure_frozen_default_ca_trust

ensure_frozen_default_ca_trust()
