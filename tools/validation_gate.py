#!/usr/bin/env python3
"""Sticky, repository-local validation stop gate; not a trading actuator.

Only an evidence review can grant one retry of the failed command. This checks
process requirements, not the truth of a diagnosis or external authorization.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / ".artifacts/validation-gate/state.json"
STATUSES = {"READY", "RUNNING", "BLOCKED", "RETRY_APPROVED", "HALTED"}
RELEASE_REPO = "skw2026/ai-trade"
RELEASE_STEPS = {
    "build-test-push": ("Wait for Exact CI Success", "Preflight pinned ECS connection",
                        "Build and Push Runtime Image", "Build and Push Research Image",
                        "Verify Offline Learning Loop", "Upload Offline Learning Evidence",
                        "Build and Push Web Image", "Compute Deploy Gate"),
    "deploy-ecs": ("Upload Deployment Bundle", "Deploy to ECS"),
}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load(path):
    require(not path.is_symlink(), "state must not be a symlink")
    if not path.exists():
        return {"schema_version": 1, "status": "READY", "active": None, "history": []}
    state = json.loads(path.read_text())
    require(isinstance(state, dict) and state.get("schema_version") == 1 and
            state.get("status") in STATUSES and isinstance(state.get("history"), list),
            "invalid state; do not reset or bypass it")
    require((state["status"] == "READY" and state.get("active") is None) or
            (state["status"] != "READY" and isinstance(state.get("active"), dict)),
            "inconsistent active failure state")
    return state


def save(path, state):
    require(not path.is_symlink(), "state must not be a symlink")
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = pathlib.Path(output.name)
        json.dump(state, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def summary(state):
    active = state["active"] or {}
    return {"status": state["status"], "failure_id": active.get("failure_id"),
            "label": active.get("label"), "failure_count": active.get("failure_count", 0)}


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def command_hash(command, cwd):
    return sha(json.dumps({"argv": command, "cwd": cwd}, ensure_ascii=True, sort_keys=True).encode())


def release_contract(record, active):
    """One narrow exception: replace an immutable failed main/CD run, never arbitrary argv."""
    contract = record.get("corrective_release")
    if contract is None:
        return None
    require(isinstance(contract, dict), "corrective_release must be an object")
    require(contract.get("repo") == RELEASE_REPO and contract.get("branch") == "main" and
            contract.get("workflow") == ".github/workflows/cd.yml" and
            contract.get("acceptance") == "exact-cd-v1", "unsupported release contract")
    require(nonempty(contract.get("authorization")), "release correction requires recorded authorization")
    run_id = contract.get("original_run_id")
    require(type(run_id) is int and run_id > 0, "invalid original run ID")
    require(re.fullmatch(r"[0-9a-f]{40}", contract.get("original_sha", "")), "invalid original SHA")
    cwd = str(pathlib.Path.cwd().resolve())
    require(contract.get("cwd") == cwd, "release correction must retain original cwd")
    original = ["gh", "run", "watch", str(run_id), "--exit-status", "--interval", "30"]
    require(command_hash(original, cwd) == active["command_sha256"],
            "release correction only supports the exact original gh CD watcher")
    require(record.get("repair_build_argv") is None, "release preparation cannot be combined with repair-build")
    return contract


def review(path, state, review_path):
    require(state["status"] == "BLOCKED", "review requires a blocked validation")
    raw = review_path.read_bytes()
    record = json.loads(raw)
    active = state["active"]
    require(isinstance(record, dict) and record.get("failure_id") == active["failure_id"],
            "review must name the current failure_id")
    require(record.get("decision") in ("retry", "stop"), "review decision must be retry or stop")
    for field in ("goal", "expected", "observed", "root_cause", "next_action"):
        require(nonempty(record.get(field)), "missing review field: " + field)
    evidence = record.get("evidence")
    require(isinstance(evidence, list) and evidence and all(nonempty(v) for v in evidence),
            "review requires evidence references")
    if record["decision"] == "retry":
        require(record.get("root_cause_status") == "confirmed", "unknown root cause cannot authorize retry")
        require(record.get("scope_unchanged") is True and record.get("acceptance_unchanged") is True,
                "scope/acceptance change requires user direction, not a retry waiver")
        require(record.get("path_verdict") == "viable", "unproven or blocked path cannot authorize retry")
        require(type(record.get("structural_issue")) is bool, "structural_issue must be explicit")
        path_review = record.get("path_review", {})
        require(isinstance(path_review, dict), "path_review must be an object")
        for field in ("goal_alignment", "data_feasibility", "method_feasibility", "budget_and_exit"):
            require(nonempty(path_review.get(field)), "missing path review: " + field)
        if active["failure_count"] >= 2 or record["structural_issue"]:
            route = record.get("route_reassessment", {})
            require(isinstance(route, dict), "route_reassessment must be an object")
            for field in ("old_route_failure", "replacement", "feasibility_evidence", "budget", "stop_condition"):
                require(nonempty(route.get(field)), "repeated/structural failure requires route reassessment: " + field)
        contract = release_contract(record, active)
        for key in ("corrective_release", "release_prepared", "release_binding"):
            active.pop(key, None)
        if contract is not None:
            active["corrective_release"] = contract
            active["release_prepared"] = False
        state["status"] = "RETRY_APPROVED"
        active["review_path"] = str(review_path.resolve())
        active["review_sha256"] = sha(raw)
        repair = record.get("repair_build_argv")
        if repair is not None:
            require(isinstance(repair, list) and len(repair) >= 3 and
                    repair[:2] == ["cmake", "--build"] and all(nonempty(v) for v in repair),
                    "repair is restricted to one reviewed cmake --build command")
            require(record.get("repair_build_cwd") == str(pathlib.Path.cwd().resolve()),
                    "repair build cwd must be explicitly reviewed")
            active["repair_build_sha256"] = sha(json.dumps(
                {"argv": repair, "cwd": record["repair_build_cwd"]}, ensure_ascii=True, sort_keys=True).encode())
            active["repair_build_completed"] = False
        else:
            active.pop("repair_build_sha256", None)
            active.pop("repair_build_completed", None)
    else:
        state["status"] = "HALTED"
    state["history"].append({"at": stamp(), "event": "review", "failure_id": active["failure_id"],
                             "decision": record["decision"], "review_sha256": sha(raw),
                             "review_path": str(review_path.resolve())})
    save(path, state)
    return 0


def repair_build(path, state, command, timeout):
    """Reviewed preparation only; never clears the original failed acceptance."""
    require(state["status"] == "RETRY_APPROVED", "repair build requires accepted root-cause review")
    active = state["active"]
    require(active.get("repair_build_completed") is False, "no unused reviewed repair build")
    require(sha(pathlib.Path(active["review_path"]).read_bytes()) == active["review_sha256"],
            "review changed after approval")
    identity = {"argv": command, "cwd": str(pathlib.Path.cwd().resolve())}
    require(sha(json.dumps(identity, ensure_ascii=True, sort_keys=True).encode()) ==
            active.get("repair_build_sha256"), "repair build command/cwd differs from review")
    state["status"] = "RUNNING"
    save(path, state)
    try:
        result = subprocess.run(command, timeout=timeout, check=False)
        code = result.returncode if result.returncode >= 0 else 128-result.returncode
    except subprocess.TimeoutExpired:
        code = 124
    except OSError:
        code = 127
    except KeyboardInterrupt:
        code = 130
    state["history"].append({"at": stamp(), "event": "repair_build",
                             "failure_id": active["failure_id"], "exit_code": code,
                             "command_sha256": active["repair_build_sha256"]})
    if code == 0:
        active["repair_build_completed"] = True
        state["status"] = "RETRY_APPROVED"
    else:
        active["failure_count"] += 1
        active["failure_id"] = uuid.uuid4().hex
        active.pop("review_path", None)
        active.pop("review_sha256", None)
        state["status"] = "BLOCKED"
    save(path, state)
    return code


def reviewed_release(state):
    require(state["status"] == "RETRY_APPROVED", "corrective release requires accepted failure review")
    active = state["active"]
    require(sha(pathlib.Path(active["review_path"]).read_bytes()) == active["review_sha256"],
            "review changed after approval")
    contract = active.get("corrective_release")
    require(isinstance(contract, dict), "no reviewed corrective release")
    require(contract["cwd"] == str(pathlib.Path.cwd().resolve()), "release cwd differs from review")
    return active, contract


def finish_release_action(path, state, event, code, **details):
    active = state["active"]
    state["history"].append({"at": stamp(), "event": event, "exit_code": code,
                             "retry_of": active["failure_id"],
                             "original_command_sha256": active["command_sha256"], **details})
    if code:
        active["failure_count"] += 1
        active["failure_id"] = uuid.uuid4().hex
        active.pop("review_path", None)
        active.pop("review_sha256", None)
        state["status"] = "BLOCKED"
    elif event == "release_prepare":
        active["release_prepared"] = True
        state["status"] = "RETRY_APPROVED"
    else:
        state["status"], state["active"] = "READY", None
    save(path, state)
    return code


def release_prepare(path, state, timeout):
    active, _ = reviewed_release(state)
    require(active.get("release_prepared") is False, "release preparation already consumed")
    # Fixed full suite: no arbitrary preparation command or test filtering.
    command = ["ctest", "--test-dir", "build", "--output-on-failure", "--no-tests=error"]
    state["status"] = "RUNNING"
    save(path, state)
    try:
        inventory = subprocess.run(["ctest", "--test-dir", "build", "--show-only=json-v1"],
                                   capture_output=True, text=True, timeout=30, check=True)
        tests = json.loads(inventory.stdout).get("tests", [])
        require(len(tests) >= 110 and {"validation_stop_gate_test", "offline_learning_harness_test"}
                <= {test.get("name") for test in tests}, "required full regression inventory missing")
        result = subprocess.run(command, timeout=timeout, check=False)
        code = result.returncode if result.returncode >= 0 else 128-result.returncode
    except subprocess.TimeoutExpired:
        code = 124
    except OSError:
        code = 127
    except (ValueError, KeyError, TypeError, subprocess.CalledProcessError):
        code = 1
    except KeyboardInterrupt:
        code = 130
    return finish_release_action(path, state, "release_prepare", code,
                                 command_sha256=command_hash(command, str(pathlib.Path.cwd().resolve())))


def release_api(suffix):
    result = subprocess.run(["gh", "api", "repos/" + RELEASE_REPO + suffix],
                            capture_output=True, text=True, timeout=45, check=True)
    return json.loads(result.stdout)


def check_release_run(run, run_id, expected_sha):
    require(run.get("id") == run_id and run.get("head_sha") == expected_sha and
            run.get("repository", {}).get("full_name") == RELEASE_REPO and
            run.get("head_repository", {}).get("full_name") == RELEASE_REPO and
            run.get("head_branch") == "main" and run.get("event") == "push" and
            run.get("path") == ".github/workflows/cd.yml" and run.get("run_attempt") == 1,
            "CD identity mismatch (repo/workflow/branch/SHA/run/attempt)")


def check_release_jobs(payload):
    jobs = payload.get("jobs", [])
    require(payload.get("total_count") == len(jobs), "incomplete CD job evidence")
    for name, required_steps in RELEASE_STEPS.items():
        matches = [job for job in jobs if job.get("name") == name]
        require(len(matches) == 1 and matches[0].get("conclusion") == "success", "CD job not successful: " + name)
        for step in required_steps:
            matches_step = [s for s in matches[0].get("steps", []) if s.get("name") == step]
            require(len(matches_step) == 1 and matches_step[0].get("conclusion") == "success",
                    "CD required step absent/skipped/failed: " + step)


def release_retry(path, state, new_sha, run_id, timeout):
    active, contract = reviewed_release(state)
    require(active.get("release_prepared") is True, "full local release preparation required")
    require("release_binding" not in active, "corrective release binding already consumed")
    require(re.fullmatch(r"[0-9a-f]{40}", new_sha) and new_sha != contract["original_sha"] and
            run_id > contract["original_run_id"], "correction requires a new SHA and new run")
    binding = {"original_run_id": contract["original_run_id"], "original_sha": contract["original_sha"],
               "new_run_id": run_id, "new_sha": new_sha, "repo": RELEASE_REPO,
               "workflow": contract["workflow"], "acceptance": contract["acceptance"],
               "required_steps": RELEASE_STEPS, "review_sha256": active["review_sha256"]}
    active["release_binding"] = binding
    state["history"].append({"at": stamp(), "event": "release_binding", **binding})
    state["status"] = "RUNNING"
    save(path, state)  # Consume before I/O; crash is never an implicit pass or second attempt.
    code = 1
    try:
        old = release_api(f'/actions/runs/{contract["original_run_id"]}')
        check_release_run(old, contract["original_run_id"], contract["original_sha"])
        require(old.get("status") == "completed" and old.get("conclusion") == "failure",
                "original run must remain failed")
        new = release_api(f"/actions/runs/{run_id}")
        check_release_run(new, run_id, new_sha)
        ancestry = release_api(f'/compare/{contract["original_sha"]}...{new_sha}')
        require(ancestry.get("status") == "ahead" and
                ancestry.get("merge_base_commit", {}).get("sha") == contract["original_sha"],
                "corrective release must descend from failed source")
        result = subprocess.run(["gh", "run", "watch", str(run_id), "--repo", RELEASE_REPO,
                                 "--exit-status", "--interval", "30"], timeout=timeout, check=False)
        require(result.returncode == 0, "corrective CD did not succeed")
        final = release_api(f"/actions/runs/{run_id}")
        check_release_run(final, run_id, new_sha)
        require(final.get("status") == "completed" and final.get("conclusion") == "success",
                "corrective CD has no completed success")
        check_release_jobs(release_api(f"/actions/runs/{run_id}/attempts/1/jobs?per_page=100"))
        code = 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        # Never print captured API bodies or authentication diagnostics.
        print("CORRECTIVE_RELEASE_FAILED: " + type(exc).__name__, file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        code = 130
    return finish_release_action(path, state, "release_retry", code, binding=binding)


def execute(path, state, command, label, retry, timeout):
    require(command, "a validation command is required after --")
    identity = {"argv": command, "cwd": str(pathlib.Path.cwd().resolve())}
    command_sha = sha(json.dumps(identity, ensure_ascii=True, sort_keys=True).encode())
    require(state["status"] == ("RETRY_APPROVED" if retry else "READY"),
            "validation blocked: diagnose and review before retry; do not advance dependent work")
    if retry:
        active = state["active"]
        require("corrective_release" not in active, "review requires typed release-retry, not ordinary retry")
        require("repair_build_sha256" not in active or active.get("repair_build_completed") is True,
                "reviewed repair build must complete before original acceptance retry")
        require(active["command_sha256"] == command_sha,
                "retry must rerun the failed command in the same directory, not a weaker substitute")
        require(sha(pathlib.Path(active["review_path"]).read_bytes()) == active["review_sha256"],
                "review changed after approval")
    else:
        require(nonempty(label), "run requires a meaningful --label")
        active = {"label": label, "command_sha256": command_sha, "failure_count": 0}
        state["active"] = active
    state["status"] = "RUNNING"  # Crash/interruption never looks like a pass.
    save(path, state)
    try:
        result = subprocess.run(command, timeout=timeout, check=False)
        code = result.returncode if result.returncode >= 0 else 128 - result.returncode
    except subprocess.TimeoutExpired:
        code = 124
    except OSError:
        code = 127
    except KeyboardInterrupt:
        code = 130
    state["history"].append({"at": stamp(), "event": "validation", "label": active["label"],
                             "command_sha256": command_sha, "exit_code": code,
                             "retry_of": active.get("failure_id") if retry else None})
    if code == 0:
        state["status"], state["active"] = "READY", None
    else:
        active["failure_count"] += 1
        active["failure_id"] = uuid.uuid4().hex
        active.pop("review_path", None)
        active.pop("review_sha256", None)
        state["status"] = "BLOCKED"
    save(path, state)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE,
                        help="override only for isolated tests or an explicitly approved separate stage")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status")
    review_parser = commands.add_parser("review")
    review_parser.add_argument("--file", type=pathlib.Path, required=True)
    prepare_parser = commands.add_parser("release-prepare")
    prepare_parser.add_argument("--timeout", type=int, default=600)
    release_parser = commands.add_parser("release-retry")
    release_parser.add_argument("--sha", required=True)
    release_parser.add_argument("--run-id", type=int, required=True)
    release_parser.add_argument("--timeout", type=int, default=3600)
    for verb in ("run", "retry", "repair-build"):
        command_parser = commands.add_parser(verb)
        command_parser.add_argument("--label")
        command_parser.add_argument("--timeout", type=int, default=600)
        command_parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not args.state.parent.is_symlink(), "state parent must not be a symlink")
        with args.state.with_suffix(".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("another validation is active; do not run concurrently") from None
            state = load(args.state)
            if args.action == "status":
                code = 0 if state["status"] == "READY" else 2
            elif args.action == "review":
                code = review(args.state, state, args.file)
            else:
                require(0 < args.timeout <= 3600, "timeout must be in 1..3600 seconds")
                if args.action == "release-prepare":
                    code = release_prepare(args.state, state, args.timeout)
                elif args.action == "release-retry":
                    code = release_retry(args.state, state, args.sha, args.run_id, args.timeout)
                else:
                    command = args.command[1:] if args.command[:1] == ["--"] else args.command
                    code = repair_build(args.state, state, command, args.timeout) if args.action == "repair-build" else \
                        execute(args.state, state, command, args.label, args.action == "retry", args.timeout)
            print("VALIDATION_GATE " + json.dumps(summary(state), sort_keys=True), flush=True)
            return code
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("VALIDATION_GATE STOP: " + str(exc), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
