"use strict";

const storage = {
  tokenKey: "ai_trade_web_admin_token",
  actorKey: "ai_trade_web_actor",
};

const ui = {
  status: document.getElementById("top-status"),
  refreshAll: document.getElementById("refresh-all"),
  adminActor: document.getElementById("admin-actor"),
  adminToken: document.getElementById("admin-token"),

  cardRunId: document.getElementById("card-run-id"),
  cardRuntimeVerdict: document.getElementById("card-runtime-verdict"),
  cardOverallStatus: document.getElementById("card-overall-status"),
  cardModelVersion: document.getElementById("card-model-version"),
  cardWriteEnabled: document.getElementById("card-write-enabled"),

  runtimeJson: document.getElementById("runtime-json"),
  reportJson: document.getElementById("report-json"),

  runsTableBody: document.getElementById("runs-table-body"),
  draftsTableBody: document.getElementById("drafts-table-body"),
  backupsTableBody: document.getElementById("backups-table-body"),
  draftDetail: document.getElementById("draft-detail"),
  auditLog: document.getElementById("audit-log"),

  stateReadOnly: document.getElementById("state-read-only"),
  statePublishFrozen: document.getElementById("state-publish-frozen"),
  stateRequirePass: document.getElementById("state-require-pass"),
  stateAllowPassActions: document.getElementById("state-allow-pass-actions"),
  stateTwoManRule: document.getElementById("state-two-man-rule"),
  stateRequiredApprovals: document.getElementById("state-required-approvals"),
  stateCooldownSeconds: document.getElementById("state-cooldown-seconds"),

  saveGovernanceState: document.getElementById("save-governance-state"),
  draftProfile: document.getElementById("draft-profile"),
  draftNote: document.getElementById("draft-note"),
  draftContent: document.getElementById("draft-content"),
  createDraft: document.getElementById("create-draft"),
  rollbackProfile: document.getElementById("rollback-profile"),
  rollbackBackup: document.getElementById("rollback-backup"),
  rollbackSubmit: document.getElementById("rollback-submit"),
  refreshAudit: document.getElementById("refresh-audit"),

  trendEvolutionActions: document.getElementById("trend-evolution-actions"),
  trendEvolutionVirtualActions: document.getElementById("trend-evolution-virtual-actions"),
  trendEvolutionCounterfactualActions: document.getElementById(
    "trend-evolution-counterfactual-actions"
  ),
  trendEvolutionCounterfactualUpdates: document.getElementById(
    "trend-evolution-counterfactual-updates"
  ),
  trendEvolutionFactorIcActions: document.getElementById(
    "trend-evolution-factor-ic-actions"
  ),
  trendEvolutionLearnabilitySkips: document.getElementById(
    "trend-evolution-learnability-skips"
  ),
  trendFlatStartRebases: document.getElementById("trend-flat-start-rebases"),
  trendPolicyRatio: document.getElementById("trend-policy-ratio"),
  trendEquityPct: document.getElementById("trend-equity-pct"),
  trendCostFiltered: document.getElementById("trend-cost-filtered"),
  trendMakerFillRatio: document.getElementById("trend-maker-fill-ratio"),
  trendFeeBpsPerFill: document.getElementById("trend-fee-bps-per-fill"),
  trendQualityGuardActive: document.getElementById("trend-quality-guard-active"),
  trendReconcileMismatch: document.getElementById("trend-reconcile-mismatch"),
  trendReconcileAnomalyReduceOnly: document.getElementById(
    "trend-reconcile-anomaly-ro"
  ),
  trendDrawdownPct: document.getElementById("trend-drawdown-pct"),
};

let writeEnabled = false;

function nowLabel() {
  return new Date().toISOString();
}

function setStatus(message, error = false) {
  ui.status.textContent = `[${nowLabel()}] ${message}`;
  ui.status.classList.toggle("status-fail", error);
}

function pretty(payload) {
  return JSON.stringify(payload, null, 2);
}

function asText(value, fallback = "-") {
  if (value === null || value === undefined || value === "") {
    return fallback;
  }
  return String(value);
}

function asNumber(value, fallback = 0) {
  const num = Number(value);
  return Number.isFinite(num) ? num : fallback;
}

function drawLineChart(canvas, values, options) {
  if (!canvas || !(canvas instanceof HTMLCanvasElement)) {
    return;
  }
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    return;
  }
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);

  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, width, height);

  const margin = { left: 36, right: 8, top: 14, bottom: 24 };
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  if (plotW <= 0 || plotH <= 0) {
    return;
  }

  const finite = values.filter((x) => Number.isFinite(x));
  const maxValue = finite.length ? Math.max(...finite) : 1;
  const minValue = finite.length ? Math.min(...finite) : 0;
  const span = Math.max(1e-9, maxValue - minValue);
  const paddedMin = minValue - span * 0.12;
  const paddedMax = maxValue + span * 0.12;
  const denom = Math.max(1e-9, paddedMax - paddedMin);

  ctx.strokeStyle = "#d8d2c5";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(margin.left, margin.top);
  ctx.lineTo(margin.left, margin.top + plotH);
  ctx.lineTo(margin.left + plotW, margin.top + plotH);
  ctx.stroke();

  ctx.fillStyle = "#6f6a60";
  ctx.font = "11px sans-serif";
  const decimals = options.decimals || 2;
  ctx.fillText(`${paddedMax.toFixed(decimals)}${options.unit || ""}`, 4, margin.top + 4);
  ctx.fillText(
    `${paddedMin.toFixed(decimals)}${options.unit || ""}`,
    4,
    margin.top + plotH + 3
  );

  if (!values.length) {
    return;
  }

  const stepX = values.length > 1 ? plotW / (values.length - 1) : 0;
  ctx.strokeStyle = options.color || "#0a7c78";
  ctx.lineWidth = 2;
  ctx.beginPath();

  for (let i = 0; i < values.length; i += 1) {
    const x = margin.left + i * stepX;
    const yNorm = (values[i] - paddedMin) / denom;
    const y = margin.top + (1 - yNorm) * plotH;
    if (i === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();

  const last = values[values.length - 1];
  ctx.fillStyle = options.color || "#0a7c78";
  ctx.font = "12px sans-serif";
  ctx.fillText(
    `${options.label || "value"}: ${last.toFixed(decimals)}${options.unit || ""}`,
    margin.left + 6,
    height - 6
  );
}

function setVerdictCell(node, value) {
  const text = asText(value);
  node.textContent = text;
  node.classList.remove("status-pass", "status-fail");
  if (text.startsWith("PASS")) {
    node.classList.add("status-pass");
  } else if (text.startsWith("FAIL")) {
    node.classList.add("status-fail");
  }
}

function writeHeaders(method) {
  const headers = {};
  const actor = ui.adminActor.value.trim();
  const token = ui.adminToken.value.trim();
  const isWrite = method !== "GET";

  if (isWrite && actor) {
    headers["X-Actor"] = actor;
  }
  if (isWrite && token) {
    headers["X-Admin-Token"] = token;
  }
  return headers;
}

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = {
    ...writeHeaders(method),
    ...(options.headers || {}),
  };
  let body = options.body;
  if (body !== undefined && typeof body !== "string") {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }

  const response = await fetch(path, {
    method,
    headers,
    body,
  });

  const rawText = await response.text();
  let data = rawText;
  try {
    data = rawText ? JSON.parse(rawText) : {};
  } catch (_ignored) {}

  if (!response.ok) {
    const detail =
      typeof data === "object" && data && "detail" in data
        ? asText(data.detail)
        : asText(rawText, "request failed");
    throw new Error(`${method} ${path} -> ${response.status}: ${detail}`);
  }
  return data;
}

function applyWriteMode(enabled) {
  writeEnabled = enabled;
  ui.cardWriteEnabled.textContent = enabled ? "ENABLED" : "DISABLED";
  ui.cardWriteEnabled.classList.remove("status-pass", "status-fail");
  ui.cardWriteEnabled.classList.add(enabled ? "status-pass" : "status-fail");

  ui.saveGovernanceState.disabled = !enabled;
  ui.createDraft.disabled = !enabled;
  ui.rollbackSubmit.disabled = !enabled;
  ui.stateReadOnly.disabled = !enabled;
  ui.statePublishFrozen.disabled = !enabled;
  ui.stateRequirePass.disabled = !enabled;
  ui.stateAllowPassActions.disabled = !enabled;
  ui.stateTwoManRule.disabled = !enabled;
  ui.stateRequiredApprovals.disabled = !enabled;
  ui.stateCooldownSeconds.disabled = !enabled;
}

async function loadHealth() {
  const health = await api("/healthz");
  applyWriteMode(Boolean(health.write_enabled));
}

async function loadOverview() {
  const [overview, activeModel] = await Promise.all([
    api("/api/v1/overview"),
    api("/api/v1/models/active"),
  ]);

  const latest = overview.latest || {};
  const runtime = latest.runtime_assess || {};
  const report = latest.closed_loop_report || {};
  const meta = latest.run_meta || {};

  ui.cardRunId.textContent = asText(meta.run_id);
  setVerdictCell(ui.cardRuntimeVerdict, runtime.verdict);
  setVerdictCell(ui.cardOverallStatus, report.overall_status);
  ui.cardModelVersion.textContent = asText(activeModel.model_version);

  ui.runtimeJson.textContent = pretty(runtime);
  ui.reportJson.textContent = pretty(report);
}

function appendButton(parent, label, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.className = className;
  button.disabled = !writeEnabled && className.includes("write-op");
  button.addEventListener("click", onClick);
  parent.appendChild(button);
}

async function loadRuns() {
  const payload = await api("/api/v1/reports/runs?limit=40");
  const runs = payload.runs || [];
  ui.runsTableBody.innerHTML = "";

  for (const run of runs) {
    const tr = document.createElement("tr");
    const runId = asText(run.run_id);
    const status = asText(run.overall_status);
    const generated = asText(run.generated_at_utc);

    const runCell = document.createElement("td");
    runCell.textContent = runId;
    const statusCell = document.createElement("td");
    statusCell.textContent = status;
    if (status.startsWith("PASS")) {
      statusCell.classList.add("status-pass");
    } else if (status.startsWith("FAIL")) {
      statusCell.classList.add("status-fail");
    }
    const generatedCell = document.createElement("td");
    generatedCell.textContent = generated;
    const actionCell = document.createElement("td");

    appendButton(actionCell, "View", "btn-read", async () => {
      try {
        const detail = await api(`/api/v1/reports/runs/${runId}`);
        ui.runtimeJson.textContent = pretty(detail.runtime_assess || {});
        ui.reportJson.textContent = pretty(detail.closed_loop_report || {});
        setStatus(`Loaded run ${runId}`);
      } catch (error) {
        setStatus(String(error), true);
      }
    });

    tr.appendChild(runCell);
    tr.appendChild(statusCell);
    tr.appendChild(generatedCell);
    tr.appendChild(actionCell);
    ui.runsTableBody.appendChild(tr);
  }
}

async function loadTrendMetrics() {
  const payload = await api("/api/v1/reports/runs?limit=12");
  const runs = (payload.runs || []).slice().reverse();
  const points = [];

  for (const run of runs) {
    const runId = asText(run.run_id, "");
    if (!runId) {
      continue;
    }
    try {
      const detail = await api(`/api/v1/reports/runs/${runId}`);
      const runtime = detail.runtime_assess || {};
      const runtimeMetrics = runtime.metrics || {};
      const report = detail.closed_loop_report || {};
      const account = report.account_outcome || {};

      points.push({
        run_id: runId,
        evolution_actions: asNumber(runtimeMetrics.self_evolution_action_count, 0),
        evolution_virtual_actions: asNumber(
          runtimeMetrics.self_evolution_virtual_action_count,
          0
        ),
        evolution_counterfactual_actions: asNumber(
          runtimeMetrics.self_evolution_counterfactual_action_count,
          0
        ),
        evolution_counterfactual_updates: asNumber(
          runtimeMetrics.self_evolution_counterfactual_update_count,
          0
        ),
        evolution_factor_ic_actions: asNumber(
          runtimeMetrics.self_evolution_factor_ic_action_count,
          0
        ),
        evolution_learnability_skips: asNumber(
          runtimeMetrics.self_evolution_learnability_skip_count,
          0
        ),
        flat_start_rebases: asNumber(runtimeMetrics.flat_start_rebase_applied_count, 0),
        policy_applied_ratio: asNumber(runtimeMetrics.integrator_policy_applied_ratio, 0) * 100,
        equity_change_pct: asNumber(account.equity_change_pct, 0) * 100,
        cost_filtered: asNumber(runtimeMetrics.order_filtered_cost_count, 0),
        maker_fill_ratio:
          asNumber(runtimeMetrics.execution_window_maker_fill_ratio_avg, 0) * 100,
        fee_bps_per_fill: asNumber(runtimeMetrics.fee_bps_per_fill, 0),
        quality_guard_active: asNumber(
          runtimeMetrics.execution_quality_guard_active_count,
          0
        ),
        reconcile_mismatch: asNumber(runtimeMetrics.reconcile_mismatch_count, 0),
        reconcile_anomaly_reduce_only: asNumber(
          runtimeMetrics.reconcile_anomaly_reduce_only_true_count,
          0
        ),
        drawdown_pct: asNumber(account.max_drawdown_pct_observed, 0) * 100,
      });
    } catch (_ignored) {
      // Skip run detail read failures and keep chart rendering resilient.
    }
  }

  const evolution = points.map((x) => x.evolution_actions);
  const evolutionVirtual = points.map((x) => x.evolution_virtual_actions);
  const evolutionCounterfactualActions = points.map(
    (x) => x.evolution_counterfactual_actions
  );
  const evolutionCounterfactual = points.map(
    (x) => x.evolution_counterfactual_updates
  );
  const evolutionFactorIcActions = points.map((x) => x.evolution_factor_ic_actions);
  const evolutionLearnabilitySkips = points.map(
    (x) => x.evolution_learnability_skips
  );
  const flatStartRebases = points.map((x) => x.flat_start_rebases);
  const policyRatio = points.map((x) => x.policy_applied_ratio);
  const equity = points.map((x) => x.equity_change_pct);
  const costFiltered = points.map((x) => x.cost_filtered);
  const makerFillRatio = points.map((x) => x.maker_fill_ratio);
  const feeBpsPerFill = points.map((x) => x.fee_bps_per_fill);
  const qualityGuardActive = points.map((x) => x.quality_guard_active);
  const reconcileMismatch = points.map((x) => x.reconcile_mismatch);
  const reconcileAnomalyReduceOnly = points.map(
    (x) => x.reconcile_anomaly_reduce_only
  );
  const drawdown = points.map((x) => x.drawdown_pct);

  drawLineChart(ui.trendEvolutionActions, evolution, {
    label: "self_evolution_action_count",
    color: "#006d77",
    decimals: 2,
  });
  drawLineChart(ui.trendEvolutionVirtualActions, evolutionVirtual, {
    label: "self_evolution_virtual_action_count",
    color: "#2a9d8f",
    decimals: 2,
  });
  drawLineChart(ui.trendEvolutionCounterfactualActions, evolutionCounterfactualActions, {
    label: "self_evolution_counterfactual_action_count",
    color: "#7f5539",
    decimals: 2,
  });
  drawLineChart(ui.trendEvolutionCounterfactualUpdates, evolutionCounterfactual, {
    label: "self_evolution_counterfactual_update_count",
    color: "#8f2d56",
    decimals: 2,
  });
  drawLineChart(ui.trendEvolutionFactorIcActions, evolutionFactorIcActions, {
    label: "self_evolution_factor_ic_action_count",
    color: "#264653",
    decimals: 2,
  });
  drawLineChart(ui.trendEvolutionLearnabilitySkips, evolutionLearnabilitySkips, {
    label: "self_evolution_learnability_skip_count",
    color: "#c1121f",
    decimals: 2,
  });
  drawLineChart(ui.trendFlatStartRebases, flatStartRebases, {
    label: "flat_start_rebase_applied_count",
    color: "#6a4c93",
    decimals: 2,
  });
  drawLineChart(ui.trendPolicyRatio, policyRatio, {
    label: "integrator_policy_applied_ratio",
    color: "#1d3557",
    unit: "%",
    decimals: 2,
  });
  drawLineChart(ui.trendEquityPct, equity, {
    label: "equity_change_pct",
    color: "#2a9d8f",
    unit: "%",
    decimals: 3,
  });
  drawLineChart(ui.trendCostFiltered, costFiltered, {
    label: "order_filtered_cost_count",
    color: "#bc6c25",
    decimals: 2,
  });
  drawLineChart(ui.trendMakerFillRatio, makerFillRatio, {
    label: "execution_window_maker_fill_ratio_avg",
    color: "#005f73",
    unit: "%",
    decimals: 2,
  });
  drawLineChart(ui.trendFeeBpsPerFill, feeBpsPerFill, {
    label: "fee_bps_per_fill",
    color: "#9b2226",
    decimals: 3,
  });
  drawLineChart(ui.trendQualityGuardActive, qualityGuardActive, {
    label: "execution_quality_guard_active_count",
    color: "#6c757d",
    decimals: 2,
  });
  drawLineChart(ui.trendReconcileMismatch, reconcileMismatch, {
    label: "reconcile_mismatch_count",
    color: "#9c6644",
    decimals: 2,
  });
  drawLineChart(ui.trendReconcileAnomalyReduceOnly, reconcileAnomalyReduceOnly, {
    label: "reconcile_anomaly_reduce_only_true_count",
    color: "#8d0801",
    decimals: 2,
  });
  drawLineChart(ui.trendDrawdownPct, drawdown, {
    label: "max_drawdown_pct_observed",
    color: "#ae2012",
    unit: "%",
    decimals: 3,
  });
}

async function loadGovernanceState() {
  const state = await api("/api/v1/governance/state");
  ui.stateReadOnly.checked = Boolean(state.read_only_mode);
  ui.statePublishFrozen.checked = Boolean(state.publish_frozen);
  ui.stateRequirePass.checked = Boolean(state.require_latest_pass);
  ui.stateAllowPassActions.checked = Boolean(state.allow_pass_with_actions);
  ui.stateTwoManRule.checked = Boolean(state.high_risk_two_man_rule);
  ui.stateRequiredApprovals.value = asNumber(state.high_risk_required_approvals, 2);
  ui.stateCooldownSeconds.value = asNumber(state.high_risk_cooldown_seconds, 0);
}

async function saveGovernanceState() {
  const requiredApprovals = Math.max(
    2,
    Math.min(5, Math.floor(asNumber(ui.stateRequiredApprovals.value, 2)))
  );
  const cooldownSeconds = Math.max(
    0,
    Math.min(86400, Math.floor(asNumber(ui.stateCooldownSeconds.value, 0)))
  );
  const payload = {
    read_only_mode: ui.stateReadOnly.checked,
    publish_frozen: ui.statePublishFrozen.checked,
    require_latest_pass: ui.stateRequirePass.checked,
    allow_pass_with_actions: ui.stateAllowPassActions.checked,
    high_risk_two_man_rule: ui.stateTwoManRule.checked,
    high_risk_required_approvals: requiredApprovals,
    high_risk_cooldown_seconds: cooldownSeconds,
  };
  const state = await api("/api/v1/governance/state", {
    method: "PATCH",
    body: payload,
  });
  setStatus(`Governance state updated at ${asText(state.updated_at_utc)}`);
}

async function loadDrafts() {
  const payload = await api("/api/v1/config/drafts?limit=60");
  const drafts = payload.drafts || [];
  ui.draftsTableBody.innerHTML = "";

  for (const draft of drafts) {
    const tr = document.createElement("tr");
    const draftId = asText(draft.draft_id);
    const profile = asText(draft.profile_name);
    const validation = draft.validation_ok === true ? "true" : "false";
    const actor = asText(draft.actor);

    const idCell = document.createElement("td");
    idCell.textContent = draftId;
    const profileCell = document.createElement("td");
    profileCell.textContent = profile;
    const validCell = document.createElement("td");
    validCell.textContent = validation;
    validCell.classList.add(draft.validation_ok ? "status-pass" : "status-fail");
    const actorCell = document.createElement("td");
    actorCell.textContent = actor;
    const actionCell = document.createElement("td");

    appendButton(actionCell, "Detail", "btn-read", async () => {
      try {
        const detail = await api(`/api/v1/config/drafts/${draftId}`);
        ui.draftDetail.textContent = pretty(detail);
        setStatus(`Loaded draft ${draftId}`);
      } catch (error) {
        setStatus(String(error), true);
      }
    });
    appendButton(actionCell, "Preview", "btn-read", async () => {
      try {
        const preview = await api(`/api/v1/config/drafts/${draftId}/preview`);
        ui.draftDetail.textContent = pretty(preview);
        const riskCount = Array.isArray(preview.risk_flags)
          ? preview.risk_flags.length
          : 0;
        setStatus(`Preview ${draftId}: ${riskCount} risk flags`);
      } catch (error) {
        setStatus(String(error), true);
      }
    });
    appendButton(actionCell, "Validate", "btn-write-op write-op", async () => {
      try {
        const detail = await api(`/api/v1/config/drafts/${draftId}/validate`, {
          method: "POST",
        });
        ui.draftDetail.textContent = pretty(detail);
        await loadDrafts();
        setStatus(`Validated draft ${draftId}`);
      } catch (error) {
        setStatus(String(error), true);
      }
    });
    appendButton(actionCell, "Approve", "btn-write-op write-op", async () => {
      try {
        const note = window.prompt("Approval note (optional):", "");
        if (note === null) {
          return;
        }
        const result = await api(`/api/v1/config/drafts/${draftId}/approve`, {
          method: "POST",
          body: { note: note.trim() },
        });
        ui.draftDetail.textContent = pretty(result);
        await loadDrafts();
        setStatus(
          `Approved ${draftId}: ${asText(result.current_approval_count)}/${asText(
            result.required_approval_count
          )}`
        );
      } catch (error) {
        setStatus(String(error), true);
      }
    });
    appendButton(actionCell, "Publish", "btn-write-op write-op", async () => {
      try {
        const preview = await api(`/api/v1/config/drafts/${draftId}/preview`);
        ui.draftDetail.textContent = pretty(preview);
        const guard = preview.publish_guard || {};

        if (!window.confirm(`Publish draft ${draftId}?`)) {
          return;
        }

        const phrase = asText(guard.confirm_phrase, "");
        if (!phrase || !guard.preview_digest) {
          throw new Error("publish guard payload missing; refresh and retry");
        }

        const typed = window.prompt(`Type confirm phrase:\n${phrase}`, "");
        if (typed === null) {
          return;
        }
        const normalized = typed.trim();
        if (normalized !== phrase) {
          throw new Error("confirmation phrase mismatch");
        }

        if (guard.high_risk_enforced && !guard.approval_satisfied) {
          throw new Error(
            `high-risk approvals not satisfied: ${asText(
              guard.current_approval_count
            )}/${asText(guard.required_approval_count)}`
          );
        }
        if (asNumber(guard.cooldown_remaining_seconds, 0) > 0) {
          throw new Error(`high-risk cooldown active: ${guard.cooldown_remaining_seconds}s`);
        }

        const result = await api(`/api/v1/config/drafts/${draftId}/publish`, {
          method: "POST",
          body: {
            preview_digest: String(guard.preview_digest),
            confirm_phrase: normalized,
          },
        });
        ui.draftDetail.textContent = pretty(result);
        await Promise.all([loadBackups(), loadOverview(), loadAudit()]);
        setStatus(`Published draft ${draftId}`);
      } catch (error) {
        setStatus(String(error), true);
      }
    });

    tr.appendChild(idCell);
    tr.appendChild(profileCell);
    tr.appendChild(validCell);
    tr.appendChild(actorCell);
    tr.appendChild(actionCell);
    ui.draftsTableBody.appendChild(tr);
  }
}

async function createDraft() {
  const profileName = ui.draftProfile.value.trim();
  const note = ui.draftNote.value.trim();
  const content = ui.draftContent.value;
  if (!profileName || !content.trim()) {
    throw new Error("draft profile and content are required");
  }
  const draft = await api("/api/v1/config/drafts", {
    method: "POST",
    body: {
      profile_name: profileName,
      content,
      note,
    },
  });
  ui.draftDetail.textContent = pretty(draft);
  setStatus(`Created draft ${asText(draft.draft_id)}`);
  await loadDrafts();
}

async function loadBackups() {
  const payload = await api("/api/v1/config/backups?limit=80");
  const backups = payload.backups || [];
  ui.backupsTableBody.innerHTML = "";

  for (const backup of backups) {
    const tr = document.createElement("tr");
    const name = asText(backup.backup_file);

    const fileCell = document.createElement("td");
    fileCell.textContent = name;
    const bytesCell = document.createElement("td");
    bytesCell.textContent = asText(backup.size_bytes);
    const actionCell = document.createElement("td");

    appendButton(actionCell, "Use", "btn-write-op write-op", () => {
      ui.rollbackBackup.value = name;
    });

    tr.appendChild(fileCell);
    tr.appendChild(bytesCell);
    tr.appendChild(actionCell);
    ui.backupsTableBody.appendChild(tr);
  }
}

async function rollbackNow() {
  const targetProfile = ui.rollbackProfile.value.trim();
  const backupFile = ui.rollbackBackup.value.trim();
  if (!targetProfile || !backupFile) {
    throw new Error("rollback profile and backup file are required");
  }
  if (!window.confirm(`Rollback ${targetProfile} using ${backupFile}?`)) {
    return;
  }
  const result = await api("/api/v1/config/rollback", {
    method: "POST",
    body: {
      backup_file: backupFile,
      target_profile: targetProfile,
    },
  });
  ui.draftDetail.textContent = pretty(result);
  setStatus(`Rollback done for ${targetProfile}`);
  await Promise.all([loadBackups(), loadAudit()]);
}

async function loadAudit() {
  const payload = await api("/api/v1/governance/audit?limit=200");
  ui.auditLog.textContent = pretty(payload.events || []);
}

async function refreshAll() {
  setStatus("Refreshing...");
  await loadHealth();
  await Promise.all([
    loadOverview(),
    loadRuns(),
    loadTrendMetrics(),
    loadGovernanceState(),
    loadDrafts(),
    loadBackups(),
    loadAudit(),
  ]);
  setStatus("Refresh complete");
}

function bindEvents() {
  ui.refreshAll.addEventListener("click", () => {
    refreshAll().catch((error) => setStatus(String(error), true));
  });

  ui.saveGovernanceState.addEventListener("click", () => {
    saveGovernanceState().catch((error) => setStatus(String(error), true));
  });

  ui.createDraft.addEventListener("click", () => {
    createDraft().catch((error) => setStatus(String(error), true));
  });

  ui.rollbackSubmit.addEventListener("click", () => {
    rollbackNow().catch((error) => setStatus(String(error), true));
  });

  ui.refreshAudit.addEventListener("click", () => {
    loadAudit().catch((error) => setStatus(String(error), true));
  });

  ui.adminToken.addEventListener("change", () => {
    window.localStorage.setItem(storage.tokenKey, ui.adminToken.value);
  });
  ui.adminActor.addEventListener("change", () => {
    window.localStorage.setItem(storage.actorKey, ui.adminActor.value);
  });
}

function initAdminSession() {
  const cachedToken = window.localStorage.getItem(storage.tokenKey);
  const cachedActor = window.localStorage.getItem(storage.actorKey);
  if (cachedToken) {
    ui.adminToken.value = cachedToken;
  }
  if (cachedActor) {
    ui.adminActor.value = cachedActor;
  }
}

function bootstrap() {
  initAdminSession();
  bindEvents();
  refreshAll().catch((error) => setStatus(String(error), true));
}

window.addEventListener("DOMContentLoaded", bootstrap);
