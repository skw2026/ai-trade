#!/usr/bin/env python3
"""Run the deployment-safe v4 option lifecycle collector."""

import capture_bybit_option_lifecycle_v4 as collector
import run_option_lifecycle_collector_v3 as core


core.collector = collector
core.HEALTH_SCHEMA_VERSION = "option_lifecycle_collector_health_v4"
core.LATEST_SCHEMA_VERSION = "option_lifecycle_latest_segment_v4"
core.CAPTURE_SCRIPT_NAME = "capture_bybit_option_lifecycle_v4.py"


if __name__ == "__main__":
    raise SystemExit(core.main())
