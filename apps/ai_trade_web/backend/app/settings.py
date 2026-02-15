from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _parse_bool(raw: str, default: bool) -> bool:
    if raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(raw: str, default: int, min_value: int, max_value: int) -> int:
    if raw == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    if value < min_value:
        return min_value
    if value > max_value:
        return max_value
    return value


@dataclass(frozen=True)
class Settings:
    reports_root: Path
    models_root: Path
    config_root: Path
    control_root: Path
    api_title: str
    api_version: str
    write_enabled: bool
    admin_token: str
    require_latest_pass: bool
    allow_pass_with_actions: bool
    high_risk_two_man_rule: bool
    high_risk_required_approvals: int
    high_risk_cooldown_seconds: int


def load_settings() -> Settings:
    return Settings(
        reports_root=Path(os.getenv("AI_TRADE_REPORTS_ROOT", "/opt/ai-trade/data/reports/closed_loop")),
        models_root=Path(os.getenv("AI_TRADE_MODELS_ROOT", "/opt/ai-trade/data/models")),
        config_root=Path(os.getenv("AI_TRADE_CONFIG_ROOT", "/opt/ai-trade/config")),
        control_root=Path(os.getenv("AI_TRADE_CONTROL_ROOT", "/opt/ai-trade/data/control_plane")),
        api_title=os.getenv("AI_TRADE_WEB_API_TITLE", "ai-trade control plane"),
        api_version=os.getenv("AI_TRADE_WEB_API_VERSION", "v1"),
        write_enabled=_parse_bool(os.getenv("AI_TRADE_WEB_ENABLE_WRITE", ""), False),
        admin_token=os.getenv("AI_TRADE_WEB_ADMIN_TOKEN", "").strip(),
        require_latest_pass=_parse_bool(
            os.getenv("AI_TRADE_WEB_REQUIRE_LATEST_PASS", ""), True
        ),
        allow_pass_with_actions=_parse_bool(
            os.getenv("AI_TRADE_WEB_ALLOW_PASS_WITH_ACTIONS", ""), False
        ),
        high_risk_two_man_rule=_parse_bool(
            os.getenv("AI_TRADE_WEB_HIGH_RISK_TWO_MAN_RULE", ""), True
        ),
        high_risk_required_approvals=_parse_int(
            os.getenv("AI_TRADE_WEB_HIGH_RISK_REQUIRED_APPROVALS", ""), 2, 2, 5
        ),
        high_risk_cooldown_seconds=_parse_int(
            os.getenv("AI_TRADE_WEB_HIGH_RISK_COOLDOWN_SECONDS", ""), 180, 0, 86400
        ),
    )
