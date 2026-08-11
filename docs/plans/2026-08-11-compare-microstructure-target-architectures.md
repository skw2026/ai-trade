# Frozen Microstructure Target Architecture Comparison Plan

## Goal and boundaries

Compare four predeclared target architectures on the exact same checksum-bound development capture, causal feature matrix, action set, costs, latency, and six purged rolling OOS splits:

1. `binary_stress_event_baseline`
2. `direct_stress_utility_regression`
3. `two_stage_opportunity_action`
4. `joint_action_ranker`

The comparison is diagnostic only. It must never alter `development_passed`, `frozen_candidate`, candidate manifests, lifecycle state, or demo/live routing. A leader found on the already-observed OOS windows may only be preregistered for a new independent forward window.

## Task 1: Lock the comparison contract with failing tests

**Files:** `tools/test_run_microstructure_alpha_development.py`, `tools/run_microstructure_alpha_development.py`

- Add tests for the four ordered architecture IDs and a shared comparison identity containing the source assessment hash, ordered feature-name hash/count, ordered action contract, costs, latency, split time contracts, and hashes of the exact `model_fit`, model-selection, validation, and test row indices.
- Add a production research-domain guard: comparison evidence is complete only for exactly 242 ordered features, 10 ordered actions, and 6 splits. Other shapes remain usable by unit fixtures but must report `fully_verifiable=false` with a deterministic frozen-contract mismatch.
- Assert all architectures receive the same partition identity and that the comparison declares `promotion_evidence=false`, `promotion_eligible=false`, and `influences_development_passed=false`.
- Add an explicit regression assertion that changing comparison output cannot change the existing economic screen or frozen-candidate decision.
- Named red/green cases: `test_target_architecture_contract_requires_frozen_242_by_10_by_6_domain`, `test_target_architectures_share_exact_partition_identity`, and `test_architecture_comparison_cannot_influence_promotion_state`. Run `python3 tools/test_run_microstructure_alpha_development.py -k target_architecture_contract` before and after the implementation.

## Task 2: Add target builders and deterministic model adapters using TDD

**Files:** `tools/test_run_microstructure_alpha_development.py`, `tools/run_microstructure_alpha_development.py`

- Import `CatBoostRegressor`, `CatBoostRanker`, and `Pool` alongside the existing classifier with the same optional-dependency behavior.
- Test and implement stress-net utility targets as existing executable base-net outcomes minus the frozen stress-cost increment.
- Test and implement independent per-action RMSE regressors, trained only on `model_fit` and early-stopped only on fit-internal model-selection rows.
- Test and implement the two-stage target: an any-positive-stress-utility opportunity label plus the best profitable action label on opportunity rows. Combine inference scores deterministically as `P(opportunity) * P(action | opportunity)`, including fit-only constant fallbacks for degenerate labels.
- Resolve tied best actions by the predeclared action order. Bind `predict_proba` columns through the model's explicit class labels, and fill unseen actions with zero probability before recovering the complete ordered 10-action matrix; never infer action identity from raw column position.
- Test and implement grouped action features and a `QueryRMSE` ranker: repeat each timestamp's state features for every ordered action, append fixed direction/horizon descriptors, bind one group ID per timestamp, train on stress utility, and reshape predictions back to `(rows, actions)`.
- Keep model hyperparameters and random seeds fixed and auditable; do not introduce tuning on validation or OOS targets.
- Add explicit leakage sentinels for every architecture: mutating model-selection labels may only affect early stopping; mutating nested-validation labels may only affect threshold selection; mutating test labels may only affect final economics; validation/test-only classes may not change the fit-derived two-stage class mapping or output shape.
- Named red/green cases: `test_direct_regression_uses_fit_only_stress_utility`, `test_two_stage_targets_and_class_mapping_are_fit_only`, `test_two_stage_unseen_classes_restore_ordered_action_matrix`, `test_two_stage_ties_follow_predeclared_action_order`, `test_ranker_groups_all_actions_per_timestamp`, and `test_architecture_domains_are_leakage_isolated`. Run each group with `python3 tools/test_run_microstructure_alpha_development.py -k '<substring>'`.

## Task 3: Add a common non-promotional policy evaluator

**Files:** `tools/test_run_microstructure_alpha_development.py`, `tools/run_microstructure_alpha_development.py`

- Add tests for a generic joint-action score threshold selector that derives candidate thresholds only from nested-validation scores, selects the highest-score action per timestamp, and evaluates realized economics under the existing non-overlap rule.
- Preserve score units explicitly (`base_net_bps`, `stress_net_utility_bps`, `probability_product`, or `rank_score`) instead of misrepresenting every threshold as bps.
- Reuse the existing deterministic prediction-time permutation evaluator for seven trials, with architecture- and split-specific deterministic seeds.
- Re-evaluate the existing binary baseline through this common diagnostic evaluator while leaving its production/development promotion evaluator unchanged.
- Named red/green cases: `test_joint_diagnostic_threshold_uses_validation_scores_only`, `test_joint_diagnostic_policy_preserves_non_overlap`, and `test_all_architectures_receive_seven_deterministic_permutations`.

## Task 4: Execute every architecture on every frozen split

**Files:** `tools/test_run_microstructure_alpha_development.py`, `tools/run_microstructure_alpha_development.py`

- In `run_probe`, compute the split index arrays once and pass those exact arrays to all four architectures.
- Reuse already-fitted baseline predictions. Fit the three experimental architectures without reading validation/test targets during fitting or early stopping.
- Store per-split calibration, OOS base/stress economics, trade/action counts, score distributions, best iterations, and all seven permutation controls.
- Isolate architecture exceptions: record the architecture and split failure as incomplete comparison evidence, but do not append it to the existing promotion-path failures or change the report's current `fully_verifiable`/`development_passed` semantics.
- Named red/green cases: `test_run_probe_reuses_baseline_and_exact_indices_for_all_architectures`, `test_experimental_training_failure_is_non_promotional`, and `test_each_architecture_emits_complete_split_evidence`.

## Task 5: Aggregate fail-closed comparison evidence

**Files:** `tools/test_run_microstructure_alpha_development.py`, `tools/run_microstructure_alpha_development.py`

- Test and implement `microstructure_target_architecture_comparison_v1` aggregation across the required split IDs.
- Mark an architecture fully verifiable only with actual and seven-trial permutation evidence on every split. Mark the top-level comparison incomplete if any architecture/split is missing; never compare only survivors.
- Emit ordered `missing_architecture_splits` entries with architecture ID, split ID, and reason. Cover training exceptions, malformed or absent actual economics, missing trials, out-of-order trials, and malformed permutation economics. A structurally complete zero-trade result is fail-closed evidence rather than missing data: aggregate its no-position split return as 0 bps, list it in `zero_trade_split_ids`, and forbid `signal_proven`.
- Set `signal_proven=true` only when actual base and stress split LCBs are positive and strictly exceed the permutation-control requirements.
- Choose a deterministic leader only among proven architectures, ordered by stress LCB, base LCB, then predeclared architecture order.
- Emit `NO_TARGET_ARCHITECTURE_SIGNAL_PROVEN` when evidence is complete and all four fail; otherwise emit an incomplete or diagnostic-signal conclusion and an independent-forward-validation next step.
- Named red/green cases: `test_comparison_lists_every_missing_architecture_split`, `test_comparison_never_uses_survivor_only_evidence`, `test_signal_requires_positive_actual_lcbs_and_permutation_excess`, and `test_diagnostic_leader_tie_break_is_deterministic`.

## Task 6: Protect downstream artifact and routing contracts

**Files:** `tools/test_run_microstructure_alpha_development.py`, `tools/test_build_closed_loop_report.py`, `tools/test_closed_loop_mechanism_audit.py`, `tools/run_microstructure_alpha_development.py`

- Verify the new comparison is additive to schema v8 and candidate-manifest identity remains based only on the existing promotable model contract.
- Assert a diagnostic leader does not create or modify `frozen_candidate`, candidate ID, lifecycle inputs, demo policy, or closed-loop pass/fail routing.
- Add closed-loop report/audit assertions that expose the comparison conclusion for diagnosis without accepting it as mechanism proof or promotion evidence.
- Named red/green cases: `test_closed_loop_report_exposes_non_promotional_architecture_comparison` and `test_mechanism_audit_rejects_architecture_comparison_as_promotion_evidence`.

## Task 7: Verify locally and in the research image

Run in dependency order:

```bash
python3 tools/test_run_microstructure_alpha_development.py
python3 tools/test_build_closed_loop_report.py
python3 tools/test_closed_loop_mechanism_audit.py
cmake -S . -B build -DBUILD_TESTING=ON
cmake --build build -j2
ctest --test-dir build --output-on-failure
docker run --rm --entrypoint python3 -v "$PWD:/workspace" -w /workspace ai-trade-research:latest tools/test_run_microstructure_alpha_development.py
```

The host suite validates optional CatBoost isolation; the research-image suite must exercise the real CatBoost 1.2.8 adapters.

Add a small real-CatBoost smoke fixture that separately covers negative continuous `QueryRMSE` labels, contiguous/equal-sized groups, exact prediction reshape, and degenerate binary/multiclass fallbacks. Before the six-split run, benchmark one production-data split in the research image with `/usr/bin/time`; record wall time and peak RSS in the diagnostic artifact. Budget: projected six-split comparison must stay below the workflow's 120-minute timeout and available container memory, otherwise mark the comparison incomplete and optimize before deployment.

## Task 8: Deploy and obtain real six-split evidence

- Commit and push the reviewed change, then require CI and CD success.
- Trigger and collect the exact workflow reproducibly:

```bash
gh workflow run closed-loop.yml -f action=full -f stage=S5 -f since=24h -f replay_symbols=SOLUSDT -f replay_source_symbol=SOLUSDT -f replay_feature_days=0
gh run list --workflow closed-loop.yml --event workflow_dispatch --limit 1 --json databaseId,headSha,status,conclusion
gh run watch <run-id> --exit-status
mkdir -p /tmp/ai-trade-target-architecture-full-loop
gh run download <run-id> -n closed-loop-report-<run-id>-1 -D /tmp/ai-trade-target-architecture-full-loop
find /tmp/ai-trade-target-architecture-full-loop -name microstructure_alpha_development_report.json -print
jq -e '.target_architecture_comparison.schema_version == "microstructure_target_architecture_comparison_v1" and .target_architecture_comparison.fully_verifiable == true and .target_architecture_comparison.required_split_count == 6 and (.target_architecture_comparison.architectures | length) == 4 and (.target_architecture_comparison.missing_architecture_splits | length) == 0' <report-path>
```

- A run whose comparison has `fully_verifiable=false` is not accepted as completed comparison evidence even when the surrounding Full Loop correctly uploads artifacts or fails closed later.
- Acceptance requires all four architecture IDs, identical shared-contract identities, `required_split_count=6`, six complete actual OOS results per architecture, seven complete permutation trials, and top-level `fully_verifiable=true`.
- Report each architecture's trades, base/stress split mean and LCB, permutation margin, `signal_proven`, diagnostic leader (if any), `promotion_* = false`, and the requirement for a separate immutable forward preregistration artifact. Do not claim that selecting a diagnostic leader itself preregisters it. Full Loop may still fail closed because the comparison is deliberately non-promotional; that outcome is valid when no independently validated candidate exists.

## Dependency order

Tasks 1-3 define contracts and model primitives. Task 4 depends on them. Task 5 depends on Task 4. Task 6 depends on the final report shape. Task 7 gates Task 8.
