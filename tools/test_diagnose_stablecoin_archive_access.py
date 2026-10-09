import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

import diagnose_stablecoin_archive_access as diag


class Response(io.BytesIO):
    status = 200


class Opener:
    def __init__(self, raw):
        self.raw = raw
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return Response(self.raw)


class DiagnosticTests(unittest.TestCase):
    def test_four_fixed_metadata_requests(self):
        requests = diag.fixed_requests()
        self.assertEqual(len(requests), 4)
        self.assertEqual(len(set(url for _, url in requests)), 4)
        self.assertEqual({urllib.parse.urlsplit(url).hostname for _, url in requests},
                         {"archive.org", "web.archive.org"})
        for name, url in requests:
            self.assertTrue(url.startswith("https://"))
            if name.endswith("index"):
                self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["limit"], ["24"])

    def test_no_redirect(self):
        self.assertIsNone(diag.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_empty_availability_is_not_absence(self):
        result = diag.summarize({"archived_snapshots": {}}, "tether_20230601")
        self.assertEqual(result["observation"], "NO_CLOSEST_RETURNED_NOT_PROOF_OF_ABSENCE")

    def test_nearest_is_metadata_not_admission(self):
        result = diag.summarize({"archived_snapshots": {"closest": {
            "timestamp": "20240602123456", "url": "https://web.archive.org/web/20240602123456/https://app.tether.to/transparency.json",
            "status": "200", "available": True}}}, "tether_20240601")
        self.assertFalse(result["body_downloaded"])
        self.assertEqual(result["timestamp"], "20240602123456")

    def test_malformed_availability_rejected(self):
        for value in (None, {}, {"archived_snapshots": []}, {"archived_snapshots": {"closest": []}},
                      {"archived_snapshots": {"closest": {"timestamp": "20249901123456", "url": "https://web.archive.org/"}}},
                      {"archived_snapshots": {"closest": {"timestamp": "20240601123456", "url": "https://bad.example/"}}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                diag.summarize(value, "tether_20230601")

    def test_index_bounded_and_not_complete(self):
        header = ["timestamp", "original", "statuscode", "mimetype", "digest"]
        row = ["20230601000001", "https://app.tether.to/transparency.json", "200", "application/json", "A"]
        self.assertFalse(diag.summarize([header, row], "tether_index")["coverage_complete"])
        with self.assertRaises(ValueError):
            diag.summarize([header] + [row] * 25, "tether_index")
        with self.assertRaises(ValueError):
            diag.summarize([["wrong"]], "tether_index")

    def test_empty_index(self):
        self.assertEqual(diag.summarize([], "tether_index")["observation"], "EMPTY_INDEX_NOT_PROOF_OF_ABSENCE")

    def test_no_auth_or_post(self):
        opener = Opener(b'{"archived_snapshots":{}}')
        result = diag.collect_one("tether_20230601", diag.fixed_requests()[0][1], opener)
        self.assertTrue(result["parsed"])
        request, timeout = opener.requests[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(set(request.headers), {"User-agent"})
        self.assertEqual(timeout, 10)

    def test_limit_and_non_json(self):
        for raw in (b"x" * (diag.MAX_BYTES + 1), b"<html>outage</html>"):
            result = diag.collect_one("tether_20230601", diag.fixed_requests()[0][1], Opener(raw))
            self.assertFalse(result["parsed"])
            self.assertNotIn("metadata", result)

    def test_transport_failure_not_success(self):
        opener = Opener(b"")
        with patch.object(opener, "open", side_effect=TimeoutError("private diagnostic details")):
            result = diag.collect_one("tether_20230601", diag.fixed_requests()[0][1], opener)
        self.assertFalse(result["parsed"])
        self.assertIsNone(result["http_status"])
        self.assertNotIn("private", json.dumps(result))

    def test_http_status_is_preserved(self):
        opener = Opener(b"")
        with patch.object(opener, "open", side_effect=urllib.error.HTTPError("https://archive.org", 403, "", {}, None)):
            result = diag.collect_one("tether_20230601", diag.fixed_requests()[0][1], opener)
        self.assertEqual(result["http_status"], 403)
        self.assertFalse(result["parsed"])

    def test_existing_receipt_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "receipt.json"
            target.touch()
            with patch.object(diag.urllib.request, "build_opener") as opener, self.assertRaises(FileExistsError):
                diag.main(["--output", str(target)])
            opener.assert_not_called()

    def test_workflow_is_manual_without_secrets_or_deployment(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/stablecoin-archive-diagnostic.yml").read_text()
        self.assertIn("on:\n  workflow_dispatch:\n\npermissions:", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("timeout-minutes: 3", workflow)
        for forbidden in ("secrets.", "ECS_", "schedule:", "push:", "pull_request:", "docker", "ssh "):
            self.assertNotIn(forbidden, workflow)

    def test_complete_diagnosis_with_failed_requests_never_admits_input(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "receipt.json"
            with patch.object(diag, "collect_one", return_value={"parsed": False}) as collect, \
                    patch("builtins.print"):
                self.assertEqual(diag.main(["--output", str(target)]), 0)
            report = json.loads(target.read_text())
            self.assertEqual(collect.call_count, 4)
            self.assertTrue(report["diagnosis_completed"])
            self.assertFalse(report["all_responses_parsed"])
            self.assertFalse(report["input_admitted"])
            self.assertEqual(report["economic_attempts"], 0)


if __name__ == "__main__":
    unittest.main()
