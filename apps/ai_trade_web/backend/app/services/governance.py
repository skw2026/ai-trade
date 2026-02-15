from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .report_store import ReportStore


PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]+\.yaml$")
DRAFT_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z_[a-f0-9]{8}$")


class GovernanceError(RuntimeError):
    pass


@dataclass
class GovernanceService:
    report_store: ReportStore
    control_root: Path
    default_require_latest_pass: bool = True
    default_allow_pass_with_actions: bool = False
    default_high_risk_two_man_rule: bool = True
    default_high_risk_required_approvals: int = 2
    default_high_risk_cooldown_seconds: int = 180

    def __post_init__(self) -> None:
        self.control_root.mkdir(parents=True, exist_ok=True)
        self._drafts_dir().mkdir(parents=True, exist_ok=True)
        self._backups_dir().mkdir(parents=True, exist_ok=True)

    def _state_path(self) -> Path:
        return self.control_root / "state.json"

    def _audit_path(self) -> Path:
        return self.control_root / "audit.log"

    def _drafts_dir(self) -> Path:
        return self.control_root / "drafts"

    def _backups_dir(self) -> Path:
        return self.control_root / "backups"

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _now_compact(self) -> str:
        return self._now().strftime("%Y%m%dT%H%M%SZ")

    def _now_iso(self) -> str:
        return self._now().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _sha256(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _write_json_atomic(self, path: Path, payload: Dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _append_audit(self, event: Dict[str, Any]) -> None:
        with self._audit_path().open("a", encoding="utf-8") as out:
            out.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _default_state(self) -> Dict[str, Any]:
        return {
            "read_only_mode": False,
            "publish_frozen": False,
            "require_latest_pass": self.default_require_latest_pass,
            "allow_pass_with_actions": self.default_allow_pass_with_actions,
            "high_risk_two_man_rule": self.default_high_risk_two_man_rule,
            "high_risk_required_approvals": max(2, int(self.default_high_risk_required_approvals)),
            "high_risk_cooldown_seconds": max(0, int(self.default_high_risk_cooldown_seconds)),
            "updated_at_utc": self._now_iso(),
        }

    def get_state(self) -> Dict[str, Any]:
        path = self._state_path()
        if not path.exists():
            state = self._default_state()
            self._write_json_atomic(path, state)
            return state

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GovernanceError(f"state json invalid: {exc}") from exc

        if not isinstance(payload, dict):
            raise GovernanceError("state json must be object")
        state = self._default_state()
        for key, value in payload.items():
            state[key] = value
        return state

    def update_state(self, patch: Dict[str, Any], actor: str) -> Dict[str, Any]:
        state = self.get_state()
        bool_keys = {
            "read_only_mode",
            "publish_frozen",
            "require_latest_pass",
            "allow_pass_with_actions",
            "high_risk_two_man_rule",
        }
        int_keys = {
            "high_risk_required_approvals",
            "high_risk_cooldown_seconds",
        }
        allowed = bool_keys | int_keys
        unknown = sorted(k for k in patch.keys() if k not in allowed)
        if unknown:
            raise GovernanceError(f"unsupported state fields: {unknown}")

        for key, value in patch.items():
            if key in bool_keys:
                if not isinstance(value, bool):
                    raise GovernanceError(f"state field must be bool: {key}")
                state[key] = value
                continue

            # bool is a subclass of int, reject it for int fields.
            if not isinstance(value, int) or isinstance(value, bool):
                raise GovernanceError(f"state field must be int: {key}")
            if key == "high_risk_required_approvals":
                if value < 2 or value > 5:
                    raise GovernanceError("high_risk_required_approvals must be in [2, 5]")
            if key == "high_risk_cooldown_seconds":
                if value < 0 or value > 86400:
                    raise GovernanceError("high_risk_cooldown_seconds must be in [0, 86400]")
            state[key] = value

        state["updated_at_utc"] = self._now_iso()
        self._write_json_atomic(self._state_path(), state)
        self._append_audit(
            {
                "ts_utc": self._now_iso(),
                "actor": actor,
                "action": "state.update",
                "result": "ok",
                "state": state,
            }
        )
        return state

    def read_audit(self, limit: int = 200) -> List[Dict[str, Any]]:
        path = self._audit_path()
        if not path.exists():
            return []

        lines = path.read_text(encoding="utf-8").splitlines()
        output: List[Dict[str, Any]] = []
        for line in lines[-max(1, limit):]:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                output.append(parsed)
        return output

    def _draft_path(self, draft_id: str) -> Path:
        return self._drafts_dir() / f"{draft_id}.json"

    def _parse_utc(self, text: str) -> Optional[datetime]:
        raw = text.strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def _draft_approvals(self, draft: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw = draft.get("approvals")
        if not isinstance(raw, list):
            return []
        output: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            actor = str(item.get("actor", "")).strip()
            approved_at_utc = str(item.get("approved_at_utc", "")).strip()
            note = str(item.get("note", ""))
            if not actor or not approved_at_utc:
                continue
            if self._parse_utc(approved_at_utc) is None:
                continue
            output.append(
                {
                    "actor": actor,
                    "approved_at_utc": approved_at_utc,
                    "note": note,
                }
            )
        output.sort(key=lambda x: x["approved_at_utc"])
        return output

    def _preview_digest(self, payload: Dict[str, Any]) -> str:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return self._sha256(text)

    def _build_publish_guard(
        self,
        draft_id: str,
        state: Dict[str, Any],
        diff: Dict[str, Any],
        risk_flags: List[Dict[str, Any]],
        approvals: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        high_risk = any(str(item.get("severity", "")).upper() == "HIGH" for item in risk_flags)
        high_risk_enforced = high_risk and bool(state.get("high_risk_two_man_rule", True))
        required_approval_count = int(state.get("high_risk_required_approvals", 2))
        required_approval_count = max(2, min(5, required_approval_count))
        cooldown_seconds = int(state.get("high_risk_cooldown_seconds", 0))
        cooldown_seconds = max(0, min(86400, cooldown_seconds))

        unique_approval_actors = sorted({entry["actor"] for entry in approvals})
        approval_satisfied = len(unique_approval_actors) >= required_approval_count

        latest_approval_at_utc = approvals[-1]["approved_at_utc"] if approvals else None
        cooldown_remaining_seconds = 0
        if high_risk_enforced and latest_approval_at_utc and cooldown_seconds > 0:
            latest_approval_at = self._parse_utc(latest_approval_at_utc)
            if latest_approval_at is not None:
                elapsed = int((self._now() - latest_approval_at).total_seconds())
                cooldown_remaining_seconds = max(0, cooldown_seconds - elapsed)
        if not high_risk_enforced:
            cooldown_remaining_seconds = 0
            approval_satisfied = True

        preview_digest = self._preview_digest(
            {
                "draft_id": draft_id,
                "diff": diff,
                "risk_flags": risk_flags,
            }
        )

        return {
            "confirm_phrase": f"PUBLISH {draft_id}",
            "preview_digest": preview_digest,
            "high_risk_enforced": high_risk_enforced,
            "required_approval_count": required_approval_count,
            "current_approval_count": len(unique_approval_actors),
            "approval_satisfied": approval_satisfied,
            "cooldown_seconds": cooldown_seconds if high_risk_enforced else 0,
            "cooldown_remaining_seconds": cooldown_remaining_seconds,
            "latest_approval_at_utc": latest_approval_at_utc,
            "approved_actors": unique_approval_actors,
        }

    def _validate_profile_name(self, profile_name: str) -> None:
        if not PROFILE_RE.match(profile_name):
            raise GovernanceError(f"invalid profile name: {profile_name}")

    def _required_section_checks(self, content: str) -> Dict[str, Any]:
        required = ["system", "exchange", "risk", "execution", "strategy"]
        missing: List[str] = []
        for section in required:
            if not re.search(rf"(?m)^\s*{re.escape(section)}:\s*$", content):
                missing.append(section)

        both_demo_and_testnet = (
            re.search(r"(?m)^\s*demo_trading\s*:\s*true\s*$", content) is not None
            and re.search(r"(?m)^\s*testnet\s*:\s*true\s*$", content) is not None
        )

        return {
            "required_sections": required,
            "missing_sections": missing,
            "has_required_sections": len(missing) == 0,
            "conflict_demo_and_testnet": both_demo_and_testnet,
            "line_count": len(content.splitlines()),
            "size_bytes": len(content.encode("utf-8")),
        }

    def validate_config_text(self, content: str) -> Dict[str, Any]:
        checks = self._required_section_checks(content)
        ok = checks["has_required_sections"] and not checks["conflict_demo_and_testnet"]
        warnings: List[str] = []
        if checks["line_count"] < 20:
            warnings.append("config lines too short, verify template completeness")
        if checks["size_bytes"] > 512 * 1024:
            warnings.append("config file unusually large")
        return {
            "ok": ok,
            "checks": checks,
            "warnings": warnings,
        }

    def _parse_scalar(self, raw: str) -> Any:
        text = raw.strip()
        if not text:
            return ""

        # Drop wrapping quotes for common scalar values.
        if (text.startswith('"') and text.endswith('"')) or (
            text.startswith("'") and text.endswith("'")
        ):
            text = text[1:-1].strip()

        lowered = text.lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        if lowered in {"null", "~"}:
            return None
        if re.match(r"^-?\d+$", text):
            try:
                return int(text)
            except ValueError:
                return text
        if re.match(r"^-?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?$", text):
            try:
                return float(text)
            except ValueError:
                return text
        return text

    def _extract_scalar_paths(self, content: str) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        stack: List[tuple[int, str]] = []
        key_re = re.compile(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$")

        for raw_line in content.splitlines():
            # Keep parser intentionally conservative: only parse mapping-style lines.
            line = raw_line.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            if line.lstrip().startswith("- "):
                continue

            indent = len(line) - len(line.lstrip(" "))
            stripped = line.strip()
            matched = key_re.match(stripped)
            if not matched:
                continue

            key = matched.group(1)
            value = matched.group(2).strip()

            while stack and indent <= stack[-1][0]:
                stack.pop()

            if value == "":
                stack.append((indent, key))
                continue

            path = ".".join([segment for _, segment in stack] + [key])
            values[path] = self._parse_scalar(value)
        return values

    def _as_float(self, value: Any) -> Optional[float]:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            raw = value.strip()
            if re.match(r"^-?(?:\d+|\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?$", raw):
                try:
                    return float(raw)
                except ValueError:
                    return None
        return None

    def _risk_flags_from_diff(self, before: Dict[str, Any], after: Dict[str, Any]) -> List[Dict[str, Any]]:
        flags: List[Dict[str, Any]] = []

        def append_flag(
            key: str,
            severity: str,
            reason: str,
            before_value: Any,
            after_value: Any,
        ) -> None:
            flags.append(
                {
                    "key": key,
                    "severity": severity,
                    "reason": reason,
                    "before": before_value,
                    "after": after_value,
                }
            )

        for key in (
            "risk.max_abs_notional_usd",
            "execution.max_order_notional",
            "strategy.signal_notional_usd",
        ):
            old_value = self._as_float(before.get(key))
            new_value = self._as_float(after.get(key))
            if old_value is None or new_value is None or old_value <= 0:
                continue
            delta_ratio = (new_value - old_value) / old_value
            if delta_ratio >= 1.0:
                append_flag(
                    key,
                    "HIGH",
                    "numeric_limit_increase_ge_100pct",
                    old_value,
                    new_value,
                )
            elif delta_ratio >= 0.2:
                append_flag(
                    key,
                    "MEDIUM",
                    "numeric_limit_increase_ge_20pct",
                    old_value,
                    new_value,
                )

        for key in ("exchange.testnet", "exchange.demo_trading"):
            old_value = before.get(key)
            new_value = after.get(key)
            if old_value != new_value:
                append_flag(
                    key,
                    "HIGH",
                    "environment_toggle_changed",
                    old_value,
                    new_value,
                )

        return flags

    def preview_draft(self, draft_id: str) -> Dict[str, Any]:
        draft = self.read_draft(draft_id)
        state = self.get_state()
        profile_name = str(draft.get("profile_name", ""))
        self._validate_profile_name(profile_name)
        draft_content = str(draft.get("content", ""))

        target = self.report_store.config_root / profile_name
        base_exists = target.exists()
        base_content = target.read_text(encoding="utf-8") if base_exists else ""

        before_values = self._extract_scalar_paths(base_content)
        after_values = self._extract_scalar_paths(draft_content)

        added: List[Dict[str, Any]] = []
        removed: List[Dict[str, Any]] = []
        changed: List[Dict[str, Any]] = []

        for key in sorted(after_values.keys()):
            if key not in before_values:
                added.append({"key": key, "after": after_values[key]})
            elif before_values[key] != after_values[key]:
                changed.append(
                    {"key": key, "before": before_values[key], "after": after_values[key]}
                )

        for key in sorted(before_values.keys()):
            if key not in after_values:
                removed.append({"key": key, "before": before_values[key]})

        diff = {
            "counts": {
                "before_scalar_count": len(before_values),
                "after_scalar_count": len(after_values),
                "added_count": len(added),
                "removed_count": len(removed),
                "changed_count": len(changed),
            },
            "added": added,
            "removed": removed,
            "changed": changed,
        }
        risk_flags = self._risk_flags_from_diff(before_values, after_values)
        approvals = self._draft_approvals(draft)
        publish_guard = self._build_publish_guard(
            draft_id=draft_id,
            state=state,
            diff=diff,
            risk_flags=risk_flags,
            approvals=approvals,
        )
        risk_level = "LOW"
        if any(item.get("severity") == "HIGH" for item in risk_flags):
            risk_level = "HIGH"
        elif any(item.get("severity") == "MEDIUM" for item in risk_flags):
            risk_level = "MEDIUM"

        return {
            "draft_id": draft_id,
            "profile_name": profile_name,
            "base_exists": base_exists,
            "base_content_sha256": self._sha256(base_content) if base_exists else None,
            "draft_content_sha256": self._sha256(draft_content),
            "diff": diff,
            "risk_flags": risk_flags,
            "risk_level": risk_level,
            "approvals": approvals,
            "publish_guard": publish_guard,
        }

    def create_draft(
        self,
        profile_name: str,
        content: str,
        actor: str,
        note: str = "",
    ) -> Dict[str, Any]:
        self._validate_profile_name(profile_name)
        if not content.strip():
            raise GovernanceError("draft content is empty")

        validation = self.validate_config_text(content)
        draft_id = f"{self._now_compact()}_{self._sha256(content)[:8]}"
        payload = {
            "draft_id": draft_id,
            "profile_name": profile_name,
            "note": note,
            "actor": actor,
            "created_at_utc": self._now_iso(),
            "content_sha256": self._sha256(content),
            "content": content,
            "validation": validation,
        }
        self._write_json_atomic(self._drafts_dir() / f"{draft_id}.json", payload)
        self._append_audit(
            {
                "ts_utc": self._now_iso(),
                "actor": actor,
                "action": "draft.create",
                "result": "ok",
                "draft_id": draft_id,
                "profile_name": profile_name,
                "validation_ok": validation["ok"],
            }
        )
        return payload

    def list_drafts(self, limit: int = 100) -> List[Dict[str, Any]]:
        drafts: List[Path] = sorted(self._drafts_dir().glob("*.json"), reverse=True)
        output: List[Dict[str, Any]] = []
        for path in drafts[: max(1, limit)]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            output.append(
                {
                    "draft_id": payload.get("draft_id"),
                    "profile_name": payload.get("profile_name"),
                    "created_at_utc": payload.get("created_at_utc"),
                    "actor": payload.get("actor"),
                    "validation_ok": payload.get("validation", {}).get("ok")
                    if isinstance(payload.get("validation"), dict)
                    else None,
                }
            )
        return output

    def read_draft(self, draft_id: str) -> Dict[str, Any]:
        if not DRAFT_RE.match(draft_id):
            raise GovernanceError(f"invalid draft_id: {draft_id}")
        path = self._draft_path(draft_id)
        if not path.exists():
            raise GovernanceError(f"draft not found: {draft_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GovernanceError(f"invalid draft json: {exc}") from exc
        if not isinstance(payload, dict):
            raise GovernanceError("draft json must be object")
        return payload

    def validate_draft(self, draft_id: str, actor: str) -> Dict[str, Any]:
        draft = self.read_draft(draft_id)
        content = str(draft.get("content", ""))
        validation = self.validate_config_text(content)
        draft["validation"] = validation
        draft["validated_at_utc"] = self._now_iso()
        draft["validated_by"] = actor
        self._write_json_atomic(self._draft_path(draft_id), draft)
        self._append_audit(
            {
                "ts_utc": self._now_iso(),
                "actor": actor,
                "action": "draft.validate",
                "result": "ok",
                "draft_id": draft_id,
                "validation_ok": validation["ok"],
            }
        )
        return draft

    def approve_draft(self, draft_id: str, actor: str, note: str = "") -> Dict[str, Any]:
        draft = self.read_draft(draft_id)
        approvals = self._draft_approvals(draft)

        # Keep one latest approval entry per actor.
        filtered = [entry for entry in approvals if entry["actor"] != actor]
        filtered.append(
            {
                "actor": actor,
                "approved_at_utc": self._now_iso(),
                "note": note,
            }
        )
        filtered.sort(key=lambda x: x["approved_at_utc"])
        draft["approvals"] = filtered
        self._write_json_atomic(self._draft_path(draft_id), draft)

        preview = self.preview_draft(draft_id)
        guard = preview.get("publish_guard", {})
        self._append_audit(
            {
                "ts_utc": self._now_iso(),
                "actor": actor,
                "action": "draft.approve",
                "result": "ok",
                "draft_id": draft_id,
                "required_approval_count": guard.get("required_approval_count"),
                "current_approval_count": guard.get("current_approval_count"),
            }
        )
        return {
            "draft_id": draft_id,
            "approvals": draft["approvals"],
            "required_approval_count": guard.get("required_approval_count"),
            "current_approval_count": guard.get("current_approval_count"),
            "approval_satisfied": guard.get("approval_satisfied"),
            "cooldown_remaining_seconds": guard.get("cooldown_remaining_seconds"),
        }

    def _require_publish_guard(self, state: Dict[str, Any]) -> None:
        if state.get("read_only_mode", False):
            raise GovernanceError("publish blocked: read_only_mode=true")
        if state.get("publish_frozen", False):
            raise GovernanceError("publish blocked: publish_frozen=true")

    def _validate_latest_gate(self, state: Dict[str, Any]) -> None:
        if not state.get("require_latest_pass", self.default_require_latest_pass):
            return
        bundle = self.report_store.latest_bundle()

        runtime = bundle.get("runtime_assess", {})
        report = bundle.get("closed_loop_report", {})

        runtime_verdict = runtime.get("verdict") if isinstance(runtime, dict) else None
        overall_status = report.get("overall_status") if isinstance(report, dict) else None

        allowed = {"PASS"}
        if state.get("allow_pass_with_actions", self.default_allow_pass_with_actions):
            allowed.add("PASS_WITH_ACTIONS")

        if runtime_verdict not in allowed:
            raise GovernanceError(
                f"publish blocked: runtime verdict={runtime_verdict}, required one of {sorted(allowed)}"
            )
        if overall_status not in allowed:
            raise GovernanceError(
                f"publish blocked: closed_loop overall_status={overall_status}, required one of {sorted(allowed)}"
            )

    def _backup_target(self, target_profile: str, content: str, actor: str, reason: str) -> str:
        backup_name = f"{self._now_compact()}_{target_profile}.yaml"
        path = self._backups_dir() / backup_name
        path.write_text(content, encoding="utf-8")
        self._append_audit(
            {
                "ts_utc": self._now_iso(),
                "actor": actor,
                "action": "config.backup",
                "result": "ok",
                "profile_name": target_profile,
                "backup_file": backup_name,
                "reason": reason,
                "content_sha256": self._sha256(content),
            }
        )
        return backup_name

    def publish_draft(
        self,
        draft_id: str,
        actor: str,
        preview_digest: str,
        confirm_phrase: str,
    ) -> Dict[str, Any]:
        state = self.get_state()
        self._require_publish_guard(state)
        self._validate_latest_gate(state)

        preview = self.preview_draft(draft_id)
        publish_guard = preview.get("publish_guard", {})
        expected_digest = str(publish_guard.get("preview_digest", ""))
        expected_phrase = str(publish_guard.get("confirm_phrase", ""))

        if preview_digest.strip() != expected_digest:
            raise GovernanceError("publish blocked: preview digest mismatch, refresh preview")
        if confirm_phrase.strip() != expected_phrase:
            raise GovernanceError("publish blocked: confirmation phrase mismatch")
        if not bool(publish_guard.get("approval_satisfied", False)):
            required_count = publish_guard.get("required_approval_count")
            current_count = publish_guard.get("current_approval_count")
            raise GovernanceError(
                "publish blocked: high-risk approvals not satisfied: "
                f"{current_count}/{required_count}"
            )
        cooldown_remaining = int(publish_guard.get("cooldown_remaining_seconds", 0))
        if cooldown_remaining > 0:
            raise GovernanceError(
                f"publish blocked: cooldown active, remaining={cooldown_remaining}s"
            )

        draft = self.read_draft(draft_id)
        profile_name = str(draft.get("profile_name", ""))
        self._validate_profile_name(profile_name)
        content = str(draft.get("content", ""))
        validation = self.validate_config_text(content)
        if not validation["ok"]:
            raise GovernanceError("publish blocked: draft validation failed")

        target = self.report_store.config_root / profile_name
        target.parent.mkdir(parents=True, exist_ok=True)

        backup_file = None
        if target.exists():
            backup_file = self._backup_target(
                target_profile=profile_name,
                content=target.read_text(encoding="utf-8"),
                actor=actor,
                reason="before_publish",
            )

        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)

        result = {
            "published": True,
            "draft_id": draft_id,
            "profile_name": profile_name,
            "published_at_utc": self._now_iso(),
            "backup_file": backup_file,
            "content_sha256": self._sha256(content),
            "preview_digest": expected_digest,
        }
        self._write_json_atomic(self.control_root / "last_publish.json", result)
        self._append_audit(
            {
                "ts_utc": self._now_iso(),
                "actor": actor,
                "action": "draft.publish",
                "result": "ok",
                **result,
            }
        )
        return result

    def list_backups(self, profile_name: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        files = sorted(self._backups_dir().glob("*.yaml"), reverse=True)
        for path in files:
            name = path.name
            if profile_name and not name.endswith(f"_{profile_name}.yaml"):
                continue
            out.append(
                {
                    "backup_file": name,
                    "size_bytes": path.stat().st_size,
                    "path": str(path),
                }
            )
            if len(out) >= max(1, limit):
                break
        return out

    def rollback(self, backup_file: str, target_profile: str, actor: str) -> Dict[str, Any]:
        state = self.get_state()
        self._require_publish_guard(state)

        self._validate_profile_name(target_profile)
        if "/" in backup_file or ".." in backup_file:
            raise GovernanceError("invalid backup_file")

        backup_path = self._backups_dir() / backup_file
        if not backup_path.exists():
            raise GovernanceError(f"backup not found: {backup_file}")

        rollback_content = backup_path.read_text(encoding="utf-8")
        target = self.report_store.config_root / target_profile
        target.parent.mkdir(parents=True, exist_ok=True)

        before_backup = None
        if target.exists():
            before_backup = self._backup_target(
                target_profile=target_profile,
                content=target.read_text(encoding="utf-8"),
                actor=actor,
                reason="before_rollback",
            )

        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(rollback_content, encoding="utf-8")
        tmp.replace(target)

        result = {
            "rolled_back": True,
            "target_profile": target_profile,
            "source_backup_file": backup_file,
            "before_backup_file": before_backup,
            "rolled_back_at_utc": self._now_iso(),
            "content_sha256": self._sha256(rollback_content),
        }
        self._append_audit(
            {
                "ts_utc": self._now_iso(),
                "actor": actor,
                "action": "config.rollback",
                "result": "ok",
                **result,
            }
        )
        return result
