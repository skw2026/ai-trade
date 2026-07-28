#!/usr/bin/env python3
"""
模型版本注册与激活脚本。

目标：
1. 将每次训练产物（.cbm + report）注册为可追溯版本；
2. 基于成本后净经济目标决定是否激活为当前线上版本；
3. 维护 index 清单与历史版本保留上限。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    import fcntl  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - 非 POSIX 环境兜底
    fcntl = None

MIN_POSITIVE_FILLED_SEGMENT_RATIO = 0.55
EXIT_CAPTURE_MIN_MEAN_GROSS_CAPTURE_OF_PATH_MFE = 0.10
EXPECTED_MODEL_OBJECTIVE = (
    "aggregate_model_net_bps_per_unit_turnover_after_cost"
)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLOSED_LOOP_CONTRACT_PATH = PROJECT_ROOT / "config" / "closed_loop_contract.json"


def is_allowed_activation_gate_warning(reason: str) -> bool:
    text = str(reason or "").strip()
    return text.startswith(
        (
            "symbol_replay_coverage_insufficient=",
            "symbol_replay_quarantined=",
            "diagnostic_optimizer_candidate_not_promotion_authority=",
        )
    )


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc_compact() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 使用原子替换避免写一半进程中断导致 JSON 损坏。
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def validate_economic_objective_contract(
    contract: Any,
    *,
    execution_policy_sha256: str,
    trade_bot_sha256: str,
) -> List[str]:
    reasons: List[str] = []
    if not isinstance(contract, dict):
        return ["replay candidate economic objective contract is missing"]

    expected_literals = {
        "schema_version": "economic_objective_contract_v1",
        "primary_metric": "mean_realized_net_per_fill",
        "authoritative_execution": "cpp_trade_bot_replay",
        "fill_model": "next_bar_ohlc_first_touch_v1",
        "terminal_position_policy": "force_close_and_charge_exit_cost",
        "funding_policy": "per_bar_rate_from_replay_dataset",
        "accounting_source": "replay_terminal_account_state",
        "gross_pnl_formula": "realized_net_plus_fee_plus_funding_paid",
        "fee_sensitivity_formula": "gross_minus_funding_paid_minus_scaled_fee",
        "funding_sensitivity_policy": "fixed_while_scaling_fee",
        "fill_count_source": "all_fill_applied_events_current_boot",
        "incomplete_economics_policy": "hard_fail",
        "state_isolation_policy": "fresh_wal_per_symbol_segment",
        "cost_policy_source": "execution_policy_v2",
    }
    for key, expected in expected_literals.items():
        if str(contract.get(key, "")).strip() != expected:
            reasons.append(
                f"replay economic objective {key} differs from {expected}"
            )
    if contract.get("terminal_settlement_evidence_required") is not True:
        reasons.append(
            "replay economic objective does not require terminal settlement evidence"
        )
    if contract.get("selection_and_final_share_contract") is not True:
        reasons.append(
            "replay economic objective does not bind selection and final"
        )
    if (
        str(contract.get("execution_policy_sha256", "")).strip()
        != execution_policy_sha256
    ):
        reasons.append(
            "replay economic objective execution policy checksum mismatch"
        )
    if (
        str(contract.get("trade_bot_sha256", "")).strip()
        != trade_bot_sha256
    ):
        reasons.append(
            "replay economic objective trade_bot checksum mismatch"
        )

    thresholds = contract.get("thresholds")
    required_thresholds = {
        "assess_stage",
        "min_runtime_status",
        "min_execution_active_runs",
        "min_execution_pass_runs",
        "min_total_fills",
        "min_mean_realized_net_per_fill",
        "min_break_even_fee_multiplier",
        "warn_mean_filtered_cost_ratio",
        "min_tradable_symbols",
        "min_positive_filled_segment_ratio",
    }
    if not isinstance(thresholds, dict) or set(thresholds) != required_thresholds:
        reasons.append(
            "replay economic objective threshold set is incomplete or mutable"
        )
    elif any(
        isinstance(value, float) and not math.isfinite(value)
        for value in thresholds.values()
    ):
        reasons.append(
            "replay economic objective thresholds contain non-finite values"
        )

    segment_sampling = contract.get("segment_sampling")
    required_sampling = {
        "target_bucket",
        "selection_policy",
        "max_segments",
        "min_segment_bars",
        "final_outcome_ranking_forbidden",
    }
    if (
        not isinstance(segment_sampling, dict)
        or set(segment_sampling) != required_sampling
        or segment_sampling.get("final_outcome_ranking_forbidden") is not True
    ):
        reasons.append(
            "replay economic objective segment sampling contract is invalid"
        )

    implementation_sha256 = contract.get("implementation_sha256")
    expected_implementations = {
        "replay_runner": PROJECT_ROOT / "tools" / "run_replay_validation.py",
        "runtime_assessor": PROJECT_ROOT / "tools" / "assess_run_log.py",
        "policy_contract": PROJECT_ROOT / "tools" / "config_policy_contract.py",
    }
    if (
        not isinstance(implementation_sha256, dict)
        or set(implementation_sha256) != set(expected_implementations)
    ):
        reasons.append(
            "replay economic objective implementation checksum set is invalid"
        )
    else:
        for name, path in expected_implementations.items():
            expected_sha256 = sha256_file(path) if path.is_file() else ""
            if (
                str(implementation_sha256.get(name, "")).strip()
                != expected_sha256
            ):
                reasons.append(
                    "replay economic objective implementation checksum "
                    f"mismatch: {name}"
                )

    governance_contract = contract.get("governance_contract")
    expected_governance_sha256 = (
        sha256_file(CLOSED_LOOP_CONTRACT_PATH)
        if CLOSED_LOOP_CONTRACT_PATH.is_file()
        else ""
    )
    if (
        not isinstance(governance_contract, dict)
        or str(governance_contract.get("sha256", "")).strip()
        != expected_governance_sha256
    ):
        reasons.append(
            "replay economic objective governance contract checksum mismatch"
        )

    canonical_payload = dict(contract)
    reported_sha256 = str(canonical_payload.pop("sha256", "")).strip()
    computed_sha256 = hashlib.sha256(
        json.dumps(
            canonical_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if reported_sha256 != computed_sha256:
        reasons.append(
            "replay economic objective contract checksum differs from payload"
        )
    return reasons


def load_verified_holdout_ledger(ledger_path: Path) -> List[Dict[str, Any]]:
    checkpoint_path = ledger_path.with_suffix(
        ledger_path.suffix + ".checkpoint.json"
    )
    if not ledger_path.is_file():
        raise ValueError(f"final holdout ledger not found: {ledger_path}")
    entries: List[Dict[str, Any]] = []
    previous_sha256 = "0" * 64
    for line_number, raw_line in enumerate(
        ledger_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"final holdout ledger invalid JSON at line {line_number}"
            ) from exc
        if not isinstance(entry, dict):
            raise ValueError(
                f"final holdout ledger entry is not object at line {line_number}"
            )
        reported_sha256 = str(entry.get("entry_sha256") or "").strip()
        hash_payload = dict(entry)
        hash_payload.pop("entry_sha256", None)
        computed_sha256 = hashlib.sha256(
            json.dumps(
                hash_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if (
            entry.get("schema_version") != "final_holdout_consumption_v2"
            or entry.get("previous_entry_sha256") != previous_sha256
            or reported_sha256 != computed_sha256
        ):
            raise ValueError(
                f"final holdout ledger hash chain invalid at line {line_number}"
            )
        entries.append(entry)
        previous_sha256 = reported_sha256
    if not entries:
        raise ValueError("final holdout ledger has no consumption entries")
    if not checkpoint_path.is_file():
        raise ValueError("final holdout ledger checkpoint missing")
    try:
        checkpoint = read_json(checkpoint_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("final holdout ledger checkpoint unreadable") from exc
    if (
        checkpoint.get("schema_version") != "final_holdout_checkpoint_v1"
        or int(checkpoint.get("entry_count") or 0) != len(entries)
        or checkpoint.get("tail_entry_sha256")
        != entries[-1].get("entry_sha256")
    ):
        raise ValueError(
            "final holdout ledger checkpoint mismatch; deletion/truncation detected"
        )
    return entries


def validate_final_holdout_consumption(
    binding: Any,
    *,
    candidate_identity_sha256: str,
) -> Tuple[List[str], List[str]]:
    reasons: List[str] = []
    symbols: List[str] = []
    if not isinstance(binding, dict):
        return ["final holdout consumption binding is missing"], symbols
    if (
        binding.get("schema_version")
        != "final_holdout_consumption_binding_v1"
        or binding.get("claimed_before_evaluation") is not True
    ):
        reasons.append(
            "final holdout was not claimed before candidate evaluation"
        )
    experiment_id = str(binding.get("experiment_id") or "").strip()
    if not experiment_id:
        reasons.append("final holdout experiment_id is missing")
    ledger_path = canonical_path(binding.get("ledger_path"))
    claims = binding.get("claims")
    if ledger_path is None:
        reasons.append("final holdout ledger path is missing")
        return reasons, symbols
    if not isinstance(claims, list) or not claims:
        reasons.append("final holdout consumption claims are missing")
        return reasons, symbols
    try:
        entries = load_verified_holdout_ledger(ledger_path)
    except ValueError as exc:
        reasons.append(str(exc))
        return reasons, symbols

    seen_symbols: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            reasons.append("final holdout consumption claim is not object")
            continue
        symbol = str(claim.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen_symbols:
            reasons.append(
                "final holdout consumption claim symbol is missing or duplicated"
            )
        else:
            seen_symbols.add(symbol)
            symbols.append(symbol)
        if (
            claim.get("schema_version") != "final_holdout_consumption_v2"
            or str(claim.get("experiment_id") or "").strip()
            != experiment_id
            or str(claim.get("candidate_identity_sha256") or "").strip()
            != candidate_identity_sha256
            or claim.get("status") != "opened_before_evaluation"
        ):
            reasons.append(
                f"final holdout consumption identity mismatch for {symbol or 'unknown'}"
            )
        hash_payload = dict(claim)
        reported_sha256 = str(hash_payload.pop("entry_sha256", "")).strip()
        computed_sha256 = hashlib.sha256(
            json.dumps(
                hash_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if reported_sha256 != computed_sha256:
            reasons.append(
                f"final holdout consumption checksum mismatch for {symbol or 'unknown'}"
            )
        if claim not in entries:
            reasons.append(
                f"final holdout consumption claim absent from ledger for {symbol or 'unknown'}"
            )
    return reasons, sorted(symbols)


def canonical_path(path_value: Any) -> Path | None:
    path_text = str(path_value or "").strip()
    if not path_text:
        return None
    return Path(path_text).expanduser().resolve(strict=False)


def holdout_artifact_contract(domain_result: Dict[str, Any]) -> Tuple[str, str]:
    holdout = domain_result.get("holdout_feature_csv")
    if isinstance(holdout, dict):
        return (
            str(holdout.get("path") or "").strip(),
            str(holdout.get("sha256") or "").strip().lower(),
        )
    return (
        str(holdout or "").strip(),
        str(domain_result.get("holdout_feature_sha256") or "").strip().lower(),
    )


def sanitize_name(raw: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", raw.strip())
    value = value.strip("._-")
    return value or "unknown_model"


def atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def load_index(index_path: Path) -> List[Dict[str, Any]]:
    if not index_path.exists():
        return []
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return payload
    except json.JSONDecodeError:
        pass
    return []


class FileLock:
    """
    轻量文件锁（仅用于索引更新的临界区）。

    说明：
    1. POSIX 环境使用 flock；非 POSIX 环境降级为无锁（单进程仍可运行）；
    2. 锁文件与 index.json 同目录，避免跨文件系统行为不一致。
    """

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fd = None

    def __enter__(self) -> "FileLock":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = self._lock_path.open("a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is None:
            return
        try:
            self._fd.flush()
            os.fsync(self._fd.fileno())
        except OSError:
            pass
        if fcntl is not None:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        self._fd.close()
        self._fd = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ai-trade 模型版本注册/激活工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register", help="注册训练产物并按门槛激活")
    register.add_argument("--model_file", required=True, help="训练产物模型文件（.cbm）")
    register.add_argument("--integrator_report", required=True, help="integrator_report.json 路径")
    register.add_argument("--miner_report", default="", help="可选：miner_report.json 路径")
    register.add_argument("--walkforward_report", default="", help="可选：walkforward_report.json 路径")
    register.add_argument(
        "--replay_validation_report",
        default="",
        help="可选：replay_validation_report.json 路径",
    )
    register.add_argument(
        "--research_domain_split_report",
        default="",
        help="development/embargo/untouched-holdout 数据域契约报告",
    )
    register.add_argument(
        "--feature_parity_report",
        default="",
        help="生产 C++ 在线特征与 Python 训练 golden vectors 一致性报告",
    )
    register.add_argument(
        "--alpha_mechanism_probe_report",
        default="",
        help="可选：alpha_mechanism_probe_report.json 路径；market alpha 族失败时阻断注册",
    )
    register.add_argument("--registry_dir", default="./data/models/registry", help="模型注册目录")
    register.add_argument("--max_versions", type=int, default=20, help="历史版本最大保留数")
    register.add_argument(
        "--active_model_path",
        default="./data/models/integrator_latest.cbm",
        help="激活模型写入路径",
    )
    register.add_argument(
        "--active_report_path",
        default="./data/research/integrator_report.json",
        help="激活报告写入路径",
    )
    register.add_argument(
        "--active_miner_report_path",
        default="./data/research/miner_report.json",
        help="激活 miner 报告写入路径（供运行期稳定引用）",
    )
    register.add_argument(
        "--active_meta_path",
        default="./data/models/integrator_active.json",
        help="激活元信息写入路径",
    )
    register.add_argument("--min_auc_mean", type=float, default=0.50, help="最小 AUC 均值门槛")
    register.add_argument(
        "--min_delta_auc_vs_baseline",
        type=float,
        default=0.0,
        help="Delta AUC 诊断阈值（低于该值记录 warning，不再作为主激活门槛）",
    )
    register.add_argument(
        "--min_mean_model_net_edge_bps",
        type=float,
        default=0.0,
        help="主激活门槛：模型 OOS 方向扣除 round-trip cost 后的最小平均净 edge bps",
    )
    register.add_argument(
        "--min_positive_model_net_edge_ratio",
        type=float,
        default=0.50,
        help="主激活门槛：模型 OOS 净 edge 为正的最小样本比例",
    )
    register.add_argument(
        "--min_model_net_total_trades",
        type=int,
        default=20,
        help="主激活门槛：非重叠 OOS 换仓事件数下限",
    )
    register.add_argument(
        "--min_model_net_active_bars",
        type=int,
        default=100,
        help="主激活门槛：非重叠 OOS 活跃持仓 bar 下限",
    )
    register.add_argument(
        "--min_positive_model_net_splits_ratio",
        type=float,
        default=0.50,
        help="主激活门槛：成本后为正的 OOS split 比例下限",
    )
    register.add_argument(
        "--min_model_net_edge_lcb_bps",
        type=float,
        default=0.0,
        help="主激活门槛：OOS 每 bar 净收益 95% 下置信界",
    )
    register.add_argument(
        "--min_split_trained_count",
        type=int,
        default=1,
        help="最小训练成功 split 数门槛",
    )
    register.add_argument(
        "--min_split_trained_ratio",
        type=float,
        default=0.5,
        help="最小训练成功 split 比例门槛",
    )
    register.add_argument(
        "--activate_on_pass",
        action="store_true",
        help="门槛通过后自动激活为当前版本",
    )
    register.add_argument(
        "--activation_transaction",
        default="",
        help="激活前必须存在的 prepared 两阶段事务状态文件",
    )
    register.add_argument(
        "--require_walkforward_positive",
        action="store_true",
        help="要求 walk-forward 满足净收益门槛后才允许激活",
    )
    register.add_argument(
        "--min_walkforward_avg_split_return",
        type=float,
        default=0.0,
        help="walk-forward 平均 split 收益最低门槛",
    )
    register.add_argument(
        "--min_walkforward_enabled_avg_split_return",
        type=float,
        default=0.0,
        help="walk-forward 启用 split 平均收益最低门槛",
    )
    register.add_argument(
        "--min_walkforward_traded_avg_split_return",
        type=float,
        default=0.0,
        help="walk-forward 交易 split 平均收益最低门槛",
    )
    register.add_argument(
        "--walkforward_focus_bucket",
        default="",
        help="可选：以指定 regime bucket 作为主链 walk-forward 通过口径（例如 S5 使用 trend）",
    )
    register.add_argument(
        "--walkforward_min_focus_bucket_bars",
        type=int,
        default=0,
        help="focus bucket 生效所需最小 bars",
    )
    register.add_argument(
        "--walkforward_min_focus_bucket_trades",
        type=int,
        default=0,
        help="focus bucket 最小交易次数",
    )
    register.add_argument(
        "--walkforward_min_focus_bucket_sharpe",
        type=float,
        default=0.0,
        help="focus bucket 最小 Sharpe",
    )
    register.add_argument(
        "--walkforward_focus_bucket_primary",
        action="store_true",
        help="focus bucket 通过时，将全局非目标 bucket 收益失败降级为 warning",
    )
    register.add_argument(
        "--require_replay_validation_pass",
        action="store_true",
        help="要求 replay validation 状态为 pass 后才允许激活",
    )
    register.add_argument(
        "--registration_out",
        default="",
        help="可选：将本次注册结果单独输出到 JSON 文件",
    )
    return parser.parse_args()


def gate_integrator_report(
    report: Dict[str, Any],
    min_auc_mean: float,
    min_delta_auc_vs_baseline: float,
    min_mean_model_net_edge_bps: float,
    min_positive_model_net_edge_ratio: float,
    min_split_trained_count: int,
    min_split_trained_ratio: float,
    min_model_net_total_trades: int = 20,
    min_model_net_active_bars: int = 100,
    min_positive_model_net_splits_ratio: float = 0.50,
    min_model_net_edge_lcb_bps: float = 0.0,
) -> Tuple[bool, List[str], List[str], Dict[str, Any]]:
    metrics = report.get("metrics_oos", {})
    governance = report.get("governance", {})
    data = report.get("data", {})
    feature_transform = report.get("feature_transform", {})
    primary_objective = metrics.get("primary_objective")
    governance_primary_objective = (
        governance.get("primary_objective") if isinstance(governance, dict) else None
    )
    mean_model_net_edge_bps = metrics.get("mean_model_net_edge_bps")
    median_model_net_edge_bps = metrics.get("median_model_net_edge_bps")
    positive_model_net_edge_ratio = metrics.get("positive_model_net_edge_ratio")
    model_net_objective_sample_count = metrics.get("model_net_objective_sample_count")
    model_net_total_trades = metrics.get("model_net_total_trades")
    model_net_active_bar_count = metrics.get("model_net_active_bar_count")
    positive_model_net_splits_ratio = metrics.get(
        "positive_model_net_edge_ratio_by_split"
    )
    model_net_edge_lcb_bps = metrics.get("model_net_edge_lcb_bps")
    model_net_edge_lcb_method = metrics.get("model_net_edge_lcb_method")
    oos_duplicate_bar_ratio = metrics.get("oos_duplicate_bar_ratio")
    auc_mean = metrics.get("auc_mean")
    delta_auc = metrics.get("delta_auc_vs_baseline")
    trained_count = metrics.get("split_trained_count")
    split_count = metrics.get("split_count")
    split_trained_ratio = metrics.get("split_trained_ratio")
    training_symbol = (
        str(data.get("training_symbol") or "").strip().upper()
        if isinstance(data, dict)
        else ""
    )
    bar_interval_ms = data.get("bar_interval_ms") if isinstance(data, dict) else None
    online_bar_source = (
        str(data.get("online_bar_source") or "").strip()
        if isinstance(data, dict)
        else ""
    )
    source_venue = str(data.get("source_venue") or "").strip().lower()
    source_category = str(data.get("source_category") or "").strip().lower()
    price_type = str(data.get("price_type") or "").strip().lower()
    volume_unit = str(data.get("volume_unit") or "").strip().lower()

    fail_reasons: List[str] = []
    warn_reasons: List[str] = []

    if primary_objective != EXPECTED_MODEL_OBJECTIVE:
        fail_reasons.append(
            f"metrics_oos.primary_objective != {EXPECTED_MODEL_OBJECTIVE}"
        )
    if governance_primary_objective != EXPECTED_MODEL_OBJECTIVE:
        fail_reasons.append(
            f"governance.primary_objective != {EXPECTED_MODEL_OBJECTIVE}"
        )
    if metrics.get("evidence_tier") != "offline_model_economic_prescreen":
        fail_reasons.append(
            "metrics_oos.evidence_tier != offline_model_economic_prescreen"
        )
    if metrics.get("authoritative_promotion_evidence") != "live_candidate_episode_canary":
        fail_reasons.append(
            "metrics_oos.authoritative_promotion_evidence != live_candidate_episode_canary"
        )
    if (
        metrics.get("required_offline_prescreen")
        != "independent_cpp_replay_next_bar_ohlc_touch"
    ):
        fail_reasons.append(
            "metrics_oos.required_offline_prescreen != "
            "independent_cpp_replay_next_bar_ohlc_touch"
        )

    if not training_symbol:
        fail_reasons.append("data.training_symbol missing")
    if not isinstance(bar_interval_ms, int) or bar_interval_ms <= 0:
        fail_reasons.append("data.bar_interval_ms must be a positive integer")
    if online_bar_source != "closed_ohlcv":
        fail_reasons.append("data.online_bar_source != closed_ohlcv")
    if source_venue != "bybit":
        fail_reasons.append("data.source_venue != bybit")
    if source_category != "linear":
        fail_reasons.append("data.source_category != linear")
    if price_type != "trade_price":
        fail_reasons.append("data.price_type != trade_price")
    if volume_unit != "base_asset":
        fail_reasons.append("data.volume_unit != base_asset")
    time_axis_quality = data.get("time_axis_quality", {}) if isinstance(data, dict) else {}
    if (
        not isinstance(time_axis_quality, dict)
        or time_axis_quality.get("pass") is not True
    ):
        fail_reasons.append("data.time_axis_quality.pass != true")

    anti_leakage = report.get("anti_leakage", {})
    if not isinstance(anti_leakage, dict):
        anti_leakage = {}
    if anti_leakage.get("split_axis") != "raw_bar_index_before_label_filter":
        fail_reasons.append(
            "anti_leakage.split_axis != raw_bar_index_before_label_filter"
        )
    if anti_leakage.get("oos_windows_non_overlapping") is not True:
        fail_reasons.append(
            "anti_leakage.oos_windows_non_overlapping != true"
        )

    if not isinstance(mean_model_net_edge_bps, (float, int)):
        fail_reasons.append("缺少 metrics_oos.mean_model_net_edge_bps")
    elif float(mean_model_net_edge_bps) < float(min_mean_model_net_edge_bps):
        fail_reasons.append(
            "mean_model_net_edge_bps="
            f"{float(mean_model_net_edge_bps):.6f} < "
            f"min_mean_model_net_edge_bps={float(min_mean_model_net_edge_bps):.6f}"
        )

    if not isinstance(positive_model_net_edge_ratio, (float, int)):
        fail_reasons.append("缺少 metrics_oos.positive_model_net_edge_ratio")
    elif float(positive_model_net_edge_ratio) < float(min_positive_model_net_edge_ratio):
        fail_reasons.append(
            "positive_model_net_edge_ratio="
            f"{float(positive_model_net_edge_ratio):.6f} < "
            "min_positive_model_net_edge_ratio="
            f"{float(min_positive_model_net_edge_ratio):.6f}"
        )

    if not isinstance(model_net_objective_sample_count, int) or model_net_objective_sample_count <= 0:
        fail_reasons.append("metrics_oos.model_net_objective_sample_count <= 0")
    if (
        not isinstance(model_net_total_trades, int)
        or model_net_total_trades < min_model_net_total_trades
    ):
        fail_reasons.append(
            "model_net_total_trades="
            f"{model_net_total_trades} < "
            f"min_model_net_total_trades={min_model_net_total_trades}"
        )
    if (
        not isinstance(model_net_active_bar_count, int)
        or model_net_active_bar_count < min_model_net_active_bars
    ):
        fail_reasons.append(
            "model_net_active_bar_count="
            f"{model_net_active_bar_count} < "
            f"min_model_net_active_bars={min_model_net_active_bars}"
        )
    if (
        not isinstance(positive_model_net_splits_ratio, (float, int))
        or float(positive_model_net_splits_ratio)
        < min_positive_model_net_splits_ratio
    ):
        fail_reasons.append(
            "positive_model_net_edge_ratio_by_split="
            f"{positive_model_net_splits_ratio} < "
            "min_positive_model_net_splits_ratio="
            f"{min_positive_model_net_splits_ratio:.6f}"
        )
    if (
        not isinstance(model_net_edge_lcb_bps, (float, int))
        or not math.isfinite(float(model_net_edge_lcb_bps))
        or float(model_net_edge_lcb_bps) < min_model_net_edge_lcb_bps
    ):
        fail_reasons.append(
            "model_net_edge_lcb_bps="
            f"{model_net_edge_lcb_bps} < "
            f"min_model_net_edge_lcb_bps={min_model_net_edge_lcb_bps:.6f}"
        )
    if model_net_edge_lcb_method != "non_overlapping_oos_split_student_t_95":
        fail_reasons.append(
            "metrics_oos.model_net_edge_lcb_method != "
            "non_overlapping_oos_split_student_t_95"
        )
    if (
        not isinstance(oos_duplicate_bar_ratio, (float, int))
        or float(oos_duplicate_bar_ratio) != 0.0
    ):
        fail_reasons.append(
            f"oos_duplicate_bar_ratio={oos_duplicate_bar_ratio} != 0"
        )

    train_config = report.get("train_config", {})
    if not isinstance(train_config, dict):
        train_config = {}
    label_cost_bps = train_config.get("label_round_trip_cost_bps")
    objective_cost_bps = metrics.get("net_objective_round_trip_cost_bps")
    if (
        not isinstance(label_cost_bps, (float, int))
        or float(label_cost_bps) <= 0.0
    ):
        fail_reasons.append("train_config.label_round_trip_cost_bps <= 0")
    if (
        not isinstance(objective_cost_bps, (float, int))
        or not isinstance(label_cost_bps, (float, int))
        or abs(float(objective_cost_bps) - float(label_cost_bps)) > 1e-9
    ):
        fail_reasons.append(
            "training label cost differs from OOS economic objective cost"
        )
    execution_latency_bars = train_config.get("execution_latency_bars")
    if not isinstance(execution_latency_bars, int) or execution_latency_bars < 1:
        fail_reasons.append("train_config.execution_latency_bars < 1")

    if not isinstance(auc_mean, (float, int)):
        warn_reasons.append("缺少 metrics_oos.auc_mean")
    elif float(auc_mean) < min_auc_mean:
        warn_reasons.append(
            f"auc_mean={float(auc_mean):.6f} < min_auc_mean={min_auc_mean:.6f}"
        )

    if not isinstance(delta_auc, (float, int)):
        warn_reasons.append("缺少 metrics_oos.delta_auc_vs_baseline")
    elif float(delta_auc) < min_delta_auc_vs_baseline:
        warn_reasons.append(
            "delta_auc_vs_baseline="
            f"{float(delta_auc):.6f} < min_delta_auc_vs_baseline={min_delta_auc_vs_baseline:.6f}"
        )

    if not isinstance(trained_count, int) or not isinstance(split_count, int):
        fail_reasons.append("缺少 split_trained_count/split_count")
    elif trained_count <= 0 or split_count <= 0 or trained_count > split_count:
        fail_reasons.append(
            f"split 计数异常: split_trained_count={trained_count}, split_count={split_count}"
        )
    elif trained_count < min_split_trained_count:
        fail_reasons.append(
            "split_trained_count="
            f"{trained_count} < min_split_trained_count={min_split_trained_count}"
        )

    if not isinstance(split_trained_ratio, (float, int)):
        if isinstance(trained_count, int) and isinstance(split_count, int) and split_count > 0:
            split_trained_ratio = float(trained_count) / float(split_count)
        else:
            fail_reasons.append("缺少 metrics_oos.split_trained_ratio")
    if isinstance(split_trained_ratio, (float, int)):
        ratio_value = float(split_trained_ratio)
        if ratio_value < min_split_trained_ratio:
            fail_reasons.append(
                "split_trained_ratio="
                f"{ratio_value:.6f} < min_split_trained_ratio={min_split_trained_ratio:.6f}"
            )

    if isinstance(governance, dict):
        governance_pass = governance.get("pass")
        if governance_pass is not True:
            fail_reasons.append("integrator_report.governance.pass != true")
            governance_fail_reasons = governance.get("fail_reasons", [])
            if isinstance(governance_fail_reasons, list):
                for item in governance_fail_reasons:
                    item_text = str(item).strip()
                    if item_text:
                        fail_reasons.append(f"governance: {item_text}")
        governance_warn_reasons = governance.get("warn_reasons", [])
        if isinstance(governance_warn_reasons, list):
            for item in governance_warn_reasons:
                item_text = str(item).strip()
                if item_text:
                    warn_reasons.append(f"governance: {item_text}")
    else:
        fail_reasons.append("integrator_report.governance missing")
    if report.get("model_artifact_status") != "published":
        fail_reasons.append("integrator_report.model_artifact_status != published")

    gate_pass = len(fail_reasons) == 0
    summary = {
        "primary_objective": primary_objective,
        "governance_primary_objective": governance_primary_objective,
        "mean_model_net_edge_bps": mean_model_net_edge_bps,
        "median_model_net_edge_bps": median_model_net_edge_bps,
        "positive_model_net_edge_ratio": positive_model_net_edge_ratio,
        "model_net_objective_sample_count": model_net_objective_sample_count,
        "model_net_total_trades": model_net_total_trades,
        "model_net_active_bar_count": model_net_active_bar_count,
        "positive_model_net_edge_ratio_by_split": positive_model_net_splits_ratio,
        "model_net_edge_lcb_bps": model_net_edge_lcb_bps,
        "model_net_edge_lcb_method": model_net_edge_lcb_method,
        "oos_duplicate_bar_ratio": oos_duplicate_bar_ratio,
        "label_round_trip_cost_bps": label_cost_bps,
        "net_objective_round_trip_cost_bps": objective_cost_bps,
        "execution_latency_bars": execution_latency_bars,
        "auc_mean": auc_mean,
        "delta_auc_vs_baseline": delta_auc,
        "split_trained_count": trained_count,
        "split_count": split_count,
        "split_trained_ratio": split_trained_ratio,
        "training_symbol": training_symbol,
        "bar_interval_ms": bar_interval_ms,
        "online_bar_source": online_bar_source,
        "source_venue": source_venue,
        "source_category": source_category,
        "price_type": price_type,
        "volume_unit": volume_unit,
        "auc_stdev": metrics.get("auc_stdev"),
        "train_test_auc_gap_mean": metrics.get("train_test_auc_gap_mean"),
        "random_label_auc": metrics.get("random_label_auc"),
        "random_label_auc_mean": metrics.get("random_label_auc_mean"),
        "random_label_auc_stdev": metrics.get("random_label_auc_stdev"),
        "random_label_auc_max": metrics.get("random_label_auc_max"),
        "predict_horizon_bars": data.get("predict_horizon_bars") if isinstance(data, dict) else None,
        "label_policy": data.get("label_policy") if isinstance(data, dict) else None,
        "feature_transform": {
            "feature_clipping_enabled": feature_transform.get("feature_clipping_enabled"),
            "feature_normalization_enabled": feature_transform.get(
                "feature_normalization_enabled"
            ),
            "clip_quantile": feature_transform.get("clip_quantile"),
            "normalization_method": feature_transform.get("normalization_method"),
            "normalization_max_abs": feature_transform.get("normalization_max_abs"),
            "enabled_clip_bound_count": feature_transform.get("enabled_clip_bound_count"),
            "enabled_normalization_count": feature_transform.get(
                "enabled_normalization_count"
            ),
            "clip_bound_count": len(feature_transform.get("clip_bounds", []))
            if isinstance(feature_transform.get("clip_bounds"), list)
            else 0,
        }
        if isinstance(feature_transform, dict)
        else None,
    }
    return gate_pass, fail_reasons, warn_reasons, summary


def coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def gate_walkforward_report(
    report_path: Path | None,
    require_report: bool,
    min_avg_split_return: float,
    min_enabled_avg_split_return: float,
    min_traded_avg_split_return: float,
    focus_bucket: str = "",
    min_focus_bucket_bars: int = 0,
    min_focus_bucket_trades: int = 0,
    min_focus_bucket_sharpe: float = 0.0,
    focus_bucket_primary: bool = False,
) -> Tuple[bool, List[str], List[str], Dict[str, Any]]:
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    summary: Dict[str, Any] = {}

    if report_path is None:
        if require_report:
            fail_reasons.append("walkforward_report 缺失")
        return len(fail_reasons) == 0, fail_reasons, warn_reasons, summary
    if not report_path.is_file():
        fail_reasons.append(f"walkforward_report 不存在: {report_path}")
        return False, fail_reasons, warn_reasons, summary

    payload = read_json(report_path)
    raw_summary = payload.get("summary", {})
    if not isinstance(raw_summary, dict):
        fail_reasons.append("walkforward_report.summary 缺失或格式错误")
        return False, fail_reasons, warn_reasons, summary
    summary = raw_summary
    focus_validation: Dict[str, Any] = {}
    focus_bucket_name = str(focus_bucket or "").strip().lower()
    focus_bucket_pass = False
    if focus_bucket_name:
        regime_bucket_summary = summary.get("regime_bucket_summary", {})
        bucket_payload = (
            regime_bucket_summary.get(focus_bucket_name, {})
            if isinstance(regime_bucket_summary, dict)
            else {}
        )
        if not isinstance(bucket_payload, dict):
            bucket_payload = {}
        focus_bars = int(bucket_payload.get("bars", 0) or 0)
        focus_trades = int(bucket_payload.get("trades", 0) or 0)
        focus_sharpe = coerce_float(bucket_payload.get("sharpe"))
        focus_fail_reasons: List[str] = []
        if focus_bars < int(min_focus_bucket_bars):
            focus_fail_reasons.append(
                f"{focus_bucket_name} bucket bars={focus_bars} < {int(min_focus_bucket_bars)}"
            )
        if focus_trades < int(min_focus_bucket_trades):
            focus_fail_reasons.append(
                f"{focus_bucket_name} bucket trades={focus_trades} < {int(min_focus_bucket_trades)}"
            )
        if focus_sharpe is None:
            focus_fail_reasons.append(f"{focus_bucket_name} bucket sharpe missing")
        elif focus_sharpe < float(min_focus_bucket_sharpe):
            focus_fail_reasons.append(
                f"{focus_bucket_name} bucket sharpe={focus_sharpe:.6f} < {float(min_focus_bucket_sharpe):.6f}"
            )
        focus_bucket_pass = not focus_fail_reasons
        focus_validation = {
            "bucket": focus_bucket_name,
            "status": "pass" if focus_bucket_pass else "fail",
            "fail_reasons": focus_fail_reasons,
            "bars": focus_bars,
            "trades": focus_trades,
            "sharpe": focus_sharpe,
            "thresholds": {
                "min_bars": int(min_focus_bucket_bars),
                "min_trades": int(min_focus_bucket_trades),
                "min_sharpe": float(min_focus_bucket_sharpe),
            },
            "primary": bool(focus_bucket_primary),
        }
        if not focus_bucket_pass:
            fail_reasons.extend(focus_fail_reasons)

    return_checks = [
        ("avg_split_return", min_avg_split_return),
        ("enabled_avg_split_return", min_enabled_avg_split_return),
        ("traded_avg_split_return", min_traded_avg_split_return),
    ]
    for metric_name, threshold in return_checks:
        metric_value = coerce_float(summary.get(metric_name))
        if metric_value is None:
            fail_reasons.append(f"walkforward_report.summary.{metric_name} 缺失")
            continue
        if metric_value < float(threshold):
            reason = (
                f"walkforward {metric_name}={metric_value:.6f} < {float(threshold):.6f}"
            )
            if focus_bucket_primary and focus_bucket_pass:
                warn_reasons.append(
                    "walkforward global metric below threshold but focus bucket passed: "
                    + reason
                )
            else:
                fail_reasons.append(reason)

    total_trades = summary.get("total_trades")
    traded_split_count = summary.get("traded_split_count")
    if isinstance(total_trades, int) and total_trades <= 0:
        reason = f"walkforward total_trades={total_trades} <= 0"
        if focus_bucket_primary and focus_bucket_pass:
            warn_reasons.append(
                "walkforward global trade count below threshold but focus bucket passed: "
                + reason
            )
        else:
            fail_reasons.append(reason)
    if isinstance(traded_split_count, int) and traded_split_count <= 0:
        reason = f"walkforward traded_split_count={traded_split_count} <= 0"
        if focus_bucket_primary and focus_bucket_pass:
            warn_reasons.append(
                "walkforward global trade count below threshold but focus bucket passed: "
                + reason
            )
        else:
            fail_reasons.append(reason)

    if focus_validation:
        summary = dict(summary)
        summary["focus_bucket_validation"] = focus_validation
    return len(fail_reasons) == 0, fail_reasons, warn_reasons, summary


def gate_replay_validation_report(
    report_path: Path | None,
    require_report: bool,
    expected_model_version: str = "",
    expected_model_sha256: str = "",
    expected_integrator_report_sha256: str = "",
) -> Tuple[bool, List[str], List[str], Dict[str, Any]]:
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    summary: Dict[str, Any] = {}

    if report_path is None:
        if require_report:
            fail_reasons.append("replay_validation_report 缺失")
        return len(fail_reasons) == 0, fail_reasons, warn_reasons, summary
    if not report_path.is_file():
        fail_reasons.append(f"replay_validation_report 不存在: {report_path}")
        return False, fail_reasons, warn_reasons, summary

    payload = read_json(report_path)
    candidate_identity = payload.get("candidate_identity", {})
    if not isinstance(candidate_identity, dict):
        candidate_identity = {}
    if expected_model_version:
        if candidate_identity.get("config_binds_candidate") is not True:
            fail_reasons.append(
                "replay candidate config does not bind the current model"
            )
        if (
            str(candidate_identity.get("model_version", "")).strip()
            != expected_model_version
        ):
            fail_reasons.append(
                "replay candidate model_version differs from current model"
            )
        if (
            str(candidate_identity.get("model_sha256", "")).strip()
            != expected_model_sha256
        ):
            fail_reasons.append(
                "replay candidate model checksum differs from current model"
            )
        if (
            str(
                candidate_identity.get("integrator_report_sha256", "")
            ).strip()
            != expected_integrator_report_sha256
        ):
            fail_reasons.append(
                "replay candidate report checksum differs from current report"
            )
        execution_policy = candidate_identity.get("execution_policy", {})
        execution_policy_sha256 = (
            str(execution_policy.get("sha256", "")).strip()
            if isinstance(execution_policy, dict)
            else ""
        )
        if re.fullmatch(r"[0-9a-f]{64}", execution_policy_sha256) is None:
            fail_reasons.append(
                "replay candidate execution policy checksum is missing or invalid"
            )
        execution_policy_values = (
            execution_policy.get("policy", {})
            if isinstance(execution_policy, dict)
            else {}
        )
        execution_policy_schema = (
            str(execution_policy.get("schema_version", "")).strip()
            if isinstance(execution_policy, dict)
            else ""
        )
        if (
            execution_policy_schema != "execution_policy_v2"
            or not isinstance(execution_policy_values, dict)
            or not execution_policy_values
        ):
            fail_reasons.append(
                "replay candidate execution policy payload is incomplete"
            )
        elif hashlib.sha256(
            json.dumps(
                execution_policy_values,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest() != execution_policy_sha256:
            fail_reasons.append(
                "replay candidate execution policy checksum differs from payload"
            )
        if re.fullmatch(
            r"[0-9a-f]{64}",
            str(candidate_identity.get("trade_bot_sha256", "")).strip(),
        ) is None:
            fail_reasons.append(
                "replay candidate trade_bot checksum is missing or invalid"
            )
        if re.fullmatch(
            r"[0-9a-f]{64}",
            str(candidate_identity.get("runtime_config_sha256", "")).strip(),
        ) is None:
            fail_reasons.append(
                "replay candidate runtime config checksum is missing or invalid"
            )
        fail_reasons.extend(
            validate_economic_objective_contract(
                candidate_identity.get("economic_objective_contract"),
                execution_policy_sha256=execution_policy_sha256,
                trade_bot_sha256=str(
                    candidate_identity.get("trade_bot_sha256", "")
                ).strip(),
            )
        )
        identity_payload = dict(candidate_identity)
        reported_identity_sha256 = str(
            identity_payload.pop("identity_sha256", "")
        ).strip()
        computed_identity_sha256 = hashlib.sha256(
            json.dumps(
                identity_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if reported_identity_sha256 != computed_identity_sha256:
            fail_reasons.append(
                "replay candidate identity checksum differs from payload"
            )
        consumption_failures, consumed_symbols = (
            validate_final_holdout_consumption(
                payload.get("holdout_consumption"),
                candidate_identity_sha256=reported_identity_sha256,
            )
        )
        fail_reasons.extend(consumption_failures)
        selection_candidate = payload.get(
            "selection_candidate_manifest", {}
        )
        if not isinstance(selection_candidate, dict):
            selection_candidate = {}
        if (
            selection_candidate.get("evidence_domain")
            != "selection_validation"
        ):
            fail_reasons.append(
                "exact candidate selection evidence domain is missing"
            )
        if (
            str(selection_candidate.get("candidate_identity_sha256", "")).strip()
            != reported_identity_sha256
        ):
            fail_reasons.append(
                "selection candidate identity differs from final replay candidate"
            )
        selection_candidate_status = str(
            selection_candidate.get("status", "")
        ).strip().lower()
        if selection_candidate_status not in {"pass", "pass_with_actions"}:
            fail_reasons.append(
                "exact candidate did not pass selection-validation"
            )
        selection_candidate_path = Path(
            str(selection_candidate.get("path", "")).strip()
        )
        selection_candidate_sha256 = str(
            selection_candidate.get("sha256", "")
        ).strip()
        if (
            not selection_candidate_path.is_file()
            or not re.fullmatch(r"[0-9a-f]{64}", selection_candidate_sha256)
            or sha256_file(selection_candidate_path)
            != selection_candidate_sha256
        ):
            fail_reasons.append(
                "selection candidate manifest artifact checksum mismatch"
            )
    aggregate = payload.get("aggregate_validation", {})
    aggregate_status = ""
    if isinstance(aggregate, dict):
        aggregate_status = str(aggregate.get("status", "")).strip().lower()
    status = str(payload.get("status", aggregate_status)).strip().lower()
    activation_gate = payload.get("activation_gate", {})
    if not isinstance(activation_gate, dict):
        activation_gate = {}
    activation_basis = str(activation_gate.get("basis", "")).strip()
    if activation_basis == "execution_optimizer.best_deployable_candidate":
        fail_reasons.append(
            "replay final holdout optimizer candidate is diagnostic only and "
            "cannot replace aggregate promotion evidence"
        )
    activation_gate_status = str(activation_gate.get("status", "")).strip().lower()
    activation_gate_blocking_warnings: List[str] = []
    if not activation_gate:
        fail_reasons.append("replay activation_gate missing")
    elif activation_gate_status == "fail":
        gate_fail_items = activation_gate.get("fail_reasons", [])
        if isinstance(gate_fail_items, list):
            for item in gate_fail_items:
                item_text = str(item).strip()
                if item_text:
                    fail_reasons.append(f"replay activation_gate: {item_text}")
        if not gate_fail_items:
            fail_reasons.append("replay activation_gate status=fail")
    elif activation_gate_status == "pass_with_actions":
        gate_warn_items = activation_gate.get("warn_reasons", [])
        if isinstance(gate_warn_items, list):
            for item in gate_warn_items:
                item_text = str(item).strip()
                if not item_text:
                    continue
                warn_reasons.append(f"replay activation_gate: {item_text}")
                if not is_allowed_activation_gate_warning(item_text):
                    activation_gate_blocking_warnings.append(item_text)
        else:
            activation_gate_blocking_warnings.append(
                "activation_gate.pass_with_actions without warn_reasons"
            )
    elif activation_gate_status != "pass":
        fail_reasons.append(
            "replay activation_gate status="
            f"{activation_gate_status or 'UNKNOWN'} != pass"
        )
    raw_replay_fail_reasons: List[str] = []
    selection = payload.get("selection", {})
    if not isinstance(selection, dict):
        selection = {}
    skip_reason = str(
        payload.get("skip_reason") or selection.get("stop_reason") or ""
    ).strip()
    selection_mode = str(selection.get("selection_mode", "")).strip()
    validation_skipped = bool(payload.get("validation_skipped")) or (
        selection_mode == "not_run"
        and skip_reason in {"feature_store_missing", "command_failed", "not_run"}
    )
    if validation_skipped:
        raw_replay_fail_reasons.append(
            "replay_validation skipped/not_run: "
            f"reason={skip_reason or 'unknown'}"
        )
    if not validation_skipped:
        per_symbol_selection = selection.get("per_symbol_selection", {})
        if isinstance(per_symbol_selection, dict) and per_symbol_selection:
            invalid_selection_symbols = sorted(
                str(symbol).strip().upper()
                for symbol, item in per_symbol_selection.items()
                if not isinstance(item, dict)
                or item.get("selection_mode") != "selection_manifest_holdout"
                or item.get("candidate_set_frozen") is not True
                or bool(item.get("corpus_written"))
                or bool(item.get("corpus_refreshed"))
                or item.get("dynamic_appended_segment_count") not in (None, 0, "0")
            )
            if invalid_selection_symbols:
                raw_replay_fail_reasons.append(
                    "replay final holdout candidate set was not frozen by "
                    "selection manifest for symbols="
                    + ",".join(invalid_selection_symbols)
                )
        elif (
            selection_mode != "selection_manifest_holdout"
            or selection.get("candidate_set_frozen") is not True
            or bool(selection.get("corpus_written"))
            or bool(selection.get("corpus_refreshed"))
            or selection.get("dynamic_appended_segment_count") not in (None, 0, "0")
        ):
            raw_replay_fail_reasons.append(
                "replay final holdout did not consume a frozen selection manifest"
            )
    if status not in {"pass", "pass_with_actions"}:
        raw_replay_fail_reasons.append(
            f"replay_validation status={status or 'UNKNOWN'} != pass"
        )
    if aggregate_status not in {"pass", "pass_with_actions"}:
        raw_replay_fail_reasons.append(
            "replay aggregate_validation status="
            f"{aggregate_status or 'UNKNOWN'} != pass"
        )

    fail_items = payload.get("fail_reasons", [])
    if not fail_items and isinstance(aggregate, dict):
        fail_items = aggregate.get("fail_reasons", [])
    if isinstance(fail_items, list):
        for item in fail_items:
            item_text = str(item).strip()
            if item_text:
                raw_replay_fail_reasons.append(item_text)
    if isinstance(aggregate, dict):
        suppressed_items = aggregate.get("suppressed_aggregate_fail_reasons", [])
        if isinstance(suppressed_items, list) and any(
            str(item).strip() for item in suppressed_items
        ):
            raw_replay_fail_reasons.append(
                "replay aggregate failures were suppressed by holdout-derived "
                "symbol selection"
            )

    warn_items = payload.get("warn_reasons", [])
    if not warn_items and isinstance(aggregate, dict):
        warn_items = aggregate.get("warn_reasons", [])
    if isinstance(warn_items, list):
        for item in warn_items:
            item_text = str(item).strip()
            if item_text:
                warn_reasons.append(item_text)

    coverage_strength_status = payload.get("coverage_strength_status")
    if coverage_strength_status is None and isinstance(aggregate, dict):
        coverage_strength_status = aggregate.get("coverage_strength_status")
    execution_optimizer = payload.get("execution_optimizer", {})
    if not isinstance(execution_optimizer, dict):
        execution_optimizer = {}
    optimizer_status = str(execution_optimizer.get("status", "")).strip().lower()
    if optimizer_status == "fail":
        fail_reasons.append("replay execution_optimizer status=fail")
    execution_cost_plan = payload.get("execution_cost_plan", {})
    if not isinstance(execution_cost_plan, dict):
        execution_cost_plan = {}
    feature_build = payload.get("feature_build", {})
    if not isinstance(feature_build, dict):
        feature_build = {}
    exit_capture = payload.get("exit_capture", {})
    if not isinstance(exit_capture, dict):
        exit_capture = {}
    exit_capture_by_symbol = payload.get("exit_capture_by_symbol", {})
    if not isinstance(exit_capture_by_symbol, dict):
        exit_capture_by_symbol = {}
    cost_plan_status = str(execution_cost_plan.get("status", "")).strip().lower()
    if cost_plan_status == "fail":
        fail_reasons.append("replay execution_cost_plan status=fail")
    elif cost_plan_status == "candidate_requires_rerun":
        warn_reasons.append(
            "replay execution_cost_plan found lower-cost candidate requiring rerun"
        )
    exit_sample_count = exit_capture.get("sample_count")
    exit_primary_diagnosis = str(exit_capture.get("primary_diagnosis", "")).strip()
    exit_mean_capture = exit_capture.get("mean_gross_capture_of_path_mfe")
    if (
        not exit_capture_by_symbol
        and isinstance(exit_sample_count, (int, float))
        and float(exit_sample_count) > 0
    ):
        if exit_primary_diagnosis == "exit_capture_low":
            fail_reasons.append(
                "replay exit_capture_low: path MFE covers cost but gross capture is too low"
            )
        if (
            isinstance(exit_mean_capture, (int, float))
            and float(exit_mean_capture) < EXIT_CAPTURE_MIN_MEAN_GROSS_CAPTURE_OF_PATH_MFE
        ):
            reason = (
                "replay mean_gross_capture_of_path_mfe="
                f"{float(exit_mean_capture):.6f} < "
                f"{EXIT_CAPTURE_MIN_MEAN_GROSS_CAPTURE_OF_PATH_MFE:.6f}"
            )
            fail_reasons.append(reason)
    summary = {
        "status": payload.get("status", aggregate_status),
        "source_symbol": payload.get("source_symbol"),
        "source_symbols": payload.get("source_symbols", {}),
        "coverage_strength_status": coverage_strength_status,
        "aggregate_validation": aggregate if isinstance(aggregate, dict) else {},
        "execution_economics": payload.get("execution_economics", {}),
        "cost_sensitivity": payload.get("cost_sensitivity", {}),
        "exit_capture": exit_capture,
        "exit_capture_by_symbol": exit_capture_by_symbol,
        "execution_cost_plan": execution_cost_plan,
        "execution_optimizer": execution_optimizer,
        "feature_build": feature_build,
        "activation_gate": activation_gate,
        "candidate_identity": candidate_identity,
        "holdout_consumption": payload.get("holdout_consumption", {}),
    }
    if isinstance(aggregate, dict):
        for key in (
            "execution_active_runs",
            "execution_pass_runs",
            "total_fills",
            "positive_realized_net_with_fills_runs",
            "negative_realized_net_with_fills_runs",
            "mean_realized_net_per_fill",
            "mean_realized_net_per_fill_with_fills",
            "median_realized_net_per_fill_with_fills",
            "positive_filled_segment_ratio",
        ):
            if key in aggregate:
                summary[key] = aggregate.get(key)
        tradeability = aggregate.get("symbol_tradeability", {})
        if not isinstance(tradeability, dict):
            tradeability = {}
        summary["symbol_tradeability"] = tradeability
        tradable_symbols = {
            str(item).strip().upper()
            for item in tradeability.get("tradable_symbols", [])
            if str(item).strip()
        }
        quarantined_symbols = {
            str(item).strip().upper()
            for item in tradeability.get("quarantined_symbols", [])
            if str(item).strip()
        }
        decisions = tradeability.get("decisions", {})
        if isinstance(decisions, dict):
            for symbol, decision in decisions.items():
                if not isinstance(decision, dict):
                    continue
                if str(decision.get("status", "")).strip().lower() == "quarantined":
                    symbol_text = str(symbol).strip().upper()
                    if symbol_text:
                        quarantined_symbols.add(symbol_text)
        source_symbol = str(payload.get("source_symbol", "")).strip().upper()
        summary["symbol_quarantine_observation"] = {
            "tradable_symbols": sorted(tradable_symbols),
            "quarantined_symbols": sorted(quarantined_symbols),
            "promotion_authority": False,
        }

        report_symbols = {
            str(item).strip().upper()
            for item in payload.get("symbols", [])
            if str(item).strip()
        }
        feature_build_symbols = {
            str(item).strip().upper()
            for item in feature_build.get("symbols", [])
            if str(item).strip()
        }
        source_symbols = payload.get("source_symbols", {})
        source_symbol_keys = (
            {
                str(item).strip().upper()
                for item in source_symbols
                if str(item).strip()
            }
            if isinstance(source_symbols, dict)
            else set()
        )
        actual_feature_csv_by_symbol = payload.get("feature_csv_by_symbol", {})
        if not isinstance(actual_feature_csv_by_symbol, dict):
            actual_feature_csv_by_symbol = {}
            fail_reasons.append("replay feature_csv_by_symbol missing or invalid")
        actual_feature_symbols = {
            str(item).strip().upper()
            for item in actual_feature_csv_by_symbol
            if str(item).strip()
        }
        domain_contract_by_symbol = feature_build.get(
            "domain_contract_by_symbol", {}
        )
        if not isinstance(domain_contract_by_symbol, dict):
            domain_contract_by_symbol = {}
        domain_symbols = {
            str(item).strip().upper()
            for item in domain_contract_by_symbol
            if str(item).strip()
        }

        candidate_symbols = (
            report_symbols
            | feature_build_symbols
            | source_symbol_keys
            | actual_feature_symbols
            | domain_symbols
        )
        if source_symbol:
            candidate_symbols.add(source_symbol)
        summary["final_holdout_candidate_symbols"] = sorted(candidate_symbols)
        if expected_model_version and set(consumed_symbols) != candidate_symbols:
            fail_reasons.append(
                "final holdout consumption claims do not cover candidate symbols: "
                f"claims={','.join(sorted(consumed_symbols)) or 'none'}, "
                f"candidates={','.join(sorted(candidate_symbols)) or 'none'}"
            )
        if not candidate_symbols:
            fail_reasons.append("replay final holdout candidate symbols missing")
        per_symbol_selection = selection.get("per_symbol_selection", {})
        selection_symbols = (
            {
                str(item).strip().upper()
                for item in per_symbol_selection
                if str(item).strip()
            }
            if isinstance(per_symbol_selection, dict)
            else set()
        )
        if selection_symbols and selection_symbols != candidate_symbols:
            fail_reasons.append(
                "replay frozen selection manifests do not cover the complete "
                "final holdout candidate set: "
                f"selection={','.join(sorted(selection_symbols))}, "
                f"candidates={','.join(sorted(candidate_symbols))}"
            )
        if (
            report_symbols
            and feature_build_symbols
            and report_symbols != feature_build_symbols
        ):
            fail_reasons.append(
                "replay symbols differ from feature_build.symbols: "
                f"replay={','.join(sorted(report_symbols))}, "
                f"feature_build={','.join(sorted(feature_build_symbols))}"
            )
        if actual_feature_symbols != candidate_symbols:
            missing_actual = sorted(candidate_symbols - actual_feature_symbols)
            extra_actual = sorted(actual_feature_symbols - candidate_symbols)
            fail_reasons.append(
                "replay feature_csv_by_symbol does not cover the complete final "
                "holdout candidate set: "
                f"missing={','.join(missing_actual) or 'none'}, "
                f"extra={','.join(extra_actual) or 'none'}"
            )

        failed_feature_symbols = {
            str(item).strip().upper()
            for item in feature_build.get("failed_symbols", [])
            if str(item).strip()
        }
        missing_feature_symbols = {
            str(item).strip().upper()
            for item in feature_build.get("missing_symbols", [])
            if str(item).strip()
        }
        candidate_failed_features = sorted(candidate_symbols & failed_feature_symbols)
        candidate_missing_features = sorted(candidate_symbols & missing_feature_symbols)
        if candidate_failed_features:
            fail_reasons.append(
                "replay real-market feature build failed for final holdout "
                "candidate symbols=" + ",".join(candidate_failed_features)
            )
        if candidate_missing_features:
            fail_reasons.append(
                "replay real-market feature missing for final holdout "
                "candidate symbols=" + ",".join(candidate_missing_features)
            )

        feature_binding_by_symbol: Dict[str, Any] = {}
        for symbol in sorted(candidate_symbols):
            domain_result = domain_contract_by_symbol.get(symbol, {})
            if (
                not isinstance(domain_result, dict)
                or str(domain_result.get("status", "")).strip().lower()
                != "pass"
            ):
                fail_reasons.append(
                    "replay research-domain contract failed for final holdout "
                    f"candidate symbol={symbol}"
                )
                domain_result = (
                    domain_result if isinstance(domain_result, dict) else {}
                )

            actual_path_text = str(
                actual_feature_csv_by_symbol.get(symbol) or ""
            ).strip()
            expected_path_text, expected_sha256 = holdout_artifact_contract(
                domain_result
            )
            actual_path = canonical_path(actual_path_text)
            expected_path = canonical_path(expected_path_text)
            binding_summary = {
                "actual_feature_csv": actual_path_text,
                "contract_holdout_feature_csv": expected_path_text,
                "contract_holdout_feature_sha256": expected_sha256,
                "canonical_path_match": bool(
                    actual_path is not None
                    and expected_path is not None
                    and actual_path == expected_path
                ),
            }
            feature_binding_by_symbol[symbol] = binding_summary

            if actual_path is None:
                fail_reasons.append(
                    f"replay feature_csv_by_symbol missing for {symbol}"
                )
            if expected_path is None:
                fail_reasons.append(
                    f"replay holdout feature path missing for {symbol}"
                )
            if (
                actual_path is not None
                and expected_path is not None
                and actual_path != expected_path
            ):
                fail_reasons.append(
                    f"replay holdout feature path mismatch for {symbol}: "
                    f"actual={actual_path}, contract={expected_path}"
                )
            if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
                fail_reasons.append(
                    f"replay holdout feature sha256 missing or invalid for {symbol}"
                )
            if actual_path is not None and not actual_path.is_file():
                fail_reasons.append(
                    f"replay holdout feature csv not found for {symbol}: {actual_path}"
                )
            elif (
                actual_path is not None
                and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is not None
            ):
                actual_sha256 = sha256_file(actual_path)
                binding_summary["actual_sha256"] = actual_sha256
                if actual_sha256 != expected_sha256:
                    fail_reasons.append(
                        f"replay holdout feature checksum mismatch for {symbol}"
                    )
        summary["feature_binding_by_symbol"] = feature_binding_by_symbol

        for symbol in sorted(candidate_symbols):
            symbol_exit = exit_capture_by_symbol.get(symbol)
            if not isinstance(symbol_exit, dict):
                continue
            symbol_samples = symbol_exit.get("sample_count")
            if not isinstance(symbol_samples, (int, float)) or float(symbol_samples) <= 0:
                continue
            symbol_primary = str(symbol_exit.get("primary_diagnosis", "")).strip()
            symbol_mean_capture = symbol_exit.get("mean_gross_capture_of_path_mfe")
            if symbol_primary == "exit_capture_low":
                fail_reasons.append(
                    f"replay {symbol} exit_capture_low: path MFE covers cost but gross capture is too low"
                )
            if (
                isinstance(symbol_mean_capture, (int, float))
                and float(symbol_mean_capture)
                < EXIT_CAPTURE_MIN_MEAN_GROSS_CAPTURE_OF_PATH_MFE
            ):
                reason = (
                    f"replay {symbol} mean_gross_capture_of_path_mfe="
                    f"{float(symbol_mean_capture):.6f} < "
                    f"{EXIT_CAPTURE_MIN_MEAN_GROSS_CAPTURE_OF_PATH_MFE:.6f}"
                )
                fail_reasons.append(reason)
        economic_gate_basis = "aggregate_validation"
        median_net_with_fills = aggregate.get(
            "median_realized_net_per_fill_with_fills"
        )
        positive_ratio = aggregate.get("positive_filled_segment_ratio")
        fail_reasons.extend(raw_replay_fail_reasons)
        summary["suppressed_aggregate_fail_reasons"] = []
        summary["economic_gate_basis"] = economic_gate_basis
        if not isinstance(median_net_with_fills, (int, float)):
            fail_reasons.append(
                "replay aggregate_validation "
                "median_realized_net_per_fill_with_fills missing"
            )
        elif float(median_net_with_fills) < 0.0:
            fail_reasons.append(
                f"replay {economic_gate_basis} median_realized_net_per_fill_with_fills="
                f"{float(median_net_with_fills):.6f} < 0.000000"
            )
        if not isinstance(positive_ratio, (int, float)):
            fail_reasons.append(
                "replay aggregate_validation positive_filled_segment_ratio missing"
            )
        elif float(positive_ratio) < MIN_POSITIVE_FILLED_SEGMENT_RATIO:
            fail_reasons.append(
                f"replay {economic_gate_basis} positive_filled_segment_ratio="
                f"{float(positive_ratio):.6f} < {MIN_POSITIVE_FILLED_SEGMENT_RATIO:.6f}"
            )
        if activation_gate_blocking_warnings:
            fail_reasons.append(
                "replay activation_gate pass_with_actions has blocking warnings: "
                + "; ".join(activation_gate_blocking_warnings)
            )
    elif raw_replay_fail_reasons:
        fail_reasons.extend(raw_replay_fail_reasons)
    if activation_gate_blocking_warnings and not isinstance(aggregate, dict):
        fail_reasons.append(
            "replay activation_gate pass_with_actions has blocking warnings: "
            + "; ".join(activation_gate_blocking_warnings)
        )

    return len(fail_reasons) == 0, fail_reasons, warn_reasons, summary


def gate_research_domain_split(
    report_path: Path | None,
    *,
    integrator_report: Dict[str, Any],
    replay_report_path: Path | None,
    alpha_probe_report_path: Path | None = None,
    require_report: bool,
) -> Tuple[bool, List[str], Dict[str, Any]]:
    fail_reasons: List[str] = []
    summary: Dict[str, Any] = {}
    if report_path is None:
        if require_report:
            fail_reasons.append("research_domain_split_report missing")
        return not fail_reasons, fail_reasons, summary
    if not report_path.is_file():
        return False, [f"research_domain_split_report not found: {report_path}"], summary
    payload = read_json(report_path)
    summary = payload
    if payload.get("schema_version") != "research_domain_split_v2":
        fail_reasons.append("research domain split schema_version invalid")
    if str(payload.get("status", "")).upper() != "PASS":
        fail_reasons.append("research domain split status != PASS")
    contract = payload.get("contract", {})
    if not isinstance(contract, dict):
        contract = {}
    if contract.get("domains_overlap") is not False:
        fail_reasons.append("research development/selection/holdout domains overlap")
    if contract.get("holdout_must_not_influence_candidate_selection") is not True:
        fail_reasons.append("research holdout selection isolation is not enforced")
    if contract.get("candidate_selection_domain") != "selection_validation":
        fail_reasons.append("research candidate selection domain is invalid")
    if contract.get("economic_validation_domain") != "untouched_final_holdout":
        fail_reasons.append("research economic validation domain is invalid")
    for key in (
        "holdout_consumption_ledger_required",
        "final_holdout_disjoint_from_prior_experiments",
        "selection_disjoint_from_prior_final_experiments",
    ):
        if contract.get(key) is not True:
            fail_reasons.append(f"research domain contract {key} != true")
    if (
        contract.get("prior_final_reuse_policy")
        != "historical_training_only_never_selection_or_final"
    ):
        fail_reasons.append("research prior final reuse policy is invalid")
    boundaries = payload.get("boundaries", {})
    if not isinstance(boundaries, dict):
        boundaries = {}
    development_end = boundaries.get("development_end_ts_ms")
    selection_start = boundaries.get("selection_start_ts_ms")
    selection_end = boundaries.get("selection_end_ts_ms")
    holdout_start = boundaries.get("holdout_start_ts_ms")
    if (
        not isinstance(development_end, int)
        or not isinstance(selection_start, int)
        or not isinstance(selection_end, int)
        or not isinstance(holdout_start, int)
        or development_end >= selection_start
        or selection_start > selection_end
        or selection_end >= holdout_start
    ):
        fail_reasons.append("research domain time boundaries are invalid")
    holdout_consumption = payload.get("holdout_consumption", {})
    if not isinstance(holdout_consumption, dict):
        holdout_consumption = {}
    if holdout_consumption.get("current_holdout_is_fresh") is not True:
        fail_reasons.append("research final holdout freshness is not proven")
    ledger_path_text = str(
        holdout_consumption.get("ledger_path") or ""
    ).strip()
    if not ledger_path_text:
        fail_reasons.append("research holdout consumption ledger path missing")
    prior_holdout_end = holdout_consumption.get(
        "last_consumed_holdout_end_ts_ms"
    )
    if prior_holdout_end is not None and (
        not isinstance(prior_holdout_end, int)
        or not isinstance(selection_start, int)
        or selection_start <= prior_holdout_end
    ):
        fail_reasons.append(
            "research selection overlaps previously consumed final holdout"
        )

    artifacts = payload.get("artifacts", {})
    if not isinstance(artifacts, dict):
        artifacts = {}
    development = artifacts.get("development_csv", {})
    selection = artifacts.get("selection_feature_csv", {})
    holdout = artifacts.get("holdout_feature_csv", {})
    if not isinstance(development, dict):
        development = {}
    if not isinstance(selection, dict):
        selection = {}
    if not isinstance(holdout, dict):
        holdout = {}
    development_sha = str(development.get("sha256") or "").strip()
    selection_sha = str(selection.get("sha256") or "").strip()
    holdout_sha = str(holdout.get("sha256") or "").strip()
    development_path = Path(str(development.get("path") or ""))
    selection_path = Path(str(selection.get("path") or ""))
    holdout_path = Path(str(holdout.get("path") or ""))
    for name, path, expected_sha in (
        ("development_csv", development_path, development_sha),
        ("selection_feature_csv", selection_path, selection_sha),
        ("holdout_feature_csv", holdout_path, holdout_sha),
    ):
        if re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
            fail_reasons.append(f"research {name} sha256 missing or invalid")
        elif not path.is_file():
            fail_reasons.append(f"research {name} not found: {path}")
        elif sha256_file(path) != expected_sha:
            fail_reasons.append(f"research {name} checksum mismatch")

    integrator_data = integrator_report.get("data", {})
    if not isinstance(integrator_data, dict):
        integrator_data = {}
    if integrator_data.get("research_domain") != "development":
        fail_reasons.append("integrator data.research_domain != development")
    if str(integrator_data.get("csv_sha256") or "").strip() != development_sha:
        fail_reasons.append("integrator training csv does not bind development domain")

    if alpha_probe_report_path is None or not alpha_probe_report_path.is_file():
        if require_report:
            fail_reasons.append("alpha probe report unavailable for selection binding")
    else:
        alpha_probe = read_json(alpha_probe_report_path)
        alpha_by_symbol = alpha_probe.get("by_symbol", {})
        if not isinstance(alpha_by_symbol, dict):
            alpha_by_symbol = {}
        alpha_feature_paths = [
            Path(str(item.get("feature_csv") or ""))
            for item in alpha_by_symbol.values()
            if isinstance(item, dict) and str(item.get("feature_csv") or "").strip()
        ]
        if not alpha_feature_paths:
            fail_reasons.append("alpha probe has no selection feature binding")
        elif not any(
            path.is_file() and sha256_file(path) == selection_sha
            for path in alpha_feature_paths
        ):
            fail_reasons.append(
                "alpha probe input does not bind selection-validation domain"
            )

    if replay_report_path is None or not replay_report_path.is_file():
        if require_report:
            fail_reasons.append("replay report unavailable for holdout binding")
    else:
        replay = read_json(replay_report_path)
        replay_feature_path = Path(str(replay.get("feature_csv") or ""))
        if not replay_feature_path.is_file():
            fail_reasons.append("replay holdout feature csv not found")
        elif sha256_file(replay_feature_path) != holdout_sha:
            fail_reasons.append("replay input does not bind untouched holdout domain")
        execution_contract = replay.get("execution_evidence_contract", {})
        if not isinstance(execution_contract, dict):
            execution_contract = {}
        if (
            execution_contract.get("evidence_role")
            != "offline_conservative_execution_prescreen"
            or execution_contract.get("fill_model")
            != "next_bar_ohlc_touch_at_limit_no_queue_position"
            or execution_contract.get("production_promotion_authority") is not False
            or execution_contract.get("live_candidate_episode_canary_required") is not True
        ):
            fail_reasons.append("replay execution evidence contract is invalid")
    return not fail_reasons, fail_reasons, summary


def gate_feature_parity_report(
    report_path: Path | None,
    *,
    require_report: bool,
) -> Tuple[bool, List[str], Dict[str, Any]]:
    fail_reasons: List[str] = []
    summary: Dict[str, Any] = {}
    if report_path is None:
        if require_report:
            fail_reasons.append("feature_parity_report missing")
        return not fail_reasons, fail_reasons, summary
    if not report_path.is_file():
        return False, [f"feature_parity_report not found: {report_path}"], summary
    payload = read_json(report_path)
    summary = payload
    if payload.get("schema_version") != "feature_parity_report_v1":
        fail_reasons.append("feature parity schema_version invalid")
    if str(payload.get("status", "")).upper() != "PASS":
        fail_reasons.append("feature parity status != PASS")
    if payload.get("engine") != "cpp_online_feature_engine":
        fail_reasons.append("feature parity engine identity invalid")
    if payload.get("golden_source") != "python_integrator_train":
        fail_reasons.append("feature parity golden source invalid")
    check_count_value = coerce_float(payload.get("check_count"))
    passed_count_value = coerce_float(payload.get("passed_count"))
    check_count = int(check_count_value or 0)
    passed_count = int(passed_count_value or 0)
    if check_count < 20 or passed_count != check_count:
        fail_reasons.append(
            f"feature parity coverage invalid: passed={passed_count}, checks={check_count}"
        )
    max_abs_error = coerce_float(payload.get("max_abs_error"))
    if max_abs_error is None or max_abs_error > 1e-9:
        fail_reasons.append(
            f"feature parity max_abs_error={max_abs_error} exceeds 1e-9"
        )
    failures = payload.get("failures")
    if not isinstance(failures, list) or failures:
        fail_reasons.append("feature parity failures must be an empty list")
    fixture_contract = payload.get("fixture_contract")
    if not isinstance(fixture_contract, dict):
        fixture_contract = {}
    if (
        fixture_contract.get("schema_version")
        != "feature_parity_fixture_contract_v1"
    ):
        fail_reasons.append("feature parity fixture contract schema invalid")
    if not CLOSED_LOOP_CONTRACT_PATH.is_file():
        fail_reasons.append(
            f"closed-loop trust anchor missing: {CLOSED_LOOP_CONTRACT_PATH}"
        )
        trust_anchor: Dict[str, Any] = {}
    else:
        contract_payload = read_json(CLOSED_LOOP_CONTRACT_PATH)
        trust_anchors = contract_payload.get("trust_anchors", {})
        if not isinstance(trust_anchors, dict):
            trust_anchors = {}
        trust_anchor = trust_anchors.get("feature_parity_fixture", {})
        if not isinstance(trust_anchor, dict):
            trust_anchor = {}
    if (
        trust_anchor.get("schema_version")
        != "feature_parity_fixture_contract_v1"
    ):
        fail_reasons.append("feature parity immutable trust anchor schema invalid")
    fixture_specs = (
        (
            "bars",
            "tools/fixtures/feature_parity_bars_v1.csv",
            "bars_fixture",
            "bars_fixture_sha256",
        ),
        (
            "expected",
            "tools/fixtures/feature_parity_expected_v1.tsv",
            "expected_fixture",
            "expected_fixture_sha256",
        ),
    )
    for label, required_path, path_key, sha_key in fixture_specs:
        reported_path = str(fixture_contract.get(path_key, "")).strip()
        cpp_reported_path = str(payload.get(path_key, "")).strip()
        reported_sha256 = str(fixture_contract.get(sha_key, "")).strip()
        anchored_path = str(trust_anchor.get(path_key, "")).strip()
        anchored_sha256 = str(trust_anchor.get(sha_key, "")).strip()
        if (
            reported_path != required_path
            or cpp_reported_path != required_path
            or anchored_path != required_path
        ):
            fail_reasons.append(
                f"feature parity {label} fixture path does not bind {required_path}"
            )
            continue
        if (
            re.fullmatch(r"[0-9a-f]{64}", anchored_sha256) is None
            or reported_sha256 != anchored_sha256
        ):
            fail_reasons.append(
                f"feature parity {label} fixture does not match immutable trust anchor"
            )
            continue
        fixture_path = PROJECT_ROOT / anchored_path
        if not fixture_path.is_file():
            fail_reasons.append(
                f"feature parity {label} fixture not found: {reported_path}"
            )
        elif sha256_file(fixture_path) != anchored_sha256:
            fail_reasons.append(
                f"feature parity {label} fixture sha256 mismatch"
            )
    return not fail_reasons, fail_reasons, summary


def gate_alpha_mechanism_probe_report(
    report_path: Path | None,
) -> Tuple[bool, List[str], List[str], Dict[str, Any]]:
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    summary: Dict[str, Any] = {}
    if report_path is None:
        return True, fail_reasons, warn_reasons, summary
    if not report_path.is_file():
        fail_reasons.append(f"alpha_mechanism_probe_report 不存在: {report_path}")
        return False, fail_reasons, warn_reasons, summary

    payload = read_json(report_path)
    mechanism_status = str(payload.get("mechanism_control_status", "")).strip().lower()
    market_status = str(payload.get("market_alpha_family_status", "")).strip().lower()
    candidate_search = payload.get("candidate_search", {})
    if not isinstance(candidate_search, dict):
        candidate_search = {}
    manifest = payload.get("deployable_candidate_manifest", {})
    if not isinstance(manifest, dict):
        manifest = {}

    if mechanism_status != "pass":
        fail_reasons.append(
            "alpha mechanism controls did not pass: "
            f"mechanism_control_status={mechanism_status or 'missing'}"
        )
    if market_status == "fail":
        fail_reasons.append(
            "alpha mechanism market alpha family failed holdout after cost"
        )
    elif market_status not in {"pass", "pass_with_actions"}:
        fail_reasons.append(
            f"alpha mechanism market_alpha_family_status={market_status or 'missing'}"
        )

    manifest_status = str(manifest.get("status", "")).strip().lower()
    if market_status == "pass" and manifest_status != "pass":
        fail_reasons.append(
            "alpha mechanism pass without deployable candidate manifest"
        )
    elif market_status != "pass" and manifest_status == "pass":
        warn_reasons.append(
            "alpha mechanism manifest has pass candidate while market alpha family did not pass"
        )

    summary = {
        "status": payload.get("status"),
        "mechanism_control_status": payload.get("mechanism_control_status"),
        "market_alpha_family_status": payload.get("market_alpha_family_status"),
        "candidate_pass_count": candidate_search.get("pass_candidate_count"),
        "best_candidate": candidate_search.get("best_candidate"),
        "deployable_candidate_manifest_status": manifest.get("status"),
        "selected_candidate": manifest.get("selected_candidate"),
    }
    return len(fail_reasons) == 0, fail_reasons, warn_reasons, summary


def prune_old_versions(
    index_entries: List[Dict[str, Any]], registry_dir: Path, max_versions: int
) -> List[Dict[str, Any]]:
    if max_versions <= 0 or len(index_entries) <= max_versions:
        return index_entries

    keep = index_entries[:max_versions]
    drop = index_entries[max_versions:]
    for entry in drop:
        subdir = entry.get("registry_subdir")
        if isinstance(subdir, str) and subdir:
            target = registry_dir / subdir
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
    return keep


def run_register(args: argparse.Namespace) -> int:
    if not (0.0 <= float(args.min_auc_mean) <= 1.0):
        print("[ERROR] --min_auc_mean 必须在 [0,1] 范围", file=sys.stderr)
        return 2
    if not (0.0 <= float(args.min_split_trained_ratio) <= 1.0):
        print("[ERROR] --min_split_trained_ratio 必须在 [0,1] 范围", file=sys.stderr)
        return 2
    if not (0.0 <= float(args.min_positive_model_net_edge_ratio) <= 1.0):
        print(
            "[ERROR] --min_positive_model_net_edge_ratio 必须在 [0,1] 范围",
            file=sys.stderr,
        )
        return 2
    if int(args.min_split_trained_count) <= 0:
        print("[ERROR] --min_split_trained_count 必须大于 0", file=sys.stderr)
        return 2
    if int(getattr(args, "min_model_net_total_trades", 20)) <= 0:
        print("[ERROR] --min_model_net_total_trades 必须大于 0", file=sys.stderr)
        return 2
    if int(getattr(args, "min_model_net_active_bars", 100)) <= 0:
        print("[ERROR] --min_model_net_active_bars 必须大于 0", file=sys.stderr)
        return 2
    if not (
        0.0
        <= float(getattr(args, "min_positive_model_net_splits_ratio", 0.50))
        <= 1.0
    ):
        print(
            "[ERROR] --min_positive_model_net_splits_ratio 必须在 [0,1] 范围",
            file=sys.stderr,
        )
        return 2

    model_file = Path(args.model_file)
    integrator_report_path = Path(args.integrator_report)
    miner_report_path = Path(args.miner_report) if args.miner_report else None
    walkforward_report_path = Path(args.walkforward_report) if args.walkforward_report else None
    replay_validation_report_path = (
        Path(args.replay_validation_report) if args.replay_validation_report else None
    )
    research_domain_split_report_arg = str(
        getattr(args, "research_domain_split_report", "") or ""
    ).strip()
    research_domain_split_report_path = (
        Path(research_domain_split_report_arg)
        if research_domain_split_report_arg
        else None
    )
    feature_parity_report_arg = str(
        getattr(args, "feature_parity_report", "") or ""
    ).strip()
    feature_parity_report_path = (
        Path(feature_parity_report_arg) if feature_parity_report_arg else None
    )
    alpha_mechanism_probe_report_path = (
        Path(getattr(args, "alpha_mechanism_probe_report", ""))
        if getattr(args, "alpha_mechanism_probe_report", "")
        else None
    )
    activation_transaction_path = (
        Path(str(getattr(args, "activation_transaction", "")).strip())
        if str(getattr(args, "activation_transaction", "")).strip()
        else None
    )

    if not model_file.is_file():
        print(f"[ERROR] model_file 不存在: {model_file}", file=sys.stderr)
        return 2
    if not integrator_report_path.is_file():
        print(f"[ERROR] integrator_report 不存在: {integrator_report_path}", file=sys.stderr)
        return 2
    if miner_report_path is not None and not miner_report_path.is_file():
        print(f"[ERROR] miner_report 不存在: {miner_report_path}", file=sys.stderr)
        return 2

    report = read_json(integrator_report_path)
    model_version = sanitize_name(str(report.get("model_version", "unknown_model")))
    feature_schema_version = str(report.get("feature_schema_version", "unknown_schema"))
    factor_set_version = str(report.get("factor_set_version", "unknown_factor_set"))
    model_sha = sha256_file(model_file)
    report_sha = sha256_file(integrator_report_path)

    gate_pass, gate_fail_reasons, gate_warn_reasons, metric_summary = gate_integrator_report(
        report,
        args.min_auc_mean,
        args.min_delta_auc_vs_baseline,
        args.min_mean_model_net_edge_bps,
        args.min_positive_model_net_edge_ratio,
        args.min_split_trained_count,
        args.min_split_trained_ratio,
        int(getattr(args, "min_model_net_total_trades", 20)),
        int(getattr(args, "min_model_net_active_bars", 100)),
        float(getattr(args, "min_positive_model_net_splits_ratio", 0.50)),
        float(getattr(args, "min_model_net_edge_lcb_bps", 0.0)),
    )
    external_gate_summary: Dict[str, Any] = {}

    if args.require_walkforward_positive or walkforward_report_path is not None:
        (
            walkforward_pass,
            walkforward_fail_reasons,
            walkforward_warn_reasons,
            walkforward_summary,
        ) = gate_walkforward_report(
            walkforward_report_path,
            bool(args.require_walkforward_positive),
            float(args.min_walkforward_avg_split_return),
            float(args.min_walkforward_enabled_avg_split_return),
            float(args.min_walkforward_traded_avg_split_return),
            focus_bucket=str(getattr(args, "walkforward_focus_bucket", "")),
            min_focus_bucket_bars=int(
                getattr(args, "walkforward_min_focus_bucket_bars", 0)
            ),
            min_focus_bucket_trades=int(
                getattr(args, "walkforward_min_focus_bucket_trades", 0)
            ),
            min_focus_bucket_sharpe=float(
                getattr(args, "walkforward_min_focus_bucket_sharpe", 0.0)
            ),
            focus_bucket_primary=bool(
                getattr(args, "walkforward_focus_bucket_primary", False)
            ),
        )
        external_gate_summary["walkforward"] = {
            "pass": walkforward_pass,
            "authoritative_for_integrator_promotion": bool(
                args.require_walkforward_positive
            ),
            "min_avg_split_return": args.min_walkforward_avg_split_return,
            "min_enabled_avg_split_return": args.min_walkforward_enabled_avg_split_return,
            "min_traded_avg_split_return": args.min_walkforward_traded_avg_split_return,
            "focus_bucket": getattr(args, "walkforward_focus_bucket", ""),
            "min_focus_bucket_bars": getattr(args, "walkforward_min_focus_bucket_bars", 0),
            "min_focus_bucket_trades": getattr(args, "walkforward_min_focus_bucket_trades", 0),
            "min_focus_bucket_sharpe": getattr(args, "walkforward_min_focus_bucket_sharpe", 0.0),
            "focus_bucket_primary": bool(
                getattr(args, "walkforward_focus_bucket_primary", False)
            ),
            "summary": walkforward_summary,
        }
        for item in walkforward_fail_reasons:
            if args.require_walkforward_positive:
                gate_fail_reasons.append(f"walkforward: {item}")
            else:
                gate_warn_reasons.append(
                    f"walkforward diagnostic_only: {item}"
                )
        for item in walkforward_warn_reasons:
            gate_warn_reasons.append(f"walkforward: {item}")

    require_replay_for_activation = bool(args.activate_on_pass)
    replay_candidate_identity: Dict[str, Any] = {}
    if (
        args.require_replay_validation_pass
        or require_replay_for_activation
        or replay_validation_report_path is not None
    ):
        (
            replay_pass,
            replay_fail_reasons,
            replay_warn_reasons,
            replay_summary,
        ) = gate_replay_validation_report(
            replay_validation_report_path,
            bool(args.require_replay_validation_pass or require_replay_for_activation),
            expected_model_version=model_version,
            expected_model_sha256=model_sha,
            expected_integrator_report_sha256=report_sha,
        )
        external_gate_summary["replay_validation"] = {
            "pass": replay_pass,
            "summary": replay_summary,
        }
        candidate_identity_value = replay_summary.get("candidate_identity", {})
        if isinstance(candidate_identity_value, dict):
            replay_candidate_identity = candidate_identity_value
        for item in replay_fail_reasons:
            gate_fail_reasons.append(f"replay_validation: {item}")
        for item in replay_warn_reasons:
            gate_warn_reasons.append(f"replay_validation: {item}")

    require_domain_split = bool(
        args.activate_on_pass
        or args.require_replay_validation_pass
        or research_domain_split_report_path is not None
    )
    if require_domain_split:
        (
            domain_pass,
            domain_fail_reasons,
            domain_summary,
        ) = gate_research_domain_split(
            research_domain_split_report_path,
            integrator_report=report,
            replay_report_path=replay_validation_report_path,
            alpha_probe_report_path=alpha_mechanism_probe_report_path,
            require_report=True,
        )
        external_gate_summary["research_domain_split"] = {
            "pass": domain_pass,
            "summary": domain_summary,
        }
        for item in domain_fail_reasons:
            gate_fail_reasons.append(f"research_domain_split: {item}")

    if args.activate_on_pass or feature_parity_report_path is not None:
        parity_pass, parity_fail_reasons, parity_summary = (
            gate_feature_parity_report(
                feature_parity_report_path,
                require_report=True,
            )
        )
        external_gate_summary["feature_parity"] = {
            "pass": parity_pass,
            "summary": parity_summary,
        }
        for item in parity_fail_reasons:
            gate_fail_reasons.append(f"feature_parity: {item}")

    if alpha_mechanism_probe_report_path is not None:
        (
            alpha_pass,
            alpha_fail_reasons,
            alpha_warn_reasons,
            alpha_summary,
        ) = gate_alpha_mechanism_probe_report(alpha_mechanism_probe_report_path)
        external_gate_summary["alpha_mechanism_probe"] = {
            "pass": alpha_pass,
            "summary": alpha_summary,
        }
        for item in alpha_fail_reasons:
            gate_fail_reasons.append(f"alpha_mechanism_probe: {item}")
        for item in alpha_warn_reasons:
            gate_warn_reasons.append(f"alpha_mechanism_probe: {item}")

    activation_transaction_context: Dict[str, Any] = {}
    if args.activate_on_pass:
        if activation_transaction_path is None:
            gate_fail_reasons.append(
                "activation_transaction is required for activate_on_pass"
            )
        elif not activation_transaction_path.is_file():
            gate_fail_reasons.append(
                f"activation_transaction does not exist: {activation_transaction_path}"
            )
        else:
            try:
                transaction = read_json(activation_transaction_path)
            except (OSError, json.JSONDecodeError) as exc:
                gate_fail_reasons.append(
                    f"activation_transaction is unreadable: {exc}"
                )
                transaction = {}
            policy_hash = str(
                transaction.get("activation_policy_sha256") or ""
            ).strip()
            previous = transaction.get("previous")
            if (
                transaction.get("schema_version")
                != "closed_loop_activation_transaction_v2"
            ):
                gate_fail_reasons.append(
                    "activation_transaction schema is not v2"
                )
            if str(transaction.get("status") or "") != "prepared":
                gate_fail_reasons.append(
                    "activation_transaction status must be prepared"
                )
            if not str(transaction.get("run_id") or "").strip():
                gate_fail_reasons.append(
                    "activation_transaction run_id is missing"
                )
            if len(policy_hash) != 64:
                gate_fail_reasons.append(
                    "activation_transaction frozen policy hash is missing"
                )
            if not isinstance(previous, dict):
                gate_fail_reasons.append(
                    "activation_transaction previous artifact snapshot is missing"
                )
            activation_transaction_context = {
                "path": str(activation_transaction_path),
                "run_id": transaction.get("run_id"),
                "status": transaction.get("status"),
                "activation_policy_sha256": policy_hash,
            }

    gate_pass = len(gate_fail_reasons) == 0

    created_at = now_utc_iso()
    created_tag = now_utc_compact()
    entry_id = f"{created_tag}_{model_version}_{model_sha[:8]}"
    registry_subdir = sanitize_name(entry_id)

    registry_dir = Path(args.registry_dir)
    entry_dir = registry_dir / registry_subdir
    entry_dir.mkdir(parents=True, exist_ok=True)

    model_dst = entry_dir / "integrator_model.cbm"
    report_dst = entry_dir / "integrator_report.json"
    miner_dst = entry_dir / "miner_report.json"
    meta_dst = entry_dir / "metadata.json"

    shutil.copy2(model_file, model_dst)
    shutil.copy2(integrator_report_path, report_dst)
    miner_sha = ""
    if miner_report_path is not None:
        shutil.copy2(miner_report_path, miner_dst)
        miner_sha = sha256_file(miner_report_path)

    gate_payload = {
        "pass": gate_pass,
        "primary_objective": EXPECTED_MODEL_OBJECTIVE,
        "min_mean_model_net_edge_bps": args.min_mean_model_net_edge_bps,
        "min_positive_model_net_edge_ratio": args.min_positive_model_net_edge_ratio,
        "min_model_net_total_trades": int(
            getattr(args, "min_model_net_total_trades", 20)
        ),
        "min_model_net_active_bars": int(
            getattr(args, "min_model_net_active_bars", 100)
        ),
        "min_positive_model_net_splits_ratio": float(
            getattr(args, "min_positive_model_net_splits_ratio", 0.50)
        ),
        "min_model_net_edge_lcb_bps": float(
            getattr(args, "min_model_net_edge_lcb_bps", 0.0)
        ),
        "min_auc_mean": args.min_auc_mean,
        "min_delta_auc_vs_baseline": args.min_delta_auc_vs_baseline,
        "min_split_trained_count": args.min_split_trained_count,
        "min_split_trained_ratio": args.min_split_trained_ratio,
        "require_walkforward_positive": bool(args.require_walkforward_positive),
        "min_walkforward_avg_split_return": args.min_walkforward_avg_split_return,
        "min_walkforward_enabled_avg_split_return": (
            args.min_walkforward_enabled_avg_split_return
        ),
        "min_walkforward_traded_avg_split_return": (
            args.min_walkforward_traded_avg_split_return
        ),
        "require_replay_validation_pass": bool(args.require_replay_validation_pass),
        "require_replay_for_activation": require_replay_for_activation,
        "fail_reasons": gate_fail_reasons,
        "warn_reasons": gate_warn_reasons,
        "metric_summary": metric_summary,
        "external": external_gate_summary,
        "activation_transaction": activation_transaction_context,
    }

    activated = bool(args.activate_on_pass and gate_pass)
    active_model_path = Path(args.active_model_path)
    active_report_path = Path(args.active_report_path)
    active_miner_report_path = Path(args.active_miner_report_path)
    active_meta_path = Path(args.active_meta_path)
    active_model_sha = ""
    active_report_sha = ""

    if activated:
        atomic_copy(model_file, active_model_path)
        active_model_sha = sha256_file(active_model_path)
        active_report_payload = json.loads(
            json.dumps(report, ensure_ascii=False)
        )
        data_section = active_report_payload.get("data")
        if not isinstance(data_section, dict):
            data_section = {}
            active_report_payload["data"] = data_section
        if miner_report_path is not None:
            atomic_copy(miner_report_path, active_miner_report_path)
            data_section["miner_report_path"] = str(active_miner_report_path)
        write_json(active_report_path, active_report_payload)
        active_report_sha = sha256_file(active_report_path)
        execution_policy = replay_candidate_identity.get(
            "execution_policy", {}
        )
        execution_policy_sha256 = (
            str(execution_policy.get("sha256", "")).strip()
            if isinstance(execution_policy, dict)
            else ""
        )
        trade_bot_sha256 = str(
            replay_candidate_identity.get("trade_bot_sha256", "")
        ).strip()
        runtime_config_sha256 = str(
            replay_candidate_identity.get("runtime_config_sha256", "")
        ).strip()
        active_payload = {
            "active_entry_id": entry_id,
            "model_version": model_version,
            "feature_schema_version": feature_schema_version,
            "factor_set_version": factor_set_version,
            "activated_at_utc": created_at,
            "activation_transaction": activation_transaction_context,
            "model_sha256": model_sha,
            "report_sha256": active_report_sha,
            "source_report_sha256": report_sha,
            "execution_policy_sha256": execution_policy_sha256,
            "runtime_config_sha256": runtime_config_sha256,
            "trade_bot_sha256": trade_bot_sha256,
            "gate": gate_payload,
        }
        write_json(active_meta_path, active_payload)

    entry_payload: Dict[str, Any] = {
        "entry_id": entry_id,
        "registry_subdir": registry_subdir,
        "created_at_utc": created_at,
        "model_version": model_version,
        "feature_schema_version": feature_schema_version,
        "factor_set_version": factor_set_version,
        "artifacts": {
            "model_file": str(model_dst),
            "integrator_report": str(report_dst),
            "miner_report": str(miner_dst) if miner_report_path is not None else "",
        },
        "checksums": {
            "model_sha256": model_sha,
            "integrator_report_sha256": report_sha,
            "miner_report_sha256": miner_sha,
        },
        "active_checksums": {
            "model_sha256": active_model_sha,
            "report_sha256": active_report_sha,
            "execution_policy_sha256": (
                str(
                    replay_candidate_identity.get(
                        "execution_policy", {}
                    ).get("sha256", "")
                )
                if isinstance(
                    replay_candidate_identity.get("execution_policy", {}),
                    dict,
                )
                else ""
            ),
            "runtime_config_sha256": str(
                replay_candidate_identity.get("runtime_config_sha256", "")
            ).strip(),
            "trade_bot_sha256": str(
                replay_candidate_identity.get("trade_bot_sha256", "")
            ).strip(),
        },
        "sizes": {
            "model_bytes": model_file.stat().st_size,
            "integrator_report_bytes": integrator_report_path.stat().st_size,
            "miner_report_bytes": miner_report_path.stat().st_size if miner_report_path else 0,
        },
        "gate": gate_payload,
        "activated": activated,
        "activation_transaction": activation_transaction_context,
    }
    write_json(meta_dst, entry_payload)

    index_path = registry_dir / "index.json"
    index_lock_path = registry_dir / ".index.lock"
    with FileLock(index_lock_path):
        index_entries = load_index(index_path)
        index_entries = [entry for entry in index_entries if entry.get("entry_id") != entry_id]
        index_entries.insert(0, entry_payload)
        index_entries = prune_old_versions(index_entries, registry_dir, args.max_versions)
        write_json(index_path, index_entries)

    if args.registration_out:
        write_json(Path(args.registration_out), entry_payload)

    print(f"MODEL_REGISTRY_ENTRY: {entry_id}")
    print(f"GATE_PASS: {str(gate_pass).lower()}")
    print(f"ACTIVATED: {str(activated).lower()}")
    if gate_fail_reasons:
        print("GATE_FAIL_REASONS:")
        for item in gate_fail_reasons:
            print(f"  - {item}")
    print(f"INDEX_PATH: {index_path}")
    return 0 if gate_pass else 3


def main() -> int:
    args = parse_args()
    if args.command == "register":
        return run_register(args)
    print(f"[ERROR] 未知命令: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
