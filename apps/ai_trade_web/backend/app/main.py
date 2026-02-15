from __future__ import annotations

import hmac
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .services.governance import GovernanceError, GovernanceService
from .services.report_store import ReportStore, StoreError
from .settings import load_settings


SETTINGS = load_settings()
STORE = ReportStore(
    reports_root=SETTINGS.reports_root,
    models_root=SETTINGS.models_root,
    config_root=SETTINGS.config_root,
)
GOVERNANCE = GovernanceService(
    report_store=STORE,
    control_root=SETTINGS.control_root,
    default_require_latest_pass=SETTINGS.require_latest_pass,
    default_allow_pass_with_actions=SETTINGS.allow_pass_with_actions,
    default_high_risk_two_man_rule=SETTINGS.high_risk_two_man_rule,
    default_high_risk_required_approvals=SETTINGS.high_risk_required_approvals,
    default_high_risk_cooldown_seconds=SETTINGS.high_risk_cooldown_seconds,
)

app = FastAPI(title=SETTINGS.api_title, version=SETTINGS.api_version)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


ACTOR_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")


class UpdateGovernanceStateRequest(BaseModel):
    read_only_mode: Optional[bool] = None
    publish_frozen: Optional[bool] = None
    require_latest_pass: Optional[bool] = None
    allow_pass_with_actions: Optional[bool] = None
    high_risk_two_man_rule: Optional[bool] = None
    high_risk_required_approvals: Optional[int] = Field(default=None, ge=2, le=5)
    high_risk_cooldown_seconds: Optional[int] = Field(default=None, ge=0, le=86400)


class CreateDraftRequest(BaseModel):
    profile_name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1)
    note: str = Field(default="", max_length=400)


class RollbackRequest(BaseModel):
    backup_file: str = Field(min_length=1, max_length=260)
    target_profile: str = Field(min_length=1, max_length=120)


class ApproveDraftRequest(BaseModel):
    note: str = Field(default="", max_length=400)


class PublishDraftRequest(BaseModel):
    preview_digest: str = Field(min_length=16, max_length=128)
    confirm_phrase: str = Field(min_length=4, max_length=160)


def _actor(raw_actor: Optional[str]) -> str:
    if raw_actor is None:
        return "anonymous"
    raw_actor = raw_actor.strip()
    if not raw_actor:
        return "anonymous"
    if not ACTOR_RE.match(raw_actor):
        return "invalid-actor"
    return raw_actor


def _raise_governance(exc: GovernanceError) -> None:
    message = str(exc)
    if "not found" in message:
        raise HTTPException(status_code=404, detail=message) from exc
    if "blocked" in message:
        raise HTTPException(status_code=409, detail=message) from exc
    raise HTTPException(status_code=400, detail=message) from exc


def _require_write_access(raw_token: Optional[str]) -> None:
    if not SETTINGS.write_enabled:
        raise HTTPException(status_code=403, detail="write operations disabled")
    if not SETTINGS.admin_token:
        raise HTTPException(
            status_code=503, detail="write operations misconfigured: missing admin token"
        )
    if raw_token is None or not hmac.compare_digest(raw_token, SETTINGS.admin_token):
        raise HTTPException(status_code=401, detail="unauthorized")


def _ui_index() -> Path:
    return STATIC_DIR / "index.html"


@app.get("/", include_in_schema=False)
@app.get("/ui", include_in_schema=False)
def web_root() -> FileResponse:
    index = _ui_index()
    if not index.exists():
        raise HTTPException(status_code=404, detail="ui assets not found")
    return FileResponse(index)


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "reports_root": str(SETTINGS.reports_root),
        "models_root": str(SETTINGS.models_root),
        "config_root": str(SETTINGS.config_root),
        "control_root": str(SETTINGS.control_root),
        "write_enabled": SETTINGS.write_enabled,
        "admin_token_configured": bool(SETTINGS.admin_token),
        "high_risk_two_man_rule_default": SETTINGS.high_risk_two_man_rule,
        "high_risk_required_approvals_default": SETTINGS.high_risk_required_approvals,
        "high_risk_cooldown_seconds_default": SETTINGS.high_risk_cooldown_seconds,
    }


@app.get("/api/v1/overview")
def overview() -> dict:
    try:
        latest = STORE.latest_bundle()
        return {
            "latest": latest,
            "run_count": len(STORE.list_runs(limit=200)),
        }
    except StoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/v1/reports/latest")
def reports_latest() -> dict:
    try:
        return STORE.latest_bundle()
    except StoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/v1/reports/runs")
def reports_runs(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    try:
        return {"runs": STORE.list_runs(limit=limit)}
    except StoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/v1/reports/runs/{run_id}")
def reports_run_detail(run_id: str) -> dict:
    try:
        return STORE.run_bundle(run_id)
    except StoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/v1/models/active")
def models_active() -> dict:
    try:
        return STORE.active_model()
    except StoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/v1/config/profiles")
def config_profiles() -> dict:
    try:
        return {"profiles": STORE.list_config_profiles()}
    except StoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/v1/config/profiles/{name}")
def config_profile(name: str) -> dict:
    try:
        return STORE.read_config_profile(name)
    except StoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/v1/governance/state")
def governance_state() -> dict:
    try:
        return GOVERNANCE.get_state()
    except GovernanceError as exc:
        _raise_governance(exc)
    raise HTTPException(status_code=500, detail="unexpected governance state error")


@app.patch("/api/v1/governance/state")
def governance_state_update(
    request: UpdateGovernanceStateRequest,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    x_actor: Optional[str] = Header(default=None, alias="X-Actor"),
) -> dict:
    _require_write_access(x_admin_token)
    patch = request.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="empty patch")
    try:
        return GOVERNANCE.update_state(patch, actor=_actor(x_actor))
    except GovernanceError as exc:
        _raise_governance(exc)
    raise HTTPException(status_code=500, detail="unexpected governance update error")


@app.get("/api/v1/governance/audit")
def governance_audit(limit: int = Query(default=200, ge=1, le=2000)) -> dict:
    try:
        return {"events": GOVERNANCE.read_audit(limit=limit)}
    except GovernanceError as exc:
        _raise_governance(exc)
    raise HTTPException(status_code=500, detail="unexpected governance audit error")


@app.get("/api/v1/config/drafts")
def config_drafts(limit: int = Query(default=100, ge=1, le=1000)) -> dict:
    try:
        return {"drafts": GOVERNANCE.list_drafts(limit=limit)}
    except GovernanceError as exc:
        _raise_governance(exc)
    raise HTTPException(status_code=500, detail="unexpected drafts listing error")


@app.post("/api/v1/config/drafts")
def config_drafts_create(
    request: CreateDraftRequest,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    x_actor: Optional[str] = Header(default=None, alias="X-Actor"),
) -> dict:
    _require_write_access(x_admin_token)
    try:
        return GOVERNANCE.create_draft(
            profile_name=request.profile_name,
            content=request.content,
            note=request.note,
            actor=_actor(x_actor),
        )
    except GovernanceError as exc:
        _raise_governance(exc)
    raise HTTPException(status_code=500, detail="unexpected draft create error")


@app.get("/api/v1/config/drafts/{draft_id}")
def config_draft_detail(draft_id: str) -> dict:
    try:
        return GOVERNANCE.read_draft(draft_id)
    except GovernanceError as exc:
        _raise_governance(exc)
    raise HTTPException(status_code=500, detail="unexpected draft detail error")


@app.get("/api/v1/config/drafts/{draft_id}/preview")
def config_draft_preview(draft_id: str) -> dict:
    try:
        return GOVERNANCE.preview_draft(draft_id)
    except GovernanceError as exc:
        _raise_governance(exc)
    raise HTTPException(status_code=500, detail="unexpected draft preview error")


@app.post("/api/v1/config/drafts/{draft_id}/validate")
def config_draft_validate(
    draft_id: str,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    x_actor: Optional[str] = Header(default=None, alias="X-Actor"),
) -> dict:
    _require_write_access(x_admin_token)
    try:
        return GOVERNANCE.validate_draft(draft_id, actor=_actor(x_actor))
    except GovernanceError as exc:
        _raise_governance(exc)
    raise HTTPException(status_code=500, detail="unexpected draft validate error")


@app.post("/api/v1/config/drafts/{draft_id}/approve")
def config_draft_approve(
    draft_id: str,
    request: ApproveDraftRequest,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    x_actor: Optional[str] = Header(default=None, alias="X-Actor"),
) -> dict:
    _require_write_access(x_admin_token)
    try:
        return GOVERNANCE.approve_draft(draft_id, actor=_actor(x_actor), note=request.note)
    except GovernanceError as exc:
        _raise_governance(exc)
    raise HTTPException(status_code=500, detail="unexpected draft approve error")


@app.post("/api/v1/config/drafts/{draft_id}/publish")
def config_draft_publish(
    draft_id: str,
    request: PublishDraftRequest,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    x_actor: Optional[str] = Header(default=None, alias="X-Actor"),
) -> dict:
    _require_write_access(x_admin_token)
    try:
        return GOVERNANCE.publish_draft(
            draft_id=draft_id,
            actor=_actor(x_actor),
            preview_digest=request.preview_digest,
            confirm_phrase=request.confirm_phrase,
        )
    except GovernanceError as exc:
        _raise_governance(exc)
    raise HTTPException(status_code=500, detail="unexpected draft publish error")


@app.get("/api/v1/config/backups")
def config_backups(
    profile_name: str = Query(default="", max_length=120),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict:
    try:
        return {"backups": GOVERNANCE.list_backups(profile_name=profile_name, limit=limit)}
    except GovernanceError as exc:
        _raise_governance(exc)
    raise HTTPException(status_code=500, detail="unexpected backups listing error")


@app.post("/api/v1/config/rollback")
def config_rollback(
    request: RollbackRequest,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    x_actor: Optional[str] = Header(default=None, alias="X-Actor"),
) -> dict:
    _require_write_access(x_admin_token)
    try:
        return GOVERNANCE.rollback(
            backup_file=request.backup_file,
            target_profile=request.target_profile,
            actor=_actor(x_actor),
        )
    except GovernanceError as exc:
        _raise_governance(exc)
    raise HTTPException(status_code=500, detail="unexpected rollback error")
