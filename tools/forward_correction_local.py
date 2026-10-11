#!/usr/bin/env python3
"""Local-only custody and once-only recovery for the frozen correction contract."""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess

import forward_evidence_correction as correction
import forward_evidence_backup as backup
import forward_evidence_ecs as ecs

ROOT = Path(".artifacts/forward-correction-20261011")
PARENT = Path(".artifacts/forward-evidence-20261010/validation-state.json")


def read(path):
    return json.loads(Path(path).read_text())


def protected(contract):
    correction.require(correction.digest(PARENT.read_bytes()) == contract["parent_gate_sha256"], "PARENT_GATE_CHANGED")
    correction.require(read(PARENT)["status"] == "HALTED", "PARENT_NOT_HALTED")
    originals = read(".artifacts/stablecoin-access-diagnostic-20261009/freeze.json")["protected"]
    for path, expected in originals.items():
        correction.require(correction.digest(Path(path).read_bytes()) == expected, "HISTORICAL_ORIGINAL_CHANGED")
    return len(originals)


def prepare(contract):
    protected(contract)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    correction.require(not subprocess.check_output(["git", "status", "--porcelain"]), "WORKTREE_NOT_CLEAN")
    cert = (ROOT / "keys/recipient.pem").read_bytes()
    backup.check_certificate(cert)
    key = backup.read_regular(ROOT.resolve() / "keys/key.pem", 65536, private=True)
    correction.require(backup.openssl(["pkey", "-pubout"], key)
                       == backup.openssl(["x509", "-pubkey", "-noout"], cert), "LOCAL_RECIPIENT_KEY_MISMATCH")
    record = {"status": "ATTEMPT_RESERVED_BEFORE_DISPATCH", "workflow_sha": sha,
              "contract_sha256": correction.digest(correction.canonical(contract)),
              "certificate_sha256": correction.digest(cert), "parent_gate_sha256": contract["parent_gate_sha256"],
              "source_hashes": {name: correction.digest(Path("tools", name).read_bytes()) for name in correction.SOURCES}}
    correction.exclusive_json(ROOT / "attempt-start.json", record)
    inputs = {"expected_controller_sha": sha, "expected_contract_sha256": record["contract_sha256"],
              "recipient_certificate_base64": base64.b64encode(cert).decode()}
    correction.exclusive_json(ROOT / "dispatch.json", inputs)
    print(json.dumps(record, sort_keys=True))


def complete(contract):
    attempt = read(ROOT / "attempt-start.json")
    correction.require(correction.digest(correction.canonical(contract)) == attempt["contract_sha256"], "LOCAL_CONTRACT_CHANGED")
    correction.require(subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == attempt["workflow_sha"]
                       and not subprocess.check_output(["git", "status", "--porcelain"]), "LOCAL_CONTROLLER_CHANGED")
    for name, expected in attempt["source_hashes"].items():
        correction.require(correction.digest(Path("tools", name).read_bytes()) == expected, "LOCAL_SOURCE_CHANGED")
    run = read(ROOT / "github-run.json")
    correction.require(run["head_sha"] == attempt["workflow_sha"] and run["head_branch"] == "main"
                       and run["event"] == "workflow_dispatch" and run["run_attempt"] == 1
                       and run["path"] == ".github/workflows/forward-correction.yml"
                       and run["repository"]["full_name"] == "skw2026/ai-trade"
                       and run["status"] == "completed" and run["conclusion"] == "success", "GITHUB_RUN_IDENTITY_OR_RESULT")
    jobs = read(ROOT / "github-jobs.json")["jobs"]
    required_steps = {"Synthetic backup and controller checks", "Native Linux deployment lifecycle regression",
                      "Same originals precise-version correction", "Retain ciphertext and sanitized receipt"}
    correction.require(len(jobs) == 1 and jobs[0]["conclusion"] == "success"
                       and jobs[0]["run_id"] == run["id"] and jobs[0]["run_attempt"] == 1
                       and required_steps.issubset({s["name"] for s in jobs[0]["steps"]})
                       and all(s["conclusion"] == "success" for s in jobs[0]["steps"]), "GITHUB_REQUIRED_STEP_NOT_SUCCESS")
    receipt = read(ROOT / "download/remote-receipt.json")
    binding = read(ROOT / "download/run-binding.json")
    correction.require(binding == receipt["binding"], "ARTIFACT_BINDING_MISMATCH")
    for key in ("workflow_sha", "contract_sha256", "certificate_sha256", "source_hashes"):
        correction.require(binding[key] == attempt[key], "LOCAL_ATTEMPT_BINDING_MISMATCH")
    correction.require(binding["run_id"] == str(run["id"]) and binding["run_attempt"] == "1"
                       and receipt["status"] == "REMOTE_REPLAY_VERIFIED_ENCRYPTED_BACKUP_READY"
                       and receipt["transport_exit"] == 0 and receipt["trading_identity_unchanged"]
                       and receipt["inventory_static_sha256"] == contract["inventory_static_sha256"], "REMOTE_CORRECTION_INCOMPLETE")
    correction.require(receipt["runtime_flags"].get("operator_reduce_only") == "true"
                       and receipt["runtime_flags"].get("force_reduce_only") == "true", "DEMO_LOCK_NOT_VERIFIED")
    count = protected(contract)
    cipher = (ROOT / "download/first-segment.cms").read_bytes()
    correction.require(correction.digest(cipher) == receipt["ciphertext_sha256"]
                       and len(cipher) == receipt["ciphertext_bytes"], "CIPHERTEXT_HASH_MISMATCH")
    local_root = ROOT.resolve()
    destination = local_root / "restored"
    correction.exclusive_json(ROOT / "recovery-start.json", {"run_id": run["id"], "binding": binding})
    result = {"status": "INCOMPLETE", "run_id": run["id"], "binding": binding,
              "parent_status": "HALTED", "economic_evidence": False, "research_admitted": False,
              "whole_batch_replayed": False, "whole_batch_backed_up": False}
    try:
        restored = backup.restore(cipher, local_root / "keys/key.pem", local_root / "keys/recipient.pem",
                                  destination, contract["expected_file_hashes"])
        argv = ecs.replay_spec(contract["image"], destination / "code", destination / "capture/segment-000")
        argv[2:2] = ["--pull=never", "--platform", contract["platform"]]
        # Fixed digest + platform; no tolerance change or host-Python substitution.
        check = subprocess.run(argv, capture_output=True, timeout=90, check=False)
        correction.require(check.returncode == 0, "LOCAL_SAME_IMAGE_REPLAY_FAILED")
        replayed = json.loads(check.stdout)
        correction.require(replayed == receipt["first_segment"]
                           and replayed == read(destination / "capture/segment-000/receipt.json"), "OFF_HOST_REPLAY_MISMATCH")
        count = protected(contract)
        result.update(status="CORRECTED_FIRST_SEGMENT_DELIVERY_VERIFIED", restoration=restored,
                      off_host_replay_identical=True, first_segment=replayed,
                      parent_gate_sha256=contract["parent_gate_sha256"], protected_originals=count,
                      private_key_exported=False, raw_publicly_uploaded=False)
    except Exception as error:
        reason = str(error)
        result["reason_code"] = reason if correction.re.fullmatch(r"[A-Z_]+", reason) else type(error).__name__
    correction.exclusive_json(ROOT / "terminal-result.json", result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "CORRECTED_FIRST_SEGMENT_DELIVERY_VERIFIED" else 2


def fetch(run_id):
    correction.require(correction.re.fullmatch(r"[0-9]+", run_id) is not None, "RUN_ID_INVALID")
    run = json.loads(subprocess.check_output(["gh", "api", "repos/skw2026/ai-trade/actions/runs/" + run_id]))
    correction.require(run["status"] == "completed", "RUN_STILL_ACTIVE")
    jobs = json.loads(subprocess.check_output(["gh", "api", "repos/skw2026/ai-trade/actions/runs/" + run_id + "/jobs"]))
    correction.exclusive_json(ROOT / "github-run.json", run)
    correction.exclusive_json(ROOT / "github-jobs.json", jobs)
    subprocess.run(["gh", "run", "download", run_id, "--repo", "skw2026/ai-trade",
                    "--name", "forward-correction-" + run_id + "-1", "--dir", str(ROOT / "download")], check=True)
    print(json.dumps({"status": "GITHUB_EVIDENCE_DOWNLOADED", "run_id": run_id, "conclusion": run["conclusion"]}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "fetch", "complete"))
    parser.add_argument("--run-id")
    args = parser.parse_args()
    contract = read(correction.CONTRACT_PATH)
    if args.mode == "prepare":
        prepare(contract)
        return 0
    if args.mode == "fetch":
        fetch(args.run_id or "")
        return 0
    return complete(contract)


if __name__ == "__main__":
    raise SystemExit(main())
