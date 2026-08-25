#!/usr/bin/env python3
"""Audit whether verified account fees can rescue a failed structural edge."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import pathlib
import time
from typing import Any, Dict, Mapping, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SCHEMA_VERSION = "account_structural_economics_audit_v1"
POLICY_SCHEMA_VERSION = "account_structural_economics_policy_v1"
UPSTREAM_SCHEMA_VERSION = "cross_venue_funding_differential_experiment_v1"
FROZEN_POLICY_IDENTITY_SHA256 = (
    "ed31b73398cb76b347e147d279464cb6eea77df2d41de743f0e38af8b1dc065a"
)


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: pathlib.Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return payload


def atomic_write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} is not numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} is not finite")
    return result


def validate_policy(path: pathlib.Path) -> Dict[str, Any]:
    policy = read_json(path)
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError("account structural economics policy schema mismatch")
    if canonical_sha256(policy) != FROZEN_POLICY_IDENTITY_SHA256:
        raise ValueError("account structural economics policy identity mismatch")
    if not (
        policy.get("research_domain") == "development_only"
        and policy.get("promotion_evidence") is False
        and policy.get("experiment_id")
        == "sol_cross_venue_account_cost_rescue_audit_v1"
    ):
        raise ValueError("account structural economics policy domain mismatch")
    source = policy.get("input_contract")
    private = policy.get("private_account_contract")
    zero = policy.get("zero_fee_upper_bound")
    decisions = policy.get("decision_contract")
    authorities = policy.get("authorities")
    if not (
        isinstance(source, Mapping)
        and source.get("upstream_schema_version") == UPSTREAM_SCHEMA_VERSION
        and source.get("required_upstream_decision")
        == "STOP_CROSS_VENUE_FUNDING_DIFFERENTIAL_FAMILY"
        and source.get("symbol") == "SOLUSDT"
        and finite_number(
            source.get("reference_notional_usd_per_venue"),
            "reference_notional_usd_per_venue",
        )
        == 380.0
        and finite_number(
            source.get("minimum_available_balance_multiplier"),
            "minimum_available_balance_multiplier",
        )
        == 1.25
    ):
        raise ValueError("account structural economics input contract mismatch")
    if not (
        isinstance(private, Mapping)
        and private.get("read_only_requests_only") is True
        and private.get("record_api_key") is False
        and private.get("record_account_uid") is False
        and private.get("record_exact_balance") is False
        and isinstance(private.get("bybit"), Mapping)
        and private["bybit"].get("environment") == "demo"
        and private["bybit"].get("base_url") == "https://api-demo.bybit.com"
        and isinstance(private.get("binance"), Mapping)
        and private["binance"].get("environment") == "demo"
        and private["binance"].get("base_url")
        == "https://demo-fapi.binance.com"
    ):
        raise ValueError("private account read-only contract mismatch")
    expected_zero = {
        "upstream_bybit_taker_fee_bps_per_fill": 5.5,
        "upstream_binance_taker_fee_bps_per_fill": 5.5,
        "upstream_bybit_slippage_bps_per_fill": 1.0,
        "upstream_binance_slippage_bps_per_fill": 1.0,
        "bybit_round_trip_slippage_bps": 2.0,
        "binance_round_trip_slippage_bps": 2.0,
        "intervenue_round_trip_leg_risk_bps": 2.0,
        "half_spread_already_in_upstream_gross": True,
        "execution_style": "four_taker_fills_round_trip",
        "minimum_net_taker_fee_bps_per_fill_after_rebate": 0.0,
        "fee_rebates_capped_at_gross_trading_fees": True,
        "external_liquidity_subsidies_in_scope": False,
        "stress_execution_cost_multiplier": 1.25,
        "minimum_stress_net_bps": 0.0,
    }
    if zero != expected_zero:
        raise ValueError("zero-fee upper-bound contract mismatch")
    if decisions != {
        "continue_decision": "ALLOW_DISTINCT_STRUCTURAL_EDGE_INTAKE",
        "wait_decision": "WAIT_FOR_COMPLETE_ACCOUNT_COST_VERIFICATION",
        "stop_decision": "STOP_ACCOUNT_FEE_TIER_RESCUE_FOR_CROSS_VENUE_FUNDING",
        "account_verification_required_to_continue": True,
    }:
        raise ValueError("account structural economics decision contract mismatch")
    if authorities != {
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
    }:
        raise ValueError("account structural economics authority mismatch")
    return policy


def validate_upstream(path: pathlib.Path, policy: Mapping[str, Any]) -> Dict[str, Any]:
    report = read_json(path)
    if not (
        report.get("schema_version") == UPSTREAM_SCHEMA_VERSION
        and report.get("status") == "COMPLETE"
        and report.get("fully_verifiable") is True
        and report.get("research_domain") == "historical_development_only"
        and report.get("promotion_evidence") is False
        and report.get("promotion_authority") is False
        and report.get("demo_activation_authorized") is False
        and report.get("live_activation_authorized") is False
        and report.get("research_decision")
        == policy["input_contract"]["required_upstream_decision"]
    ):
        raise ValueError("upstream cross-venue funding report is not verifiable")
    execution = report.get("execution_contract")
    execution_policy = execution.get("execution") if isinstance(execution, Mapping) else None
    zero = policy["zero_fee_upper_bound"]
    if not (
        isinstance(execution_policy, Mapping)
        and finite_number(
            execution_policy.get("bybit_taker_fee_bps_per_fill"),
            "upstream bybit taker fee",
        )
        == zero["upstream_bybit_taker_fee_bps_per_fill"]
        and finite_number(
            execution_policy.get("binance_taker_fee_bps_per_fill"),
            "upstream binance taker fee",
        )
        == zero["upstream_binance_taker_fee_bps_per_fill"]
        and finite_number(
            execution_policy.get("bybit_slippage_bps_per_fill"),
            "upstream bybit slippage",
        )
        == zero["upstream_bybit_slippage_bps_per_fill"]
        and finite_number(
            execution_policy.get("binance_slippage_bps_per_fill"),
            "upstream binance slippage",
        )
        == zero["upstream_binance_slippage_bps_per_fill"]
        and finite_number(
            execution_policy.get("intervenue_leg_risk_bps_per_round_trip"),
            "upstream leg risk",
        )
        == zero["intervenue_round_trip_leg_risk_bps"]
        and finite_number(
            execution_policy.get("stress_execution_cost_multiplier"),
            "upstream stress multiplier",
        )
        == zero["stress_execution_cost_multiplier"]
        and execution.get("historical_price_is_executable_bbo") is False
        and execution.get("historical_proxy_can_authorize_demo") is False
    ):
        raise ValueError("upstream execution contract drift")
    maximum = report.get("hindsight_oracle", {}).get("maximum_candidate")
    if not isinstance(maximum, Mapping):
        raise ValueError("upstream maximum candidate is unavailable")
    for field in (
        "gross_bps",
        "base_bps",
        "stress_bps",
        "execution_cost_bps",
        "basis_bps",
        "funding_bps",
    ):
        finite_number(maximum.get(field), f"maximum_candidate.{field}")
    return report


def _request_json(
    url: str, *, headers: Mapping[str, str] | None, timeout_sec: float
) -> Tuple[Dict[str, Any] | None, str | None]:
    request = Request(url, headers=dict(headers or {}))
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return None, f"HTTP_ERROR_{int(exc.code)}"
    except (URLError, TimeoutError):
        return None, "TRANSPORT_ERROR"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "INVALID_JSON"
    if not isinstance(payload, dict):
        return None, "INVALID_RESPONSE_ROOT"
    return payload, None


def _credential_pair(
    spec: Mapping[str, Any], *, allow_fallback: bool
) -> Tuple[str, str, str]:
    key = str(os.environ.get(str(spec["api_key_env"]), "")).strip()
    secret = str(os.environ.get(str(spec["api_secret_env"]), "")).strip()
    source = "dedicated"
    if not key and not secret and allow_fallback:
        key = str(os.environ.get(str(spec["fallback_api_key_env"]), "")).strip()
        secret = str(os.environ.get(str(spec["fallback_api_secret_env"]), "")).strip()
        source = "legacy_fallback"
    return key, secret, source


def _unavailable_account(reason: str, attempted: bool = False) -> Dict[str, Any]:
    return {
        "status": "NOT_READY",
        "request_attempted": attempted,
        "fee_rate_verified": False,
        "capital_sufficiency_verified": False,
        "capital_sufficient_for_frozen_reference": False,
        "reason_code": reason,
    }


def query_bybit_account(
    policy: Mapping[str, Any], *, timeout_sec: float
) -> Dict[str, Any]:
    spec = policy["private_account_contract"]["bybit"]
    key, secret, source = _credential_pair(spec, allow_fallback=True)
    if not key and not secret:
        return _unavailable_account("CREDENTIALS_UNAVAILABLE")
    if not key or not secret:
        return _unavailable_account("CREDENTIAL_PAIR_INCOMPLETE")

    def signed_get(path: str, query: str) -> Tuple[Dict[str, Any] | None, str | None]:
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        signature = hmac.new(
            secret.encode("utf-8"),
            f"{timestamp}{key}{recv_window}{query}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-BAPI-API-KEY": key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "User-Agent": "ai-trade-account-economics/1.0",
        }
        payload, error = _request_json(
            f"{spec['base_url']}{path}?{query}",
            headers=headers,
            timeout_sec=timeout_sec,
        )
        if error is not None or payload is None:
            return None, error
        try:
            code = int(payload.get("retCode", -1))
        except (TypeError, ValueError):
            return None, "INVALID_API_CODE"
        if code != 0:
            return None, f"API_ERROR_{code}"
        return payload, None

    symbol = policy["input_contract"]["symbol"]
    fee_query = urlencode((('category', 'linear'), ('symbol', symbol)))
    fee_payload, fee_error = signed_get(str(spec["fee_endpoint"]), fee_query)
    if fee_error is not None or fee_payload is None:
        return _unavailable_account(f"FEE_{fee_error}", attempted=True)
    rows = fee_payload.get("result", {}).get("list", [])
    row = next(
        (item for item in rows if isinstance(item, Mapping) and item.get("symbol") == symbol),
        None,
    )
    if not isinstance(row, Mapping):
        return _unavailable_account("FEE_SYMBOL_MISSING", attempted=True)
    try:
        maker_bps = float(row["makerFeeRate"]) * 10_000.0
        taker_bps = float(row["takerFeeRate"]) * 10_000.0
    except (KeyError, TypeError, ValueError):
        return _unavailable_account("FEE_RATE_INVALID", attempted=True)
    if not all(math.isfinite(value) and -100.0 <= value <= 100.0 for value in (maker_bps, taker_bps)):
        return _unavailable_account("FEE_RATE_OUT_OF_RANGE", attempted=True)

    balance_query = urlencode((('accountType', 'UNIFIED'),))
    balance_payload, balance_error = signed_get(
        str(spec["balance_endpoint"]), balance_query
    )
    if balance_error is not None or balance_payload is None:
        result = _unavailable_account(f"BALANCE_{balance_error}", attempted=True)
        result.update(
            {
                "fee_rate_verified": True,
                "maker_fee_bps": maker_bps,
                "taker_fee_bps": taker_bps,
                "credential_source": source,
            }
        )
        return result
    accounts = balance_payload.get("result", {}).get("list", [])
    account = accounts[0] if isinstance(accounts, list) and accounts else None
    try:
        available = float(account["totalAvailableBalance"])
    except (KeyError, TypeError, ValueError):
        result = _unavailable_account("BALANCE_AVAILABLE_INVALID", attempted=True)
        result.update(
            {
                "fee_rate_verified": True,
                "maker_fee_bps": maker_bps,
                "taker_fee_bps": taker_bps,
                "credential_source": source,
            }
        )
        return result
    required = (
        float(policy["input_contract"]["reference_notional_usd_per_venue"])
        * float(policy["input_contract"]["minimum_available_balance_multiplier"])
    )
    sufficient = math.isfinite(available) and available >= required
    return {
        "status": "VERIFIED",
        "request_attempted": True,
        "fee_rate_verified": True,
        "maker_fee_bps": maker_bps,
        "taker_fee_bps": taker_bps,
        "capital_sufficiency_verified": math.isfinite(available),
        "capital_sufficient_for_frozen_reference": sufficient,
        "credential_source": source,
        "exact_balance_recorded": False,
    }


def query_binance_account(
    policy: Mapping[str, Any], *, timeout_sec: float
) -> Dict[str, Any]:
    spec = policy["private_account_contract"]["binance"]
    key, secret, source = _credential_pair(spec, allow_fallback=False)
    if not key and not secret:
        return _unavailable_account("CREDENTIALS_UNAVAILABLE")
    if not key or not secret:
        return _unavailable_account("CREDENTIAL_PAIR_INCOMPLETE")
    headers = {
        "X-MBX-APIKEY": key,
        "User-Agent": "ai-trade-account-economics/1.0",
    }
    time_payload, time_error = _request_json(
        f"{spec['base_url']}{spec['time_endpoint']}",
        headers=None,
        timeout_sec=timeout_sec,
    )
    if time_error is not None or time_payload is None:
        return _unavailable_account(f"SERVER_TIME_{time_error}", attempted=True)
    try:
        server_time = int(time_payload["serverTime"])
    except (KeyError, TypeError, ValueError):
        return _unavailable_account("SERVER_TIME_INVALID", attempted=True)

    def signed_get(path: str, parameters: Tuple[Tuple[str, str], ...]):
        query = urlencode(parameters + (("recvWindow", "5000"), ("timestamp", str(server_time))))
        signature = hmac.new(
            secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        payload, error = _request_json(
            f"{spec['base_url']}{path}?{query}&signature={signature}",
            headers=headers,
            timeout_sec=timeout_sec,
        )
        if error is not None or payload is None:
            return None, error
        if "code" in payload:
            try:
                code = int(payload["code"])
            except (TypeError, ValueError):
                return None, "INVALID_API_CODE"
            if code < 0:
                return None, f"API_ERROR_{code}"
        return payload, None

    symbol = str(policy["input_contract"]["symbol"])
    fee_payload, fee_error = signed_get(
        str(spec["fee_endpoint"]), (("symbol", symbol),)
    )
    if fee_error is not None or fee_payload is None:
        return _unavailable_account(f"FEE_{fee_error}", attempted=True)
    try:
        maker_bps = float(fee_payload["makerCommissionRate"]) * 10_000.0
        taker_bps = float(fee_payload["takerCommissionRate"]) * 10_000.0
        rpi_bps = float(fee_payload["rpiCommissionRate"]) * 10_000.0
    except (KeyError, TypeError, ValueError):
        return _unavailable_account("FEE_RATE_INVALID", attempted=True)
    if not all(
        math.isfinite(value) and -100.0 <= value <= 100.0
        for value in (maker_bps, taker_bps, rpi_bps)
    ):
        return _unavailable_account("FEE_RATE_OUT_OF_RANGE", attempted=True)
    balance_payload, balance_error = signed_get(str(spec["balance_endpoint"]), ())
    if balance_error is not None or balance_payload is None:
        result = _unavailable_account(f"BALANCE_{balance_error}", attempted=True)
        result.update(
            {
                "fee_rate_verified": True,
                "maker_fee_bps": maker_bps,
                "taker_fee_bps": taker_bps,
                "rpi_fee_bps": rpi_bps,
                "credential_source": source,
            }
        )
        return result
    try:
        available = float(balance_payload["availableBalance"])
    except (KeyError, TypeError, ValueError):
        result = _unavailable_account("BALANCE_AVAILABLE_INVALID", attempted=True)
        result.update(
            {
                "fee_rate_verified": True,
                "maker_fee_bps": maker_bps,
                "taker_fee_bps": taker_bps,
                "rpi_fee_bps": rpi_bps,
                "credential_source": source,
            }
        )
        return result
    required = (
        float(policy["input_contract"]["reference_notional_usd_per_venue"])
        * float(policy["input_contract"]["minimum_available_balance_multiplier"])
    )
    sufficient = math.isfinite(available) and available >= required
    return {
        "status": "VERIFIED",
        "request_attempted": True,
        "fee_rate_verified": True,
        "maker_fee_bps": maker_bps,
        "taker_fee_bps": taker_bps,
        "rpi_fee_bps": rpi_bps,
        "capital_sufficiency_verified": math.isfinite(available),
        "capital_sufficient_for_frozen_reference": sufficient,
        "credential_source": source,
        "exact_balance_recorded": False,
    }


def zero_fee_economics(
    upstream: Mapping[str, Any], policy: Mapping[str, Any]
) -> Dict[str, Any]:
    maximum = upstream["hindsight_oracle"]["maximum_candidate"]
    gross = finite_number(maximum["gross_bps"], "maximum gross")
    base = finite_number(maximum["base_bps"], "maximum base")
    stress = finite_number(maximum["stress_bps"], "maximum stress")
    execution = finite_number(maximum["execution_cost_bps"], "maximum execution")
    zero = policy["zero_fee_upper_bound"]
    multiplier = float(zero["stress_execution_cost_multiplier"])
    leg = float(zero["intervenue_round_trip_leg_risk_bps"])
    frozen_rate = float(zero["upstream_bybit_taker_fee_bps_per_fill"]) + float(
        zero["upstream_bybit_slippage_bps_per_fill"]
    )
    if execution <= leg or frozen_rate <= 0.0:
        raise ValueError("upstream execution cost cannot be decomposed")
    variable_execution = execution - leg
    slippage_fraction = float(
        zero["upstream_bybit_slippage_bps_per_fill"]
    ) / frozen_rate
    zero_fee_execution = variable_execution * slippage_fraction + leg
    base_capital = gross - execution - base
    stress_capital = gross - execution * multiplier - stress
    zero_fee_base = gross - zero_fee_execution - base_capital
    zero_fee_stress = gross - zero_fee_execution * multiplier - stress_capital
    return {
        "method": "exact_upstream_variable_cost_decomposition_v1",
        "direction": maximum.get("direction"),
        "horizon_hours": maximum.get("horizon_hours"),
        "upstream_gross_bps": gross,
        "upstream_basis_bps": finite_number(maximum["basis_bps"], "maximum basis"),
        "upstream_funding_bps": finite_number(
            maximum["funding_bps"], "maximum funding"
        ),
        "upstream_execution_cost_bps": execution,
        "inferred_base_capital_cost_bps": base_capital,
        "inferred_stress_capital_cost_bps": stress_capital,
        "zero_fee_non_fee_execution_cost_bps": zero_fee_execution,
        "zero_fee_base_net_bps": zero_fee_base,
        "zero_fee_stress_net_bps": zero_fee_stress,
        "minimum_stress_net_bps": float(zero["minimum_stress_net_bps"]),
        "passes": zero_fee_stress > float(zero["minimum_stress_net_bps"]),
        "all_account_trading_fees_assumed_zero": True,
        "four_taker_fills_round_trip": True,
        "fee_rebates_capped_at_gross_trading_fees": True,
        "external_liquidity_subsidies_in_scope": False,
        "maker_fill_assumed": False,
        "historical_price_is_executable_bbo": False,
    }


def nominal_verified_account_economics(
    zero_fee: Mapping[str, Any], accounts: Mapping[str, Mapping[str, Any]]
) -> Dict[str, Any] | None:
    if not all(accounts[venue].get("fee_rate_verified") is True for venue in accounts):
        return None
    bybit = finite_number(accounts["bybit"].get("taker_fee_bps"), "Bybit taker fee")
    binance = finite_number(
        accounts["binance"].get("taker_fee_bps"), "Binance taker fee"
    )
    nominal_round_trip_fee = 2.0 * (bybit + binance)
    execution = (
        float(zero_fee["zero_fee_non_fee_execution_cost_bps"])
        + nominal_round_trip_fee
    )
    stress = (
        float(zero_fee["upstream_gross_bps"])
        - execution * 1.25
        - float(zero_fee["inferred_stress_capital_cost_bps"])
    )
    return {
        "method": "nominal_matched_notional_account_fee_repricing_v1",
        "round_trip_account_taker_fee_bps": nominal_round_trip_fee,
        "repriced_execution_cost_bps": execution,
        "repriced_stress_net_bps": stress,
        "passes": stress > 0.0,
        "promotion_evidence": False,
    }


def run(args: argparse.Namespace) -> Dict[str, Any]:
    config_path = pathlib.Path(args.config).resolve()
    upstream_path = pathlib.Path(args.upstream_report).resolve()
    policy = validate_policy(config_path)
    upstream = validate_upstream(upstream_path, policy)
    zero_fee = zero_fee_economics(upstream, policy)
    if args.private_mode == "skip":
        accounts = {
            "bybit": _unavailable_account("PRIVATE_REQUESTS_SKIPPED"),
            "binance": _unavailable_account("PRIVATE_REQUESTS_SKIPPED"),
        }
    else:
        accounts = {
            "bybit": query_bybit_account(policy, timeout_sec=args.timeout_sec),
            "binance": query_binance_account(policy, timeout_sec=args.timeout_sec),
        }
    verified_count = sum(
        account.get("status") == "VERIFIED" for account in accounts.values()
    )
    account_status = (
        "COMPLETE"
        if verified_count == 2
        else "PARTIAL"
        if verified_count == 1
        else "UNAVAILABLE"
    )
    repriced = nominal_verified_account_economics(zero_fee, accounts)
    decisions = policy["decision_contract"]
    reasons = []
    if not zero_fee["passes"]:
        decision = decisions["stop_decision"]
        reasons.extend(
            [
                "zero_fee_stress_upper_bound_non_positive",
                "account_fee_tier_cannot_rescue_upstream_mechanism",
            ]
        )
        next_action = "require_materially_distinct_structural_edge_proposal"
    elif account_status != "COMPLETE":
        decision = decisions["wait_decision"]
        reasons.append("complete_account_cost_verification_unavailable")
        next_action = "provide_read_only_demo_credentials_for_both_venues"
    else:
        decision = decisions["continue_decision"]
        reasons.append("zero_fee_bound_and_account_verification_passed")
        next_action = "preregister_distinct_structural_edge_intake"
    for venue, account in accounts.items():
        if account.get("status") != "VERIFIED":
            reasons.append(
                f"{venue}_account_verification_{str(account.get('reason_code') or 'not_ready').lower()}"
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "COMPLETE",
        "fully_verifiable_zero_fee_upper_bound": True,
        "account_cost_verification_status": account_status,
        "research_domain": "account_cost_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "experiment_id": policy["experiment_id"],
        "policy": {
            "path": str(config_path),
            "sha256": sha256_file(config_path),
            "identity_sha256": canonical_sha256(policy),
        },
        "input": {
            "upstream_report_path": str(upstream_path),
            "upstream_report_sha256": sha256_file(upstream_path),
            "upstream_research_decision": upstream["research_decision"],
        },
        "privacy_contract": {
            "read_only_requests_only": True,
            "api_key_recorded": False,
            "api_secret_recorded": False,
            "account_uid_recorded": False,
            "exact_balance_recorded": False,
        },
        "reference_capital_contract": {
            "notional_usd_per_venue": policy["input_contract"][
                "reference_notional_usd_per_venue"
            ],
            "minimum_available_balance_multiplier": policy["input_contract"][
                "minimum_available_balance_multiplier"
            ],
            "independent_margin_required": True,
        },
        "account_observations": accounts,
        "zero_fee_upper_bound": zero_fee,
        "verified_account_repricing": repriced,
        "structural_decision": decision,
        "reason_codes": reasons,
        "next_action": next_action,
    }


def not_ready(args: argparse.Namespace, reason: str) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "NOT_READY",
        "fully_verifiable_zero_fee_upper_bound": False,
        "account_cost_verification_status": "UNAVAILABLE",
        "research_domain": "account_cost_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "structural_decision": "NOT_READY",
        "reason_codes": [reason],
        "next_action": "restore_verifiable_upstream_and_policy_inputs",
        "policy": {"path": str(pathlib.Path(args.config).resolve())},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-report", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout-sec", type=float, default=12.0)
    parser.add_argument("--private-mode", choices=("auto", "skip"), default="auto")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = run(args)
    except Exception as exc:
        report = not_ready(args, f"invalid_input:{type(exc).__name__}:{exc}")
    atomic_write_json(pathlib.Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report.get("status") == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
