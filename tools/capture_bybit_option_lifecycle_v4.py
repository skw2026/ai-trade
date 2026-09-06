#!/usr/bin/env python3
"""V4 identity wrapper for the deployment-safe sticky lifecycle collector."""

from __future__ import annotations

from typing import Any

import capture_bybit_option_lifecycle_v3 as core


SCHEMA_VERSION = "bybit_btc_option_lifecycle_capture_v4"
SNAPSHOT_SCHEMA_VERSION = "bybit_btc_option_lifecycle_snapshot_v4"
STATE_SCHEMA_VERSION = "bybit_btc_option_lifecycle_state_v4"
CAPTURE_ROOT_NAME = "bybit_btc_option_lifecycle_v4"
FROZEN_POLICY_CANONICAL_SHA256 = "3057e78a46208d72ec4990ea9604744f0ed211e90b3c5f407a1132ee0927d253"
FROZEN_MANIFEST_CANONICAL_SHA256 = "ea77d54faa383ad5b5f21c520d2d90ecf8ea8b304af8be316f55602d16e20036"
POLICY_RELATIVE_PATH = "config/option_lifecycle_capture_v4.json"


def activate() -> None:
    core.SCHEMA_VERSION = SCHEMA_VERSION
    core.SNAPSHOT_SCHEMA_VERSION = SNAPSHOT_SCHEMA_VERSION
    core.STATE_SCHEMA_VERSION = STATE_SCHEMA_VERSION
    core.CAPTURE_ROOT_NAME = CAPTURE_ROOT_NAME
    core.FROZEN_POLICY_CANONICAL_SHA256 = FROZEN_POLICY_CANONICAL_SHA256
    core.FROZEN_MANIFEST_CANONICAL_SHA256 = FROZEN_MANIFEST_CANONICAL_SHA256
    core.POLICY_RELATIVE_PATH = POLICY_RELATIVE_PATH


activate()


def __getattr__(name: str) -> Any:
    return getattr(core, name)


if __name__ == "__main__":
    raise SystemExit(core.main())
