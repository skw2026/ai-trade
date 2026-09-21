#!/usr/bin/env python3
"""Prepare immutable local inputs/configs; execute only an explicitly approved
one-shot offline reference screen. This stage invokes execution ONLY in
synthetic tests. No downloads, account APIs, credentials, tuning or retries.
"""
import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from mvp_reference_inputs import (ROOT, CONTRACT_SHA256, compile_archive, load_contract,
                                  require, sha, strict_json)
from mvp_reference_verdict import assess, decision, parse_events

BASE_CONFIG = ROOT / "config/bybit.replay.mvp-reference.yaml"


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sources():
    files = sorted([*ROOT.glob("src/**/*.cpp"), *ROOT.glob("src/**/*.h")])
    files += [ROOT / "tools" / f for f in ("mvp_reference_inputs.py", "mvp_reference_verdict.py",
                                            "mvp_reference_pipeline.py")]
    return {str(p.relative_to(ROOT)): digest(p) for p in files}


def config_text(directory, scenario, contract):
    require(scenario in ("base", "stress"), "INVALID_COST_SCENARIO")
    require(not any(c in str(directory) for c in ('"', '\\', '\n', '\r')), "UNSAFE_OUTPUT_PATH")
    text = BASE_CONFIG.read_text()
    old = '  data_path: "./.artifacts/mvp-capital-reference-v1"'
    require(text.count(old) == 1, "REFERENCE_CONFIG_PATH_NOT_FIXED")
    text = text.replace(old, '  data_path: "' + str(directory / (scenario + "-account")) + '"')
    text = text.replace('    category: "linear"', '    category: "linear"\n    replay_market_data_path: "' +
                        str(directory / "replay.csv") + '"')
    if scenario == "stress":
        for key in ("entry_fee_bps", "exit_fee_bps", "slippage_bps"):
            yaml_key = "expected_slippage_bps" if key == "slippage_bps" else key
            old_value = "1.0" if key == "slippage_bps" else "5.5"
            text = text.replace(f"  {yaml_key}: {old_value}\n",
                                f"  {yaml_key}: {contract['costs']['stress'][key]}\n")
    return text


def write_json(path, value):
    with Path(path).open("x") as f:
        json.dump(value, f, sort_keys=True, indent=2)
        f.write("\n")


def prepare(manifest, directory, binary, contract=None):
    c = load_contract() if contract is None else contract
    raw, proof = compile_archive(manifest, c)
    directory, binary = Path(directory).resolve(), Path(binary).resolve()
    require(binary.is_file(), "BUILT_BINARY_REQUIRED")
    directory.mkdir(parents=False, exist_ok=False)
    with (directory / "replay.csv").open("xb") as f:
        f.write(raw)
    write_json(directory / "input-proof.json", proof)
    configs = {}
    for scenario in ("base", "stress"):
        path = directory / (scenario + ".yaml")
        with path.open("x") as f:
            f.write(config_text(directory, scenario, c))
        configs[scenario] = {"file": path.name, "sha256": digest(path)}
    plan = {"schema": "mvp_reference_run_plan_v1", "contract": c,
            "contract_sha256": proof["contract_sha256"], "synthetic_only": proof["synthetic_only"],
            "proof_sha256": digest(directory / "input-proof.json"), "csv_sha256": proof["csv_sha256"],
            "raw_manifest": str(Path(manifest).resolve()), "raw_manifest_sha256": proof["manifest_sha256"],
            "binary": str(binary), "binary_sha256": digest(binary), "configs": configs,
            "base_config_sha256": digest(BASE_CONFIG), "source_sha256": sources(),
            "automatic_history_authorization": False}
    write_json(directory / "run-plan.json", plan)
    return plan


def verify(plan_path, *, synthetic=False):
    plan_path = Path(plan_path).resolve()
    directory = plan_path.parent
    plan = strict_json(plan_path.read_bytes())
    require(plan["schema"] == "mvp_reference_run_plan_v1" and plan["synthetic_only"] is synthetic,
            "RUN_PLAN_SCOPE")
    if not synthetic:
        require(plan["contract_sha256"] == CONTRACT_SHA256 and plan["contract"] == load_contract(),
                "HISTORICAL_CONTRACT_CHANGED")
    # Recompile/rehash every raw page; a cached proof alone never establishes input identity.
    raw, proof = compile_archive(plan["raw_manifest"], plan["contract"])
    require(proof["synthetic_only"] is synthetic and proof["contract_sha256"] == plan["contract_sha256"],
            "SYNTHETIC_CONTRACT_IDENTITY")
    require(proof == strict_json((directory/"input-proof.json").read_bytes()) and
            digest(directory/"input-proof.json") == plan["proof_sha256"] and
            sha(raw) == digest(directory/"replay.csv") == plan["csv_sha256"] and
            digest(plan["raw_manifest"]) == plan["raw_manifest_sha256"], "INPUT_BUNDLE_CHANGED")
    require(digest(plan["binary"]) == plan["binary_sha256"] and digest(BASE_CONFIG) == plan["base_config_sha256"] and
            plan["source_sha256"] == sources(), "CODE_CONFIG_OR_BINARY_CHANGED")
    require(set(plan["configs"]) == {"base", "stress"}, "COST_CONFIGS_MISSING")
    for scenario, record in plan["configs"].items():
        require(record["file"] == scenario+".yaml", "CONFIG_PATH_SCOPE")
        path = directory/record["file"]
        require(digest(path) == record["sha256"] and path.read_text() == config_text(directory,scenario,plan["contract"]),
                "GENERATED_CONFIG_CHANGED")
    return plan, directory


def execute(plan_path, *, approved=False, synthetic=False):
    require(approved or synthetic, "NEW_EXPLICIT_HISTORY_APPROVAL_REQUIRED")
    plan, directory = verify(plan_path, synthetic=synthetic)
    plan_identity = digest(plan_path)
    # O_EXCL claim prevents replay/restart in this directory, including failed/timed-out attempts.
    with (directory / "execution-claim.json").open("x") as f:
        json.dump({"plan_sha256": plan_identity, "synthetic_only": synthetic}, f)
    start = time.monotonic()
    budget = 30 if synthetic else plan["contract"]["future_history_budget_proposal_not_authorization"]["max_compute_seconds"]
    runs, receipts = {}, []
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ") if key in os.environ}
    for scenario in ("base", "stress"):
        require(digest(plan_path) == plan_identity, "RUN_PLAN_CHANGED")
        verify(plan_path, synthetic=synthetic)
        remaining = budget - (time.monotonic()-start)
        require(remaining > 0, "COMPUTE_BUDGET_EXHAUSTED")
        log_path = directory/(scenario+".log")
        argv = [plan["binary"], "--config=" + str(directory/plan["configs"][scenario]["file"])]
        with log_path.open("x") as log:
            try:
                code = subprocess.run(argv, cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT,
                                      timeout=remaining, check=False).returncode
            except subprocess.TimeoutExpired:
                code = 124
            except OSError:
                code = 126
        try:
            with log_path.open() as log:
                runs[scenario] = parse_events(log)
        except (ValueError, UnicodeError):
            runs[scenario] = {"bars": [], "fills": [], "terminals": [],
                              "stops": [{"reason": "INSUFFICIENT_LOG_PARSE"}]}
        receipts.append({"scenario": scenario, "argv": argv, "cwd": str(directory), "exit_code": code,
                         "log_sha256": digest(log_path), "binary_sha256": plan["binary_sha256"],
                         "config_sha256": plan["configs"][scenario]["sha256"], "csv_sha256": plan["csv_sha256"]})
        try:
            require(digest(plan_path) == plan_identity, "RUN_PLAN_CHANGED")
            verify(plan_path, synthetic=synthetic)
        except (ValueError, OSError, KeyError, TypeError):
            # An altered identity invalidates even an otherwise explicit risk reject.
            runs = {scenario: {"bars": [], "fills": [], "terminals": [],
                    "stops": [{"reason": "INSUFFICIENT_POST_RUN_IDENTITY"}]}}
            break
        # Stop immediately: no second scenario or automatic retry after an aborted run.
        if code != 0:
            if not runs[scenario]["stops"]:
                runs[scenario]["stops"] = [{"reason": "INSUFFICIENT_EXIT_OR_TIMEOUT"}]
            break
        if runs[scenario]["stops"]:
            break
    result = assess(runs, plan["contract"])
    result.update(synthetic_only=synthetic, plan_sha256=plan_identity, receipts=receipts,
                  elapsed_seconds=time.monotonic()-start)
    write_json(directory/"result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--binary", type=Path, default=ROOT/"build/trade_bot")
    p = sub.add_parser("execute")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--approved-one-shot-history", action="store_true",
                   help="does not obtain permission: use only AFTER a new explicit user approval")
    args = parser.parse_args()
    if args.action == "prepare":
        plan = prepare(args.manifest,args.output,args.binary)
        print(json.dumps({"prepared":True,"historical_execution_started":False,"contract_sha256":plan["contract_sha256"]}))
    else:
        print(json.dumps(execute(args.plan,approved=args.approved_one_shot_history),sort_keys=True))


if __name__ == "__main__":main()
