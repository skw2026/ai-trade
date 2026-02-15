from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")


class StoreError(RuntimeError):
    pass


@dataclass
class ReportStore:
    reports_root: Path
    models_root: Path
    config_root: Path

    def _load_json(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            raise StoreError(f"file not found: {path}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"invalid json: {path}: {exc}") from exc

    def latest_bundle(self) -> Dict[str, Any]:
        return {
            "runtime_assess": self._load_json(self.reports_root / "latest_runtime_assess.json"),
            "closed_loop_report": self._load_json(self.reports_root / "latest_closed_loop_report.json"),
            "run_meta": self._load_json(self.reports_root / "latest_run_meta.json"),
            "daily_summary": self._load_json(self.reports_root / "summary" / "daily_latest.json"),
            "weekly_summary": self._load_json(self.reports_root / "summary" / "weekly_latest.json"),
        }

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not self.reports_root.exists():
            return []

        runs: List[Path] = []
        for entry in self.reports_root.iterdir():
            if not entry.is_dir():
                continue
            if RUN_ID_RE.match(entry.name):
                runs.append(entry)

        runs.sort(key=lambda p: p.name, reverse=True)
        output: List[Dict[str, Any]] = []
        for run_dir in runs[: max(1, limit)]:
            item: Dict[str, Any] = {
                "run_id": run_dir.name,
                "path": str(run_dir),
            }
            report_path = run_dir / "closed_loop_report.json"
            if report_path.exists():
                try:
                    payload = self._load_json(report_path)
                    item["overall_status"] = payload.get("overall_status")
                    item["generated_at_utc"] = payload.get("generated_at_utc")
                except StoreError:
                    item["overall_status"] = "INVALID"
            output.append(item)
        return output

    def run_bundle(self, run_id: str) -> Dict[str, Any]:
        if not RUN_ID_RE.match(run_id):
            raise StoreError(f"invalid run_id: {run_id}")

        run_dir = self.reports_root / run_id
        return {
            "run_id": run_id,
            "runtime_assess": self._load_json(run_dir / "runtime_assess.json"),
            "closed_loop_report": self._load_json(run_dir / "closed_loop_report.json"),
        }

    def active_model(self) -> Dict[str, Any]:
        return self._load_json(self.models_root / "integrator_active.json")

    def list_config_profiles(self) -> List[str]:
        if not self.config_root.exists():
            return []
        names = [p.name for p in self.config_root.glob("*.yaml") if p.is_file()]
        names.sort()
        return names

    def read_config_profile(self, name: str) -> Dict[str, Any]:
        if not re.match(r"^[A-Za-z0-9_.-]+\.yaml$", name):
            raise StoreError(f"invalid config profile name: {name}")
        path = self.config_root / name
        if not path.exists():
            raise StoreError(f"config profile not found: {name}")

        return {
            "name": name,
            "path": str(path),
            "content": path.read_text(encoding="utf-8"),
        }
