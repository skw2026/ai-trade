#!/usr/bin/env python3

import importlib.util
import pathlib
import sys
import unittest


def load_watchdog_module():
    module_path = pathlib.Path(__file__).resolve().parents[1] / "ops" / "watchdog.py"
    spec = importlib.util.spec_from_file_location("watchdog", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


WATCHDOG = load_watchdog_module()


class WatchdogUtilsTest(unittest.TestCase):
    def test_decode_chunked_body(self):
        body = b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n"
        self.assertEqual(WATCHDOG.decode_chunked_body(body), b"Wikipedia")

    def test_decode_docker_log_stream(self):
        frame_1 = b"\x01\x00\x00\x00" + (6).to_bytes(4, "big") + b"line1\n"
        frame_2 = b"\x02\x00\x00\x00" + (6).to_bytes(4, "big") + b"line2\n"
        decoded = WATCHDOG.decode_docker_log_stream(frame_1 + frame_2)
        self.assertEqual(decoded, "line1\nline2\n")

    def test_parse_log_time(self):
        ts = WATCHDOG.parse_log_time("2026-02-14 15:02:18 [INFO] RUNTIME_STATUS: ticks=1")
        self.assertIsNotNone(ts)
        self.assertEqual(ts.year, 2026)
        self.assertIsNone(WATCHDOG.parse_log_time("invalid line"))


if __name__ == "__main__":
    unittest.main()

