#!/usr/bin/env python3
"""Reconcile deployment-safe v4 lifecycles under the frozen v2 payoff policy."""

from typing import Any

import audit_option_lifecycle_v4 as lifecycle_audit
import capture_bybit_option_lifecycle_v4 as capture
import audit_option_lifecycle_payoff_v3 as core


core.capture = capture
core.lifecycle_audit = lifecycle_audit
core.SCHEMA_VERSION = "option_lifecycle_payoff_audit_v2"
core.FROZEN_POLICY_CANONICAL_SHA256 = "4b0eb39aa8b8d75e37d9b7bde52e2a163b629168d85de22f699b0898c17d8931"
core.FROZEN_MANIFEST_CANONICAL_SHA256 = "a2fc098e992a6b09667dcce83dc94e4f2a98dc9dae89b7a7d0b924d8ffb0cfc8"
core.PAYOFF_POLICY_RELATIVE_PATH = "config/option_lifecycle_payoff_v2.json"
core.SOURCE_EXPERIMENT_ID = "btc_bybit_usdt_option_lifecycle_capture_v4"


def __getattr__(name: str) -> Any:
    return getattr(core, name)


if __name__ == "__main__":
    raise SystemExit(core.main())
