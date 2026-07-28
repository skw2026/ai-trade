import unittest
from pathlib import Path

from tools.build_replay_candidate_config import derive_candidate_config
from tools.config_policy_contract import extract_policy


class BuildReplayCandidateConfigTests(unittest.TestCase):
    def test_s5_candidate_preserves_entire_execution_policy(self):
        runtime_text = Path("config/bybit.demo.s5.yaml").read_text(
            encoding="utf-8"
        )
        candidate_text = derive_candidate_config(
            runtime_text,
            model_path="/tmp/candidate.cbm",
            report_path="/tmp/candidate.json",
        )
        self.assertEqual(
            extract_policy(runtime_text),
            extract_policy(candidate_text),
        )
        self.assertIn('  mode: "replay"', candidate_text)
        self.assertIn('    model_path: "/tmp/candidate.cbm"', candidate_text)
        self.assertIn(
            '    model_report_path: "/tmp/candidate.json"',
            candidate_text,
        )
        self.assertEqual(candidate_text.count("candidate_validation_mode:"), 1)
        self.assertEqual(
            candidate_text.count("source_runtime_config_sha256:"),
            1,
        )

    def test_missing_integrator_contract_fails(self):
        with self.assertRaisesRegex(ValueError, "integrator"):
            derive_candidate_config(
                "system:\n  mode: paper\n",
                model_path="candidate.cbm",
                report_path="candidate.json",
            )


if __name__ == "__main__":
    unittest.main()
