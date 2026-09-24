"""Fail-closed promotion evidence; never clears the process safety journal."""
from __future__ import annotations

import re

SCHEMA = "evolution_safety_evidence_v1"


def extract(text: str) -> dict:
    # Use the original log, BEFORE flat-start rebasing. A later clean window or
    # boot must not erase a withdrawal that is present in this evidence input.
    samples = 0
    invalid = 0
    missing = 0
    withdrawn = 0
    identities = set()
    for line in text.splitlines():
        if "RUNTIME_STATUS:" not in line:
            continue
        samples += 1
        values = re.findall(r"\bevolution_safety_withdrawn=([^,\s}]+)", line)
        boots = re.findall(r"\bboot=\{id=([^,\s}]+)", line)
        blocks = re.findall(r"\bevolution_safety_identity=\{([^}]*)\}", line)
        if not values or not blocks or not boots:
            missing += 1
        if len(values) != 1 or values[0] not in ("true", "false"):
            invalid += 1
        withdrawn += int("true" in values)
        if len(boots) != 1 or len(blocks) != 1:
            invalid += 1
            continue
        match = re.fullmatch(
            r"runtime_config_sha256=([0-9a-f]{64}), trade_bot_sha256=([0-9a-f]{64})",
            blocks[0],
        )
        if not match:
            invalid += 1
            continue
        identities.add((boots[0], match[1], match[2]))
    latched = len(re.findall(r"\bEVOLUTION_SAFETY_WITHDRAWAL_LATCHED:", text))
    persist_failed = len(re.findall(r"\bEVOLUTION_SAFETY_PERSIST_FAILED:", text))
    clear = samples > 0 and invalid == 0 and missing == 0 and len(identities) == 1
    status = "WITHDRAWN" if withdrawn or latched or persist_failed else "CLEAR" if clear else "UNPROVEN"
    identity = next(iter(identities)) if len(identities) == 1 else ("", "", "")
    return {
        "schema_version": SCHEMA, "scope": "ORIGINAL_LOG_WINDOW_NO_REBASE",
        "status": status, "runtime_status_count": samples, "missing_count": missing,
        "invalid_count": invalid, "identity_count": len(identities),
        "withdrawn_count": withdrawn, "latched_event_count": latched,
        "persist_failed_count": persist_failed,
        "boot_id": identity[0], "runtime_config_sha256": identity[1],
        "trade_bot_sha256": identity[2],
        "grants_trading_authority": False, "proves_economic_qualification": False,
    }


def rejection_reasons(evidence: object, expected: dict, boot_id: str) -> list[str]:
    prefix = "evolution safety: "
    if not isinstance(evidence, dict) or evidence.get("schema_version") != SCHEMA:
        return [prefix + "evidence missing or unsupported"]
    reasons = []
    if evidence.get("scope") != "ORIGINAL_LOG_WINDOW_NO_REBASE":
        reasons.append(prefix + "evidence scope invalid")
    if evidence.get("status") != "CLEAR":
        reasons.append(prefix + "state is not explicitly CLEAR")
    for key in ("grants_trading_authority", "proves_economic_qualification"):
        if evidence.get(key) is not False:
            reasons.append(prefix + "evidence authority invalid")
    counts = ("runtime_status_count", "missing_count", "invalid_count", "identity_count",
              "withdrawn_count", "latched_event_count", "persist_failed_count")
    if any(type(evidence.get(key)) is not int or evidence[key] < 0 for key in counts):
        reasons.append(prefix + "evidence counts missing or invalid")
    elif (evidence["runtime_status_count"] <= 0 or evidence["identity_count"] != 1
          or any(evidence[key] != 0 for key in counts if key not in ("runtime_status_count", "identity_count"))):
        reasons.append(prefix + "evidence incomplete or withdrawal observed")
    for key, value in (("boot_id", boot_id),
                       ("runtime_config_sha256", expected.get("runtime_config_sha256")),
                       ("trade_bot_sha256", expected.get("trade_bot_sha256"))):
        if not value or evidence.get(key) != value:
            reasons.append(prefix + "identity mismatch: " + key)
    return reasons
