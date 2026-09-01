"""Immutable identities shared by option VRP audit and summary layers."""

from __future__ import annotations

from typing import Any, Dict


FROZEN_POLICY_IDENTITY_SHA256 = (
    "e1902110278fb2c72ec091a73f2cdb38ba394dfbc4741864ca85b9c3d08a17ee"
)
FROZEN_MANIFEST_IDENTITY_SHA256 = (
    "446625e67754f1fd07e149e4ff5bd1623677138aef028e40ce0d35b8a0284a9d"
)
FROZEN_POLICY_IDENTITY_SHA256_V2 = (
    "6f23634e0f5e6a708d76387f6552e9089a0ef830bbb82790300d97ececd5530b"
)
FROZEN_MANIFEST_IDENTITY_SHA256_V2 = (
    "13b62a179c2e3131762918063bfecfb1a2f9c853693144d0dc2a8428b2f58aeb"
)

FROZEN_CONTRACTS: Dict[str, Dict[str, Any]] = {
    FROZEN_POLICY_IDENTITY_SHA256: {
        "manifest_sha256": FROZEN_MANIFEST_IDENTITY_SHA256,
        "experiment_id": "btc_bybit_usdt_option_vrp_sequential_payoff_v1",
        "policy_path": "config/option_variance_risk_premium_sequential_payoff.json",
        "observation_start_epoch_ms": 1787686200000,
        "action_ids": [
            "no_trade",
            "short_atm_straddle_7d",
            "long_atm_straddle_7d",
        ],
    },
    FROZEN_POLICY_IDENTITY_SHA256_V2: {
        "manifest_sha256": FROZEN_MANIFEST_IDENTITY_SHA256_V2,
        "experiment_id": "btc_bybit_usdt_option_vrp_1d_sequential_payoff_v2",
        "policy_path": "config/option_variance_risk_premium_sequential_payoff_v2.json",
        "observation_start_epoch_ms": 1788242400000,
        "action_ids": [
            "no_trade",
            "short_atm_straddle_1d",
            "long_atm_straddle_1d",
        ],
    },
}

FROZEN_IDENTITIES_BY_EXPERIMENT: Dict[str, Dict[str, Any]] = {
    contract["experiment_id"]: {
        "policy_sha256": policy_sha256,
        "manifest_sha256": contract["manifest_sha256"],
        "observation_start_epoch_ms": contract["observation_start_epoch_ms"],
    }
    for policy_sha256, contract in FROZEN_CONTRACTS.items()
}
