#!/usr/bin/env python3
"""
运行日志自动验收脚本（DEPLOY/SMOKE/S3/S5）。

用途：
1. 对 `run_s3.log` / `run_s5.log` 做统一 PASS/FAIL 判定；
2. 汇总关键运行指标，减少人工翻日志成本；
3. 生成可归档 JSON，便于阶段对比。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class StageRule:
    name: str
    min_runtime_status: int
    require_gate_window: bool
    require_gate_pass: bool
    require_evolution_init: bool
    require_execution_activity: bool
    require_flat_start: bool
    max_start_abs_notional_usd: float
    max_trading_halted_true_ratio: float
    gate_fail_hard_min_windows: int
    gate_fail_hard_max_fail_ratio: float
    gate_warn_min_windows: int
    gate_warn_max_fail_ratio: float
    min_strategy_mix_nonzero_windows: int


STAGE_RULES: Dict[str, StageRule] = {
    "DEPLOY": StageRule(
        name="DEPLOY",
        min_runtime_status=0,
        require_gate_window=False,
        require_gate_pass=False,
        require_evolution_init=False,
        require_execution_activity=False,
        require_flat_start=False,
        max_start_abs_notional_usd=0.0,
        max_trading_halted_true_ratio=0.50,
        gate_fail_hard_min_windows=0,
        gate_fail_hard_max_fail_ratio=1.10,
        gate_warn_min_windows=10,
        gate_warn_max_fail_ratio=0.95,
        min_strategy_mix_nonzero_windows=0,
    ),
    "SMOKE": StageRule(
        name="SMOKE",
        min_runtime_status=30,
        require_gate_window=False,
        require_gate_pass=False,
        require_evolution_init=False,
        require_execution_activity=False,
        require_flat_start=False,
        max_start_abs_notional_usd=0.0,
        max_trading_halted_true_ratio=0.0,
        gate_fail_hard_min_windows=0,
        gate_fail_hard_max_fail_ratio=1.10,
        gate_warn_min_windows=0,
        gate_warn_max_fail_ratio=1.10,
        min_strategy_mix_nonzero_windows=0,
    ),
    "S3": StageRule(
        name="S3",
        min_runtime_status=10,
        require_gate_window=True,
        require_gate_pass=False,
        require_evolution_init=False,
        require_execution_activity=False,
        require_flat_start=False,
        max_start_abs_notional_usd=0.0,
        max_trading_halted_true_ratio=0.20,
        gate_fail_hard_min_windows=0,
        gate_fail_hard_max_fail_ratio=1.10,
        gate_warn_min_windows=20,
        gate_warn_max_fail_ratio=0.90,
        min_strategy_mix_nonzero_windows=0,
    ),
    "S5": StageRule(
        name="S5",
        min_runtime_status=50,
        require_gate_window=True,
        require_gate_pass=True,
        require_evolution_init=True,
        require_execution_activity=True,
        require_flat_start=True,
        max_start_abs_notional_usd=50.0,
        max_trading_halted_true_ratio=0.10,
        gate_fail_hard_min_windows=10,
        gate_fail_hard_max_fail_ratio=0.90,
        gate_warn_min_windows=8,
        gate_warn_max_fail_ratio=0.85,
        min_strategy_mix_nonzero_windows=1,
    ),
}

MIN_EXPLICIT_LIQUIDITY_FILL_RATIO_WARN = 0.70
MAX_UNKNOWN_FILL_RATIO_WARN = 0.20
MAX_FEE_SIGN_FALLBACK_FILL_RATIO_WARN = 0.30
MAX_TOP1_CONCENTRATION_SHARE_WARN = 0.90
MIN_S5_EVOLUTION_ACTIONS_FOR_UPDATE_WARN = 30
MIN_S5_LEARNABILITY_PASS_FOR_UPDATE_WARN = 10
DEFAULT_S5_MIN_EFFECTIVE_UPDATES = 1
DEFAULT_S5_MIN_REALIZED_NET_PER_FILL_USD = 0.0
DEFAULT_S5_MIN_REALIZED_NET_PER_FILL_WINDOWS = 10
DEFAULT_S5_MIN_FILL_WINDOWS = 10
DEFAULT_S5_MIN_EQUITY_CHANGE_USD: Optional[float] = None
DEFAULT_S5_MIN_EQUITY_CHANGE_SAMPLES = 0
DEFAULT_S5_MAX_EQUITY_VS_REALIZED_GAP_USD: Optional[float] = None
DEFAULT_S5_PROTECTION_ENABLED = False
DEFAULT_S5_PROFIT_PROTECTION_ENABLED = False
DEFAULT_S5_MAX_PROTECTIVE_ORDER_MISSING_COUNT = 0
DEFAULT_S5_MIN_TREND_RUNTIME_WINDOWS = 60
DEFAULT_S5_MIN_EFFECTIVE_RUNTIME_AGE_SECONDS = 3600
MIN_S5_EXECUTION_NEGATIVE_SYMBOL_FILL_COUNT_WARN = 10

RUNTIME_ACCOUNT_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?"
    r"RUNTIME_STATUS:.*?equity=(?P<equity>-?[0-9]+(?:\.[0-9]+)?), "
    r"drawdown_pct=(?P<drawdown_pct>-?[0-9]+(?:\.[0-9]+)?), "
    r"notional=(?P<notional>-?[0-9]+(?:\.[0-9]+)?)"
)
RUNTIME_ACCOUNT_SAMPLE_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?"
    r"RUNTIME_STATUS:\s*ticks=(?P<tick>\d+),.*?"
    r"account=\{equity=(?P<equity>-?[0-9]+(?:\.[0-9]+)?), "
    r"drawdown_pct=(?P<drawdown_pct>-?[0-9]+(?:\.[0-9]+)?), "
    r"notional=(?P<notional>-?[0-9]+(?:\.[0-9]+)?), "
    r"realized_pnl=(?P<realized>-?[0-9]+(?:\.[0-9]+)?), "
    r"fees=(?P<fees>-?[0-9]+(?:\.[0-9]+)?), "
    r"realized_net=(?P<net>-?[0-9]+(?:\.[0-9]+)?)"
)
RUNTIME_ACCOUNT_REALIZED_RE = re.compile(
    r"RUNTIME_STATUS:.*?account=\{[^}]*?"
    r"realized_pnl=(?P<realized>-?[0-9]+(?:\.[0-9]+)?), "
    r"fees=(?P<fees>-?[0-9]+(?:\.[0-9]+)?), "
    r"realized_net=(?P<net>-?[0-9]+(?:\.[0-9]+)?)"
)
RUNTIME_STRATEGY_MIX_RE = re.compile(
    r"RUNTIME_STATUS:.*?strategy_mix=\{[^}]*?"
    r"latest_trend_notional=(?P<latest_trend>-?[0-9]+(?:\.[0-9]+)?), "
    r"latest_defensive_notional=(?P<latest_defensive>-?[0-9]+(?:\.[0-9]+)?), "
    r"latest_blended_notional=(?P<latest_blended>-?[0-9]+(?:\.[0-9]+)?), "
    r"avg_abs_trend_notional=(?P<avg_trend>[0-9]+(?:\.[0-9]+)?), "
    r"avg_abs_defensive_notional=(?P<avg_defensive>[0-9]+(?:\.[0-9]+)?), "
    r"avg_abs_blended_notional=(?P<avg_blended>[0-9]+(?:\.[0-9]+)?), "
    r"samples=(?P<samples>\d+)"
    r"(?:, policy_flat_samples=(?P<policy_flat_samples>\d+))?"
)
RUNTIME_EXECUTION_WINDOW_RE = re.compile(
    r"RUNTIME_STATUS:.*?execution_window=\{(?P<body>[^}]*)\}"
)
RUNTIME_ENTRY_GATE_RE = re.compile(
    r"RUNTIME_STATUS:.*?entry_gate=\{[^}]*?"
    r"near_miss_tolerance_bps=(?P<near_miss_tolerance_bps>-?[0-9]+(?:\.[0-9]+)?), "
    r"[^}]*?"
    r"quality_guard_penalty_bps=(?P<quality_guard_penalty_bps>-?[0-9]+(?:\.[0-9]+)?), "
    r"[^}]*?"
    r"observed_filtered_ratio=(?P<observed_filtered_ratio>-?[0-9]+(?:\.[0-9]+)?), "
    r"[^}]*?"
    r"observed_near_miss_ratio=(?P<observed_near_miss_ratio>-?[0-9]+(?:\.[0-9]+)?)"
    r"(?:, [^}]*?observed_near_miss_allowed_ratio="
    r"(?P<observed_near_miss_allowed_ratio>-?[0-9]+(?:\.[0-9]+)?))?"
)
RUNTIME_CONCENTRATION_RE = re.compile(
    r"RUNTIME_STATUS:.*?concentration=\{[^}]*?"
    r"gross_notional_usd=(?P<gross_notional_usd>-?[0-9]+(?:\.[0-9]+)?), "
    r"top1_abs_notional_usd=(?P<top1_abs_notional_usd>-?[0-9]+(?:\.[0-9]+)?), "
    r"top1_symbol=(?P<top1_symbol>[^,}]+), "
    r"top1_share=(?P<top1_share>-?[0-9]+(?:\.[0-9]+)?), "
    r"symbol_count=(?P<symbol_count>\d+)"
)
RUNTIME_EXECUTION_QUALITY_GUARD_RE = re.compile(
    r"RUNTIME_STATUS:.*?execution_quality_guard=\{[^}]*?"
    r"enabled=(?P<enabled>true|false), "
    r"active=(?P<active>true|false), "
    r"bad_streak=(?P<bad_streak>-?[0-9]+), "
    r"good_streak=(?P<good_streak>-?[0-9]+), "
    r"no_fill_windows=(?P<no_fill_windows>-?[0-9]+), "
    r"min_fills=(?P<min_fills>-?[0-9]+), "
    r"trigger_streak=(?P<trigger_streak>-?[0-9]+), "
    r"release_streak=(?P<release_streak>-?[0-9]+), "
    r"min_realized_net_per_fill_usd=(?P<min_realized_net_per_fill_usd>-?[0-9]+(?:\.[0-9]+)?), "
    r"max_fee_bps_per_fill=(?P<max_fee_bps_per_fill>-?[0-9]+(?:\.[0-9]+)?), "
    r"applied_penalty_bps=(?P<applied_penalty_bps>-?[0-9]+(?:\.[0-9]+)?)"
    r"(?:, symbol_active_count=(?P<symbol_active_count>-?[0-9]+), "
    r"symbol_state_count=(?P<symbol_state_count>-?[0-9]+))?"
)
RUNTIME_ENTRY_EDGE_ADJUST_RE = re.compile(
    r"RUNTIME_STATUS:.*?funnel_window=\{[^}]*?"
    r"entry_regime_adjust_avg_bps=(?P<regime_adjust>-?[0-9]+(?:\.[0-9]+)?), "
    r"entry_volatility_adjust_avg_bps=(?P<volatility_adjust>-?[0-9]+(?:\.[0-9]+)?), "
    r"entry_liquidity_adjust_avg_bps=(?P<liquidity_adjust>-?[0-9]+(?:\.[0-9]+)?)"
    r"(?:, entry_concentration_adjust_avg_bps="
    r"(?P<concentration_adjust>-?[0-9]+(?:\.[0-9]+)?))?"
)
RUNTIME_RECONCILE_RUNTIME_RE = re.compile(
    r"RUNTIME_STATUS:.*?reconcile_runtime=\{[^}]*?"
    r"anomaly_streak=(?P<anomaly_streak>-?[0-9]+), "
    r"healthy_streak=(?P<healthy_streak>-?[0-9]+), "
    r"anomaly_reduce_only=(?P<anomaly_reduce_only>true|false), "
    r"anomaly_reduce_only_threshold=(?P<anomaly_reduce_only_threshold>-?[0-9]+), "
    r"anomaly_halt_threshold=(?P<anomaly_halt_threshold>-?[0-9]+), "
    r"anomaly_resume_threshold=(?P<anomaly_resume_threshold>-?[0-9]+)"
)
RUNTIME_REGIME_CURRENT_RE = re.compile(
    r"RUNTIME_STATUS:.*?regime_current=\{[^}]*?bucket=(?P<bucket>[A-Z_]+)"
)
RUNTIME_REGIME_CURRENT_DIAGNOSTICS_RE = re.compile(
    r"RUNTIME_STATUS:.*?regime_current=\{[^}]*?"
    r"(?:warmup=(?P<warmup>true|false)[^}]*?)?"
    r"trend_threshold_ratio=(?P<trend_threshold_ratio>[0-9]+(?:\.[0-9]+)?), "
    r"(?:volatility_threshold_ratio=(?P<volatility_threshold_ratio>[0-9]+(?:\.[0-9]+)?), )?"
    r"trend_candidate=(?P<trend_candidate>true|false)"
    r"(?:, warmup_trend_candidate=(?P<warmup_trend_candidate>true|false))?"
    r"(?:, raw_regime=(?P<raw_regime>[A-Z_]+), "
    r"raw_bucket=(?P<raw_bucket>[A-Z_]+), "
    r"pending_regime=(?P<pending_regime>[A-Z_]+), "
    r"pending_bucket=(?P<pending_bucket>[A-Z_]+), "
    r"pending_regime_ticks=(?P<pending_regime_ticks>-?\d+), "
    r"confirm_ticks_required=(?P<confirm_ticks_required>-?\d+), "
    r"pending_regime_elapsed_ms=(?P<pending_regime_elapsed_ms>-?\d+), "
    r"confirm_elapsed_ms_required=(?P<confirm_elapsed_ms_required>-?\d+), "
    r"pending_trend_confirmation=(?P<pending_trend_confirmation>true|false))?"
)
RUNTIME_REGIME_WINDOW_CANDIDATE_RE = re.compile(
    r"RUNTIME_STATUS:.*?regime_window=\{[^}]*?"
    r"trend_candidate_ticks=(?P<trend_candidate_ticks>[0-9]+)"
    r"(?:, warmup_trend_candidate_ticks=(?P<warmup_trend_candidate_ticks>[0-9]+))?"
)
REGIME_CHANGE_RE = re.compile(
    r"REGIME_CHANGE: symbol=(?P<symbol>[^,]+), "
    r"regime=(?P<regime>[A-Z_]+), bucket=(?P<bucket>[A-Z_]+), "
    r"warmup=(?P<warmup>true|false), decision_interval_ms=(?P<decision_interval_ms>-?\d+), "
    r"aggregated_events=(?P<aggregated_events>\d+), "
    r"instant_return=(?P<instant_return>-?[0-9]+(?:\.[0-9]+)?), "
    r"trend_strength=(?P<trend_strength>-?[0-9]+(?:\.[0-9]+)?), "
    r"volatility=(?P<volatility>-?[0-9]+(?:\.[0-9]+)?)"
    r"(?:, trend_threshold_ratio=(?P<trend_threshold_ratio>[0-9]+(?:\.[0-9]+)?), "
    r"volatility_threshold_ratio=(?P<volatility_threshold_ratio>[0-9]+(?:\.[0-9]+)?), "
    r"trend_candidate=(?P<trend_candidate>true|false)"
    r"(?:, warmup_trend_candidate=(?P<warmup_trend_candidate>true|false))?"
    r"(?:, raw_regime=(?P<raw_regime>[A-Z_]+), "
    r"raw_bucket=(?P<raw_bucket>[A-Z_]+), "
    r"pending_regime=(?P<pending_regime>[A-Z_]+), "
    r"pending_bucket=(?P<pending_bucket>[A-Z_]+), "
    r"pending_regime_ticks=(?P<pending_regime_ticks>-?\d+), "
    r"confirm_ticks_required=(?P<confirm_ticks_required>-?\d+), "
    r"pending_regime_elapsed_ms=(?P<pending_regime_elapsed_ms>-?\d+), "
    r"confirm_elapsed_ms_required=(?P<confirm_elapsed_ms_required>-?\d+), "
    r"pending_trend_confirmation=(?P<pending_trend_confirmation>true|false))?)?"
)
LOG_LINE_TS_RE = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
PROCESS_START_RE = re.compile(
    r"PROCESS_START:.*?startup_utc=(?P<startup>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)"
)
RUNTIME_STATUS_BOOT_START_RE = re.compile(
    r"RUNTIME_STATUS:.*?boot=\{[^}]*?"
    r"startup_utc=(?P<startup>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)"
)
RUNTIME_STATUS_TS_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?RUNTIME_STATUS:"
)
FEATURES_RE = re.compile(r"FEATURES:\s*(?P<body>.*)")
FEATURE_VALUE_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)="
    r"(?P<value>-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|nan|inf|-inf)"
)
INTEGRATOR_NAN_SKIP_FIELD_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>[^,\s]+)"
)
INTEGRATOR_NAN_SKIP_LEGACY_RE = re.compile(
    r"INTEGRATOR_SKIP: NaN feature detected at index "
    r"(?P<feature_index>\d+) \((?P<feature_name>[^)]+)\)"
)
TREND_CANDIDATE_MIN_THRESHOLD_RATIO = 0.60
FEATURE_LARGE_ABS_WARN_THRESHOLD = 1000.0
FEATURE_LARGE_ABS_RATIO_WARN_THRESHOLD = 0.10


def parse_bool_arg(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"非法布尔值: {raw}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ai-trade 运行日志自动验收")
    parser.add_argument(
        "--log",
        required=True,
        help="运行日志文件路径，例如 ./run_s5.log",
    )
    parser.add_argument(
        "--stage",
        default="S5",
        choices=sorted(STAGE_RULES.keys()),
        help="验收阶段（默认 S5）",
    )
    parser.add_argument(
        "--min_runtime_status",
        type=int,
        default=None,
        help="覆盖阶段默认最小 RUNTIME_STATUS 条数",
    )
    parser.add_argument(
        "--json_out",
        default="",
        help="可选：将结构化结果输出到 JSON 文件",
    )
    parser.add_argument(
        "--s5-min-effective-updates",
        type=int,
        default=DEFAULT_S5_MIN_EFFECTIVE_UPDATES,
        help=(
            "S5 硬门槛：有效学习更新最小次数 "
            "(counterfactual_update + factor_ic_action + objective_update)"
        ),
    )
    parser.add_argument(
        "--s5-min-realized-net-per-fill-usd",
        type=float,
        default=DEFAULT_S5_MIN_REALIZED_NET_PER_FILL_USD,
        help="S5 硬门槛：单位成交净收益下限（USD/Fill）",
    )
    parser.add_argument(
        "--s5-min-realized-net-per-fill-windows",
        type=int,
        default=DEFAULT_S5_MIN_REALIZED_NET_PER_FILL_WINDOWS,
        help="S5 生效条件：至少 N 个窗口观测到 fills>0 才检查单位成交净收益门槛",
    )
    parser.add_argument(
        "--s5-min-fill-windows",
        type=int,
        default=DEFAULT_S5_MIN_FILL_WINDOWS,
        help="S5 强门槛：至少 N 个窗口观测到 fills>0，避免低成交弱通过",
    )
    parser.add_argument(
        "--s5-min-equity-change-usd",
        type=float,
        default=DEFAULT_S5_MIN_EQUITY_CHANGE_USD,
        help="S5 可选硬门槛：权益变化下限（USD）；未设置则不校验",
    )
    parser.add_argument(
        "--s5-min-equity-change-samples",
        type=int,
        default=DEFAULT_S5_MIN_EQUITY_CHANGE_SAMPLES,
        help="S5 权益门槛生效条件：至少 N 个账户采样点（默认 0）",
    )
    parser.add_argument(
        "--s5-max-equity-vs-realized-gap-usd",
        type=float,
        default=DEFAULT_S5_MAX_EQUITY_VS_REALIZED_GAP_USD,
        help="S5 可选硬门槛：|equity_change - realized_net_change| 上限（USD）；未设置则不校验",
    )
    parser.add_argument(
        "--s5-protection-enabled",
        type=parse_bool_arg,
        default=DEFAULT_S5_PROTECTION_ENABLED,
        help="S5 主链是否启用保护单必检项（默认 false）",
    )
    parser.add_argument(
        "--s5-profit-protection-enabled",
        type=parse_bool_arg,
        default=DEFAULT_S5_PROFIT_PROTECTION_ENABLED,
        help="S5 是否启用盈利保护观测项（默认 false）",
    )
    parser.add_argument(
        "--s5-max-protective-order-missing-count",
        type=int,
        default=DEFAULT_S5_MAX_PROTECTIVE_ORDER_MISSING_COUNT,
        help="S5 强门禁：保护单缺失事件最大允许次数（默认 0）",
    )
    parser.add_argument(
        "--s5-min-trend-runtime-windows",
        type=int,
        default=DEFAULT_S5_MIN_TREND_RUNTIME_WINDOWS,
        help="S5 反退化门禁：若 TREND 桶窗口达到该数量，必须形成策略参与或执行样本",
    )
    return parser.parse_args()


def load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, flags=re.MULTILINE))


def max_tick(text: str) -> int:
    matches = re.findall(r"RUNTIME_STATUS:\s*ticks=(\d+)", text)
    if not matches:
        return 0
    return max(int(x) for x in matches)


def extract_log_field(line: str, key: str) -> Optional[str]:
    match = re.search(rf"\b{re.escape(key)}=([^,\s}}]+)", line)
    if not match:
        return None
    value = match.group(1).strip()
    return value if value else None


def extract_float_log_field(line: str, key: str) -> Optional[float]:
    raw_value = extract_log_field(line, key)
    if raw_value is None:
        return None
    try:
        return float(raw_value)
    except ValueError:
        return None


def bump_counter(counter: Dict[str, int], key: Optional[str], amount: int = 1) -> None:
    normalized = str(key or "UNKNOWN").strip().upper()
    if not normalized:
        normalized = "UNKNOWN"
    counter[normalized] = int(counter.get(normalized, 0)) + int(amount)


def bump_float(counter: Dict[str, float], key: Optional[str], amount: float) -> None:
    normalized = str(key or "UNKNOWN").strip().upper()
    if not normalized:
        normalized = "UNKNOWN"
    counter[normalized] = rounded(float(counter.get(normalized, 0.0)) + float(amount))


def rounded(value: float) -> float:
    return round(float(value), 8)


def new_fill_bucket() -> Dict[str, object]:
    return {
        "total": 0,
        "fee_usd": 0.0,
        "notional_abs_usd": 0.0,
        "by_symbol": {},
        "by_liquidity": {},
        "fee_by_liquidity": {},
        "notional_abs_by_liquidity": {},
    }


def record_fill(
    bucket: Dict[str, object],
    symbol: Optional[str],
    liquidity: Optional[str],
    fee_usd: float,
    notional_abs_usd: float,
) -> None:
    bucket["total"] = int(bucket.get("total", 0)) + 1
    bucket["fee_usd"] = rounded(float(bucket.get("fee_usd", 0.0)) + abs(fee_usd))
    bucket["notional_abs_usd"] = rounded(
        float(bucket.get("notional_abs_usd", 0.0)) + abs(notional_abs_usd)
    )
    by_symbol = bucket.setdefault("by_symbol", {})
    by_liquidity = bucket.setdefault("by_liquidity", {})
    fee_by_liquidity = bucket.setdefault("fee_by_liquidity", {})
    notional_by_liquidity = bucket.setdefault("notional_abs_by_liquidity", {})
    if isinstance(by_symbol, dict):
        bump_counter(by_symbol, symbol)
    if isinstance(by_liquidity, dict):
        bump_counter(by_liquidity, liquidity)
    if isinstance(fee_by_liquidity, dict):
        bump_float(fee_by_liquidity, liquidity, abs(fee_usd))
    if isinstance(notional_by_liquidity, dict):
        bump_float(notional_by_liquidity, liquidity, abs(notional_abs_usd))


def normalize_counter_key(key: Optional[str]) -> str:
    normalized = str(key or "UNKNOWN").strip().upper()
    return normalized or "UNKNOWN"


def extract_fill_direction(line: str) -> int:
    raw_direction = extract_float_log_field(line, "direction")
    if raw_direction is not None:
        if raw_direction > 0:
            return 1
        if raw_direction < 0:
            return -1
    side = str(extract_log_field(line, "side") or "").strip().lower()
    if side == "buy":
        return 1
    if side == "sell":
        return -1
    return 0


def record_symbol_fill_quality(
    quality_by_symbol: Dict[str, object],
    position_state: Dict[str, Dict[str, float]],
    *,
    symbol: Optional[str],
    direction: int,
    qty: float,
    price: float,
    fee_usd: float,
    notional_abs_usd: float,
    liquidity: Optional[str],
) -> None:
    normalized_symbol = normalize_counter_key(symbol)
    bucket = quality_by_symbol.setdefault(
        normalized_symbol,
        {
            "fills": 0,
            "fee_usd": 0.0,
            "notional_abs_usd": 0.0,
            "realized_pnl_usd": 0.0,
            "realized_net_usd": 0.0,
            "realized_net_per_fill": 0.0,
            "maker_fills": 0,
            "taker_fills": 0,
            "unknown_liquidity_fills": 0,
        },
    )
    if not isinstance(bucket, dict):
        return

    abs_fee = abs(float(fee_usd))
    bucket["fills"] = int(bucket.get("fills", 0)) + 1
    bucket["fee_usd"] = rounded(float(bucket.get("fee_usd", 0.0)) + abs_fee)
    bucket["notional_abs_usd"] = rounded(
        float(bucket.get("notional_abs_usd", 0.0)) + abs(float(notional_abs_usd))
    )
    liquidity_normalized = normalize_counter_key(liquidity)
    if liquidity_normalized == "MAKER":
        bucket["maker_fills"] = int(bucket.get("maker_fills", 0)) + 1
    elif liquidity_normalized == "TAKER":
        bucket["taker_fills"] = int(bucket.get("taker_fills", 0)) + 1
    else:
        bucket["unknown_liquidity_fills"] = int(
            bucket.get("unknown_liquidity_fills", 0)
        ) + 1

    realized_pnl = 0.0
    if direction != 0 and qty > 0.0 and price > 0.0:
        state = position_state.setdefault(
            normalized_symbol,
            {
                "position": 0.0,
                "avg_price": 0.0,
            },
        )
        position = float(state.get("position", 0.0))
        avg_price = float(state.get("avg_price", 0.0))
        signed_qty = float(direction) * float(qty)
        if abs(position) < 1e-12 or position * signed_qty > 0.0:
            new_position = position + signed_qty
            if abs(new_position) > 1e-12:
                avg_price = (
                    abs(position) * avg_price + abs(signed_qty) * price
                ) / abs(new_position)
            else:
                avg_price = 0.0
            position = new_position
        else:
            close_qty = min(abs(position), abs(signed_qty))
            if position > 0.0:
                realized_pnl += (price - avg_price) * close_qty
            else:
                realized_pnl += (avg_price - price) * close_qty
            new_position = position + signed_qty
            if abs(new_position) < 1e-12:
                position = 0.0
                avg_price = 0.0
            elif position * new_position > 0.0:
                position = new_position
            else:
                position = new_position
                avg_price = price
        state["position"] = position
        state["avg_price"] = avg_price

    bucket["realized_pnl_usd"] = rounded(
        float(bucket.get("realized_pnl_usd", 0.0)) + realized_pnl
    )
    bucket["realized_net_usd"] = rounded(
        float(bucket.get("realized_pnl_usd", 0.0)) - float(bucket.get("fee_usd", 0.0))
    )
    fills = int(bucket.get("fills", 0))
    bucket["realized_net_per_fill"] = (
        rounded(float(bucket.get("realized_net_usd", 0.0)) / fills)
        if fills > 0
        else 0.0
    )


def extract_execution_attribution(text: str) -> Dict[str, object]:
    probe_client_order_ids: set[str] = set()
    probe_fill_ids: set[str] = set()
    position_state: Dict[str, Dict[str, float]] = {}

    for line in text.splitlines():
        if (
            "TREND_CANDIDATE_PROBE_SIGNAL:" in line
            or "TREND_CANDIDATE_PROBE_ENQUEUED:" in line
            or "TREND_CANDIDATE_PROBE_FILL:" in line
        ):
            client_order_id = extract_log_field(line, "client_order_id")
            if client_order_id:
                probe_client_order_ids.add(client_order_id)
        if "TREND_CANDIDATE_PROBE_FILL:" in line:
            fill_id = extract_log_field(line, "fill_id")
            if fill_id:
                probe_fill_ids.add(fill_id)

    attribution: Dict[str, object] = {
        "submit": {
            "total": 0,
            "by_symbol": {},
            "by_order_type": {},
            "by_liquidity": {},
            "by_purpose": {},
        },
        "fills": {
            "total": 0,
            "fee_usd": 0.0,
            "notional_abs_usd": 0.0,
            "maker_count": 0,
            "taker_count": 0,
            "unknown_liquidity_count": 0,
            "by_symbol": {},
            "by_liquidity": {},
            "quality_by_symbol": {},
            "probe": new_fill_bucket(),
            "main": new_fill_bucket(),
        },
        "orders": {
            "filtered_cost_by_symbol": {},
            "near_miss_maker_allowed_by_symbol": {},
            "throttled_by_symbol": {},
        },
        "runtime_fill_windows": {
            "count": 0,
            "fills": 0,
            "maker_fills": 0,
            "taker_fills": 0,
            "unknown_fills": 0,
            "realized_net_delta_usd": 0.0,
            "fee_delta_usd": 0.0,
        },
    }

    submit = attribution["submit"]
    fills = attribution["fills"]
    orders = attribution["orders"]
    runtime_fill_windows = attribution["runtime_fill_windows"]
    assert isinstance(submit, dict)
    assert isinstance(fills, dict)
    assert isinstance(orders, dict)
    assert isinstance(runtime_fill_windows, dict)

    for line in text.splitlines():
        if "BYBIT_SUBMIT:" in line:
            submit["total"] = int(submit.get("total", 0)) + 1
            for field_name, counter_name in (
                ("symbol", "by_symbol"),
                ("order_type", "by_order_type"),
                ("liquidity_preference", "by_liquidity"),
                ("purpose", "by_purpose"),
            ):
                counter = submit.get(counter_name)
                if isinstance(counter, dict):
                    bump_counter(counter, extract_log_field(line, field_name))

        if "ORDER_FILTERED_COST:" in line:
            counter = orders.get("filtered_cost_by_symbol")
            if isinstance(counter, dict):
                bump_counter(counter, extract_log_field(line, "symbol"))
        if "ORDER_NEAR_MISS_MAKER_ALLOWED:" in line:
            counter = orders.get("near_miss_maker_allowed_by_symbol")
            if isinstance(counter, dict):
                bump_counter(counter, extract_log_field(line, "symbol"))
        if "ORDER_THROTTLED:" in line:
            counter = orders.get("throttled_by_symbol")
            if isinstance(counter, dict):
                bump_counter(counter, extract_log_field(line, "symbol"))

        if "FILL_APPLIED:" in line:
            symbol = extract_log_field(line, "symbol")
            liquidity = extract_log_field(line, "liquidity")
            fill_id = extract_log_field(line, "fill_id")
            client_order_id = extract_log_field(line, "client_order_id")
            fee_usd = extract_float_log_field(line, "fee") or 0.0
            qty = extract_float_log_field(line, "qty") or 0.0
            price = extract_float_log_field(line, "price") or 0.0
            notional_abs_usd = extract_float_log_field(line, "notional_abs_usd")
            if notional_abs_usd is None:
                if qty > 0.0 and price > 0.0:
                    notional_abs_usd = abs(qty * price)
                else:
                    notional_abs_usd = 0.0

            fills["total"] = int(fills.get("total", 0)) + 1
            fills["fee_usd"] = rounded(float(fills.get("fee_usd", 0.0)) + abs(fee_usd))
            fills["notional_abs_usd"] = rounded(
                float(fills.get("notional_abs_usd", 0.0)) + abs(notional_abs_usd)
            )
            fill_by_symbol = fills.get("by_symbol")
            fill_by_liquidity = fills.get("by_liquidity")
            if isinstance(fill_by_symbol, dict):
                bump_counter(fill_by_symbol, symbol)
            if isinstance(fill_by_liquidity, dict):
                bump_counter(fill_by_liquidity, liquidity)

            liquidity_normalized = str(liquidity or "UNKNOWN").upper()
            if liquidity_normalized == "MAKER":
                fills["maker_count"] = int(fills.get("maker_count", 0)) + 1
            elif liquidity_normalized == "TAKER":
                fills["taker_count"] = int(fills.get("taker_count", 0)) + 1
            else:
                fills["unknown_liquidity_count"] = int(
                    fills.get("unknown_liquidity_count", 0)
                ) + 1

            bucket_name = (
                "probe"
                if (fill_id and fill_id in probe_fill_ids)
                or (client_order_id and client_order_id in probe_client_order_ids)
                else "main"
            )
            bucket = fills.get(bucket_name)
            if isinstance(bucket, dict):
                record_fill(bucket, symbol, liquidity, fee_usd, notional_abs_usd)
            quality_by_symbol = fills.get("quality_by_symbol")
            if isinstance(quality_by_symbol, dict):
                record_symbol_fill_quality(
                    quality_by_symbol,
                    position_state,
                    symbol=symbol,
                    direction=extract_fill_direction(line),
                    qty=qty,
                    price=price,
                    fee_usd=fee_usd,
                    notional_abs_usd=notional_abs_usd,
                    liquidity=liquidity,
                )

        if "RUNTIME_STATUS:" in line:
            window_fills = int(extract_float_log_field(line, "fills") or 0)
            if window_fills > 0:
                runtime_fill_windows["count"] = int(
                    runtime_fill_windows.get("count", 0)
                ) + 1
                runtime_fill_windows["fills"] = int(
                    runtime_fill_windows.get("fills", 0)
                ) + window_fills
                for field in ("maker_fills", "taker_fills", "unknown_fills"):
                    runtime_fill_windows[field] = int(
                        runtime_fill_windows.get(field, 0)
                    ) + int(extract_float_log_field(line, field) or 0)
                for field in ("realized_net_delta_usd", "fee_delta_usd"):
                    runtime_fill_windows[field] = rounded(
                        float(runtime_fill_windows.get(field, 0.0))
                        + float(extract_float_log_field(line, field) or 0.0)
                    )

    return attribution


def _new_exit_capture_symbol_bucket() -> Dict[str, object]:
    return {
        "samples": 0,
        "low_capture_count": 0,
        "path_mfe_bps_sum": 0.0,
        "captured_gross_bps_sum": 0.0,
        "captured_net_bps_sum": 0.0,
        "fee_bps_sum": 0.0,
        "capture_ratio_sum": 0.0,
        "path_mfe_bps_avg": 0.0,
        "captured_gross_bps_avg": 0.0,
        "captured_net_bps_avg": 0.0,
        "fee_bps_avg": 0.0,
        "capture_ratio_avg": 0.0,
        "low_capture_ratio": 0.0,
    }


def extract_exit_capture_samples(text: str) -> Dict[str, object]:
    path_mfe_values: list[float] = []
    captured_gross_values: list[float] = []
    captured_net_values: list[float] = []
    fee_values: list[float] = []
    capture_ratio_values: list[float] = []
    by_symbol: Dict[str, object] = {}
    by_purpose: Dict[str, int] = {}
    low_by_symbol: Dict[str, int] = {}
    low_capture_count = 0

    for line in text.splitlines():
        if "EXIT_CAPTURE_SAMPLE:" not in line:
            continue
        symbol = normalize_counter_key(extract_log_field(line, "symbol"))
        purpose = normalize_counter_key(extract_log_field(line, "purpose"))
        path_mfe_bps = float(extract_float_log_field(line, "path_mfe_bps") or 0.0)
        captured_gross_bps = float(
            extract_float_log_field(line, "captured_gross_bps") or 0.0
        )
        captured_net_bps = float(
            extract_float_log_field(line, "captured_net_bps") or 0.0
        )
        fee_bps = float(extract_float_log_field(line, "fee_bps") or 0.0)
        capture_ratio = float(extract_float_log_field(line, "capture_ratio") or 0.0)
        low_capture = (
            str(extract_log_field(line, "low_capture") or "").strip().lower()
            == "true"
        )

        path_mfe_values.append(path_mfe_bps)
        captured_gross_values.append(captured_gross_bps)
        captured_net_values.append(captured_net_bps)
        fee_values.append(fee_bps)
        capture_ratio_values.append(capture_ratio)
        bump_counter(by_purpose, purpose)
        bucket = by_symbol.setdefault(symbol, _new_exit_capture_symbol_bucket())
        if isinstance(bucket, dict):
            bucket["samples"] = int(bucket.get("samples", 0)) + 1
            bucket["path_mfe_bps_sum"] = rounded(
                float(bucket.get("path_mfe_bps_sum", 0.0)) + path_mfe_bps
            )
            bucket["captured_gross_bps_sum"] = rounded(
                float(bucket.get("captured_gross_bps_sum", 0.0)) + captured_gross_bps
            )
            bucket["captured_net_bps_sum"] = rounded(
                float(bucket.get("captured_net_bps_sum", 0.0)) + captured_net_bps
            )
            bucket["fee_bps_sum"] = rounded(
                float(bucket.get("fee_bps_sum", 0.0)) + fee_bps
            )
            bucket["capture_ratio_sum"] = rounded(
                float(bucket.get("capture_ratio_sum", 0.0)) + capture_ratio
            )
            if low_capture:
                bucket["low_capture_count"] = int(
                    bucket.get("low_capture_count", 0)
                ) + 1
        if low_capture:
            low_capture_count += 1
            bump_counter(low_by_symbol, symbol)

    sample_count = len(path_mfe_values)
    for payload in by_symbol.values():
        if not isinstance(payload, dict):
            continue
        samples = int(payload.get("samples", 0))
        if samples <= 0:
            continue
        payload["path_mfe_bps_avg"] = rounded(
            float(payload.get("path_mfe_bps_sum", 0.0)) / samples
        )
        payload["captured_gross_bps_avg"] = rounded(
            float(payload.get("captured_gross_bps_sum", 0.0)) / samples
        )
        payload["captured_net_bps_avg"] = rounded(
            float(payload.get("captured_net_bps_sum", 0.0)) / samples
        )
        payload["fee_bps_avg"] = rounded(float(payload.get("fee_bps_sum", 0.0)) / samples)
        payload["capture_ratio_avg"] = rounded(
            float(payload.get("capture_ratio_sum", 0.0)) / samples
        )
        payload["low_capture_ratio"] = rounded(
            int(payload.get("low_capture_count", 0)) / samples
        )

    def avg(values: list[float]) -> float:
        return rounded(sum(values) / len(values)) if values else 0.0

    return {
        "sample_count": sample_count,
        "low_capture_count": low_capture_count,
        "low_capture_ratio": rounded(low_capture_count / sample_count)
        if sample_count > 0
        else 0.0,
        "mean_path_mfe_bps": avg(path_mfe_values),
        "mean_captured_gross_bps": avg(captured_gross_values),
        "mean_captured_net_bps": avg(captured_net_values),
        "mean_fee_bps": avg(fee_values),
        "mean_capture_ratio": avg(capture_ratio_values),
        "p25_capture_ratio": rounded(percentile(capture_ratio_values, 0.25)),
        "p50_capture_ratio": rounded(percentile(capture_ratio_values, 0.50)),
        "p75_capture_ratio": rounded(percentile(capture_ratio_values, 0.75)),
        "by_symbol": by_symbol,
        "low_by_symbol": low_by_symbol,
        "by_purpose": by_purpose,
    }


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    clamped_q = min(1.0, max(0.0, q))
    index = min(
        len(sorted_values) - 1, max(0, int(round(clamped_q * (len(sorted_values) - 1))))
    )
    return sorted_values[index]


def extract_runtime_notional_samples(text: str) -> list[tuple[dt.datetime, float]]:
    samples: list[tuple[dt.datetime, float]] = []
    for match in RUNTIME_ACCOUNT_RE.finditer(text):
        try:
            ts = dt.datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S")
            notional = float(match.group("notional"))
        except ValueError:
            continue
        samples.append((ts, notional))
    return samples


def filter_log_since(text: str, cutoff_ts: dt.datetime) -> str:
    lines_out: list[str] = []
    include_line = False
    for raw_line in text.splitlines():
        ts_match = LOG_LINE_TS_RE.search(raw_line)
        if ts_match:
            try:
                line_ts = dt.datetime.strptime(
                    ts_match.group("ts"), "%Y-%m-%d %H:%M:%S"
                )
                include_line = line_ts >= cutoff_ts
            except ValueError:
                pass
        if include_line:
            lines_out.append(raw_line)
    if not lines_out:
        return ""
    return "\n".join(lines_out) + "\n"


def _parse_utc_timestamp(value: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def extract_runtime_timing(text: str) -> Dict[str, object]:
    runtime_timestamps: list[dt.datetime] = []
    for match in RUNTIME_STATUS_TS_RE.finditer(text):
        try:
            runtime_timestamps.append(
                dt.datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S")
            )
        except ValueError:
            continue

    process_start_times: list[dt.datetime] = []
    for match in PROCESS_START_RE.finditer(text):
        parsed = _parse_utc_timestamp(match.group("startup"))
        if parsed is not None:
            process_start_times.append(parsed)
    for match in RUNTIME_STATUS_BOOT_START_RE.finditer(text):
        parsed = _parse_utc_timestamp(match.group("startup"))
        if parsed is not None:
            process_start_times.append(parsed)

    first_runtime = min(runtime_timestamps) if runtime_timestamps else None
    last_runtime = max(runtime_timestamps) if runtime_timestamps else None
    process_start = max(process_start_times) if process_start_times else None
    runtime_window_seconds: Optional[float] = None
    runtime_boot_age_seconds: Optional[float] = None
    if first_runtime is not None and last_runtime is not None:
        runtime_window_seconds = max(0.0, (last_runtime - first_runtime).total_seconds())
    if process_start is not None and last_runtime is not None:
        runtime_boot_age_seconds = max(
            0.0, (last_runtime - process_start).total_seconds()
        )

    return {
        "process_start_utc": (
            process_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            if process_start is not None
            else None
        ),
        "runtime_first_status_utc": (
            first_runtime.strftime("%Y-%m-%dT%H:%M:%SZ")
            if first_runtime is not None
            else None
        ),
        "runtime_last_status_utc": (
            last_runtime.strftime("%Y-%m-%dT%H:%M:%SZ")
            if last_runtime is not None
            else None
        ),
        "runtime_window_seconds": runtime_window_seconds,
        "runtime_boot_age_seconds": runtime_boot_age_seconds,
    }


def extract_runtime_account_series(text: str) -> Dict[str, object]:
    timestamps: list[dt.datetime] = []
    equities: list[float] = []
    drawdowns: list[float] = []
    notionals: list[float] = []
    realized_pnls: list[float] = []
    fees: list[float] = []
    realized_nets: list[float] = []
    account_samples: list[Dict[str, float | dt.datetime]] = []

    for m in RUNTIME_ACCOUNT_SAMPLE_RE.finditer(text):
        try:
            ts = dt.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
            sample = {
                "ts": ts,
                "tick": int(m.group("tick")),
                "equity": float(m.group("equity")),
                "drawdown_pct": float(m.group("drawdown_pct")),
                "notional": float(m.group("notional")),
                "realized_pnl": float(m.group("realized")),
                "fees": float(m.group("fees")),
                "realized_net": float(m.group("net")),
            }
        except ValueError:
            continue
        account_samples.append(sample)
        timestamps.append(ts)
        equities.append(float(sample["equity"]))
        drawdowns.append(float(sample["drawdown_pct"]))
        notionals.append(float(sample["notional"]))
        realized_pnls.append(float(sample["realized_pnl"]))
        fees.append(float(sample["fees"]))
        realized_nets.append(float(sample["realized_net"]))

    if not account_samples:
        for m in RUNTIME_ACCOUNT_RE.finditer(text):
            try:
                timestamps.append(
                    dt.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
                )
                equities.append(float(m.group("equity")))
                drawdowns.append(float(m.group("drawdown_pct")))
                notionals.append(float(m.group("notional")))
            except ValueError:
                continue

        for m in RUNTIME_ACCOUNT_REALIZED_RE.finditer(text):
            try:
                realized_pnls.append(float(m.group("realized")))
                fees.append(float(m.group("fees")))
                realized_nets.append(float(m.group("net")))
            except ValueError:
                continue

    if not equities:
        return {
            "samples": 0,
            "first_equity_usd": None,
            "last_equity_usd": None,
            "equity_change_usd": None,
            "equity_change_pct": None,
            "first_sample_utc": None,
            "last_sample_utc": None,
            "day_start_equity_usd": None,
            "equity_change_vs_day_start_usd": None,
            "equity_change_vs_day_start_pct": None,
            "max_equity_usd_observed": None,
            "peak_to_last_drawdown_pct": None,
            "max_drawdown_pct_observed": None,
            "max_abs_notional_usd_observed": None,
            "first_notional_usd": None,
            "first_abs_notional_usd": None,
            "last_notional_usd": None,
            "last_abs_notional_usd": None,
            "start_flat": None,
            "end_flat": None,
            "fee_samples": 0,
            "first_realized_pnl_usd": None,
            "last_realized_pnl_usd": None,
            "realized_pnl_change_usd": None,
            "first_fee_usd": None,
            "last_fee_usd": None,
            "fee_change_usd": None,
            "fee_change_raw_usd": None,
            "first_realized_net_pnl_usd": None,
            "last_realized_net_pnl_usd": None,
            "realized_net_pnl_change_usd": None,
            "realized_pnl_change_raw_usd": None,
            "realized_net_pnl_change_raw_usd": None,
            "account_counter_reset_count": 0,
            "account_counter_reset_detected": False,
        }

    first_equity = equities[0]
    last_equity = equities[-1]
    equity_change = last_equity - first_equity
    equity_change_pct = None
    if abs(first_equity) > 1e-12:
        equity_change_pct = equity_change / first_equity

    first_ts = timestamps[0]
    last_ts = timestamps[-1]
    current_day = last_ts.date()
    day_start_index = 0
    for idx, ts in enumerate(timestamps):
        if ts.date() == current_day:
            day_start_index = idx
            break
    day_start_equity = equities[day_start_index]
    equity_change_vs_day_start = last_equity - day_start_equity
    equity_change_vs_day_start_pct = None
    if abs(day_start_equity) > 1e-12:
        equity_change_vs_day_start_pct = equity_change_vs_day_start / day_start_equity

    max_equity_observed = max(equities)
    peak_to_last_drawdown_pct = None
    if abs(max_equity_observed) > 1e-12:
        peak_to_last_drawdown_pct = (max_equity_observed - last_equity) / max_equity_observed

    max_drawdown_observed = max(drawdowns) if drawdowns else None
    max_abs_notional_observed = max(abs(x) for x in notionals) if notionals else None
    first_notional = notionals[0] if notionals else None
    first_abs_notional = abs(first_notional) if first_notional is not None else None
    last_notional = notionals[-1] if notionals else None
    last_abs_notional = abs(last_notional) if last_notional is not None else None

    first_realized = realized_pnls[0] if realized_pnls else None
    last_realized = realized_pnls[-1] if realized_pnls else None
    raw_realized_change = (
        (last_realized - first_realized)
        if first_realized is not None and last_realized is not None
        else None
    )
    first_fee = fees[0] if fees else None
    last_fee = fees[-1] if fees else None
    raw_fee_change = (
        (last_fee - first_fee)
        if first_fee is not None and last_fee is not None
        else None
    )
    first_realized_net = realized_nets[0] if realized_nets else None
    last_realized_net = realized_nets[-1] if realized_nets else None
    raw_realized_net_change = (
        (last_realized_net - first_realized_net)
        if first_realized_net is not None and last_realized_net is not None
        else None
    )
    realized_change = raw_realized_change
    fee_change = raw_fee_change
    realized_net_change = raw_realized_net_change
    account_counter_reset_count = 0
    if account_samples:
        segment_start = account_samples[0]
        previous_sample = account_samples[0]
        realized_change = 0.0
        fee_change = 0.0
        realized_net_change = 0.0
        reset_tolerance = 1e-9
        for sample in account_samples[1:]:
            tick_reset = int(sample["tick"]) < int(previous_sample["tick"])
            fee_reset = float(sample["fees"]) + reset_tolerance < float(
                previous_sample["fees"]
            )
            if tick_reset or fee_reset:
                realized_change += float(previous_sample["realized_pnl"]) - float(
                    segment_start["realized_pnl"]
                )
                fee_change += float(previous_sample["fees"]) - float(
                    segment_start["fees"]
                )
                realized_net_change += float(previous_sample["realized_net"]) - float(
                    segment_start["realized_net"]
                )
                account_counter_reset_count += 1
                segment_start = sample
            previous_sample = sample
        realized_change += float(previous_sample["realized_pnl"]) - float(
            segment_start["realized_pnl"]
        )
        fee_change += float(previous_sample["fees"]) - float(segment_start["fees"])
        realized_net_change += float(previous_sample["realized_net"]) - float(
            segment_start["realized_net"]
        )

    return {
        "samples": len(equities),
        "first_equity_usd": first_equity,
        "last_equity_usd": last_equity,
        "equity_change_usd": equity_change,
        "equity_change_pct": equity_change_pct,
        "first_sample_utc": first_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_sample_utc": last_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "day_start_equity_usd": day_start_equity,
        "equity_change_vs_day_start_usd": equity_change_vs_day_start,
        "equity_change_vs_day_start_pct": equity_change_vs_day_start_pct,
        "max_equity_usd_observed": max_equity_observed,
        "peak_to_last_drawdown_pct": peak_to_last_drawdown_pct,
        "max_drawdown_pct_observed": max_drawdown_observed,
        "max_abs_notional_usd_observed": max_abs_notional_observed,
        "first_notional_usd": first_notional,
        "first_abs_notional_usd": first_abs_notional,
        "last_notional_usd": last_notional,
        "last_abs_notional_usd": last_abs_notional,
        "start_flat": bool(first_abs_notional is not None and first_abs_notional <= 1e-6),
        "end_flat": bool(last_abs_notional is not None and last_abs_notional <= 1e-6),
        "fee_samples": len(fees),
        "first_realized_pnl_usd": first_realized,
        "last_realized_pnl_usd": last_realized,
        "realized_pnl_change_usd": realized_change,
        "realized_pnl_change_raw_usd": raw_realized_change,
        "first_fee_usd": first_fee,
        "last_fee_usd": last_fee,
        "fee_change_usd": fee_change,
        "fee_change_raw_usd": raw_fee_change,
        "first_realized_net_pnl_usd": first_realized_net,
        "last_realized_net_pnl_usd": last_realized_net,
        "realized_net_pnl_change_usd": realized_net_change,
        "realized_net_pnl_change_raw_usd": raw_realized_net_change,
        "account_counter_reset_count": account_counter_reset_count,
        "account_counter_reset_detected": account_counter_reset_count > 0,
    }


def extract_strategy_mix_series(text: str) -> Dict[str, float]:
    latest_trend_values: list[float] = []
    latest_defensive_values: list[float] = []
    avg_trend_values: list[float] = []
    avg_defensive_values: list[float] = []
    avg_blended_values: list[float] = []
    sample_values: list[int] = []
    policy_flat_values: list[int] = []

    for m in RUNTIME_STRATEGY_MIX_RE.finditer(text):
        try:
            latest_trend_values.append(float(m.group("latest_trend")))
            latest_defensive_values.append(float(m.group("latest_defensive")))
            avg_trend_values.append(float(m.group("avg_trend")))
            avg_defensive_values.append(float(m.group("avg_defensive")))
            avg_blended_values.append(float(m.group("avg_blended")))
            sample_values.append(int(m.group("samples")))
            policy_flat_values.append(int(m.group("policy_flat_samples") or "0"))
        except ValueError:
            continue

    runtime_count = len(sample_values)
    if runtime_count <= 0:
        return {
            "runtime_count": 0.0,
            "nonzero_window_count": 0.0,
            "policy_flat_window_count": 0.0,
            "defensive_active_count": 0.0,
            "avg_abs_trend_notional": 0.0,
            "avg_abs_defensive_notional": 0.0,
            "avg_abs_blended_notional": 0.0,
            "avg_defensive_share": 0.0,
        }

    nonzero_window_count = sum(1 for x in sample_values if x > 0)
    policy_flat_window_count = sum(1 for x in policy_flat_values if x > 0)
    defensive_active_count = sum(
        1
        for avg_defensive, window_samples in zip(avg_defensive_values, sample_values)
        if window_samples > 0 and avg_defensive > 1e-9
    )
    avg_abs_trend_notional = sum(avg_trend_values) / runtime_count
    avg_abs_defensive_notional = sum(avg_defensive_values) / runtime_count
    avg_abs_blended_notional = sum(avg_blended_values) / runtime_count
    defensive_share_den = avg_abs_trend_notional + avg_abs_defensive_notional
    avg_defensive_share = (
        avg_abs_defensive_notional / defensive_share_den
        if defensive_share_den > 1e-12
        else 0.0
    )

    return {
        "runtime_count": float(runtime_count),
        "nonzero_window_count": float(nonzero_window_count),
        "policy_flat_window_count": float(policy_flat_window_count),
        "defensive_active_count": float(defensive_active_count),
        "avg_abs_trend_notional": avg_abs_trend_notional,
        "avg_abs_defensive_notional": avg_abs_defensive_notional,
        "avg_abs_blended_notional": avg_abs_blended_notional,
        "avg_defensive_share": avg_defensive_share,
    }


def extract_regime_current_counts(text: str) -> Dict[str, int]:
    counts = {"TREND": 0, "RANGE": 0, "EXTREME": 0}
    for m in RUNTIME_REGIME_CURRENT_RE.finditer(text):
        bucket = str(m.group("bucket") or "").upper()
        if bucket in counts:
            counts[bucket] += 1
    return counts


def extract_regime_runtime_diagnostics(text: str) -> Dict[str, Any]:
    trend_threshold_ratios: list[float] = []
    volatility_threshold_ratios: list[float] = []
    current_candidate_count = 0
    current_warmup_candidate_count = 0
    current_pending_trend_confirmation_count = 0
    pending_trend_ticks: list[int] = []
    confirm_ticks_required_values: list[int] = []
    pending_trend_elapsed_ms: list[int] = []
    confirm_elapsed_required_values: list[int] = []
    window_candidate_count = 0
    window_warmup_candidate_count = 0

    for match in RUNTIME_REGIME_CURRENT_DIAGNOSTICS_RE.finditer(text):
        try:
            trend_ratio = float(match.group("trend_threshold_ratio"))
            trend_threshold_ratios.append(trend_ratio)
            if match.group("volatility_threshold_ratio") is not None:
                volatility_threshold_ratios.append(
                    float(match.group("volatility_threshold_ratio"))
                )
        except ValueError:
            continue
        if str(match.group("trend_candidate")).lower() == "true":
            current_candidate_count += 1
        warmup_candidate = (
            str(match.group("warmup_trend_candidate") or "").lower() == "true"
        )
        if not warmup_candidate:
            warmup_candidate = (
                str(match.group("warmup") or "").lower() == "true"
                and trend_ratio >= TREND_CANDIDATE_MIN_THRESHOLD_RATIO
            )
        if warmup_candidate:
            current_warmup_candidate_count += 1
        if (
            str(match.group("pending_trend_confirmation") or "").lower()
            == "true"
        ):
            current_pending_trend_confirmation_count += 1
            try:
                pending_trend_ticks.append(int(match.group("pending_regime_ticks")))
                confirm_ticks_required_values.append(
                    int(match.group("confirm_ticks_required"))
                )
                pending_trend_elapsed_ms.append(
                    int(match.group("pending_regime_elapsed_ms"))
                )
                confirm_elapsed_required_values.append(
                    int(match.group("confirm_elapsed_ms_required"))
                )
            except (TypeError, ValueError):
                pass

    for match in RUNTIME_REGIME_WINDOW_CANDIDATE_RE.finditer(text):
        try:
            if int(match.group("trend_candidate_ticks")) > 0:
                window_candidate_count += 1
            if (
                match.group("warmup_trend_candidate_ticks") is not None
                and int(match.group("warmup_trend_candidate_ticks")) > 0
            ):
                window_warmup_candidate_count += 1
        except ValueError:
            continue

    if not trend_threshold_ratios:
        return {
            "runtime_count": 0,
            "current_candidate_count": 0,
            "current_warmup_candidate_count": 0,
            "current_pending_trend_confirmation_count": 0,
            "window_candidate_count": window_candidate_count,
            "window_warmup_candidate_count": window_warmup_candidate_count,
            "trend_threshold_ratio_avg": 0.0,
            "trend_threshold_ratio_max": 0.0,
            "volatility_threshold_ratio_avg": 0.0,
            "volatility_threshold_ratio_max": 0.0,
            "pending_trend_confirmation_ticks_max": 0,
            "confirm_ticks_required_max": 0,
            "pending_trend_confirmation_elapsed_ms_max": 0,
            "confirm_elapsed_ms_required_max": 0,
        }

    return {
        "runtime_count": len(trend_threshold_ratios),
        "current_candidate_count": current_candidate_count,
        "current_warmup_candidate_count": current_warmup_candidate_count,
        "current_pending_trend_confirmation_count": (
            current_pending_trend_confirmation_count
        ),
        "window_candidate_count": window_candidate_count,
        "window_warmup_candidate_count": window_warmup_candidate_count,
        "trend_threshold_ratio_avg": sum(trend_threshold_ratios)
        / len(trend_threshold_ratios),
        "trend_threshold_ratio_max": max(trend_threshold_ratios),
        "volatility_threshold_ratio_avg": (
            sum(volatility_threshold_ratios) / len(volatility_threshold_ratios)
            if volatility_threshold_ratios
            else 0.0
        ),
        "volatility_threshold_ratio_max": max(volatility_threshold_ratios)
        if volatility_threshold_ratios
        else 0.0,
        "pending_trend_confirmation_ticks_max": max(pending_trend_ticks)
        if pending_trend_ticks
        else 0,
        "confirm_ticks_required_max": max(confirm_ticks_required_values)
        if confirm_ticks_required_values
        else 0,
        "pending_trend_confirmation_elapsed_ms_max": max(pending_trend_elapsed_ms)
        if pending_trend_elapsed_ms
        else 0,
        "confirm_elapsed_ms_required_max": max(confirm_elapsed_required_values)
        if confirm_elapsed_required_values
        else 0,
    }


def extract_regime_change_series(text: str) -> Dict[str, Any]:
    abs_trend_strength_values: list[float] = []
    abs_instant_return_values: list[float] = []
    volatility_values: list[float] = []
    trend_threshold_ratios: list[float] = []
    warmup_trend_threshold_ratios: list[float] = []
    nonwarmup_trend_threshold_ratios: list[float] = []
    volatility_threshold_ratios: list[float] = []
    bucket_counts = {"TREND": 0, "RANGE": 0, "EXTREME": 0}
    trend_symbols: set[str] = set()
    trend_candidate_count = 0
    trend_candidate_symbols: set[str] = set()
    warmup_trend_candidate_count = 0
    warmup_trend_candidate_symbols: set[str] = set()
    pending_trend_confirmation_count = 0
    pending_trend_confirmation_symbols: set[str] = set()
    pending_trend_confirmation_ticks: list[int] = []
    pending_trend_confirmation_elapsed_ms: list[int] = []
    confirm_ticks_required_values: list[int] = []
    confirm_elapsed_ms_required_values: list[int] = []

    for match in REGIME_CHANGE_RE.finditer(text):
        bucket = str(match.group("bucket") or "").upper()
        symbol = str(match.group("symbol") or "")
        is_warmup = str(match.group("warmup") or "").lower() == "true"
        trend_ratio: float | None = None
        if bucket in bucket_counts:
            bucket_counts[bucket] += 1
        if bucket == "TREND":
            trend_symbols.add(symbol)
        try:
            abs_instant_return_values.append(abs(float(match.group("instant_return"))))
            abs_trend_strength_values.append(abs(float(match.group("trend_strength"))))
            volatility_values.append(float(match.group("volatility")))
            if match.group("trend_threshold_ratio") is not None:
                trend_ratio = float(match.group("trend_threshold_ratio"))
                trend_threshold_ratios.append(trend_ratio)
                if is_warmup:
                    warmup_trend_threshold_ratios.append(trend_ratio)
                else:
                    nonwarmup_trend_threshold_ratios.append(trend_ratio)
            if match.group("volatility_threshold_ratio") is not None:
                volatility_threshold_ratios.append(
                    float(match.group("volatility_threshold_ratio"))
                )
        except ValueError:
            continue
        if str(match.group("trend_candidate") or "").lower() == "true":
            trend_candidate_count += 1
            if symbol:
                trend_candidate_symbols.add(symbol)
        explicit_warmup_candidate = (
            str(match.group("warmup_trend_candidate") or "").lower() == "true"
        )
        inferred_warmup_candidate = (
            is_warmup
            and bucket == "RANGE"
            and trend_ratio is not None
            and trend_ratio >= TREND_CANDIDATE_MIN_THRESHOLD_RATIO
        )
        if explicit_warmup_candidate or inferred_warmup_candidate:
            warmup_trend_candidate_count += 1
            if symbol:
                warmup_trend_candidate_symbols.add(symbol)
        if (
            str(match.group("pending_trend_confirmation") or "").lower()
            == "true"
        ):
            pending_trend_confirmation_count += 1
            if symbol:
                pending_trend_confirmation_symbols.add(symbol)
            try:
                pending_trend_confirmation_ticks.append(
                    int(match.group("pending_regime_ticks"))
                )
                pending_trend_confirmation_elapsed_ms.append(
                    int(match.group("pending_regime_elapsed_ms"))
                )
                confirm_ticks_required_values.append(
                    int(match.group("confirm_ticks_required"))
                )
                confirm_elapsed_ms_required_values.append(
                    int(match.group("confirm_elapsed_ms_required"))
                )
            except (TypeError, ValueError):
                pass

    runtime_count = len(abs_trend_strength_values)
    if runtime_count <= 0:
        return {
            "runtime_count": 0.0,
            "trend_strength_abs_p50": 0.0,
            "trend_strength_abs_p95": 0.0,
            "trend_strength_abs_p99": 0.0,
            "trend_strength_abs_max": 0.0,
            "instant_return_abs_p50": 0.0,
            "instant_return_abs_p95": 0.0,
            "instant_return_abs_p99": 0.0,
            "instant_return_abs_max": 0.0,
            "volatility_level_p50": 0.0,
            "volatility_level_p95": 0.0,
            "volatility_level_p99": 0.0,
            "volatility_level_max": 0.0,
            "trend_count": 0,
            "range_count": 0,
            "extreme_count": 0,
            "trend_symbol_count": 0,
            "trend_symbols": [],
            "trend_candidate_count": 0,
            "trend_candidate_symbol_count": 0,
            "trend_candidate_symbols": [],
            "warmup_trend_candidate_count": 0,
            "warmup_trend_candidate_symbol_count": 0,
            "warmup_trend_candidate_symbols": [],
            "pending_trend_confirmation_count": 0,
            "pending_trend_confirmation_symbol_count": 0,
            "pending_trend_confirmation_symbols": [],
            "pending_trend_confirmation_ticks_max": 0,
            "pending_trend_confirmation_elapsed_ms_max": 0,
            "confirm_ticks_required_max": 0,
            "confirm_elapsed_ms_required_max": 0,
            "trend_threshold_ratio_p50": 0.0,
            "trend_threshold_ratio_p95": 0.0,
            "trend_threshold_ratio_p99": 0.0,
            "trend_threshold_ratio_max": 0.0,
            "warmup_trend_threshold_ratio_max": 0.0,
            "nonwarmup_trend_threshold_ratio_max": 0.0,
            "volatility_threshold_ratio_p50": 0.0,
            "volatility_threshold_ratio_p95": 0.0,
            "volatility_threshold_ratio_p99": 0.0,
            "volatility_threshold_ratio_max": 0.0,
        }

    return {
        "runtime_count": float(runtime_count),
        "trend_strength_abs_p50": percentile(abs_trend_strength_values, 0.50),
        "trend_strength_abs_p95": percentile(abs_trend_strength_values, 0.95),
        "trend_strength_abs_p99": percentile(abs_trend_strength_values, 0.99),
        "trend_strength_abs_max": max(abs_trend_strength_values),
        "instant_return_abs_p50": percentile(abs_instant_return_values, 0.50),
        "instant_return_abs_p95": percentile(abs_instant_return_values, 0.95),
        "instant_return_abs_p99": percentile(abs_instant_return_values, 0.99),
        "instant_return_abs_max": max(abs_instant_return_values),
        "volatility_level_p50": percentile(volatility_values, 0.50),
        "volatility_level_p95": percentile(volatility_values, 0.95),
        "volatility_level_p99": percentile(volatility_values, 0.99),
        "volatility_level_max": max(volatility_values),
        "trend_count": bucket_counts["TREND"],
        "range_count": bucket_counts["RANGE"],
        "extreme_count": bucket_counts["EXTREME"],
        "trend_symbol_count": len(trend_symbols),
        "trend_symbols": sorted(x for x in trend_symbols if x),
        "trend_candidate_count": trend_candidate_count,
        "trend_candidate_symbol_count": len(trend_candidate_symbols),
        "trend_candidate_symbols": sorted(x for x in trend_candidate_symbols if x),
        "warmup_trend_candidate_count": warmup_trend_candidate_count,
        "warmup_trend_candidate_symbol_count": len(warmup_trend_candidate_symbols),
        "warmup_trend_candidate_symbols": sorted(
            x for x in warmup_trend_candidate_symbols if x
        ),
        "pending_trend_confirmation_count": pending_trend_confirmation_count,
        "pending_trend_confirmation_symbol_count": len(
            pending_trend_confirmation_symbols
        ),
        "pending_trend_confirmation_symbols": sorted(
            x for x in pending_trend_confirmation_symbols if x
        ),
        "pending_trend_confirmation_ticks_max": max(
            pending_trend_confirmation_ticks
        )
        if pending_trend_confirmation_ticks
        else 0,
        "pending_trend_confirmation_elapsed_ms_max": max(
            pending_trend_confirmation_elapsed_ms
        )
        if pending_trend_confirmation_elapsed_ms
        else 0,
        "confirm_ticks_required_max": max(confirm_ticks_required_values)
        if confirm_ticks_required_values
        else 0,
        "confirm_elapsed_ms_required_max": max(confirm_elapsed_ms_required_values)
        if confirm_elapsed_ms_required_values
        else 0,
        "trend_threshold_ratio_p50": percentile(trend_threshold_ratios, 0.50),
        "trend_threshold_ratio_p95": percentile(trend_threshold_ratios, 0.95),
        "trend_threshold_ratio_p99": percentile(trend_threshold_ratios, 0.99),
        "trend_threshold_ratio_max": max(trend_threshold_ratios)
        if trend_threshold_ratios
        else 0.0,
        "warmup_trend_threshold_ratio_max": max(warmup_trend_threshold_ratios)
        if warmup_trend_threshold_ratios
        else 0.0,
        "nonwarmup_trend_threshold_ratio_max": max(nonwarmup_trend_threshold_ratios)
        if nonwarmup_trend_threshold_ratios
        else 0.0,
        "volatility_threshold_ratio_p50": percentile(
            volatility_threshold_ratios, 0.50
        ),
        "volatility_threshold_ratio_p95": percentile(
            volatility_threshold_ratios, 0.95
        ),
        "volatility_threshold_ratio_p99": percentile(
            volatility_threshold_ratios, 0.99
        ),
        "volatility_threshold_ratio_max": max(volatility_threshold_ratios)
        if volatility_threshold_ratios
        else 0.0,
    }


def extract_execution_window_series(text: str) -> Dict[str, float]:
    def parse_kv_map(raw: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for item in raw.split(","):
            token = item.strip()
            if not token or "=" not in token:
                continue
            key, value = token.split("=", 1)
            out[key.strip()] = value.strip()
        return out

    def map_float(raw_map: Dict[str, str], key: str, default: float = 0.0) -> float:
        raw = raw_map.get(key)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    filtered_cost_ratios: list[float] = []
    filtered_cost_near_miss_ratios: list[float] = []
    passed_cost_near_miss_ratios: list[float] = []
    entry_edge_gap_avg_bps_values: list[float] = []
    realized_net_per_fills: list[float] = []
    fee_bps_per_fills: list[float] = []
    maker_fills: list[float] = []
    taker_fills: list[float] = []
    unknown_fills: list[float] = []
    explicit_liquidity_fills: list[float] = []
    fee_sign_fallback_fills: list[float] = []
    unknown_fill_ratios: list[float] = []
    explicit_liquidity_fill_ratios: list[float] = []
    fee_sign_fallback_fill_ratios: list[float] = []
    maker_fee_bps_values: list[float] = []
    taker_fee_bps_values: list[float] = []
    maker_fill_ratios: list[float] = []
    liquidity_source_runtime_count = 0

    for m in RUNTIME_EXECUTION_WINDOW_RE.finditer(text):
        values = parse_kv_map(m.group("body"))
        if "filtered_cost_ratio" not in values:
            continue
        filtered_cost_ratios.append(map_float(values, "filtered_cost_ratio", 0.0))
        filtered_cost_near_miss_ratios.append(
            map_float(values, "filtered_cost_near_miss_ratio", 0.0)
        )
        passed_cost_near_miss_ratios.append(
            map_float(values, "passed_cost_near_miss_ratio", 0.0)
        )
        entry_edge_gap_avg_bps_values.append(
            map_float(values, "entry_edge_gap_avg_bps", 0.0)
        )
        realized_net_per_fills.append(map_float(values, "realized_net_per_fill", 0.0))
        fee_bps_per_fills.append(map_float(values, "fee_bps_per_fill", 0.0))

        maker_fill_value = map_float(values, "maker_fills", 0.0)
        taker_fill_value = map_float(values, "taker_fills", 0.0)
        unknown_fill_value = map_float(values, "unknown_fills", 0.0)
        maker_fills.append(maker_fill_value)
        taker_fills.append(taker_fill_value)
        unknown_fills.append(unknown_fill_value)

        explicit_fill_value = map_float(values, "explicit_liquidity_fills", 0.0)
        fallback_fill_value = map_float(values, "fee_sign_fallback_fills", 0.0)
        explicit_liquidity_fills.append(explicit_fill_value)
        fee_sign_fallback_fills.append(fallback_fill_value)
        if "explicit_liquidity_fills" in values:
            liquidity_source_runtime_count += 1

        total_liquidity_samples = maker_fill_value + taker_fill_value + unknown_fill_value
        unknown_fill_ratios.append(
            map_float(
                values,
                "unknown_fill_ratio",
                (unknown_fill_value / total_liquidity_samples)
                if total_liquidity_samples > 0
                else 0.0,
            )
        )
        explicit_liquidity_fill_ratios.append(
            map_float(values, "explicit_liquidity_fill_ratio", 0.0)
        )
        fee_sign_fallback_fill_ratios.append(
            map_float(values, "fee_sign_fallback_fill_ratio", 0.0)
        )
        maker_fee_bps_values.append(map_float(values, "maker_fee_bps", 0.0))
        taker_fee_bps_values.append(map_float(values, "taker_fee_bps", 0.0))
        maker_fill_ratios.append(map_float(values, "maker_fill_ratio", 0.0))

    runtime_count = len(filtered_cost_ratios)
    if runtime_count <= 0:
        return {
            "runtime_count": 0.0,
            "filtered_cost_ratio_avg": 0.0,
            "filtered_cost_ratio_latest": 0.0,
            "filtered_cost_near_miss_ratio_avg": 0.0,
            "filtered_cost_near_miss_ratio_latest": 0.0,
            "passed_cost_near_miss_ratio_avg": 0.0,
            "passed_cost_near_miss_ratio_latest": 0.0,
            "entry_edge_gap_avg_bps_avg": 0.0,
            "realized_net_per_fill_avg": 0.0,
            "fee_bps_per_fill_avg": 0.0,
            "maker_fills_avg": 0.0,
            "taker_fills_avg": 0.0,
            "unknown_fills_avg": 0.0,
            "explicit_liquidity_fills_avg": 0.0,
            "fee_sign_fallback_fills_avg": 0.0,
            "unknown_fill_ratio_avg": 0.0,
            "explicit_liquidity_fill_ratio_avg": 0.0,
            "fee_sign_fallback_fill_ratio_avg": 0.0,
            "maker_fee_bps_avg": 0.0,
            "taker_fee_bps_avg": 0.0,
            "maker_fill_ratio_avg": 0.0,
            "liquidity_source_runtime_count": 0.0,
        }

    return {
        "runtime_count": float(runtime_count),
        "filtered_cost_ratio_avg": sum(filtered_cost_ratios) / runtime_count,
        "filtered_cost_ratio_latest": filtered_cost_ratios[-1],
        "filtered_cost_near_miss_ratio_avg": sum(filtered_cost_near_miss_ratios)
        / runtime_count,
        "filtered_cost_near_miss_ratio_latest": filtered_cost_near_miss_ratios[-1],
        "passed_cost_near_miss_ratio_avg": sum(passed_cost_near_miss_ratios)
        / runtime_count,
        "passed_cost_near_miss_ratio_latest": passed_cost_near_miss_ratios[-1],
        "entry_edge_gap_avg_bps_avg": sum(entry_edge_gap_avg_bps_values)
        / runtime_count,
        "realized_net_per_fill_avg": sum(realized_net_per_fills) / runtime_count,
        "fee_bps_per_fill_avg": sum(fee_bps_per_fills) / runtime_count,
        "maker_fills_avg": sum(maker_fills) / runtime_count,
        "taker_fills_avg": sum(taker_fills) / runtime_count,
        "unknown_fills_avg": sum(unknown_fills) / runtime_count,
        "explicit_liquidity_fills_avg": sum(explicit_liquidity_fills) / runtime_count,
        "fee_sign_fallback_fills_avg": sum(fee_sign_fallback_fills) / runtime_count,
        "unknown_fill_ratio_avg": sum(unknown_fill_ratios) / runtime_count,
        "explicit_liquidity_fill_ratio_avg": sum(explicit_liquidity_fill_ratios) / runtime_count,
        "fee_sign_fallback_fill_ratio_avg": sum(fee_sign_fallback_fill_ratios) / runtime_count,
        "maker_fee_bps_avg": sum(maker_fee_bps_values) / runtime_count,
        "taker_fee_bps_avg": sum(taker_fee_bps_values) / runtime_count,
        "maker_fill_ratio_avg": sum(maker_fill_ratios) / runtime_count,
        "liquidity_source_runtime_count": float(liquidity_source_runtime_count),
    }


def extract_execution_quality_guard_series(text: str) -> Dict[str, float]:
    active_flags: list[float] = []
    enabled_flags: list[float] = []
    applied_penalty_bps: list[float] = []
    bad_streaks: list[int] = []
    good_streaks: list[int] = []
    no_fill_windows: list[int] = []
    symbol_active_counts: list[int] = []
    symbol_state_counts: list[int] = []

    for m in RUNTIME_EXECUTION_QUALITY_GUARD_RE.finditer(text):
        active_flags.append(1.0 if m.group("active") == "true" else 0.0)
        enabled_flags.append(1.0 if m.group("enabled") == "true" else 0.0)
        try:
            applied_penalty_bps.append(float(m.group("applied_penalty_bps")))
            bad_streaks.append(int(m.group("bad_streak")))
            good_streaks.append(int(m.group("good_streak")))
            no_fill_windows.append(int(m.group("no_fill_windows")))
            if m.group("symbol_active_count") is not None:
                symbol_active_counts.append(int(m.group("symbol_active_count")))
            if m.group("symbol_state_count") is not None:
                symbol_state_counts.append(int(m.group("symbol_state_count")))
        except ValueError:
            continue

    runtime_count = len(active_flags)
    if runtime_count <= 0:
        return {
            "runtime_count": 0.0,
            "enabled_count": 0.0,
            "active_count": 0.0,
            "active_ratio": 0.0,
            "applied_penalty_bps_avg": 0.0,
            "bad_streak_max": 0.0,
            "good_streak_max": 0.0,
            "no_fill_windows_max": 0.0,
            "symbol_active_count_max": 0.0,
            "symbol_active_count_latest": 0.0,
            "symbol_state_count_max": 0.0,
            "symbol_state_count_latest": 0.0,
        }

    return {
        "runtime_count": float(runtime_count),
        "enabled_count": float(sum(enabled_flags)),
        "active_count": float(sum(active_flags)),
        "active_ratio": sum(active_flags) / runtime_count,
        "applied_penalty_bps_avg": sum(applied_penalty_bps) / runtime_count,
        "bad_streak_max": float(max(bad_streaks) if bad_streaks else 0),
        "good_streak_max": float(max(good_streaks) if good_streaks else 0),
        "no_fill_windows_max": float(max(no_fill_windows) if no_fill_windows else 0),
        "symbol_active_count_max": float(
            max(symbol_active_counts) if symbol_active_counts else 0
        ),
        "symbol_active_count_latest": float(
            symbol_active_counts[-1] if symbol_active_counts else 0
        ),
        "symbol_state_count_max": float(
            max(symbol_state_counts) if symbol_state_counts else 0
        ),
        "symbol_state_count_latest": float(
            symbol_state_counts[-1] if symbol_state_counts else 0
        ),
    }


def extract_entry_gate_series(text: str) -> Dict[str, float]:
    near_miss_tolerance_values: list[float] = []
    observed_filtered_ratio_values: list[float] = []
    observed_near_miss_ratio_values: list[float] = []
    observed_near_miss_allowed_ratio_values: list[float] = []

    for m in RUNTIME_ENTRY_GATE_RE.finditer(text):
        try:
            near_miss_tolerance_values.append(
                float(m.group("near_miss_tolerance_bps"))
            )
            observed_filtered_ratio_values.append(
                float(m.group("observed_filtered_ratio"))
            )
            observed_near_miss_ratio_values.append(
                float(m.group("observed_near_miss_ratio"))
            )
            observed_near_miss_allowed_ratio_values.append(
                float(m.group("observed_near_miss_allowed_ratio") or 0.0)
            )
        except ValueError:
            continue

    runtime_count = len(near_miss_tolerance_values)
    if runtime_count <= 0:
        return {
            "runtime_count": 0.0,
            "near_miss_tolerance_bps_avg": 0.0,
            "observed_filtered_ratio_avg": 0.0,
            "observed_near_miss_ratio_avg": 0.0,
            "observed_near_miss_ratio_latest": 0.0,
            "observed_near_miss_allowed_ratio_avg": 0.0,
            "observed_near_miss_allowed_ratio_latest": 0.0,
        }
    return {
        "runtime_count": float(runtime_count),
        "near_miss_tolerance_bps_avg": sum(near_miss_tolerance_values) / runtime_count,
        "observed_filtered_ratio_avg": sum(observed_filtered_ratio_values)
        / runtime_count,
        "observed_near_miss_ratio_avg": sum(observed_near_miss_ratio_values)
        / runtime_count,
        "observed_near_miss_ratio_latest": observed_near_miss_ratio_values[-1],
        "observed_near_miss_allowed_ratio_avg": sum(
            observed_near_miss_allowed_ratio_values
        )
        / runtime_count,
        "observed_near_miss_allowed_ratio_latest": observed_near_miss_allowed_ratio_values[-1],
    }


def extract_concentration_series(text: str) -> Dict[str, float]:
    top1_share_values: list[float] = []
    symbol_count_values: list[int] = []
    top1_abs_notional_values: list[float] = []
    gross_notional_values: list[float] = []

    for m in RUNTIME_CONCENTRATION_RE.finditer(text):
        try:
            top1_share_values.append(float(m.group("top1_share")))
            symbol_count_values.append(int(m.group("symbol_count")))
            top1_abs_notional_values.append(float(m.group("top1_abs_notional_usd")))
            gross_notional_values.append(float(m.group("gross_notional_usd")))
        except ValueError:
            continue

    runtime_count = len(top1_share_values)
    if runtime_count <= 0:
        return {
            "runtime_count": 0.0,
            "top1_share_avg": 0.0,
            "top1_share_max": 0.0,
            "high_concentration_count": 0.0,
            "symbol_count_avg": 0.0,
            "top1_abs_notional_avg": 0.0,
            "gross_notional_avg": 0.0,
        }
    high_concentration_count = sum(
        1 for share in top1_share_values if share >= MAX_TOP1_CONCENTRATION_SHARE_WARN
    )
    return {
        "runtime_count": float(runtime_count),
        "top1_share_avg": sum(top1_share_values) / runtime_count,
        "top1_share_max": max(top1_share_values),
        "high_concentration_count": float(high_concentration_count),
        "symbol_count_avg": sum(symbol_count_values) / runtime_count,
        "top1_abs_notional_avg": sum(top1_abs_notional_values) / runtime_count,
        "gross_notional_avg": sum(gross_notional_values) / runtime_count,
    }


def extract_entry_edge_adjust_series(text: str) -> Dict[str, float]:
    regime_adjust_values: list[float] = []
    volatility_adjust_values: list[float] = []
    liquidity_adjust_values: list[float] = []
    concentration_adjust_values: list[float] = []

    for m in RUNTIME_ENTRY_EDGE_ADJUST_RE.finditer(text):
        try:
            regime_adjust_values.append(float(m.group("regime_adjust")))
            volatility_adjust_values.append(float(m.group("volatility_adjust")))
            liquidity_adjust_values.append(float(m.group("liquidity_adjust")))
            concentration_adjust_values.append(
                float(m.group("concentration_adjust") or 0.0)
            )
        except ValueError:
            continue

    runtime_count = len(regime_adjust_values)
    if runtime_count <= 0:
        return {
            "runtime_count": 0.0,
            "regime_adjust_bps_avg": 0.0,
            "volatility_adjust_bps_avg": 0.0,
            "liquidity_adjust_bps_avg": 0.0,
            "concentration_adjust_bps_avg": 0.0,
        }
    return {
        "runtime_count": float(runtime_count),
        "regime_adjust_bps_avg": sum(regime_adjust_values) / runtime_count,
        "volatility_adjust_bps_avg": sum(volatility_adjust_values) / runtime_count,
        "liquidity_adjust_bps_avg": sum(liquidity_adjust_values) / runtime_count,
        "concentration_adjust_bps_avg": sum(concentration_adjust_values)
        / runtime_count,
    }


def extract_reconcile_runtime_series(text: str) -> Dict[str, float]:
    anomaly_streaks: list[int] = []
    reduce_only_flags: list[float] = []

    for m in RUNTIME_RECONCILE_RUNTIME_RE.finditer(text):
        reduce_only_flags.append(
            1.0 if m.group("anomaly_reduce_only") == "true" else 0.0
        )
        try:
            anomaly_streaks.append(int(m.group("anomaly_streak")))
        except ValueError:
            continue

    runtime_count = len(reduce_only_flags)
    if runtime_count <= 0:
        return {
            "runtime_count": 0.0,
            "anomaly_streak_nonzero_count": 0.0,
            "anomaly_streak_max": 0.0,
            "anomaly_reduce_only_true_count": 0.0,
            "anomaly_reduce_only_true_ratio": 0.0,
        }

    anomaly_nonzero_count = sum(1 for x in anomaly_streaks if x > 0)
    reduce_only_true_count = sum(reduce_only_flags)
    return {
        "runtime_count": float(runtime_count),
        "anomaly_streak_nonzero_count": float(anomaly_nonzero_count),
        "anomaly_streak_max": float(max(anomaly_streaks) if anomaly_streaks else 0),
        "anomaly_reduce_only_true_count": float(reduce_only_true_count),
        "anomaly_reduce_only_true_ratio": reduce_only_true_count / runtime_count,
    }


def extract_feature_scale_diagnostics(text: str) -> Dict[str, object]:
    feature_line_count = 0
    feature_value_count = 0
    nonfinite_count = 0
    large_abs_value_count = 0
    large_abs_line_count = 0
    max_abs_value = 0.0
    max_abs_feature = ""
    large_abs_by_feature: Dict[str, int] = {}
    max_abs_by_feature: Dict[str, float] = {}
    nan_skip_count = 0
    nan_skip_by_feature: Dict[str, int] = {}
    nan_skip_by_symbol: Dict[str, int] = {}
    nan_skip_examples: List[Dict[str, object]] = []

    for line in text.splitlines():
        if "INTEGRATOR_SKIP: NaN feature detected" in line:
            nan_skip_count += 1
            fields = {
                item.group("name"): item.group("value")
                for item in INTEGRATOR_NAN_SKIP_FIELD_RE.finditer(line)
            }
            if "feature_name" not in fields or "feature_index" not in fields:
                legacy = INTEGRATOR_NAN_SKIP_LEGACY_RE.search(line)
                if legacy:
                    fields.setdefault("feature_index", legacy.group("feature_index"))
                    fields.setdefault("feature_name", legacy.group("feature_name"))
            feature_name = fields.get("feature_name", "unknown")
            symbol = fields.get("symbol", "unknown")
            nan_skip_by_feature[feature_name] = int(nan_skip_by_feature.get(feature_name, 0)) + 1
            nan_skip_by_symbol[symbol] = int(nan_skip_by_symbol.get(symbol, 0)) + 1
            if len(nan_skip_examples) < 5:
                example: Dict[str, object] = {
                    "feature_name": feature_name,
                    "symbol": symbol,
                }
                for key in (
                    "feature_index",
                    "raw_value",
                    "regime",
                    "bucket",
                    "raw_regime",
                    "raw_bucket",
                    "model_version",
                    "skip_count",
                ):
                    if key in fields:
                        example[key] = fields[key]
                nan_skip_examples.append(example)

        match = FEATURES_RE.search(line)
        if not match:
            continue
        feature_line_count += 1
        line_has_large = False
        for item in FEATURE_VALUE_RE.finditer(match.group("body")):
            name = item.group("name")
            raw_value = item.group("value")
            feature_value_count += 1
            try:
                value = float(raw_value)
            except ValueError:
                nonfinite_count += 1
                continue
            if not math.isfinite(value):
                nonfinite_count += 1
                continue
            abs_value = abs(value)
            current_max = float(max_abs_by_feature.get(name, 0.0))
            if abs_value > current_max:
                max_abs_by_feature[name] = rounded(abs_value)
            if abs_value > max_abs_value:
                max_abs_value = abs_value
                max_abs_feature = name
            if abs_value >= FEATURE_LARGE_ABS_WARN_THRESHOLD:
                large_abs_value_count += 1
                line_has_large = True
                large_abs_by_feature[name] = int(large_abs_by_feature.get(name, 0)) + 1
        if line_has_large:
            large_abs_line_count += 1

    large_abs_ratio = (
        large_abs_line_count / feature_line_count if feature_line_count > 0 else 0.0
    )
    return {
        "feature_line_count": feature_line_count,
        "feature_value_count": feature_value_count,
        "feature_nonfinite_count": nonfinite_count,
        "feature_large_abs_value_count": large_abs_value_count,
        "feature_large_abs_line_count": large_abs_line_count,
        "feature_large_abs_line_ratio": rounded(large_abs_ratio),
        "feature_max_abs_value": rounded(max_abs_value),
        "feature_max_abs_feature": max_abs_feature,
        "feature_large_abs_by_feature": large_abs_by_feature,
        "feature_max_abs_by_feature": max_abs_by_feature,
        "integrator_nan_feature_skip_count": nan_skip_count,
        "integrator_nan_feature_skip_by_feature": nan_skip_by_feature,
        "integrator_nan_feature_skip_by_symbol": nan_skip_by_symbol,
        "integrator_nan_feature_skip_examples": nan_skip_examples,
    }


def assess(
    text: str,
    stage: StageRule,
    min_runtime_status: int,
    s5_min_effective_updates: int = DEFAULT_S5_MIN_EFFECTIVE_UPDATES,
    s5_min_realized_net_per_fill_usd: float = DEFAULT_S5_MIN_REALIZED_NET_PER_FILL_USD,
    s5_min_realized_net_per_fill_windows: int = DEFAULT_S5_MIN_REALIZED_NET_PER_FILL_WINDOWS,
    s5_min_fill_windows: int = DEFAULT_S5_MIN_FILL_WINDOWS,
    s5_min_equity_change_usd: Optional[float] = DEFAULT_S5_MIN_EQUITY_CHANGE_USD,
    s5_min_equity_change_samples: int = DEFAULT_S5_MIN_EQUITY_CHANGE_SAMPLES,
    s5_max_equity_vs_realized_gap_usd: Optional[float] = DEFAULT_S5_MAX_EQUITY_VS_REALIZED_GAP_USD,
    s5_protection_enabled: bool = DEFAULT_S5_PROTECTION_ENABLED,
    s5_profit_protection_enabled: bool = DEFAULT_S5_PROFIT_PROTECTION_ENABLED,
    s5_max_protective_order_missing_count: int = DEFAULT_S5_MAX_PROTECTIVE_ORDER_MISSING_COUNT,
    s5_min_trend_runtime_windows: int = DEFAULT_S5_MIN_TREND_RUNTIME_WINDOWS,
) -> Dict[str, object]:
    original_text = text
    flat_start_rebased = False
    flat_start_rebase_cutoff_utc = None
    flat_start_rebase_fallback = False
    if stage.require_flat_start:
        notional_samples = extract_runtime_notional_samples(text)
        if notional_samples:
            first_abs_notional = abs(notional_samples[0][1])
            if first_abs_notional > stage.max_start_abs_notional_usd:
                first_flat_sample = next(
                    (
                        sample
                        for sample in notional_samples
                        if abs(sample[1]) <= stage.max_start_abs_notional_usd
                    ),
                    None,
                )
                if first_flat_sample is not None:
                    rebased_text = filter_log_since(text, first_flat_sample[0])
                    if rebased_text.strip():
                        text = rebased_text
                        flat_start_rebased = True
                        flat_start_rebase_cutoff_utc = first_flat_sample[0].strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        )

    def extract_views(
        raw_text: str,
    ) -> tuple[
        Dict[str, object],
        Dict[str, float],
        Dict[str, float],
        Dict[str, float],
        Dict[str, float],
        Dict[str, float],
        Dict[str, float],
        Dict[str, float],
        Dict[str, float],
    ]:
        return (
            extract_runtime_account_series(raw_text),
            extract_strategy_mix_series(raw_text),
            extract_execution_window_series(raw_text),
            extract_execution_quality_guard_series(raw_text),
            extract_entry_gate_series(raw_text),
            extract_concentration_series(raw_text),
            extract_entry_edge_adjust_series(raw_text),
            extract_reconcile_runtime_series(raw_text),
            extract_regime_change_series(raw_text),
        )

    (
        account_pnl,
        strategy_mix,
        execution_window,
        execution_quality_guard,
        entry_gate,
        concentration,
        entry_edge_adjust,
        reconcile_runtime,
        regime_change,
    ) = extract_views(text)

    if flat_start_rebased:
        original_strategy_mix = extract_strategy_mix_series(original_text)
        rebased_nonzero = int(strategy_mix.get("nonzero_window_count", 0.0))
        original_nonzero = int(original_strategy_mix.get("nonzero_window_count", 0.0))
        if rebased_nonzero <= 0 and original_nonzero > 0:
            text = original_text
            flat_start_rebased = False
            flat_start_rebase_cutoff_utc = None
            flat_start_rebase_fallback = True
            (
                account_pnl,
                strategy_mix,
                execution_window,
                execution_quality_guard,
                entry_gate,
                concentration,
                entry_edge_adjust,
                reconcile_runtime,
                regime_change,
            ) = extract_views(text)

    regime_current_counts = extract_regime_current_counts(text)
    regime_runtime_diagnostics = extract_regime_runtime_diagnostics(text)
    execution_attribution = extract_execution_attribution(text)
    exit_capture_live = extract_exit_capture_samples(text)
    feature_scale = extract_feature_scale_diagnostics(text)

    global_self_evolution_init_count = count(r"SELF_EVOLUTION_INIT", original_text)
    global_self_evolution_action_count = count(r"SELF_EVOLUTION_ACTION", original_text)
    global_self_evolution_runtime_enabled_count = count(
        r"RUNTIME_STATUS:.*evolution=\{enabled=true", original_text
    )
    metrics = {
        "runtime_status_count": count(r"RUNTIME_STATUS:", text),
        "max_runtime_tick": max_tick(text),
        "critical_count": count(r"\bCRITICAL\b", text),
        "trading_halted_event_count": count(r"\bTRADING_HALTED\b", text),
        "trading_halted_true_count": count(r"RUNTIME_STATUS:.*trading_halted=true", text),
        "gate_reduce_only_true_count": count(
            r"RUNTIME_STATUS:.*gate_runtime=\{[^}]*reduce_only=true", text
        ),
        "gate_halted_true_count": count(
            r"RUNTIME_STATUS:.*gate_runtime=\{[^}]*gate_halted=true", text
        ),
        "ws_unhealthy_count": count(
            r"RUNTIME_STATUS:.*(?:public_ws_healthy=false|private_ws_healthy=false)", text
        ),
        "ws_degraded_count": count(r"\bDEGRADED\b", text),
        "gate_check_passed_count": count(r"GATE_CHECK_PASSED", text),
        "gate_policy_flat_pass_count": count(
            r"GATE_CHECK_PASSED:.*policy_flat=true", text
        ),
        "gate_check_failed_count": count(r"GATE_CHECK_FAILED", text),
        "gate_runtime_policy_flat_exempt_count": count(
            r"GATE_RUNTIME_POLICY_FLAT_EXEMPT", text
        ),
        "policy_flat_residual_position_count": count(
            r"POLICY_FLAT_RESIDUAL_POSITION", text
        ),
        "gate_alert_count": count(r"GATE_ALERT", text),
        "reconcile_mismatch_count": count(r"OMS_RECONCILE_MISMATCH", text),
        "reconcile_autoresync_count": count(r"OMS_RECONCILE_AUTORESYNC", text),
        "reconcile_deferred_count": count(r"OMS_RECONCILE_DEFERRED", text),
        "reconcile_degraded_flat_idle_count": count(
            r"OMS_RECONCILE_DEGRADED_FLAT_IDLE", text
        ),
        "reconcile_anomaly_event_count": count(r"OMS_RECONCILE_ANOMALY_STREAK", text),
        "reconcile_anomaly_protection_enter_count": count(
            r"OMS_RECONCILE_ANOMALY_PROTECTION_ENTER", text
        ),
        "reconcile_anomaly_protection_exit_count": count(
            r"OMS_RECONCILE_ANOMALY_PROTECTION_EXIT", text
        ),
        "reconcile_anomaly_halt_enter_count": count(
            r"OMS_RECONCILE_ANOMALY_HALT_ENTER", text
        ),
        "reconcile_anomaly_halt_exit_count": count(
            r"OMS_RECONCILE_ANOMALY_HALT_EXIT", text
        ),
        "reconcile_anomaly_halted_true_count": count(
            r"RUNTIME_STATUS:.*reconcile_runtime=\{[^}]*anomaly_halted=true", text
        ),
        "fill_overfill_drop_count": count(r"FILL_OVERFILL_DROP", text),
        "fill_duplicate_drop_count": count(r"FILL_DUPLICATE_DROP", text),
        "bybit_exec_dedup_drop_count": count(r"BYBIT_EXEC_DEDUP_DROP", text),
        "fill_account_already_reflected_count": count(
            r"FILL_ACCOUNT_ALREADY_REFLECTED", text
        ),
        "fill_applied_account_already_reflected_count": count(
            r"FILL_APPLIED:.*account_already_reflected=true", text
        ),
        "fill_cancelled_order_applied_count": count(
            r"FILL_APPLIED:.*order_state_before=cancelled", text
        ),
        "self_evolution_init_count": count(r"SELF_EVOLUTION_INIT", text),
        "self_evolution_action_count": count(r"SELF_EVOLUTION_ACTION", text),
        "self_evolution_init_total_count": global_self_evolution_init_count,
        "self_evolution_action_total_count": global_self_evolution_action_count,
        "self_evolution_runtime_enabled_total_count": global_self_evolution_runtime_enabled_count,
        "self_evolution_virtual_action_count": count(
            r"SELF_EVOLUTION_ACTION:.*pnl_source=virtual", text
        ),
        "self_evolution_counterfactual_action_count": count(
            r"SELF_EVOLUTION_ACTION:.*counterfactual_search=true", text
        ),
        "self_evolution_counterfactual_update_count": count(
            r"SELF_EVOLUTION_ACTION:.*reason=EVOLUTION_COUNTERFACTUAL_(?:INCREASE|DECREASE)_TREND",
            text,
        ),
        "self_evolution_factor_ic_action_count": count(
            r"SELF_EVOLUTION_ACTION:.*reason=EVOLUTION_FACTOR_IC_(?:INCREASE|DECREASE)_TREND",
            text,
        ),
        "self_evolution_objective_update_count": count(
            r"SELF_EVOLUTION_ACTION:.*reason=EVOLUTION_WEIGHT_(?:INCREASE|DECREASE)_TREND",
            text,
        ),
        "self_evolution_counterfactual_fallback_used_count": count(
            r"SELF_EVOLUTION_ACTION:.*counterfactual_fallback=\{enabled=true, used=true\}",
            text,
        ),
        "self_evolution_counterfactual_superiority_skip_count": count(
            r"SELF_EVOLUTION_ACTION:.*reason=EVOLUTION_COUNTERFACTUAL_SUPERIORITY_(?:SAMPLES|TSTAT)_TOO_LOW",
            text,
        ),
        "self_evolution_learnability_skip_count": count(
            r"SELF_EVOLUTION_ACTION:.*reason=EVOLUTION_LEARNABILITY_(?:INSUFFICIENT_SAMPLES|TSTAT_TOO_LOW)",
            text,
        ),
        "self_evolution_learnability_pass_count": count(
            r"SELF_EVOLUTION_ACTION:.*learnability=\{enabled=true, passed=true",
            text,
        ),
        "self_evolution_direction_consistency_pending_count": count(
            r"SELF_EVOLUTION_ACTION:.*reason=EVOLUTION_DIRECTION_CONSISTENCY_PENDING",
            text,
        ),
        "integrator_policy_applied_count": count(r"INTEGRATOR_POLICY_APPLIED:", text),
        "integrator_policy_canary_count": count(
            r"INTEGRATOR_POLICY_APPLIED:.*mode=canary", text
        ),
        "integrator_policy_active_count": count(
            r"INTEGRATOR_POLICY_APPLIED:.*mode=active", text
        ),
        "integrator_mode_off_count": count(r"RUNTIME_STATUS:.*integrator_mode=off", text),
        "integrator_mode_shadow_count": count(
            r"RUNTIME_STATUS:.*integrator_mode=shadow", text
        ),
        "integrator_mode_canary_count": count(
            r"RUNTIME_STATUS:.*integrator_mode=canary", text
        ),
        "integrator_mode_active_count": count(
            r"RUNTIME_STATUS:.*integrator_mode=active", text
        ),
        "integrator_shadow_scored_runtime_count": count(
            r"RUNTIME_STATUS:.*shadow_window=\{[^}]*scored=(?:[1-9][0-9]*)", text
        ),
        "order_filtered_cost_count": count(r"ORDER_FILTERED_COST:", text),
        "trend_candidate_probe_signal_count": count(
            r"TREND_CANDIDATE_PROBE_SIGNAL:", text
        ),
        "trend_candidate_probe_strong_signal_count": count(
            r"TREND_CANDIDATE_PROBE_SIGNAL:.*strong_filter=true", text
        ),
        "trend_candidate_probe_cost_cooldown_bypass_count": count(
            r"TREND_CANDIDATE_PROBE_COST_COOLDOWN_BYPASS:", text
        ),
        "trend_candidate_probe_fee_override_count": count(
            r"TREND_CANDIDATE_PROBE_FEE_OVERRIDE:", text
        ),
        "trend_candidate_probe_filtered_fee_count": count(
            r"TREND_CANDIDATE_PROBE_FILTERED_FEE:",
            text,
        ),
        "trend_candidate_probe_quality_guard_blocked_count": count(
            r"TREND_CANDIDATE_PROBE_FILTERED_FEE:.*quality_guard_override_blocked=true",
            text,
        ),
        "trend_candidate_probe_quality_guard_memory_skip_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:.*reason=QUALITY_GUARD_MEMORY",
            text,
        ),
        "trend_candidate_probe_downweight_count": count(
            r"TREND_CANDIDATE_PROBE_DOWNWEIGHT:", text
        ),
        "trend_candidate_probe_enqueued_count": count(
            r"TREND_CANDIDATE_PROBE_ENQUEUED:", text
        ),
        "trend_candidate_probe_fill_count": count(
            r"TREND_CANDIDATE_PROBE_FILL:", text
        ),
        "trend_candidate_probe_skip_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:", text
        ),
        "trend_candidate_probe_skip_trade_not_ok_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:.*reason=TRADE_NOT_OK\b", text
        ),
        "trend_candidate_probe_skip_existing_intent_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:.*reason=EXISTING_INTENT\b", text
        ),
        "trend_candidate_probe_skip_pending_orders_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:.*reason=PENDING_ORDERS\b", text
        ),
        "trend_candidate_probe_skip_exposure_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:.*reason=EXPOSURE\b", text
        ),
        "trend_candidate_probe_skip_trend_ratio_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:.*reason=TREND_RATIO_LOW\b", text
        ),
        "trend_candidate_probe_skip_strong_trend_ratio_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:.*reason=STRONG_TREND_RATIO_LOW\b",
            text,
        ),
        "trend_candidate_probe_skip_cooldown_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:.*reason=COOLDOWN\b", text
        ),
        "trend_candidate_probe_skip_window_limit_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:.*reason=WINDOW_LIMIT\b", text
        ),
        "trend_candidate_probe_skip_direction_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:.*reason=DIRECTION_ZERO\b", text
        ),
        "trend_candidate_probe_skip_invalid_price_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:.*reason=INVALID_PRICE\b", text
        ),
        "trend_candidate_probe_skip_notional_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:.*reason=NOTIONAL_ZERO\b", text
        ),
        "trend_candidate_probe_skip_budget_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:.*reason=BUDGET_ZERO\b", text
        ),
        "trend_candidate_probe_skip_build_intent_count": count(
            r"TREND_CANDIDATE_PROBE_SKIPPED:.*reason=BUILD_INTENT_FAILED\b",
            text,
        ),
        "trend_candidate_probe_runtime_count": count(
            r"RUNTIME_STATUS:.*funnel_window=\{[^}]*candidate_probe_signals=(?:[1-9][0-9]*)",
            text,
        ),
        "entry_gate_enabled_count": count(r"RUNTIME_STATUS:.*entry_gate=\{enabled=true", text),
        "order_throttled_symbol_quality_min_hold_count": count(
            r"ORDER_THROTTLED:.*symbol_quality_min_hold_remaining_ticks",
            text,
        ),
        "order_throttled_symbol_quality_quarantine_count": count(
            r"ORDER_THROTTLED:.*symbol_quality_quarantine_remaining_ticks",
            text,
        ),
        "strategy_reduce_cost_guard_blocked_count": count(
            r"STRATEGY_REDUCE_COST_GUARD_BLOCKED:", text
        ),
        "strategy_reduce_cost_guard_bypass_count": count(
            r"STRATEGY_REDUCE_COST_GUARD_BYPASS:", text
        ),
        "order_throttled_strategy_reduce_cost_guard_count": count(
            r"ORDER_THROTTLED:.*reason=strategy_reduce_cost_guard\b", text
        ),
        "funnel_enqueued_runtime_count": count(
            r"RUNTIME_STATUS:.*funnel_window=\{[^}]*enqueued=(?:[1-9][0-9]*)",
            text,
        ),
        "funnel_fills_runtime_count": count(
            r"RUNTIME_STATUS:.*funnel_window=\{[^}]*fills=(?:[1-9][0-9]*)",
            text,
        ),
        "bybit_submit_limit_count": count(r"BYBIT_SUBMIT:.*order_type=Limit", text),
        "bybit_submit_market_count": count(r"BYBIT_SUBMIT:.*order_type=Market", text),
        "protective_order_missing_count": count(
            r"EXEC_PROTECTIVE_ORDER_MISSING", text
        ),
        "tp_attach_failed_count": count(r"EXEC_TP_ATTACH_FAILED", text),
        "protection_refresh_count": count(r"PROTECTION_REFRESH", text),
        "protection_cancelled_count": count(r"PROTECTION_CANCELLED", text),
        "profit_protection_update_count": count(
            r"PROFIT_PROTECTION_UPDATE", text
        ),
        "profit_protection_armed_count": count(
            r"PROFIT_PROTECTION_ARMED", text
        ),
        "profit_protection_crossed_count": count(
            r"PROFIT_PROTECTION_CROSSED", text
        ),
        "exit_capture_sample_count": int(exit_capture_live["sample_count"]),
        "exit_capture_low_count": int(exit_capture_live["low_capture_count"]),
        "exit_capture_low_ratio": float(exit_capture_live["low_capture_ratio"]),
        "exit_capture_mean_path_mfe_bps": float(
            exit_capture_live["mean_path_mfe_bps"]
        ),
        "exit_capture_mean_captured_gross_bps": float(
            exit_capture_live["mean_captured_gross_bps"]
        ),
        "exit_capture_mean_captured_net_bps": float(
            exit_capture_live["mean_captured_net_bps"]
        ),
        "exit_capture_mean_fee_bps": float(exit_capture_live["mean_fee_bps"]),
        "exit_capture_mean_capture_ratio": float(
            exit_capture_live["mean_capture_ratio"]
        ),
        "exit_capture_p25_capture_ratio": float(
            exit_capture_live["p25_capture_ratio"]
        ),
        "exit_capture_p50_capture_ratio": float(
            exit_capture_live["p50_capture_ratio"]
        ),
        "exit_capture_p75_capture_ratio": float(
            exit_capture_live["p75_capture_ratio"]
        ),
        "exit_capture_by_symbol": exit_capture_live["by_symbol"],
        "exit_capture_low_by_symbol": exit_capture_live["low_by_symbol"],
        "exit_capture_by_purpose": exit_capture_live["by_purpose"],
        "s5_protection_enabled": 1 if s5_protection_enabled else 0,
        "s5_profit_protection_enabled": 1
        if s5_profit_protection_enabled
        else 0,
        "runtime_account_samples": account_pnl["samples"],
        "start_flat": account_pnl["start_flat"],
        "start_abs_notional_usd": account_pnl["first_abs_notional_usd"],
        "strategy_mix_runtime_count": int(strategy_mix["runtime_count"]),
        "strategy_mix_nonzero_window_count": int(strategy_mix["nonzero_window_count"]),
        "strategy_mix_policy_flat_window_count": int(
            strategy_mix["policy_flat_window_count"]
        ),
        "strategy_mix_defensive_active_count": int(
            strategy_mix["defensive_active_count"]
        ),
        "strategy_mix_avg_abs_trend_notional": strategy_mix["avg_abs_trend_notional"],
        "strategy_mix_avg_abs_defensive_notional": strategy_mix[
            "avg_abs_defensive_notional"
        ],
        "strategy_mix_avg_abs_blended_notional": strategy_mix[
            "avg_abs_blended_notional"
        ],
        "strategy_mix_avg_defensive_share": strategy_mix["avg_defensive_share"],
        "execution_window_runtime_count": int(execution_window["runtime_count"]),
        "filtered_cost_ratio": execution_window["filtered_cost_ratio_latest"],
        "filtered_cost_ratio_avg": execution_window["filtered_cost_ratio_avg"],
        "filtered_cost_near_miss_ratio": execution_window[
            "filtered_cost_near_miss_ratio_latest"
        ],
        "filtered_cost_near_miss_ratio_avg": execution_window[
            "filtered_cost_near_miss_ratio_avg"
        ],
        "passed_cost_near_miss_ratio": execution_window[
            "passed_cost_near_miss_ratio_latest"
        ],
        "passed_cost_near_miss_ratio_avg": execution_window[
            "passed_cost_near_miss_ratio_avg"
        ],
        "entry_edge_gap_avg_bps": execution_window["entry_edge_gap_avg_bps_avg"],
        "realized_net_per_fill": execution_window["realized_net_per_fill_avg"],
        "fee_bps_per_fill": execution_window["fee_bps_per_fill_avg"],
        "execution_window_maker_fills_avg": execution_window["maker_fills_avg"],
        "execution_window_taker_fills_avg": execution_window["taker_fills_avg"],
        "execution_window_unknown_fills_avg": execution_window["unknown_fills_avg"],
        "execution_window_explicit_liquidity_fills_avg": execution_window[
            "explicit_liquidity_fills_avg"
        ],
        "execution_window_fee_sign_fallback_fills_avg": execution_window[
            "fee_sign_fallback_fills_avg"
        ],
        "execution_window_unknown_fill_ratio_avg": execution_window[
            "unknown_fill_ratio_avg"
        ],
        "execution_window_explicit_liquidity_fill_ratio_avg": execution_window[
            "explicit_liquidity_fill_ratio_avg"
        ],
        "execution_window_fee_sign_fallback_fill_ratio_avg": execution_window[
            "fee_sign_fallback_fill_ratio_avg"
        ],
        "execution_window_maker_fee_bps_avg": execution_window["maker_fee_bps_avg"],
        "execution_window_taker_fee_bps_avg": execution_window["taker_fee_bps_avg"],
        "execution_window_maker_fill_ratio_avg": execution_window[
            "maker_fill_ratio_avg"
        ],
        "execution_window_liquidity_source_runtime_count": int(
            execution_window["liquidity_source_runtime_count"]
        ),
        "entry_gate_runtime_count": int(entry_gate["runtime_count"]),
        "entry_gate_near_miss_tolerance_bps_avg": entry_gate[
            "near_miss_tolerance_bps_avg"
        ],
        "entry_gate_observed_filtered_ratio_avg": entry_gate[
            "observed_filtered_ratio_avg"
        ],
        "entry_gate_observed_near_miss_ratio": entry_gate[
            "observed_near_miss_ratio_latest"
        ],
        "entry_gate_observed_near_miss_ratio_avg": entry_gate[
            "observed_near_miss_ratio_avg"
        ],
        "entry_gate_observed_near_miss_allowed_ratio": entry_gate[
            "observed_near_miss_allowed_ratio_latest"
        ],
        "entry_gate_observed_near_miss_allowed_ratio_avg": entry_gate[
            "observed_near_miss_allowed_ratio_avg"
        ],
        "concentration_runtime_count": int(concentration["runtime_count"]),
        "concentration_top1_share_avg": concentration["top1_share_avg"],
        "concentration_top1_share_max": concentration["top1_share_max"],
        "concentration_high_count": int(concentration["high_concentration_count"]),
        "concentration_symbol_count_avg": concentration["symbol_count_avg"],
        "execution_quality_guard_runtime_count": int(
            execution_quality_guard["runtime_count"]
        ),
        "execution_quality_guard_enabled_count": int(
            execution_quality_guard["enabled_count"]
        ),
        "execution_quality_guard_active_count": int(
            execution_quality_guard["active_count"]
        ),
        "execution_quality_guard_active_ratio": execution_quality_guard[
            "active_ratio"
        ],
        "execution_quality_guard_penalty_bps_avg": execution_quality_guard[
            "applied_penalty_bps_avg"
        ],
        "execution_quality_guard_bad_streak_max": int(
            execution_quality_guard["bad_streak_max"]
        ),
        "execution_quality_guard_good_streak_max": int(
            execution_quality_guard["good_streak_max"]
        ),
        "execution_quality_guard_no_fill_windows_max": int(
            execution_quality_guard["no_fill_windows_max"]
        ),
        "execution_quality_guard_symbol_active_count_max": int(
            execution_quality_guard["symbol_active_count_max"]
        ),
        "execution_quality_guard_symbol_active_count_latest": int(
            execution_quality_guard["symbol_active_count_latest"]
        ),
        "execution_quality_guard_symbol_state_count_max": int(
            execution_quality_guard["symbol_state_count_max"]
        ),
        "execution_quality_guard_symbol_state_count_latest": int(
            execution_quality_guard["symbol_state_count_latest"]
        ),
        "entry_edge_adjust_runtime_count": int(entry_edge_adjust["runtime_count"]),
        "entry_regime_adjust_bps_avg": entry_edge_adjust["regime_adjust_bps_avg"],
        "entry_volatility_adjust_bps_avg": entry_edge_adjust[
            "volatility_adjust_bps_avg"
        ],
        "entry_liquidity_adjust_bps_avg": entry_edge_adjust[
            "liquidity_adjust_bps_avg"
        ],
        "entry_concentration_adjust_bps_avg": entry_edge_adjust[
            "concentration_adjust_bps_avg"
        ],
        "regime_change_count": int(regime_change["runtime_count"]),
        "regime_change_trend_count": int(regime_change["trend_count"]),
        "regime_change_range_count": int(regime_change["range_count"]),
        "regime_change_extreme_count": int(regime_change["extreme_count"]),
        "regime_change_trend_symbol_count": int(
            regime_change["trend_symbol_count"]
        ),
        "regime_change_trend_symbols": regime_change["trend_symbols"],
        "regime_change_trend_candidate_count": int(
            regime_change["trend_candidate_count"]
        ),
        "regime_change_trend_candidate_symbol_count": int(
            regime_change["trend_candidate_symbol_count"]
        ),
        "regime_change_trend_candidate_symbols": regime_change[
            "trend_candidate_symbols"
        ],
        "regime_change_warmup_trend_candidate_count": int(
            regime_change["warmup_trend_candidate_count"]
        ),
        "regime_change_warmup_trend_candidate_symbol_count": int(
            regime_change["warmup_trend_candidate_symbol_count"]
        ),
        "regime_change_warmup_trend_candidate_symbols": regime_change[
            "warmup_trend_candidate_symbols"
        ],
        "regime_change_pending_trend_confirmation_count": int(
            regime_change["pending_trend_confirmation_count"]
        ),
        "regime_change_pending_trend_confirmation_symbol_count": int(
            regime_change["pending_trend_confirmation_symbol_count"]
        ),
        "regime_change_pending_trend_confirmation_symbols": regime_change[
            "pending_trend_confirmation_symbols"
        ],
        "regime_change_pending_trend_confirmation_ticks_max": int(
            regime_change["pending_trend_confirmation_ticks_max"]
        ),
        "regime_change_pending_trend_confirmation_elapsed_ms_max": int(
            regime_change["pending_trend_confirmation_elapsed_ms_max"]
        ),
        "regime_change_confirm_ticks_required_max": int(
            regime_change["confirm_ticks_required_max"]
        ),
        "regime_change_confirm_elapsed_ms_required_max": int(
            regime_change["confirm_elapsed_ms_required_max"]
        ),
        "trend_strength_abs_p50": regime_change["trend_strength_abs_p50"],
        "trend_strength_abs_p95": regime_change["trend_strength_abs_p95"],
        "trend_strength_abs_p99": regime_change["trend_strength_abs_p99"],
        "trend_strength_abs_max": regime_change["trend_strength_abs_max"],
        "trend_threshold_ratio_p50": regime_change["trend_threshold_ratio_p50"],
        "trend_threshold_ratio_p95": regime_change["trend_threshold_ratio_p95"],
        "trend_threshold_ratio_p99": regime_change["trend_threshold_ratio_p99"],
        "trend_threshold_ratio_max": regime_change["trend_threshold_ratio_max"],
        "warmup_trend_threshold_ratio_max": regime_change[
            "warmup_trend_threshold_ratio_max"
        ],
        "nonwarmup_trend_threshold_ratio_max": regime_change[
            "nonwarmup_trend_threshold_ratio_max"
        ],
        "instant_return_abs_p50": regime_change["instant_return_abs_p50"],
        "instant_return_abs_p95": regime_change["instant_return_abs_p95"],
        "instant_return_abs_p99": regime_change["instant_return_abs_p99"],
        "instant_return_abs_max": regime_change["instant_return_abs_max"],
        "volatility_level_p50": regime_change["volatility_level_p50"],
        "volatility_level_p95": regime_change["volatility_level_p95"],
        "volatility_level_p99": regime_change["volatility_level_p99"],
        "volatility_level_max": regime_change["volatility_level_max"],
        "volatility_threshold_ratio_p50": regime_change[
            "volatility_threshold_ratio_p50"
        ],
        "volatility_threshold_ratio_p95": regime_change[
            "volatility_threshold_ratio_p95"
        ],
        "volatility_threshold_ratio_p99": regime_change[
            "volatility_threshold_ratio_p99"
        ],
        "volatility_threshold_ratio_max": regime_change[
            "volatility_threshold_ratio_max"
        ],
        "execution_quality_guard_enter_count": count(
            r"EXECUTION_QUALITY_GUARD_ENTER", text
        ),
        "execution_quality_guard_exit_count": count(
            r"EXECUTION_QUALITY_GUARD_EXIT", text
        ),
        "execution_symbol_quality_guard_enter_count": count(
            r"EXECUTION_SYMBOL_QUALITY_GUARD_ENTER", text
        ),
        "execution_symbol_quality_guard_reinforce_count": count(
            r"EXECUTION_SYMBOL_QUALITY_GUARD_REINFORCE", text
        ),
        "execution_symbol_quality_guard_exit_count": count(
            r"EXECUTION_SYMBOL_QUALITY_GUARD_EXIT", text
        ),
        "execution_symbol_quality_memory_decay_count": count(
            r"EXECUTION_SYMBOL_QUALITY_MEMORY_DECAY", text
        ),
        "reconcile_runtime_count": int(reconcile_runtime["runtime_count"]),
        "reconcile_anomaly_streak_nonzero_count": int(
            reconcile_runtime["anomaly_streak_nonzero_count"]
        ),
        "reconcile_anomaly_streak_max": int(reconcile_runtime["anomaly_streak_max"]),
        "reconcile_anomaly_reduce_only_true_count": int(
            reconcile_runtime["anomaly_reduce_only_true_count"]
        ),
        "reconcile_anomaly_reduce_only_true_ratio": reconcile_runtime[
            "anomaly_reduce_only_true_ratio"
        ],
        "regime_trend_runtime_count": int(regime_current_counts["TREND"]),
        "regime_range_runtime_count": int(regime_current_counts["RANGE"]),
        "regime_extreme_runtime_count": int(regime_current_counts["EXTREME"]),
        "regime_trend_candidate_runtime_count": int(
            regime_runtime_diagnostics["window_candidate_count"]
        ),
        "regime_warmup_trend_candidate_runtime_count": int(
            regime_runtime_diagnostics["window_warmup_candidate_count"]
        ),
        "regime_current_trend_candidate_count": int(
            regime_runtime_diagnostics["current_candidate_count"]
        ),
        "regime_current_warmup_trend_candidate_count": int(
            regime_runtime_diagnostics["current_warmup_candidate_count"]
        ),
        "regime_current_pending_trend_confirmation_count": int(
            regime_runtime_diagnostics[
                "current_pending_trend_confirmation_count"
            ]
        ),
        "regime_current_trend_threshold_ratio_avg": regime_runtime_diagnostics[
            "trend_threshold_ratio_avg"
        ],
        "regime_current_trend_threshold_ratio_max": regime_runtime_diagnostics[
            "trend_threshold_ratio_max"
        ],
        "regime_current_volatility_threshold_ratio_avg": regime_runtime_diagnostics[
            "volatility_threshold_ratio_avg"
        ],
        "regime_current_volatility_threshold_ratio_max": regime_runtime_diagnostics[
            "volatility_threshold_ratio_max"
        ],
        "regime_current_pending_trend_confirmation_ticks_max": int(
            regime_runtime_diagnostics[
                "pending_trend_confirmation_ticks_max"
            ]
        ),
        "regime_current_confirm_ticks_required_max": int(
            regime_runtime_diagnostics["confirm_ticks_required_max"]
        ),
        "regime_current_pending_trend_confirmation_elapsed_ms_max": int(
            regime_runtime_diagnostics[
                "pending_trend_confirmation_elapsed_ms_max"
            ]
        ),
        "regime_current_confirm_elapsed_ms_required_max": int(
            regime_runtime_diagnostics["confirm_elapsed_ms_required_max"]
        ),
        "flat_start_rebase_applied_count": 1 if flat_start_rebased else 0,
    }
    metrics.update(extract_runtime_timing(text))
    metrics.update(feature_scale)
    attribution_fills = execution_attribution.get("fills", {})
    attribution_submit = execution_attribution.get("submit", {})
    attribution_runtime_windows = execution_attribution.get("runtime_fill_windows", {})
    if isinstance(attribution_fills, dict) and isinstance(
        attribution_submit, dict
    ) and isinstance(attribution_runtime_windows, dict):
        probe_fills = attribution_fills.get("probe", {})
        main_fills = attribution_fills.get("main", {})
        if not isinstance(probe_fills, dict):
            probe_fills = {}
        if not isinstance(main_fills, dict):
            main_fills = {}
        probe_by_liquidity = probe_fills.get("by_liquidity", {})
        main_by_liquidity = main_fills.get("by_liquidity", {})
        probe_fee_by_liquidity = probe_fills.get("fee_by_liquidity", {})
        main_fee_by_liquidity = main_fills.get("fee_by_liquidity", {})
        if not isinstance(probe_by_liquidity, dict):
            probe_by_liquidity = {}
        if not isinstance(main_by_liquidity, dict):
            main_by_liquidity = {}
        if not isinstance(probe_fee_by_liquidity, dict):
            probe_fee_by_liquidity = {}
        if not isinstance(main_fee_by_liquidity, dict):
            main_fee_by_liquidity = {}
        quality_by_symbol = attribution_fills.get("quality_by_symbol", {})
        if not isinstance(quality_by_symbol, dict):
            quality_by_symbol = {}
        worst_symbol = ""
        worst_symbol_net = 0.0
        worst_symbol_net_per_fill = 0.0
        best_symbol = ""
        best_symbol_net = 0.0
        best_symbol_net_per_fill = 0.0
        negative_symbol_count = 0
        positive_symbol_count = 0
        quality_fill_count = 0
        quality_realized_pnl_usd = 0.0
        quality_fee_usd = 0.0
        quality_realized_net_usd = 0.0
        if quality_by_symbol:
            for payload in quality_by_symbol.values():
                if not isinstance(payload, dict):
                    continue
                quality_fill_count += int(payload.get("fills", 0) or 0)
                quality_realized_pnl_usd += float(
                    payload.get("realized_pnl_usd", 0.0) or 0.0
                )
                quality_fee_usd += float(payload.get("fee_usd", 0.0) or 0.0)
                quality_realized_net_usd += float(
                    payload.get("realized_net_usd", 0.0) or 0.0
                )
            ranked_symbols = sorted(
                (
                    (
                        symbol,
                        float(payload.get("realized_net_usd", 0.0)),
                        float(payload.get("realized_net_per_fill", 0.0)),
                    )
                    for symbol, payload in quality_by_symbol.items()
                    if isinstance(payload, dict)
                ),
                key=lambda item: (item[2], item[1]),
            )
            if ranked_symbols:
                worst_symbol, worst_symbol_net, worst_symbol_net_per_fill = (
                    ranked_symbols[0]
                )
                best_symbol, best_symbol_net, best_symbol_net_per_fill = (
                    ranked_symbols[-1]
                )
                negative_symbol_count = sum(
                    1 for _, _, net_per_fill in ranked_symbols if net_per_fill < 0.0
                )
                positive_symbol_count = sum(
                    1 for _, _, net_per_fill in ranked_symbols if net_per_fill > 0.0
                )
        quality_realized_net_per_fill = (
            quality_realized_net_usd / quality_fill_count
            if quality_fill_count > 0
            else 0.0
        )
        metrics.update(
            {
                "execution_attribution_submit_count": int(
                    attribution_submit.get("total", 0)
                ),
                "execution_attribution_fill_count": int(
                    attribution_fills.get("total", 0)
                ),
                "execution_attribution_probe_fill_count": int(
                    probe_fills.get("total", 0)
                ),
                "execution_attribution_main_fill_count": int(
                    main_fills.get("total", 0)
                ),
                "execution_attribution_maker_fill_count": int(
                    attribution_fills.get("maker_count", 0)
                ),
                "execution_attribution_taker_fill_count": int(
                    attribution_fills.get("taker_count", 0)
                ),
                "execution_attribution_unknown_liquidity_fill_count": int(
                    attribution_fills.get("unknown_liquidity_count", 0)
                ),
                "execution_attribution_probe_maker_fill_count": int(
                    probe_by_liquidity.get("MAKER", 0)
                ),
                "execution_attribution_probe_taker_fill_count": int(
                    probe_by_liquidity.get("TAKER", 0)
                ),
                "execution_attribution_probe_unknown_liquidity_fill_count": int(
                    probe_by_liquidity.get("UNKNOWN", 0)
                ),
                "execution_attribution_main_maker_fill_count": int(
                    main_by_liquidity.get("MAKER", 0)
                ),
                "execution_attribution_main_taker_fill_count": int(
                    main_by_liquidity.get("TAKER", 0)
                ),
                "execution_attribution_main_unknown_liquidity_fill_count": int(
                    main_by_liquidity.get("UNKNOWN", 0)
                ),
                "execution_attribution_fee_usd": float(
                    attribution_fills.get("fee_usd", 0.0)
                ),
                "execution_attribution_probe_fee_usd": float(
                    probe_fills.get("fee_usd", 0.0)
                ),
                "execution_attribution_main_fee_usd": float(
                    main_fills.get("fee_usd", 0.0)
                ),
                "execution_attribution_probe_maker_fee_usd": float(
                    probe_fee_by_liquidity.get("MAKER", 0.0)
                ),
                "execution_attribution_probe_taker_fee_usd": float(
                    probe_fee_by_liquidity.get("TAKER", 0.0)
                ),
                "execution_attribution_probe_unknown_liquidity_fee_usd": float(
                    probe_fee_by_liquidity.get("UNKNOWN", 0.0)
                ),
                "execution_attribution_main_maker_fee_usd": float(
                    main_fee_by_liquidity.get("MAKER", 0.0)
                ),
                "execution_attribution_main_taker_fee_usd": float(
                    main_fee_by_liquidity.get("TAKER", 0.0)
                ),
                "execution_attribution_main_unknown_liquidity_fee_usd": float(
                    main_fee_by_liquidity.get("UNKNOWN", 0.0)
                ),
                "execution_attribution_quality_fill_count": int(quality_fill_count),
                "execution_attribution_realized_pnl_usd": float(
                    quality_realized_pnl_usd
                ),
                "execution_attribution_realized_net_usd": float(
                    quality_realized_net_usd
                ),
                "execution_attribution_realized_net_per_fill": float(
                    quality_realized_net_per_fill
                ),
                "execution_attribution_quality_fee_usd": float(quality_fee_usd),
                "execution_attribution_runtime_fill_window_count": int(
                    attribution_runtime_windows.get("count", 0)
                ),
                "execution_attribution_runtime_fills": int(
                    attribution_runtime_windows.get("fills", 0)
                ),
                "execution_attribution_runtime_realized_net_delta_usd": float(
                    attribution_runtime_windows.get("realized_net_delta_usd", 0.0)
                ),
                "execution_attribution_runtime_fee_delta_usd": float(
                    attribution_runtime_windows.get("fee_delta_usd", 0.0)
                ),
                "execution_attribution_symbol_count": int(len(quality_by_symbol)),
                "execution_attribution_negative_symbol_count": int(
                    negative_symbol_count
                ),
                "execution_attribution_positive_symbol_count": int(
                    positive_symbol_count
                ),
                "execution_attribution_worst_symbol": worst_symbol,
                "execution_attribution_worst_symbol_realized_net_usd": float(
                    worst_symbol_net
                ),
                "execution_attribution_worst_symbol_realized_net_per_fill": float(
                    worst_symbol_net_per_fill
                ),
                "execution_attribution_best_symbol": best_symbol,
                "execution_attribution_best_symbol_realized_net_usd": float(
                    best_symbol_net
                ),
                "execution_attribution_best_symbol_realized_net_per_fill": float(
                    best_symbol_net_per_fill
                ),
            }
        )
    metrics["self_evolution_effective_update_count"] = (
        metrics["self_evolution_counterfactual_update_count"]
        + metrics["self_evolution_factor_ic_action_count"]
        + metrics["self_evolution_objective_update_count"]
    )
    if metrics["runtime_status_count"] > 0:
        metrics["trading_halted_true_ratio"] = (
            metrics["trading_halted_true_count"] / metrics["runtime_status_count"]
        )
        metrics["integrator_policy_applied_ratio"] = (
            metrics["integrator_policy_applied_count"] / metrics["runtime_status_count"]
        )
    else:
        metrics["trading_halted_true_ratio"] = 0.0
        metrics["integrator_policy_applied_ratio"] = 0.0

    execution_activity_count = (
        metrics["bybit_submit_limit_count"]
        + metrics["bybit_submit_market_count"]
        + metrics["funnel_enqueued_runtime_count"]
        + metrics["funnel_fills_runtime_count"]
    )
    metrics["execution_activity_count"] = execution_activity_count

    gate_window_count = metrics["gate_check_passed_count"] + metrics["gate_check_failed_count"]
    if gate_window_count > 0:
        metrics["gate_check_fail_ratio"] = (
            metrics["gate_check_failed_count"] / gate_window_count
        )
    else:
        metrics["gate_check_fail_ratio"] = 0.0

    regime_runtime_total = (
        metrics["regime_trend_runtime_count"]
        + metrics["regime_range_runtime_count"]
        + metrics["regime_extreme_runtime_count"]
    )
    if regime_runtime_total > 0:
        metrics["regime_trend_runtime_ratio"] = (
            metrics["regime_trend_runtime_count"] / regime_runtime_total
        )
        metrics["regime_range_runtime_ratio"] = (
            metrics["regime_range_runtime_count"] / regime_runtime_total
        )
        metrics["regime_extreme_runtime_ratio"] = (
            metrics["regime_extreme_runtime_count"] / regime_runtime_total
        )
    else:
        metrics["regime_trend_runtime_ratio"] = 0.0
        metrics["regime_range_runtime_ratio"] = 0.0
        metrics["regime_extreme_runtime_ratio"] = 0.0

    equity_change_usd = account_pnl.get("equity_change_usd")
    realized_net_change_usd = account_pnl.get("realized_net_pnl_change_usd")
    if isinstance(equity_change_usd, (int, float)) and isinstance(
        realized_net_change_usd, (int, float)
    ):
        metrics["equity_vs_realized_net_gap_usd"] = (
            equity_change_usd - realized_net_change_usd
        )
    else:
        metrics["equity_vs_realized_net_gap_usd"] = None

    if metrics["regime_trend_runtime_count"] > 0:
        market_context_status = "TREND_PRESENT"
    elif metrics["regime_change_trend_count"] > 0:
        market_context_status = "TREND_TRANSIENT"
    elif (
        metrics["regime_trend_candidate_runtime_count"] > 0
        or metrics["regime_change_trend_candidate_count"] > 0
    ):
        market_context_status = "TREND_CANDIDATE"
    elif (
        metrics["regime_range_runtime_count"] > 0
        and metrics["regime_extreme_runtime_count"] > 0
    ):
        market_context_status = "RANGE_EXTREME_ONLY"
    elif metrics["regime_range_runtime_count"] > 0:
        market_context_status = "RANGE_ONLY"
    elif metrics["regime_extreme_runtime_count"] > 0:
        market_context_status = "EXTREME_ONLY"
    else:
        market_context_status = "UNKNOWN"

    gap_usd = metrics["equity_vs_realized_net_gap_usd"]
    end_flat = bool(account_pnl.get("end_flat"))
    last_abs_notional_usd = account_pnl.get("last_abs_notional_usd")
    flat_no_execution_balance_drift = (
        execution_activity_count <= 0
        and bool(account_pnl.get("start_flat"))
        and isinstance(account_pnl.get("max_abs_notional_usd_observed"), (int, float))
        and abs(account_pnl["max_abs_notional_usd_observed"]) <= 1e-9
        and isinstance(account_pnl.get("realized_net_pnl_change_usd"), (int, float))
        and abs(account_pnl["realized_net_pnl_change_usd"]) <= 1e-9
        and isinstance(account_pnl.get("fee_change_usd"), (int, float))
        and abs(account_pnl["fee_change_usd"]) <= 1e-9
    )
    if isinstance(gap_usd, (int, float)) and abs(gap_usd) >= 50.0:
        if (
            isinstance(last_abs_notional_usd, (int, float))
            and last_abs_notional_usd > 1e-9
            and not end_flat
        ):
            account_sync_status = "OPEN_POSITION_GAP"
        elif flat_no_execution_balance_drift:
            account_sync_status = "EQUITY_DRIFT_WHILE_FLAT"
        elif execution_activity_count <= 0:
            account_sync_status = "NOISY_WHILE_FLAT"
        else:
            account_sync_status = "NOISY"
    else:
        account_sync_status = "OK"

    protection_fail_reasons: list[str] = []
    execution_fail_reasons: list[str] = []
    fail_reasons: list[str] = []
    warn_reasons: list[str] = []
    if flat_start_rebase_fallback:
        warn_reasons.append(
            "FLAT_START_REBASE 已回退原始窗口：rebase 后 strategy_mix 非零窗口被清零，"
            "已避免误判"
        )

    if metrics["critical_count"] > 0:
        protection_fail_reasons.append(f"出现 CRITICAL: {metrics['critical_count']}")
    if metrics["ws_unhealthy_count"] > 0:
        protection_fail_reasons.append(
            f"运行态 WS 健康检查失败次数: {metrics['ws_unhealthy_count']}"
        )
    if metrics["runtime_status_count"] < min_runtime_status:
        protection_fail_reasons.append(
            f"RUNTIME_STATUS 条数不足: {metrics['runtime_status_count']} < {min_runtime_status}"
        )
    if stage.name == "SMOKE":
        if metrics["gate_reduce_only_true_count"] > 0:
            protection_fail_reasons.append(
                "SMOKE 检测到 gate reduce_only=true: "
                f"count={metrics['gate_reduce_only_true_count']}"
            )
        if metrics["gate_halted_true_count"] > 0:
            protection_fail_reasons.append(
                "SMOKE 检测到 gate_halted=true: "
                f"count={metrics['gate_halted_true_count']}"
            )
        if metrics["reconcile_mismatch_count"] > 0:
            protection_fail_reasons.append(
                "SMOKE 检测到对账不一致: "
                f"reconcile_mismatch_count={metrics['reconcile_mismatch_count']}"
            )
        if metrics["reconcile_autoresync_count"] > 0:
            protection_fail_reasons.append(
                "SMOKE 检测到自动重对齐: "
                f"reconcile_autoresync_count={metrics['reconcile_autoresync_count']}"
            )
        if metrics["fill_overfill_drop_count"] > 0:
            protection_fail_reasons.append(
                "SMOKE 检测到 fill overfill 防线触发: "
                f"fill_overfill_drop_count={metrics['fill_overfill_drop_count']}"
            )

    policy_flat_window_count = int(metrics["strategy_mix_policy_flat_window_count"])
    policy_flat_dominant = (
        policy_flat_window_count > 0
        and metrics["strategy_mix_nonzero_window_count"] <= 0
        and metrics["gate_policy_flat_pass_count"] > 0
    )
    policy_flat_open_position = (
        policy_flat_window_count > 0
        and metrics["strategy_mix_nonzero_window_count"] <= 0
        and not end_flat
        and isinstance(last_abs_notional_usd, (int, float))
        and last_abs_notional_usd > 1e-9
    )
    if policy_flat_open_position:
        account_sync_status = "POLICY_FLAT_OPEN_POSITION"
    evolution_runtime_evidence_available = (
        metrics["self_evolution_runtime_enabled_total_count"] > 0
    )
    evolution_runtime_evidence_sufficient = (
        policy_flat_dominant and evolution_runtime_evidence_available
    )

    # DEPLOY 门禁仅看“健康硬指标”，避免冷启动阶段的策略类指标误触发回滚。
    if stage.name != "DEPLOY":
        if metrics["trading_halted_true_ratio"] > stage.max_trading_halted_true_ratio:
            protection_fail_reasons.append(
                "trading_halted=true 占比超阈值: "
                f"{metrics['trading_halted_true_ratio']:.4f} > "
                f"{stage.max_trading_halted_true_ratio:.4f}"
            )

        if stage.require_gate_window and gate_window_count <= 0:
            protection_fail_reasons.append("未检测到 Gate 窗口判定（GATE_CHECK_PASSED/FAILED）")
        if (
            stage.require_gate_pass
            and gate_window_count > 0
            and metrics["gate_check_passed_count"] <= 0
        ):
            protection_fail_reasons.append("未检测到 GATE_CHECK_PASSED（S5 要求至少一个通过窗口）")

        if (
            stage.require_evolution_init
            and metrics["self_evolution_init_total_count"] <= 0
            and metrics["self_evolution_action_total_count"] <= 0
            and not evolution_runtime_evidence_sufficient
        ):
            protection_fail_reasons.append("未检测到 SELF_EVOLUTION_INIT/SELF_EVOLUTION_ACTION")

        if (
            stage.require_execution_activity
            and execution_activity_count <= 0
            and not policy_flat_dominant
        ):
            execution_fail_reasons.append("未检测到执行活动（BYBIT_SUBMIT/enqueued/fills 全为 0）")
        if stage.name == "S5" and policy_flat_open_position:
            execution_fail_reasons.append(
                "policy-flat 窗口结束仍有残余仓位（S5 强门禁）: "
                f"last_abs_notional_usd={last_abs_notional_usd:.6f}, "
                f"policy_flat_window_count={policy_flat_window_count}"
            )
        if (
            stage.min_strategy_mix_nonzero_windows > 0
            and metrics["strategy_mix_nonzero_window_count"]
            < stage.min_strategy_mix_nonzero_windows
            and not policy_flat_dominant
        ):
            execution_fail_reasons.append(
                "未检测到有效策略信号窗口（strategy_mix.samples>0），"
                f"S5 至少要求 {stage.min_strategy_mix_nonzero_windows} 个窗口"
            )

        start_abs_notional = account_pnl.get("first_abs_notional_usd")
        if (
            stage.require_flat_start
            and isinstance(start_abs_notional, (int, float))
            and start_abs_notional > stage.max_start_abs_notional_usd
        ):
            protection_fail_reasons.append(
                "运行窗口起点非平仓状态，S5 验收要求平仓起跑: "
                f"abs_notional={start_abs_notional:.6f} > "
                f"threshold={stage.max_start_abs_notional_usd:.6f}"
            )

        if (
            gate_window_count >= stage.gate_fail_hard_min_windows
            and metrics["gate_check_fail_ratio"] > stage.gate_fail_hard_max_fail_ratio
        ):
            protection_fail_reasons.append(
                "Gate 失败率过高（强闭环阻断）: "
                f"fail_ratio={metrics['gate_check_fail_ratio']:.4f}, "
                f"threshold={stage.gate_fail_hard_max_fail_ratio:.4f}"
            )
        if (
            stage.name == "S5"
            and metrics["self_evolution_action_count"]
            >= MIN_S5_EVOLUTION_ACTIONS_FOR_UPDATE_WARN
            and metrics["self_evolution_learnability_pass_count"]
            >= MIN_S5_LEARNABILITY_PASS_FOR_UPDATE_WARN
            and metrics["self_evolution_effective_update_count"]
            < max(0, s5_min_effective_updates)
        ):
            protection_fail_reasons.append(
                "SELF_EVOLUTION 有评估无有效更新（S5 强门禁）: "
                f"action_count={metrics['self_evolution_action_count']}, "
                f"learnability_pass_count={metrics['self_evolution_learnability_pass_count']}, "
                f"effective_update_count={metrics['self_evolution_effective_update_count']}, "
                f"required>={max(0, s5_min_effective_updates)}"
            )
        if (
            stage.name == "S5"
            and metrics["funnel_fills_runtime_count"] < max(0, s5_min_fill_windows)
            and not policy_flat_dominant
        ):
            execution_fail_reasons.append(
                "执行样本不足（S5 强门禁）: "
                f"fill_windows={metrics['funnel_fills_runtime_count']}, "
                f"required>={max(0, s5_min_fill_windows)}"
            )
        if (
            stage.name == "S5"
            and metrics["regime_trend_runtime_count"] >= max(0, s5_min_trend_runtime_windows)
            and metrics["strategy_mix_nonzero_window_count"] <= 0
            and execution_activity_count <= 0
        ):
            execution_fail_reasons.append(
                "TREND 桶出现但未形成策略参与或执行样本（S5 反退化门禁）: "
                f"trend_runtime_windows={metrics['regime_trend_runtime_count']}, "
                f"required>={max(0, s5_min_trend_runtime_windows)}"
            )
        s5_net_quality_required_fills = max(0, s5_min_realized_net_per_fill_windows)
        s5_attribution_quality_required_fills = max(
            1, s5_net_quality_required_fills
        )
        attribution_quality_fill_count = int(
            metrics.get("execution_attribution_quality_fill_count", 0) or 0
        )
        attribution_realized_net_per_fill = metrics.get(
            "execution_attribution_realized_net_per_fill"
        )
        attribution_quality_ready = (
            attribution_quality_fill_count >= s5_attribution_quality_required_fills
            and isinstance(attribution_realized_net_per_fill, (int, float))
        )
        if (
            stage.name == "S5"
            and metrics["funnel_fills_runtime_count"]
            >= s5_net_quality_required_fills
            and not attribution_quality_ready
            and metrics["execution_window_runtime_count"] <= 0
        ):
            execution_fail_reasons.append(
                "执行净收益质量门禁无法评估（S5 强门禁）: "
                "fills 窗口已达标但 execution_window 指标缺失，"
                "请检查运行时日志字段与 assess 解析口径是否一致"
            )
        elif (
            stage.name == "S5"
            and attribution_quality_ready
            and attribution_realized_net_per_fill < s5_min_realized_net_per_fill_usd
        ):
            execution_fail_reasons.append(
                "执行净收益质量未达标（S5 强门禁，execution_attribution）: "
                f"realized_net_per_fill={attribution_realized_net_per_fill:.6f}, "
                f"threshold={s5_min_realized_net_per_fill_usd:.6f}, "
                f"attribution_fills={attribution_quality_fill_count}, "
                f"required_fills>={s5_attribution_quality_required_fills}"
            )
        elif (
            stage.name == "S5"
            and metrics["funnel_fills_runtime_count"]
            >= s5_net_quality_required_fills
            and isinstance(metrics["realized_net_per_fill"], (int, float))
            and metrics["realized_net_per_fill"] < s5_min_realized_net_per_fill_usd
        ):
            execution_fail_reasons.append(
                "执行净收益质量未达标（S5 强门禁）: "
                f"realized_net_per_fill={metrics['realized_net_per_fill']:.6f}, "
                f"threshold={s5_min_realized_net_per_fill_usd:.6f}, "
                f"fill_windows={metrics['funnel_fills_runtime_count']}, "
                f"required_windows>={s5_net_quality_required_fills}"
            )
        if (
            stage.name == "S5"
            and s5_min_equity_change_usd is not None
            and metrics["runtime_account_samples"]
            >= max(0, s5_min_equity_change_samples)
        ):
            equity_change_usd = account_pnl.get("equity_change_usd")
            if not isinstance(equity_change_usd, (int, float)):
                protection_fail_reasons.append(
                    "权益变化门禁无法评估：缺少 equity_change_usd 采样"
                )
            elif equity_change_usd < s5_min_equity_change_usd:
                protection_fail_reasons.append(
                    "权益变化未达标（S5 强门禁）: "
                    f"equity_change_usd={equity_change_usd:.6f}, "
                    f"threshold={s5_min_equity_change_usd:.6f}, "
                    f"account_samples={metrics['runtime_account_samples']}, "
                    f"required_samples>={max(0, s5_min_equity_change_samples)}"
                )
        if (
            stage.name == "S5"
            and s5_max_equity_vs_realized_gap_usd is not None
            and metrics["runtime_account_samples"]
            >= max(0, s5_min_equity_change_samples)
        ):
            gap_usd = metrics.get("equity_vs_realized_net_gap_usd")
            if not isinstance(gap_usd, (int, float)):
                protection_fail_reasons.append(
                    "权益与已实现净盈亏偏差门禁无法评估：缺少 gap 采样"
                )
            elif abs(gap_usd) > s5_max_equity_vs_realized_gap_usd:
                protection_fail_reasons.append(
                    "权益与已实现净盈亏偏差过大（S5 强门禁）: "
                    f"gap_usd={gap_usd:.6f}, "
                    f"threshold={s5_max_equity_vs_realized_gap_usd:.6f}, "
                    f"account_samples={metrics['runtime_account_samples']}, "
                    f"required_samples>={max(0, s5_min_equity_change_samples)}"
                )
        if (
            stage.name == "S5"
            and s5_protection_enabled
            and metrics["protective_order_missing_count"]
            > max(0, s5_max_protective_order_missing_count)
        ):
            protection_fail_reasons.append(
                "保护单缺失事件超阈值（S5 必检项）: "
                f"protective_order_missing_count={metrics['protective_order_missing_count']}, "
                f"threshold<={max(0, s5_max_protective_order_missing_count)}"
            )

    fail_reasons.extend(protection_fail_reasons)
    fail_reasons.extend(execution_fail_reasons)

    if protection_fail_reasons:
        protection_status = "FAIL"
    else:
        protection_status = "PASS"

    has_execution_activity = execution_activity_count > 0
    has_strategy_activity = metrics["strategy_mix_nonzero_window_count"] > 0

    if execution_fail_reasons:
        execution_status = "FAIL"
    elif policy_flat_dominant:
        execution_status = "NOT_EVALUATED"
    elif has_execution_activity or has_strategy_activity:
        execution_status = "PASS"
    else:
        execution_status = "NOT_EVALUATED"

    if policy_flat_dominant:
        runtime_validation_mode = "POLICY_FLAT_PROTECTION"
    elif has_execution_activity:
        runtime_validation_mode = "EXECUTION_ACTIVE"
    elif execution_status == "PASS":
        runtime_validation_mode = "STRATEGY_ACTIVE_NO_EXECUTION"
    else:
        runtime_validation_mode = "IDLE_OR_INSUFFICIENT_SAMPLE"

    # 软告警：DEPLOY 仅保留硬失败项，避免上线门禁被策略类黄灯误阻断。
    if stage.name != "DEPLOY":
        runtime_boot_age_seconds = metrics.get("runtime_boot_age_seconds")
        if (
            stage.name == "S5"
            and isinstance(runtime_boot_age_seconds, (int, float))
            and runtime_boot_age_seconds < DEFAULT_S5_MIN_EFFECTIVE_RUNTIME_AGE_SECONDS
        ):
            warn_reasons.append(
                "运行进程启动时间过短，当前窗口不能代表 12/24h live 验证："
                f"boot_age_seconds={runtime_boot_age_seconds:.0f}, "
                f"min_effective_seconds={DEFAULT_S5_MIN_EFFECTIVE_RUNTIME_AGE_SECONDS}, "
                "请确认 assess 未紧跟 deploy/restart 执行"
            )
        if (
            metrics["reconcile_mismatch_count"] > 0
            and metrics["reconcile_autoresync_count"] <= 0
        ):
            warn_reasons.append("出现对账不一致但未观察到 AUTORESYNC")
        if (
            gate_window_count >= stage.gate_warn_min_windows
            and metrics["gate_check_fail_ratio"] > stage.gate_warn_max_fail_ratio
        ):
            warn_reasons.append(
                "Gate 失败率偏高，建议复核策略活跃度/门槛参数: "
                f"fail_ratio={metrics['gate_check_fail_ratio']:.4f}, "
                f"threshold={stage.gate_warn_max_fail_ratio:.4f}"
            )
        if (
            metrics["trading_halted_true_count"] > 0
            and metrics["trading_halted_true_ratio"]
            <= stage.max_trading_halted_true_ratio
        ):
            warn_reasons.append(
                "出现短暂 trading_halted=true，但占比在阈值内，建议复核 Gate/对账参数"
            )
        if (
            stage.name == "S5"
            and metrics["self_evolution_action_count"] <= 0
            and metrics["self_evolution_init_count"] > 0
            and not policy_flat_dominant
        ):
            warn_reasons.append(
                "未观测到 SELF_EVOLUTION_ACTION，建议检查 update_interval 与样本门槛"
            )
        evolution_effective_update_count = metrics[
            "self_evolution_effective_update_count"
        ]
        if (
            stage.name == "S5"
            and metrics["self_evolution_action_count"]
            >= MIN_S5_EVOLUTION_ACTIONS_FOR_UPDATE_WARN
            and metrics["self_evolution_learnability_pass_count"]
            >= MIN_S5_LEARNABILITY_PASS_FOR_UPDATE_WARN
            and evolution_effective_update_count <= 0
        ):
            warn_reasons.append(
                "SELF_EVOLUTION 长时间仅评估未更新，建议放宽反事实/因子IC更新门槛: "
                f"action_count={metrics['self_evolution_action_count']}, "
                f"learnability_pass_count={metrics['self_evolution_learnability_pass_count']}, "
                f"effective_update_count={evolution_effective_update_count}"
            )
        if (
            metrics["execution_window_runtime_count"] > 0
            and metrics["order_filtered_cost_count"] >= 20
            and metrics["filtered_cost_ratio_avg"] >= 0.90
        ):
            warn_reasons.append(
                "ORDER_FILTERED_COST 占比较高，建议复核 entry gate 参数: "
                f"filtered_cost_ratio_avg={metrics['filtered_cost_ratio_avg']:.4f}"
            )
        if (
            metrics["execution_window_runtime_count"] > 0
            and metrics["filtered_cost_ratio_avg"] >= 0.70
            and metrics["filtered_cost_near_miss_ratio_avg"] >= 0.10
        ):
            warn_reasons.append(
                "成本门近阈值拦截占比偏高，建议复核 near-miss 容差与信号边际估计: "
                f"filtered_cost_near_miss_ratio_avg={metrics['filtered_cost_near_miss_ratio_avg']:.4f}, "
                f"entry_edge_gap_avg_bps={metrics['entry_edge_gap_avg_bps']:.4f}"
            )
        if metrics["execution_quality_guard_active_count"] > 0:
            warn_reasons.append(
                "执行质量守卫触发，入场门槛已抬升，建议复核手续费与执行路径: "
                f"active_count={metrics['execution_quality_guard_active_count']}, "
                f"penalty_bps_avg={metrics['execution_quality_guard_penalty_bps_avg']:.4f}"
            )
        symbol_guard_event_unrecovered = (
            metrics["execution_symbol_quality_guard_enter_count"]
            > metrics["execution_symbol_quality_guard_exit_count"]
        )
        symbol_guard_still_active = (
            metrics["execution_quality_guard_symbol_active_count_latest"] > 0
            or symbol_guard_event_unrecovered
        )
        symbol_guard_reinforced = (
            metrics["execution_symbol_quality_guard_reinforce_count"] > 0
        )
        if symbol_guard_reinforced or symbol_guard_still_active:
            warn_reasons.append(
                "symbol 级执行质量守卫触发，低质量币对已进入单币冷却/惩罚: "
                f"enter={metrics['execution_symbol_quality_guard_enter_count']}, "
                f"reinforce={metrics['execution_symbol_quality_guard_reinforce_count']}, "
                f"exit={metrics['execution_symbol_quality_guard_exit_count']}, "
                f"symbol_active_latest={metrics['execution_quality_guard_symbol_active_count_latest']}, "
                f"symbol_active_max={metrics['execution_quality_guard_symbol_active_count_max']}"
            )
        if (
            stage.name == "S5"
            and has_execution_activity
            and 0
            < attribution_quality_fill_count
            < s5_attribution_quality_required_fills
            and isinstance(attribution_realized_net_per_fill, (int, float))
            and attribution_realized_net_per_fill < s5_min_realized_net_per_fill_usd
        ):
            warn_reasons.append(
                "执行归因净收益已为负但样本尚未达到 S5 强门槛，"
                "建议继续观察或触发单币质量冷却: "
                f"execution_attribution_realized_net_per_fill="
                f"{attribution_realized_net_per_fill:.6f}, "
                f"threshold={s5_min_realized_net_per_fill_usd:.6f}, "
                f"attribution_fills={attribution_quality_fill_count}, "
                f"required_fills>={s5_attribution_quality_required_fills}"
            )
        if (
            stage.name == "S5"
            and has_execution_activity
            and metrics["execution_attribution_fill_count"]
            >= MIN_S5_EXECUTION_NEGATIVE_SYMBOL_FILL_COUNT_WARN
            and metrics["execution_attribution_symbol_count"] > 0
            and metrics["execution_attribution_negative_symbol_count"]
            == metrics["execution_attribution_symbol_count"]
        ):
            warn_reasons.append(
                "成交已活跃但所有成交币对按 realized_net_per_fill 统计均为负，"
                "当前瓶颈已从采样转为执行净质量/费用控制: "
                f"symbols={metrics['execution_attribution_symbol_count']}, "
                f"fills={metrics['execution_attribution_fill_count']}, "
                f"best_symbol={metrics['execution_attribution_best_symbol']}, "
                "best_net_per_fill="
                f"{metrics['execution_attribution_best_symbol_realized_net_per_fill']:.6f}, "
                f"worst_symbol={metrics['execution_attribution_worst_symbol']}, "
                "worst_net_per_fill="
                f"{metrics['execution_attribution_worst_symbol_realized_net_per_fill']:.6f}"
            )
        if (
            s5_protection_enabled
            and metrics["funnel_fills_runtime_count"] > 0
            and metrics["protection_refresh_count"] <= 0
            and metrics["protection_cancelled_count"] <= 0
        ):
            warn_reasons.append(
                "保护单已启用但未观测到 PROTECTION_REFRESH/PROTECTION_CANCELLED，"
                "建议核查保护挂单主链是否生效"
            )
        if s5_protection_enabled and metrics["tp_attach_failed_count"] > 0:
            warn_reasons.append(
                "TP 挂单失败事件已观测到（S5 观测项）: "
                f"tp_attach_failed_count={metrics['tp_attach_failed_count']}"
            )
        if stage.name == "S5" and metrics["profit_protection_crossed_count"] > 0:
            warn_reasons.append(
                "盈利保护候选 SL 已被当前价格穿越，系统已跳过提交以避免无效/即时触发条件单: "
                f"profit_protection_crossed_count={metrics['profit_protection_crossed_count']}"
            )
        if (
            stage.name == "S5"
            and metrics["exit_capture_sample_count"] >= 3
            and metrics["exit_capture_mean_path_mfe_bps"]
            > max(float(metrics.get("fee_bps_per_fill", 0.0) or 0.0), 1.0)
            and metrics["exit_capture_mean_capture_ratio"] < 0.20
        ):
            warn_reasons.append(
                "live 退出捕获偏低，行情给过盈利空间但保护/退出未充分锁定: "
                f"samples={metrics['exit_capture_sample_count']}, "
                f"mean_path_mfe_bps={metrics['exit_capture_mean_path_mfe_bps']:.4f}, "
                "mean_captured_net_bps="
                f"{metrics['exit_capture_mean_captured_net_bps']:.4f}, "
                f"mean_capture_ratio={metrics['exit_capture_mean_capture_ratio']:.4f}"
            )
        if (
            stage.name == "S5"
            and metrics["exit_capture_sample_count"] >= 5
            and metrics["exit_capture_low_ratio"] >= 0.50
        ):
            warn_reasons.append(
                "live low_capture 样本占比偏高，下一轮应优先调退出而不是继续加交易频率: "
                f"low_capture_ratio={metrics['exit_capture_low_ratio']:.4f}, "
                f"low_by_symbol={metrics['exit_capture_low_by_symbol']}"
            )
        if (
            metrics["execution_window_runtime_count"] > 0
            and metrics["execution_window_maker_fill_ratio_avg"] < 0.20
            and metrics["fee_bps_per_fill"] >= 6.0
        ):
            warn_reasons.append(
                "Maker 成交占比偏低且成交成本偏高，建议优化挂单成交质量: "
                f"maker_fill_ratio_avg={metrics['execution_window_maker_fill_ratio_avg']:.4f}, "
                f"fee_bps_per_fill={metrics['fee_bps_per_fill']:.4f}"
            )
        liquidity_classified_fills_avg = (
            metrics["execution_window_maker_fills_avg"]
            + metrics["execution_window_taker_fills_avg"]
            + metrics["execution_window_unknown_fills_avg"]
        )
        if (
            metrics["execution_window_runtime_count"] > 0
            and liquidity_classified_fills_avg >= 1.0
        ):
            if metrics["execution_window_liquidity_source_runtime_count"] <= 0:
                warn_reasons.append(
                    "成交存在但未观测到流动性来源细分字段，建议升级到最新运行时二进制（explicit/fallback 口径）"
                )
            else:
                if (
                    metrics["execution_window_explicit_liquidity_fill_ratio_avg"]
                    < MIN_EXPLICIT_LIQUIDITY_FILL_RATIO_WARN
                ):
                    warn_reasons.append(
                        "显式流动性标签覆盖率偏低，建议排查交易所回报字段/解析链路: "
                        "execution_window_explicit_liquidity_fill_ratio_avg="
                        f"{metrics['execution_window_explicit_liquidity_fill_ratio_avg']:.4f}, "
                        f"threshold={MIN_EXPLICIT_LIQUIDITY_FILL_RATIO_WARN:.4f}"
                    )
                if (
                    metrics["execution_window_unknown_fill_ratio_avg"]
                    > MAX_UNKNOWN_FILL_RATIO_WARN
                ):
                    warn_reasons.append(
                        "Unknown 流动性成交占比偏高，建议检查成交回报完整性: "
                        "execution_window_unknown_fill_ratio_avg="
                        f"{metrics['execution_window_unknown_fill_ratio_avg']:.4f}, "
                        f"threshold={MAX_UNKNOWN_FILL_RATIO_WARN:.4f}"
                    )
                if (
                    metrics["execution_window_fee_sign_fallback_fill_ratio_avg"]
                    > MAX_FEE_SIGN_FALLBACK_FILL_RATIO_WARN
                ):
                    warn_reasons.append(
                        "fee 符号兜底占比偏高，建议优先修复显式流动性标签覆盖: "
                        "execution_window_fee_sign_fallback_fill_ratio_avg="
                        f"{metrics['execution_window_fee_sign_fallback_fill_ratio_avg']:.4f}, "
                        f"threshold={MAX_FEE_SIGN_FALLBACK_FILL_RATIO_WARN:.4f}"
                    )
        if policy_flat_dominant:
            warn_reasons.append(
                "策略窗口以 policy-flat 为主：本轮主要反映 RANGE/EXTREME 主动空仓保护，"
                f"policy_flat_window_count={policy_flat_window_count}"
            )
            if policy_flat_open_position:
                warn_reasons.append(
                    "policy-flat 窗口仍检测到残余仓位，系统应生成 reduce-only 回收单: "
                    f"last_abs_notional_usd={last_abs_notional_usd:.6f}, "
                    "policy_flat_residual_position_count="
                    f"{metrics['policy_flat_residual_position_count']}"
                )
            if metrics["gate_runtime_policy_flat_exempt_count"] > 0:
                warn_reasons.append(
                    "Gate runtime 已豁免 policy-flat 部分窗口，未将其计入 reduce-only/halt 失败连击: "
                    "gate_runtime_policy_flat_exempt_count="
                    f"{metrics['gate_runtime_policy_flat_exempt_count']}"
                )
            if metrics["regime_trend_runtime_count"] <= 0:
                if metrics["regime_change_trend_count"] > 0:
                    warn_reasons.append(
                        "当前窗口仅出现短暂 TREND 样本但未进入 runtime 状态窗口："
                        "执行质量仍处于等待稳定趋势样本阶段, "
                        f"regime_change_trend_count={metrics['regime_change_trend_count']}, "
                        "symbols="
                        f"{','.join(metrics['regime_change_trend_symbols'])}"
                    )
                elif (
                    metrics["regime_trend_candidate_runtime_count"] > 0
                    or metrics["regime_change_trend_candidate_count"] > 0
                ):
                    warn_reasons.append(
                        "当前窗口出现 TREND_CANDIDATE 但未确认 TREND："
                        "建议复核 regime 门槛/确认周期与多周期趋势口径, "
                        "candidate_windows="
                        f"{metrics['regime_trend_candidate_runtime_count']}, "
                        "candidate_events="
                        f"{metrics['regime_change_trend_candidate_count']}, "
                        "trend_ratio_max="
                        f"{metrics['trend_threshold_ratio_max']:.4f}, "
                        "pending_trend_confirm_events="
                        f"{metrics.get('regime_change_pending_trend_confirmation_count', 0)}, "
                        "pending_ticks_max="
                        f"{metrics.get('regime_change_pending_trend_confirmation_ticks_max', 0)}, "
                        "confirm_ticks_required="
                        f"{metrics.get('regime_change_confirm_ticks_required_max', 0)}"
                    )
                    if metrics["trend_candidate_probe_signal_count"] <= 0:
                        skip_count = metrics.get("trend_candidate_probe_skip_count", 0)
                        skip_detail = ""
                        if skip_count:
                            skip_detail = (
                                ", skip_count="
                                f"{skip_count}, ratio_low="
                                f"{metrics.get('trend_candidate_probe_skip_trend_ratio_count', 0)}, "
                                "strong_ratio_low="
                                f"{metrics.get('trend_candidate_probe_skip_strong_trend_ratio_count', 0)}, "
                                "cooldown="
                                f"{metrics.get('trend_candidate_probe_skip_cooldown_count', 0)}, "
                                "exposure="
                                f"{metrics.get('trend_candidate_probe_skip_exposure_count', 0)}, "
                                "pending_orders="
                                f"{metrics.get('trend_candidate_probe_skip_pending_orders_count', 0)}, "
                                "existing_intent="
                                f"{metrics.get('trend_candidate_probe_skip_existing_intent_count', 0)}, "
                                "window_limit="
                                f"{metrics.get('trend_candidate_probe_skip_window_limit_count', 0)}"
                            )
                        warn_reasons.append(
                            "TREND_CANDIDATE 未产生探针信号：建议检查 "
                            "execution.candidate_probe_* 配置、候选强度阈值、"
                            "空仓/在途订单约束与 Universe 活跃池"
                            f"{skip_detail}"
                        )
                    elif metrics["trend_candidate_probe_enqueued_count"] <= 0:
                        warn_reasons.append(
                            "TREND_CANDIDATE 探针已生成但未入队：建议检查最小下单量、"
                            "成本门 gap、同向在途单限额与节流配置, "
                            "probe_signal_count="
                            f"{metrics['trend_candidate_probe_signal_count']}, "
                            "fee_override_count="
                            f"{metrics['trend_candidate_probe_fee_override_count']}, "
                            "filtered_fee_count="
                            f"{metrics['trend_candidate_probe_filtered_fee_count']}"
                        )
                    elif metrics["trend_candidate_probe_fill_count"] <= 0:
                        warn_reasons.append(
                            "TREND_CANDIDATE 探针已入队但未成交：建议检查 maker post-only "
                            "挂单偏移、撤单/超时、BYBIT_SUBMIT 与流动性标签, "
                            "probe_enqueued_count="
                            f"{metrics['trend_candidate_probe_enqueued_count']}"
                        )
                elif (
                    metrics["regime_warmup_trend_candidate_runtime_count"] > 0
                    or metrics["regime_change_warmup_trend_candidate_count"] > 0
                ):
                    warn_reasons.append(
                        "当前窗口仅在 warmup 阶段出现趋势候选，未转为可交易 TREND_CANDIDATE："
                        "建议延长 trend reserve 驻留、复核 warmup_ticks/Universe 轮换周期, "
                        "warmup_candidate_windows="
                        f"{metrics['regime_warmup_trend_candidate_runtime_count']}, "
                        "warmup_candidate_events="
                        f"{metrics['regime_change_warmup_trend_candidate_count']}, "
                        "symbols="
                        f"{','.join(metrics['regime_change_warmup_trend_candidate_symbols'])}, "
                        "warmup_ratio_max="
                        f"{metrics['warmup_trend_threshold_ratio_max']:.4f}, "
                        "nonwarmup_ratio_max="
                        f"{metrics['nonwarmup_trend_threshold_ratio_max']:.4f}"
                    )
                else:
                    warn_reasons.append(
                        "当前窗口未出现 TREND 样本：runtime 通过仅代表保护逻辑通过，"
                        "执行质量仍处于等待趋势样本阶段"
                    )
            else:
                warn_reasons.append(
                    "本轮 runtime 通过仅代表保护逻辑通过，执行质量未完成验证"
                )
        if metrics["reconcile_anomaly_reduce_only_true_count"] > 0:
            warn_reasons.append(
                "对账异常保护触发 reduce-only，建议核查回报链路与对账口径: "
                f"reduce_only_true_count={metrics['reconcile_anomaly_reduce_only_true_count']}, "
                f"mismatch_count={metrics['reconcile_mismatch_count']}, "
                f"autoresync_count={metrics['reconcile_autoresync_count']}, "
                f"deferred_count={metrics['reconcile_deferred_count']}, "
                f"streak_max={metrics['reconcile_anomaly_streak_max']}"
            )
        if (
            metrics["concentration_runtime_count"] > 0
            and metrics["concentration_top1_share_max"]
            >= MAX_TOP1_CONCENTRATION_SHARE_WARN
            and metrics["concentration_symbol_count_avg"] >= 1.5
        ):
            warn_reasons.append(
                "仓位集中度偏高，建议复核 Universe 与单币风险预算: "
                f"top1_share_max={metrics['concentration_top1_share_max']:.4f}, "
                f"threshold={MAX_TOP1_CONCENTRATION_SHARE_WARN:.4f}"
            )

        integrator_takeover_mode_count = (
            metrics["integrator_mode_canary_count"] + metrics["integrator_mode_active_count"]
        )
        if (
            integrator_takeover_mode_count > 0
            and metrics["integrator_policy_applied_count"] <= 0
        ):
            warn_reasons.append("Integrator 处于 canary/active 但未观测到策略接管事件")
        if (
            integrator_takeover_mode_count > 0
            and metrics["integrator_shadow_scored_runtime_count"] <= 0
        ):
            warn_reasons.append("Integrator 处于 canary/active 但未观测到 shadow scored>0")
        if int(metrics.get("integrator_nan_feature_skip_count", 0)) > 0:
            nan_skip_features = metrics.get("integrator_nan_feature_skip_by_feature", {})
            feature_detail = ""
            if isinstance(nan_skip_features, dict) and nan_skip_features:
                feature_items = sorted(
                    nan_skip_features.items(),
                    key=lambda item: (-int(item[1]), str(item[0])),
                )
                feature_detail = ", features=" + ",".join(
                    f"{name}:{count}" for name, count in feature_items[:5]
                )
            warn_reasons.append(
                "Integrator 观测到 NaN feature skip，建议复核 miner/live 特征对齐: "
                f"count={metrics['integrator_nan_feature_skip_count']}"
                f"{feature_detail}"
            )
        if (
            int(metrics.get("feature_large_abs_line_count", 0)) > 0
            and float(metrics.get("feature_large_abs_line_ratio", 0.0))
            >= FEATURE_LARGE_ABS_RATIO_WARN_THRESHOLD
        ):
            warn_reasons.append(
                "Integrator FEATURES 出现大尺度特征，可能存在 live/replay 特征分布不一致或未归一化 raw price 泄漏: "
                f"large_lines={metrics['feature_large_abs_line_count']}, "
                f"feature_lines={metrics['feature_line_count']}, "
                f"ratio={metrics['feature_large_abs_line_ratio']:.4f}, "
                f"max_abs={metrics['feature_max_abs_value']:.4f}, "
                f"max_feature={metrics['feature_max_abs_feature']}"
            )

        if execution_activity_count <= 0:
            gap_usd = metrics.get("equity_vs_realized_net_gap_usd")
            if isinstance(gap_usd, (int, float)) and abs(gap_usd) >= 50.0:
                if account_sync_status == "NOISY_WHILE_FLAT":
                    warn_reasons.append(
                        "权益变化与已实现净盈亏偏差较大且无执行活动，建议检查资金同步/统计口径: "
                        f"gap_usd={gap_usd:.6f}"
                    )
        if account_sync_status == "OPEN_POSITION_GAP":
            warn_reasons.append(
                "权益变化与已实现净盈亏偏差较大且末尾仍有持仓，偏差可能包含未实现盈亏或资金项: "
                f"gap_usd={gap_usd:.6f}, last_abs_notional_usd={float(last_abs_notional_usd):.6f}"
            )
    if fail_reasons:
        verdict = "FAIL"
    elif warn_reasons:
        verdict = "PASS_WITH_ACTIONS"
    else:
        verdict = "PASS"

    return {
        "stage": stage.name,
        "verdict": verdict,
        "runtime_validation_mode": runtime_validation_mode,
        "protection_status": protection_status,
        "execution_status": execution_status,
        "market_context_status": market_context_status,
        "account_sync_status": account_sync_status,
        "metrics": metrics,
        "account_pnl": account_pnl,
        "execution_attribution": execution_attribution,
        "exit_capture_live": exit_capture_live,
        "fail_reasons": fail_reasons,
        "protection_fail_reasons": protection_fail_reasons,
        "execution_fail_reasons": execution_fail_reasons,
        "warn_reasons": warn_reasons,
        "flat_start_rebased": flat_start_rebased,
        "flat_start_rebase_cutoff_utc": flat_start_rebase_cutoff_utc,
        "flat_start_rebase_fallback": flat_start_rebase_fallback,
    }


def print_report(report: Dict[str, object]) -> None:
    print(f"STAGE: {report['stage']}")
    print(f"VERDICT: {report['verdict']}")
    if "runtime_validation_mode" in report:
        print(f"RUNTIME_VALIDATION_MODE: {report['runtime_validation_mode']}")
    if "protection_status" in report:
        print(f"PROTECTION_STATUS: {report['protection_status']}")
    if "execution_status" in report:
        print(f"EXECUTION_STATUS: {report['execution_status']}")
    if "market_context_status" in report:
        print(f"MARKET_CONTEXT_STATUS: {report['market_context_status']}")
    if "account_sync_status" in report:
        print(f"ACCOUNT_SYNC_STATUS: {report['account_sync_status']}")
    print("METRICS:")
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    for key in sorted(metrics.keys()):
        print(f"  - {key}: {metrics[key]}")
    if bool(report.get("flat_start_rebased")):
        print(
            "FLAT_START_REBASE: "
            f"applied=true, cutoff_utc={report.get('flat_start_rebase_cutoff_utc')}"
        )
    if bool(report.get("flat_start_rebase_fallback")):
        print("FLAT_START_REBASE: applied=false, fallback_to_original=true")

    account_pnl = report.get("account_pnl", {})
    if isinstance(account_pnl, dict):
        print("ACCOUNT_PNL:")
        for key in (
            "samples",
            "first_sample_utc",
            "last_sample_utc",
            "first_equity_usd",
            "last_equity_usd",
            "equity_change_usd",
            "equity_change_pct",
            "day_start_equity_usd",
            "equity_change_vs_day_start_usd",
            "equity_change_vs_day_start_pct",
            "max_equity_usd_observed",
            "peak_to_last_drawdown_pct",
            "max_drawdown_pct_observed",
            "max_abs_notional_usd_observed",
            "first_notional_usd",
            "first_abs_notional_usd",
            "last_notional_usd",
            "last_abs_notional_usd",
            "start_flat",
            "end_flat",
            "fee_samples",
            "first_realized_pnl_usd",
            "last_realized_pnl_usd",
            "realized_pnl_change_usd",
            "realized_pnl_change_raw_usd",
            "first_fee_usd",
            "last_fee_usd",
            "fee_change_usd",
            "fee_change_raw_usd",
            "first_realized_net_pnl_usd",
            "last_realized_net_pnl_usd",
            "realized_net_pnl_change_usd",
            "realized_net_pnl_change_raw_usd",
            "account_counter_reset_count",
            "account_counter_reset_detected",
        ):
            print(f"  - {key}: {account_pnl.get(key)}")

    execution_attribution = report.get("execution_attribution", {})
    if isinstance(execution_attribution, dict) and execution_attribution:
        fills = execution_attribution.get("fills", {})
        submit = execution_attribution.get("submit", {})
        runtime_fill_windows = execution_attribution.get("runtime_fill_windows", {})
        print("EXECUTION_ATTRIBUTION:")
        if isinstance(submit, dict):
            print(f"  - submit_total: {submit.get('total')}")
            print(f"  - submit_by_symbol: {submit.get('by_symbol')}")
            print(f"  - submit_by_order_type: {submit.get('by_order_type')}")
            print(f"  - submit_by_liquidity: {submit.get('by_liquidity')}")
            print(f"  - submit_by_purpose: {submit.get('by_purpose')}")
        if isinstance(fills, dict):
            print(f"  - fill_total: {fills.get('total')}")
            print(f"  - fill_fee_usd: {fills.get('fee_usd')}")
            print(f"  - fill_notional_abs_usd: {fills.get('notional_abs_usd')}")
            print(f"  - fill_by_symbol: {fills.get('by_symbol')}")
            print(f"  - fill_by_liquidity: {fills.get('by_liquidity')}")
            print(f"  - quality_by_symbol: {fills.get('quality_by_symbol')}")
            print(f"  - probe: {fills.get('probe')}")
            print(f"  - main: {fills.get('main')}")
        if isinstance(runtime_fill_windows, dict):
            print(f"  - runtime_fill_windows: {runtime_fill_windows}")

    exit_capture_live = report.get("exit_capture_live", {})
    if isinstance(exit_capture_live, dict) and exit_capture_live.get("sample_count", 0):
        print("EXIT_CAPTURE_LIVE:")
        for key in (
            "sample_count",
            "low_capture_count",
            "low_capture_ratio",
            "mean_path_mfe_bps",
            "mean_captured_gross_bps",
            "mean_captured_net_bps",
            "mean_fee_bps",
            "mean_capture_ratio",
            "p50_capture_ratio",
            "by_symbol",
            "low_by_symbol",
            "by_purpose",
        ):
            print(f"  - {key}: {exit_capture_live.get(key)}")

    fail_reasons = report["fail_reasons"]
    warn_reasons = report["warn_reasons"]
    assert isinstance(fail_reasons, list)
    assert isinstance(warn_reasons, list)

    if fail_reasons:
        print("FAIL_REASONS:")
        for item in fail_reasons:
            print(f"  - {item}")
    if warn_reasons:
        print("WARN_REASONS:")
        for item in warn_reasons:
            print(f"  - {item}")


def main() -> int:
    args = parse_args()
    log_path = Path(args.log)
    if not log_path.exists():
        print(f"[ERROR] 日志文件不存在: {log_path}", file=sys.stderr)
        return 2

    stage = STAGE_RULES[args.stage]
    min_runtime_status = (
        args.min_runtime_status
        if args.min_runtime_status is not None
        else stage.min_runtime_status
    )
    if min_runtime_status < 0:
        print("[ERROR] --min_runtime_status 必须大于等于 0", file=sys.stderr)
        return 2
    if args.s5_min_effective_updates < 0:
        print("[ERROR] --s5-min-effective-updates 必须大于等于 0", file=sys.stderr)
        return 2
    if args.s5_min_realized_net_per_fill_windows < 0:
        print(
            "[ERROR] --s5-min-realized-net-per-fill-windows 必须大于等于 0",
            file=sys.stderr,
        )
        return 2
    if args.s5_min_fill_windows < 0:
        print("[ERROR] --s5-min-fill-windows 必须大于等于 0", file=sys.stderr)
        return 2
    if args.s5_max_protective_order_missing_count < 0:
        print(
            "[ERROR] --s5-max-protective-order-missing-count 必须大于等于 0",
            file=sys.stderr,
        )
        return 2
    if args.s5_min_equity_change_samples < 0:
        print(
            "[ERROR] --s5-min-equity-change-samples 必须大于等于 0",
            file=sys.stderr,
        )
        return 2
    if (
        args.s5_max_equity_vs_realized_gap_usd is not None
        and args.s5_max_equity_vs_realized_gap_usd < 0
    ):
        print(
            "[ERROR] --s5-max-equity-vs-realized-gap-usd 不能为负数",
            file=sys.stderr,
        )
        return 2

    text = load_text(log_path)
    report = assess(
        text,
        stage,
        min_runtime_status,
        s5_min_effective_updates=args.s5_min_effective_updates,
        s5_min_realized_net_per_fill_usd=args.s5_min_realized_net_per_fill_usd,
        s5_min_realized_net_per_fill_windows=args.s5_min_realized_net_per_fill_windows,
        s5_min_fill_windows=args.s5_min_fill_windows,
        s5_min_equity_change_usd=args.s5_min_equity_change_usd,
        s5_min_equity_change_samples=args.s5_min_equity_change_samples,
        s5_max_equity_vs_realized_gap_usd=args.s5_max_equity_vs_realized_gap_usd,
        s5_protection_enabled=args.s5_protection_enabled,
        s5_profit_protection_enabled=args.s5_profit_protection_enabled,
        s5_max_protective_order_missing_count=args.s5_max_protective_order_missing_count,
        s5_min_trend_runtime_windows=args.s5_min_trend_runtime_windows,
    )
    print_report(report)

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON written: {out_path}")

    verdict = report["verdict"]
    return 0 if verdict in {"PASS", "PASS_WITH_ACTIONS"} else 1


if __name__ == "__main__":
    sys.exit(main())
