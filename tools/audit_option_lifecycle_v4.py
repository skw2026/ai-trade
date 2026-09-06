#!/usr/bin/env python3
"""Audit checksum-bound deployment-safe v4 lifecycle evidence."""

from typing import Any

import audit_option_lifecycle_v3 as core
import capture_bybit_option_lifecycle_v4 as capture


core.capture = capture
core.SCHEMA_VERSION = "option_lifecycle_audit_v4"


def __getattr__(name: str) -> Any:
    return getattr(core, name)


if __name__ == "__main__":
    raise SystemExit(core.main())
