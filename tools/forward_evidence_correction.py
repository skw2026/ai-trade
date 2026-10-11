#!/usr/bin/env python3
"""One frozen first-segment correction; never resets the parent HALTED gate."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

CONTRACT_PATH = "docs/plans/2026-10-11-forward-correction.contract.json"
SOURCES = ("forward_evidence_ecs.py", "forward_evidence_backup.py", "forward_evidence_correction.py")
STATIC_KEYS = ("capture_commit_sha", "container", "source_files", "started_file", "health_file", "image_platform", "segments")
SCHEMA = "forward_correction_once_receipt_v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def require(condition, code):
    if not condition:
        raise ValueError(code)


def exclusive_json(path, value):
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    parent = os.open(str(Path(path).parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def bound_inventory(ecs, contract):
    result = ecs["execute"]("inventory", contract["release_sha"], contract["capture_commit_sha"], "")
    snapshot = result["inventory"]["snapshot"]
    require(snapshot["identity_bound"] and not snapshot["anomalies"], "INVENTORY_IDENTITY_OR_CONTENT")
    require(snapshot["reported_health_status"] == "BOUNDED_COLLECTION_COMPLETE"
            and snapshot["container"]["running"] is False, "CAPTURE_NOT_TERMINAL")
    require(digest(canonical({key: snapshot[key] for key in STATIC_KEYS}))
            == contract["inventory_static_sha256"], "FROZEN_INVENTORY_CHANGED")
    return result


def remote(bundle):
    """Run from stdin on ECS; transfer only ciphertext and sanitized metadata."""
    os.umask(0o077)
    contract, binding = bundle["contract"], bundle["binding"]
    result = {"schema_version": SCHEMA, "status": "INCOMPLETE", "stage": "binding",
              "binding": binding, "parent_gate_modified": False, "economic_evidence": False,
              "research_admitted": False, "account_access": False, "order_submission": False}
    try:
        require(binding["contract_sha256"] == digest(canonical(contract)), "CONTRACT_HASH_MISMATCH")
        require(binding["run_attempt"] == "1" and re.fullmatch(r"[0-9a-f]{40}", binding["workflow_sha"])
                and re.fullmatch(r"[0-9]+", binding["run_id"]), "WORKFLOW_BINDING_INVALID")
        require(set(bundle["sources"]) == set(SOURCES), "CONTROLLER_FILES_INVALID")
        for name, source in bundle["sources"].items():
            require(binding["source_hashes"][name] == digest(source.encode()), "CONTROLLER_HASH_MISMATCH")
        ecs, backup = {"__name__": "forward_ecs"}, {"__name__": "forward_backup"}
        exec(compile(bundle["sources"][SOURCES[0]], SOURCES[0], "exec"), ecs)
        exec(compile(bundle["sources"][SOURCES[1]], SOURCES[1], "exec"), backup)
        certificate = base64.b64decode(bundle["certificate_base64"], validate=True)
        require(digest(certificate) == binding["certificate_sha256"], "CERTIFICATE_HASH_MISMATCH")
        backup["check_certificate"](certificate)
        # An exclusive marker lives OUTSIDE all frozen evidence, never deleted/reset.
        marker = ecs["ROOT"] / (contract["correction_id"] + ".attempt.json")
        result["stage"] = "consume_once"
        exclusive_json(marker, binding)
        result["stage"] = "readonly_preflight"
        before = bound_inventory(ecs, contract)
        batch = ecs["ROOT"] / contract["capture_commit_sha"]
        plaintext = backup["pack"](batch)
        backup["validate_archive"](plaintext, contract["expected_file_hashes"])
        ciphertext = backup["encrypt"](plaintext, certificate)
        result["stage"] = "same_image_first_segment_replay"
        verified = ecs["execute"]("verify", contract["release_sha"], contract["capture_commit_sha"], "")
        require(verified["status"] == "FIRST_SEGMENT_REPLAY_VERIFIED", "FIRST_REPLAY_NOT_VERIFIED")
        require(verified["research_image"] == contract["image"], "REPLAY_IMAGE_CHANGED")
        result["stage"] = "after_identity_check"
        after = bound_inventory(ecs, contract)
        require(before["containers"]["ai-trade"] == after["containers"]["ai-trade"], "TRADING_IDENTITY_CHANGED")
        require(backup["pack"](batch) == plaintext, "ORIGINALS_CHANGED_DURING_REPLAY")
        result.update(status="REMOTE_REPLAY_VERIFIED_ENCRYPTED_BACKUP_READY", stage="remote_complete",
                      first_segment=verified["first_segment"], source_hashes=verified["source_sha256"],
                      inventory_static_sha256=contract["inventory_static_sha256"],
                      ciphertext_sha256=digest(ciphertext), ciphertext_bytes=len(ciphertext),
                      ciphertext_base64=base64.b64encode(ciphertext).decode(),
                      trading_identity_unchanged=True, runtime_flags=after["runtime_flags"],
                      off_host_restored=False, whole_batch_replayed=False)
    except Exception as error:
        reason = str(error)
        result["reason_code"] = reason if re.fullmatch(r"[A-Z_]+", reason) else type(error).__name__
    return result


def workflow():
    os.umask(0o077)
    out = Path(".artifacts/forward-correction")
    out.mkdir(parents=True, exist_ok=False)
    require(os.environ["GITHUB_REPOSITORY"] == "skw2026/ai-trade"
            and os.environ["GITHUB_REF"] == "refs/heads/main"
            and os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
            and os.environ["GITHUB_RUN_ATTEMPT"] == "1", "WRONG_WORKFLOW_CONTEXT")
    sha = os.environ["GITHUB_SHA"]
    require(sha == os.environ["EXPECTED_CONTROLLER_SHA"], "WORKFLOW_SHA_MISMATCH")
    sources = {name: Path("tools", name).read_text() for name in SOURCES}
    contract = json.loads(Path(CONTRACT_PATH).read_text())
    certificate = base64.b64decode(os.environ["RECIPIENT_CERTIFICATE_BASE64"], validate=True)
    require(0 < len(certificate) <= 65536 and b"PRIVATE KEY" not in certificate, "PUBLIC_CERTIFICATE_ONLY")
    binding = {"contract_sha256": digest(canonical(contract)), "workflow_sha": sha,
               "run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
               "certificate_sha256": digest(certificate),
               "source_hashes": {name: digest(source.encode()) for name, source in sources.items()}}
    require(binding["contract_sha256"] == os.environ["EXPECTED_CONTRACT_SHA256"], "WORKFLOW_CONTRACT_MISMATCH")
    exclusive_json(out / "run-binding.json", binding)
    bundle = {"sources": sources, "contract": contract, "binding": binding,
              "certificate_base64": base64.b64encode(certificate).decode()}
    encoded = base64.b64encode(canonical(bundle)).decode()
    body = ("import base64,json\n"
            "b=json.loads(base64.b64decode(" + repr(encoded) + "))\n"
            "n={'__name__':'forward_correction'}\n"
            "exec(compile(b['sources']['forward_evidence_correction.py'],'forward_evidence_correction.py','exec'),n)\n"
            "r=n['remote'](b)\nprint(json.dumps(r,sort_keys=True,allow_nan=False))\n"
            "raise SystemExit(2 if r['status']=='INCOMPLETE' else 0)\n")
    script = "python3 -B - <<'FORWARD_CORRECTION_STDIN'\n" + body + "FORWARD_CORRECTION_STDIN\n"
    env = dict(os.environ, ECS_TRANSPORT_MODE="script", ECS_TRANSPORT_TIMEOUT="360s",
               ECS_TRANSPORT_ENVS="", ECS_TRANSPORT_SCRIPT=script)
    receipt = {"schema_version": SCHEMA, "status": "INCOMPLETE", "binding": binding,
               "reason_code": "NO_REMOTE_RECEIPT"}
    try:
        child = subprocess.run([sys.executable, "deploy/ecs_ssh.py"], env=env, capture_output=True,
                               timeout=390, check=False)
        for line in child.stdout.splitlines():
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(value, dict) and value.get("schema_version") == SCHEMA:
                receipt = value
        cipher_text = receipt.pop("ciphertext_base64", None)
        require(receipt.get("binding") == binding, "RETURNED_BINDING_CHANGED")
        receipt["transport_exit"] = child.returncode
        if cipher_text is not None:
            ciphertext = base64.b64decode(cipher_text, validate=True)
            require(digest(ciphertext) == receipt["ciphertext_sha256"], "CIPHERTEXT_TRANSFER_HASH")
            with (out / "first-segment.cms").open("xb") as handle:
                handle.write(ciphertext)
        require(child.returncode == 0 and receipt["status"] == "REMOTE_REPLAY_VERIFIED_ENCRYPTED_BACKUP_READY",
                "REMOTE_CORRECTION_INCOMPLETE")
    except Exception as error:
        receipt["status"] = "INCOMPLETE"
        reason = str(error)
        receipt["transport_reason"] = reason if re.fullmatch(r"[A-Z_]+", reason) else type(error).__name__
    exclusive_json(out / "remote-receipt.json", receipt)
    print(json.dumps(receipt, sort_keys=True, allow_nan=False))
    return 0 if receipt["status"] == "REMOTE_REPLAY_VERIFIED_ENCRYPTED_BACKUP_READY" else 2


if __name__ == "__main__":
    sys.exit(workflow())
