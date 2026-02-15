# ai-trade-web backend (phase-1)

Phase-1 scope: read-only control-plane API.

Phase-B adds governed write APIs (draft/validate/publish/rollback/audit), guarded by:
- `AI_TRADE_WEB_ENABLE_WRITE=true`
- `AI_TRADE_WEB_ADMIN_TOKEN` (required when write enabled)

## Endpoints

- `GET /` (web console)
- `GET /ui` (web console alias)
- `GET /healthz`
- `GET /api/v1/overview`
- `GET /api/v1/reports/latest`
- `GET /api/v1/reports/runs?limit=50`
- `GET /api/v1/reports/runs/{run_id}`
- `GET /api/v1/models/active`
- `GET /api/v1/config/profiles`
- `GET /api/v1/config/profiles/{name}`
- `GET /api/v1/governance/state`
- `PATCH /api/v1/governance/state`
- `GET /api/v1/governance/audit`
- `GET /api/v1/config/drafts`
- `POST /api/v1/config/drafts`
- `GET /api/v1/config/drafts/{draft_id}`
- `GET /api/v1/config/drafts/{draft_id}/preview`
- `POST /api/v1/config/drafts/{draft_id}/validate`
- `POST /api/v1/config/drafts/{draft_id}/approve`
- `POST /api/v1/config/drafts/{draft_id}/publish`
- `GET /api/v1/config/backups`
- `POST /api/v1/config/rollback`

Publish guard:
- `publish` must include `preview_digest` + `confirm_phrase` returned by `preview`.
- High-risk drafts may require multi-actor approvals and cooldown before publish.

Web trend cards (last 12 runs) include:
- `self_evolution_action_count`
- `self_evolution_virtual_action_count`
- `self_evolution_counterfactual_action_count`
- `self_evolution_counterfactual_update_count`
- `self_evolution_factor_ic_action_count`
- `self_evolution_learnability_skip_count`
- `integrator_policy_applied_ratio`
- `order_filtered_cost_count`
- `reconcile_mismatch_count`
- `equity_change_pct`
- `max_drawdown_pct_observed`

## Environment Variables

- `AI_TRADE_REPORTS_ROOT` (default: `/opt/ai-trade/data/reports/closed_loop`)
- `AI_TRADE_MODELS_ROOT` (default: `/opt/ai-trade/data/models`)
- `AI_TRADE_CONFIG_ROOT` (default: `/opt/ai-trade/config`)
- `AI_TRADE_CONTROL_ROOT` (default: `/opt/ai-trade/data/control_plane`)
- `AI_TRADE_WEB_API_TITLE` (default: `ai-trade control plane`)
- `AI_TRADE_WEB_API_VERSION` (default: `v1`)
- `AI_TRADE_WEB_ENABLE_WRITE` (default: `false`)
- `AI_TRADE_WEB_ADMIN_TOKEN` (default: empty; write mode requires non-empty token)
- `AI_TRADE_WEB_REQUIRE_LATEST_PASS` (default: `true`)
- `AI_TRADE_WEB_ALLOW_PASS_WITH_ACTIONS` (default: `false`)
- `AI_TRADE_WEB_HIGH_RISK_TWO_MAN_RULE` (default: `true`)
- `AI_TRADE_WEB_HIGH_RISK_REQUIRED_APPROVALS` (default: `2`, range: `2-5`)
- `AI_TRADE_WEB_HIGH_RISK_COOLDOWN_SECONDS` (default: `180`, range: `0-86400`)

## Local Run

```bash
cd apps/ai_trade_web/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```
