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
import subprocess
import sys
import tempfile
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / ".artifacts/validation-gate/state.json"
STATUSES = {"READY", "RUNNING", "BLOCKED", "RETRY_APPROVED", "HALTED"}


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


def execute(path, state, command, label, retry, timeout):
    require(command, "a validation command is required after --")
    identity = {"argv": command, "cwd": str(pathlib.Path.cwd().resolve())}
    command_sha = sha(json.dumps(identity, ensure_ascii=True, sort_keys=True).encode())
    require(state["status"] == ("RETRY_APPROVED" if retry else "READY"),
            "validation blocked: diagnose and review before retry; do not advance dependent work")
    if retry:
        active = state["active"]
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
