#!/usr/bin/env python3
"""Exact-SHA, once-only read-only research transport; no trading deployment."""
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zlib

PREFIX = "docs/plans/2026-10-12-two-sided-quote-screen"
SOURCES = ("forward_evidence_ecs.py", "forward_evidence_correction.py", "two_sided_quote_screen.py", "two_sided_screen_remote.py")
SCHEMA = "two_sided_screen_remote_v1"
EXPERIMENT = "two-sided-quote-screen-20261012"


def require(ok, code):
    if not ok:
        raise ValueError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def exclusive(path, value):
    with Path(path).open("x", encoding="utf-8") as f:
        json.dump(value, f, sort_keys=True, allow_nan=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    fd = os.open(str(Path(path).parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def reason(error):
    value = str(error)
    return value if re.fullmatch(r"[A-Z_]{1,100}", value) else type(error).__name__


def budget_check(contract, now=None):
    start = datetime.datetime.fromisoformat(contract["budget_start_utc"].replace("Z", "+00:00"))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    deadline = start + datetime.timedelta(seconds=contract["budget_seconds"])
    # Reserve the full compute allowance AND inventory/transport/closeout margin.
    require(start <= now and now + datetime.timedelta(seconds=contract["compute_timeout_seconds"] + 600) <= deadline,
            "BATCH_BUDGET_CANNOT_FIT")


def validate_binding(bundle):
    c, b = bundle["contract"], bundle["binding"]
    require(c["experiment_id"] == EXPERIMENT and c["maximum_economic_attempts"] == 1, "EXPERIMENT_ID")
    require(c["capture_commit_sha"] == "1efbac875f971a1c72840e528d27d9e788634d71"
            and c["release_sha"] == "f6cc4ea7f03c1c8f251c730e3642d2ca19b00ca4", "TARGET_IDENTITY")
    require(b["contract_sha256"] == digest(canonical(c))
            and b["manifest_sha256"] == digest(canonical(bundle["manifest"])), "INPUT_BINDING")
    require(b["run_attempt"] == "1" and re.fullmatch(r"[1-9][0-9]*", b["run_id"])
            and re.fullmatch(r"[0-9a-f]{40}", b["workflow_sha"]), "WORKFLOW_BINDING")
    require(set(bundle["sources"]) == set(SOURCES) and set(b["source_hashes"]) == set(SOURCES), "SOURCE_SET")
    for name, source in bundle["sources"].items():
        require(isinstance(source, str) and len(source.encode()) < 128 * 1024
                and digest(source.encode()) == b["source_hashes"][name], "SOURCE_BINDING")


def docker_spec(contract, batch, run_id):
    require(re.fullmatch(r"[1-9][0-9]*", run_id), "RUN_ID")
    return ["docker", "run", "--rm", "-i", "--name", EXPERIMENT,
            "--label", "ai-trade.research-run=" + run_id, "--pull=never", "--platform", contract["platform"],
            "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--user", "65534:65534", "--memory=512m", "--cpus=1", "--pids-limit=64", "--log-driver=none",
            "--mount", "type=bind,source=%s,target=/capture-code,readonly" % (batch / "code"),
            "--mount", "type=bind,source=%s,target=/evidence,readonly" % (batch / "capture"),
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python3", contract["image"], "-B", "-c",
            "import json,sys,re\nb=json.load(sys.stdin)\nn={'__name__':'quote_screen'}\n"
            "exec(compile(b['source'],'quote_screen.py','exec'),n)\n"
            "try:\n r=n['execute'](b['contract'],b['manifest'],'/evidence','/capture-code')\n"
            "except Exception as e:\n s=str(e)\n r={'status':'INPUT_OR_REPLAY_FAILED','reason_code':s if re.fullmatch(r'[A-Z_]{1,100}',s) else type(e).__name__}\n"
            "print(json.dumps(r,sort_keys=True,allow_nan=False))\n"
            "raise SystemExit(0 if r['status']=='SCREEN_COMPLETED' else 2)\n"]


def stop_own_container(run_id):
    check = subprocess.run(["docker", "inspect", "--format", '{{index .Config.Labels "ai-trade.research-run"}}',
                            EXPERIMENT], capture_output=True, timeout=15, check=False)
    if check.returncode == 0:
        require(check.stdout.strip().decode() == run_id, "CLEANUP_CONTAINER_IDENTITY")
        stop = subprocess.run(["docker", "stop", "--time", "5", EXPERIMENT], capture_output=True, timeout=20, check=False)
        require(stop.returncode == 0, "RESEARCH_CONTAINER_STOP_FAILED")


def trader_instance():
    value = subprocess.run(["docker", "inspect", "--format", '{{json .Id}} {{json .State.StartedAt}}', "ai-trade"],
                           capture_output=True, timeout=15, check=False)
    require(value.returncode == 0 and value.stdout.strip(), "TRADING_INSTANCE_UNAVAILABLE")
    return digest(value.stdout.strip())


def remote(bundle):
    os.umask(0o077)
    result = {"schema_version": SCHEMA, "status": "INCOMPLETE", "stage": "binding", "binding": bundle.get("binding"),
              "account_access": False, "order_submission": False, "trading_deployment": False, "g4_passed": False}
    before, ecs, correction, consumed, instance = None, None, None, False, None
    try:
        validate_binding(bundle)
        c, b = bundle["contract"], bundle["binding"]
        budget_check(c)
        ecs, correction = {"__name__": "ecs_readonly"}, {"__name__": "inventory_binding"}
        exec(compile(bundle["sources"][SOURCES[0]], SOURCES[0], "exec"), ecs)
        exec(compile(bundle["sources"][SOURCES[1]], SOURCES[1], "exec"), correction)
        result["stage"] = "readonly_preflight"
        before = correction["bound_inventory"](ecs, c)
        require(before["research_image"] == c["image"] and c["platform"] == "linux/amd64"
                and c["segments"] == 94, "FROZEN_IMAGE_OR_SEGMENT_CONTRACT")
        instance = trader_instance()
        require(before["memory_available_bytes"] >= 1024**3, "MEMORY_RESERVE")
        env = subprocess.run(["docker", "image", "inspect", "--format", "{{json .Config.Env}}", c["image"]],
                             capture_output=True, timeout=20, check=False)
        require(env.returncode == 0, "PINNED_IMAGE_NOT_LOCAL")
        require(not any(re.search(r"KEY|SECRET|TOKEN|PASSWORD", s.split("=", 1)[0], re.I)
                        for s in json.loads(env.stdout) or []), "IMAGE_CREDENTIAL_ENV")
        batch = ecs["ROOT"] / c["capture_commit_sha"]
        for directory in (batch / "code", batch / "capture"):
            require(directory.is_dir() and not directory.is_symlink(), "BIND_DIRECTORY")
        owner = (batch / "capture").stat()
        require(owner.st_uid == 65534 and owner.st_gid == 65534, "EVIDENCE_OWNER")
        result["stage"] = "consume_once"
        budget_check(c)
        exclusive(ecs["ROOT"] / (EXPERIMENT + ".attempt.json"), b)
        consumed = True
        result["stage"] = "same_image_all_segments_and_screen"
        payload = canonical({"source": bundle["sources"]["two_sided_quote_screen.py"],
                             "contract": c, "manifest": bundle["manifest"]})
        try:
            run = subprocess.run(docker_spec(c, batch, b["run_id"]), input=payload, capture_output=True,
                                 timeout=c["compute_timeout_seconds"], check=False)
        except subprocess.TimeoutExpired:
            stop_own_container(b["run_id"])
            raise ValueError("RESEARCH_COMPUTE_TIMEOUT") from None
        require(len(run.stdout) < 8 * 1024**2, "RESULT_SIZE_LIMIT")
        value = json.loads(run.stdout)
        result["screen"] = value
        require(run.returncode == 0 and value.get("status") == "SCREEN_COMPLETED", "SCREEN_NOT_COMPLETED")
        require(value["replayed_segments"] == c["segments"] and value["g4_passed"] is False
                and value["profitability_proven"] is False and value["account_access"] is False, "SCREEN_BOUNDARY")
        result.update(status="READONLY_SCREEN_COMPLETED", stage="screen_complete")
    except Exception as error:
        result["reason_code"] = reason(error)
    finally:
        if before is not None:
            try:
                after = correction["bound_inventory"](ecs, bundle["contract"])
                require(before["containers"]["ai-trade"] == after["containers"]["ai-trade"], "TRADING_IDENTITY_CHANGED")
                require(instance is not None and instance == trader_instance(), "TRADING_INSTANCE_CHANGED")
                result["original_inventory_unchanged"] = True
                result["trading_identity_unchanged"] = True
                result["trading_instance_unchanged"] = True
                result["runtime_flags"] = after["runtime_flags"]
            except Exception as error:
                result.update(status="INCOMPLETE", stage="after_identity_check", reason_code=reason(error))
        if consumed:
            try:
                exclusive(ecs["ROOT"] / (EXPERIMENT + ".result.json"), result)
            except Exception as error:
                result.update(status="INCOMPLETE", stage="receipt_custody", reason_code=reason(error))
    return result


def transport_script(bundle):
    compressed = base64.b64encode(zlib.compress(canonical(bundle), 9)).decode()
    script = ("python3 -B - <<'TWO_SIDED_SCREEN_STDIN'\nimport base64,json,zlib\n"
              "b=json.loads(zlib.decompress(base64.b64decode(" + repr(compressed) + ")))\n"
              "n={'__name__':'screen_controller'}\n"
              "exec(compile(b['sources']['two_sided_screen_remote.py'],'two_sided_screen_remote.py','exec'),n)\n"
              "r=n['remote'](b)\nprint(json.dumps(r,sort_keys=True,allow_nan=False))\n"
              "raise SystemExit(0 if r['status']=='READONLY_SCREEN_COMPLETED' else 2)\nTWO_SIDED_SCREEN_STDIN\n")
    require(len(script.encode()) < 96 * 1024, "TRANSPORT_ENV_SIZE")
    return script


def workflow():
    os.umask(0o077)
    out = Path(".artifacts/two-sided-screen")
    out.mkdir(parents=True, exist_ok=False)
    require(os.environ["GITHUB_REPOSITORY"] == "skw2026/ai-trade"
            and os.environ["GITHUB_REF"] == "refs/heads/main"
            and os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
            and os.environ["GITHUB_RUN_ATTEMPT"] == "1", "WORKFLOW_CONTEXT")
    sha = os.environ["GITHUB_SHA"]
    require(sha == os.environ["EXPECTED_CONTROLLER_SHA"], "WORKFLOW_SHA")
    c = json.loads(Path(PREFIX + ".contract.json").read_text())
    budget_check(c)
    manifest = json.loads(Path(PREFIX + ".manifest.json").read_text())
    sources = {name: Path("tools", name).read_text() for name in SOURCES}
    b = {"workflow_sha": sha, "run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
         "contract_sha256": digest(canonical(c)), "manifest_sha256": digest(canonical(manifest)),
         "source_hashes": {k: digest(v.encode()) for k, v in sources.items()}}
    require(b["contract_sha256"] == os.environ["EXPECTED_CONTRACT_SHA256"], "WORKFLOW_CONTRACT")
    exclusive(out / "run-binding.json", b)
    bundle = {"contract": c, "manifest": manifest, "sources": sources, "binding": b}
    validate_binding(bundle)
    env = dict(os.environ, ECS_TRANSPORT_MODE="script", ECS_TRANSPORT_ENVS="",
               ECS_TRANSPORT_TIMEOUT=str(c["ssh_timeout_seconds"]) + "s", ECS_TRANSPORT_SCRIPT=transport_script(bundle))
    receipt = {"schema_version": SCHEMA, "status": "INCOMPLETE", "binding": b, "reason_code": "NO_REMOTE_RECEIPT"}
    try:
        run = subprocess.run([sys.executable, "deploy/ecs_ssh.py"], env=env, capture_output=True,
                             timeout=c["ssh_timeout_seconds"] + 30, check=False)
        for line in run.stdout.splitlines():
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(value, dict) and value.get("schema_version") == SCHEMA:
                receipt = value
        require(receipt.get("binding") == b, "RETURNED_BINDING")
        receipt["transport_exit"] = run.returncode
        require(run.returncode == 0 and receipt["status"] == "READONLY_SCREEN_COMPLETED", "REMOTE_SCREEN_INCOMPLETE")
    except Exception as error:
        receipt.update(status="INCOMPLETE", transport_reason=reason(error))
    exclusive(out / "remote-receipt.json", receipt)
    # Do not duplicate the normalized ledger into logs; it is in the artifact.
    print(json.dumps({k: v for k, v in receipt.items() if k != "screen"}, sort_keys=True))
    return 0 if receipt["status"] == "READONLY_SCREEN_COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(workflow())
