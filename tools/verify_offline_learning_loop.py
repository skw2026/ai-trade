#!/usr/bin/env python3
"""TEST_ONLY causal learning integration. No network, market data or promotion.

Uses the real Miner, Integrator feature/fit functions, native inference, policy,
risk, execution, accounting and evolution components. This is NOT the production
training/registry transaction or an exchange fill simulation certification.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import pathlib
import random
import statistics
import subprocess
import sys

import integrator_train as train

TRAIN_BARS = 2048
EMBARGO = 128
START = TRAIN_BARS + EMBARGO
EVAL_BARS = 1024
BAR_MS = 300000
SEED = 20260922
FIELDS = ("timestamp", "open", "high", "low", "close", "volume")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")


def generate(count, mode="learnable"):
    """Volume cue at t affects return at t+2, never the contemporaneous return."""
    rng = random.Random(SEED)
    noise_rng = random.Random(SEED + 1)
    cues = [rng.choice((-1, 1)) * rng.uniform(0.8, 1.2) for _ in range(count)]
    rows, price = [], 100.0
    for i in range(count):
        cue = cues[i - 2] if i >= 2 else 0
        noise = noise_rng.gauss(0, 0.00015)
        ret = 0.003 * cue + noise
        if mode == "noise" and i >= START:
            ret = noise_rng.gauss(0, 0.003)
        if mode == "drift" and i >= START + 768:
            ret = -0.003 * cue + noise
        opening = price
        price *= 1 + ret
        rows.append((1704067200000 + i * BAR_MS, opening,
                     max(opening, price) * 1.0001, min(opening, price) * 0.9999,
                     price, 1000 + 200 * cues[i]))
    return rows


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(FIELDS)
        writer.writerows(rows)


def run_driver(binary, args, log_path, expected_error=None):
    result = subprocess.run([str(binary), *map(str, args)], text=True,
                            capture_output=True, timeout=120)
    log_path.write_text(result.stdout + result.stderr)
    if expected_error:
        require(result.returncode != 0 and expected_error in result.stderr,
                f"expected {expected_error}; got {result.returncode}: {result.stderr[-1200:]}")
    else:
        require(result.returncode == 0,
                f"native driver failed ({log_path.name}): {result.stderr[-1600:]}")


def summarize(trace, rows, fee_bps):
    """Independent ledger reconstruction from decisions, not supplied PnL."""
    with trace.open() as stream:
        records = list(csv.DictReader(stream))
    require(records, "empty native trace")
    episodes = []
    total_fee = total_funding = total_net = 0.0
    for record in records:
        if record["applied"] != "1":
            continue
        i, direction = int(record["index"]), int(record["direction"])
        qty = 80 * float(record["weight"]) / rows[i + 1][4]
        entry = rows[i + 1][4] * (1 + direction * 0.0001)
        exit_price = rows[i + 2][4] * (1 - direction * 0.0001)
        fee = qty * (entry + exit_price) * fee_bps / 10000
        funding = direction * qty * rows[i + 2][4] * 0.000025
        net = direction * qty * (exit_price - entry) - fee - funding
        episodes.append(net)
        total_fee += fee
        total_funding += funding
        total_net += net
    last = records[-1]
    for field, expected in (("net", total_net), ("fee", total_fee), ("funding", total_funding)):
        require(abs(float(last[field]) - expected) < 1e-7, f"independent {field} ledger mismatch")
    require(int(last["episodes"]) == len(episodes), "episode count mismatch")
    require(int(last["fills"]) == 2 * len(episodes), "fill count mismatch")
    require(abs(float(last["qty"])) < 1e-10, "nonflat terminal")
    mean = statistics.mean(episodes) if episodes else None
    lcb = (mean - train.student_t_975(len(episodes) - 1) * statistics.stdev(episodes) /
           math.sqrt(len(episodes))) if len(episodes) > 1 else None
    actions = [r["action"] for r in records]
    previous_weight, weight_changes = 0.5, 0
    for record in records:
        weight = float(record["weight"])
        if record["action"].startswith("EVOLUTION_WEIGHT_") and abs(weight - previous_weight) > 1e-10:
            weight_changes += 1
        previous_weight = weight
    return {
        "episodes": len(episodes), "mean_net": mean, "mean_net_lcb95": lcb,
        "net": total_net, "fee": total_fee, "funding": total_funding,
        "offline_control_accepted": len(episodes) >= 80 and lcb is not None and lcb > 0,
        "update_decisions": sum(a.startswith("EVOLUTION_WEIGHT_") for a in actions),
        "weight_changes": weight_changes,
        "rollbacks": actions.count("EVOLUTION_ROLLBACK_TRIGGERED"),
        "cooldown_evaluations": actions.count("EVOLUTION_COOLDOWN_ACTIVE"),
        "native_ledger_matched": True, "terminal_flat": True,
    }, records


def check_case(name, result, cases):
    """Stop dependent scenarios at the first unexpected verdict, not at EOF."""
    if name == "positive":
        require(result["offline_control_accepted"], "learnable control rejected")
    elif name == "adaptive":
        require(result["offline_control_accepted"] and result["weight_changes"] > 0,
                "adaptive path never updated on known learnable data")
        require(result["net"] > cases["positive"]["net"], "synthetic adaptive uplift absent")
    elif name in ("noise", "shuffled", "cost"):
        require(not result["offline_control_accepted"], f"{name} wrongly qualified")
    elif name == "drift":
        require(result["rollbacks"] > 0 and result["cooldown_evaluations"] > 0,
                "drift did not trigger rollback and freeze")


def verify(binary, output):
    require(train.np is not None and train.CatBoostClassifier is not None,
            "real numpy/CatBoost required; no skipped or mocked learning")
    training_root = output
    restore = output.exists()
    if restore:
        # Reviewed retries reuse the exact trained candidates, without
        # deleting the failed trace, retraining or looking for a better seed.
        require((output / "positive.log").is_file() and not (output / "result.json").exists(),
                "existing output is not an incomplete first attempt")
        output = output / ("retry-2" if (output / "retry-1").exists() else "retry-1")
    output.mkdir(parents=True, exist_ok=False)
    # Separate generator calls have identical development prefixes, independent
    # of future domain length and mode (checked below).
    positive_rows = generate(START + EVAL_BARS)
    development = positive_rows[:TRAIN_BARS]
    dev_csv = training_root / "development.csv"
    miner = training_root / "miner.json"
    if not restore:
        write_csv(dev_csv, development)
        run_driver(binary, ["mine", dev_csv, miner], output / "miner.log")
    factor_version, specs = train.load_factor_specs(miner, 3,
        expected_horizon_bars=1, expected_execution_latency_bars=1)
    raw, names, _ = train.build_feature_matrix(train.load_ohlcv_csv(dev_csv), specs)
    # Finite-window features only: the native engine and training share their
    # exact causal history. EMA approximation is covered by existing parity tests.
    selected = [i for i, name in enumerate(names) if name.startswith("miner_") or name == "ret_1"]
    names = [names[i] for i in selected]
    raw = raw[:, selected]
    y, _ = train.build_label(train.np.asarray([r[4] for r in development]), 1,
                             execution_latency_bars=1)
    fit_raw, fit_y = raw[300:-2], y[300:-2]
    fit, transform = train.build_feature_transform(fit_raw, names, 0.01)
    require(train.np.isfinite(fit).all() and train.np.isfinite(fit_y).all(), "nonfinite training")
    models = {}
    for label in ("learned", "shuffled"):
        model_path = training_root / f"{label}.cbm"
        metadata = training_root / f"{label}.json"
        if restore:
            frozen = json.loads(metadata.read_text())
            require(frozen["model_sha256"] == sha(model_path) and
                    frozen["data"]["training_csv_sha256"] == sha(dev_csv),
                    "frozen candidate/data identity changed")
            model = train.CatBoostClassifier()
            model.load_model(str(model_path))
            require(frozen["feature_names"] == names and frozen["feature_transform"] == transform,
                    "frozen feature contract changed")
            models[label] = (model, model_path, metadata, frozen["model_version"])
            continue
        model = train.build_catboost_classifier(random_seed=42, iterations=80, depth=4,
            learning_rate=0.1, l2_leaf_reg=3, random_strength=0.2, subsample=1, rsm=1)
        model.set_params(thread_count=2)
        labels = fit_y.copy()
        if label == "shuffled":
            train.np.random.default_rng(SEED).shuffle(labels)
        model.fit(fit, labels)
        model.save_model(str(model_path))
        version = "TEST_ONLY_" + sha(model_path)[:20]
        write_json(metadata, {
            "test_only": True, "production_promotion_authority": False,
            "model_version": version, "model_sha256": sha(model_path),
            "feature_names": names, "feature_transform": transform,
            "governance": {"pass": False, "reason": "SYNTHETIC_TEST_ONLY"},
            "data": {"csv_path": str(dev_csv), "miner_report_path": str(miner),
                     "training_symbol": "SYNTHBTC", "bar_interval_ms": BAR_MS,
                     "online_bar_source": "closed_ohlcv", "source_venue": "synthetic",
                     "source_category": "test_only", "price_type": "generated",
                     "volume_unit": "synthetic", "training_csv_sha256": sha(dev_csv)},
        })
        models[label] = (model, model_path, metadata, version)
    cases = {}
    scenarios = (("positive", "learnable", "learned", 5.5, False, EVAL_BARS),
                 ("adaptive", "learnable", "learned", 5.5, True, EVAL_BARS),
                 ("noise", "noise", "learned", 5.5, False, EVAL_BARS),
                 ("shuffled", "learnable", "shuffled", 5.5, False, EVAL_BARS),
                 ("cost", "learnable", "learned", 100.0, False, EVAL_BARS),
                 ("drift", "drift", "learned", 5.5, True, 1536))
    for name, mode, label, fee, adaptive, count in scenarios:
        # Prefix invariance: generate the same length to keep RNG independent
        # of whether future rows were requested; generation itself enforces this.
        rows = generate(START + count, mode)
        require(rows[:TRAIN_BARS] == development, "future generation changed training prefix")
        csv_path, trace = output / f"{name}.csv", output / f"{name}.trace.csv"
        write_csv(csv_path, rows)
        model, model_path, metadata, version = models[label]
        before_model = sha(model_path)
        args = ["replay", metadata, model_path, csv_path, START, fee, int(adaptive), version, trace]
        run_driver(binary, args, output / f"{name}.log")
        result, records = summarize(trace, rows, fee)
        require(sha(model_path) == before_model, "model changed during holdout")
        matrix, all_names, _ = train.build_feature_matrix(train.load_ohlcv_csv(csv_path), specs)
        matrix = matrix[:, [all_names.index(n) for n in names]]
        transformed = train.apply_feature_transform(matrix, names, transform)
        expected = model.predict_proba(transformed[START:])[:, 1]
        error = max(abs(float(r["p_up"]) - float(p)) for r, p in zip(records, expected))
        require(len(records) == len(expected) and error <= 1e-5,
                f"{name}: native/Python probability mismatch {error}")
        result["max_probability_error"] = error
        cases[name] = result
        check_case(name, result, cases)
        print(json.dumps({"case": name, **result}), flush=True)
        if name == "positive":
            repeat = output / "repeat.trace.csv"
            run_driver(binary, [*args[:-1], repeat], output / "repeat.log")
            require(sha(trace) == sha(repeat), "same-candidate replay not reproducible")
            for fault, replacement, expected_error in (
                ("identity", {7: "WRONG_CANDIDATE"}, "CANDIDATE_IDENTITY_MISMATCH"),
                ("missing_model", {2: output / "absent.cbm"}, "model load:"),
            ):
                bad = args.copy()
                for index, value in replacement.items():
                    bad[index] = value
                bad[-1] = output / f"{fault}.trace.csv"
                run_driver(binary, bad, output / f"{fault}.log", expected_error)
                require(not bad[-1].exists(), "failed input created execution trace")
            gap = output / "gap.csv"
            write_csv(gap, rows[:START + 1] + rows[START + 2:])
            bad = args.copy()
            bad[3], bad[-1] = gap, output / "gap.trace.csv"
            run_driver(binary, bad, output / "gap.log", "INVALID_SYNTHETIC_TIME_AXIS_OR_OHLC")
            require(not bad[-1].exists(), "gapped input created execution trace")
    # Fixed-direction comparison uses the same declared latency and costs.
    fixed_returns = []
    for i in range(START, len(positive_rows) - 2, 2):
        entry = positive_rows[i + 1][4] * 1.0001
        exit_price = positive_rows[i + 2][4] * 0.9999
        qty = 40 / positive_rows[i + 1][4]
        fixed_returns.append(qty * (exit_price - entry) - qty * (entry + exit_price) * 0.00055
                             - qty * positive_rows[i + 2][4] * 0.000025)
    require(cases["positive"]["net"] > sum(fixed_returns), "no uplift over fixed direction")
    report = {
        "schema_version": "offline_learning_loop_v1", "status": "PASS",
        "scope": "TEST_ONLY_OFFLINE_COMPONENT_INTEGRATION",
        "production_promotion_authority": False, "market_economic_evidence": False,
        "demo_trading_authority": False, "full_mechanism_valid": False,
        "verification_git_sha": os.environ.get("VERIFICATION_SHA", "uncommitted_local"),
        "verifier_sha256": sha(pathlib.Path(__file__)),
        "training_implementation_sha256": sha(pathlib.Path(train.__file__)),
        "seed": SEED, "development_bars": TRAIN_BARS, "embargo_bars": EMBARGO,
        "fit_last_feature_index": TRAIN_BARS - 3, "last_training_label_index": TRAIN_BARS - 1,
        "first_acceptance_feature_index": START, "factor_set_version": factor_version,
        "cases": cases, "fixed_direction_net": sum(fixed_returns),
        "driver_sha256": sha(binary),
        "training_artifact_sha256": {name: sha(training_root / name) for name in
            ("development.csv", "miner.json", "learned.cbm", "learned.json", "shuffled.cbm", "shuffled.json")},
        "artifact_sha256": {p.name: sha(p) for p in sorted(output.iterdir()) if p.is_file()},
        "limits": ["synthetic fills, not exchange matching", "no production registry promotion",
                   "no live canary", "no real-market adaptive uplift claim",
                   "adaptive-vs-frozen exposure differs; not risk-normalized alpha evidence",
                   "counterfactual holdout selection covered separately, not this controller fixture",
                   "controller combines evaluation/update intervals; independent hourly evaluation unproven",
                   "uses production training functions, not full research orchestration"],
    }
    write_json(output / "result.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        report = verify(args.driver.resolve(), args.output.resolve())
        print(json.dumps({"status": report["status"], "scope": report["scope"]}))
        return 0
    except Exception as error:
        print(f"OFFLINE_LEARNING_LOOP_FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
