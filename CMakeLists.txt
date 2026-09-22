cmake_minimum_required(VERSION 3.20)
project(ai_trade VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

option(AI_TRADE_WARNINGS_AS_ERRORS "Treat warnings as errors" OFF)
option(AI_TRADE_USE_BEAST_WEBSOCKET
       "Use Boost.Beast implementation for WebSocket client" OFF)
option(AI_TRADE_ENABLE_CATBOOST "Enable CatBoost model inference" OFF)

if(MSVC)
  add_compile_options(/W4)
  if(AI_TRADE_WARNINGS_AS_ERRORS)
    add_compile_options(/WX)
  endif()
else()
  add_compile_options(-Wall -Wextra -Wpedantic)
  if(AI_TRADE_WARNINGS_AS_ERRORS)
    add_compile_options(-Werror)
  endif()
endif()

add_library(ai_trade_core STATIC
  src/core/config.cpp
  src/core/json_utils.cpp
  src/core/log.cpp
)
target_include_directories(ai_trade_core PUBLIC src)

add_library(ai_trade_system STATIC
  src/app/bot_app.cpp
  src/exchange/bybit_rest_client.cpp
  src/exchange/websocket_client.cpp
  src/exchange/bybit_private_stream.cpp
  src/exchange/bybit_public_stream.cpp
  src/exchange/mock_exchange_adapter.cpp
  src/exchange/bybit_exchange_adapter.cpp
  src/exchange/binance_exchange_adapter.cpp
  src/market/market_data.cpp
  src/market/closed_bar_clock.cpp
  src/universe/universe_selector.cpp
  src/strategy/strategy_engine.cpp
  src/strategy/integrator_shadow.cpp
  src/strategy/microstructure_demo_overlay.cpp
  src/regime/regime_engine.cpp
  src/risk/risk_engine.cpp
  src/risk/manual_risk_cycle.cpp
  src/risk/replay_reference_account.cpp
  src/execution/execution_engine.cpp
  src/execution/order_throttle.cpp
  src/execution/async_executor.cpp
  src/evolution/self_evolution_controller.cpp
  src/monitor/gate_monitor.cpp
  src/research/time_series_operators.cpp
  src/research/ic_evaluator.cpp
  src/research/miner.cpp
  src/research/online_feature_engine.cpp
  src/oms/account_state.cpp
  src/oms/order_manager.cpp
  src/oms/reconciler.cpp
  src/storage/wal_store.cpp
  src/system/trade_system.cpp
)
target_include_directories(ai_trade_system PUBLIC src)
find_package(CURL REQUIRED)

if(AI_TRADE_ENABLE_CATBOOST)
  # 使用 dlopen 动态加载，无需链接时依赖
  target_compile_definitions(ai_trade_system PUBLIC AI_TRADE_ENABLE_CATBOOST=1)
  target_link_libraries(ai_trade_system PUBLIC ${CMAKE_DL_LIBS})
endif()

target_link_libraries(ai_trade_system PUBLIC ai_trade_core CURL::libcurl)
if(AI_TRADE_USE_BEAST_WEBSOCKET)
  find_package(Boost REQUIRED COMPONENTS system)
  find_package(OpenSSL REQUIRED)
  target_compile_definitions(ai_trade_system PUBLIC AI_TRADE_ENABLE_BEAST_WS=1)
  target_link_libraries(ai_trade_system PUBLIC Boost::system OpenSSL::SSL OpenSSL::Crypto)
elseif(NOT APPLE)
  find_package(OpenSSL REQUIRED)
  target_link_libraries(ai_trade_system PUBLIC OpenSSL::Crypto)
endif()

add_executable(trade_bot src/main.cpp)
target_link_libraries(trade_bot PRIVATE ai_trade_system)

include(CTest)
if(BUILD_TESTING)
  # Offline integration driver only: never installed into the runtime image.
  add_executable(offline_learning_driver tests/offline_learning_driver.cpp)
  target_link_libraries(offline_learning_driver PRIVATE ai_trade_system)
  add_executable(replay_reference_account_test tests/test_replay_reference_account.cpp)
  target_link_libraries(replay_reference_account_test PRIVATE ai_trade_system)
  add_test(NAME replay_reference_account_test COMMAND replay_reference_account_test
           ${CMAKE_SOURCE_DIR}/config/bybit.replay.mvp-reference.yaml)
  set_tests_properties(replay_reference_account_test PROPERTIES TIMEOUT 30 LABELS "offline;mvp;reference")
  add_executable(manual_risk_cycle_test tests/test_manual_risk_cycle.cpp)
  target_link_libraries(manual_risk_cycle_test PRIVATE ai_trade_system)
  add_test(NAME manual_risk_cycle_test COMMAND manual_risk_cycle_test
           ${CMAKE_SOURCE_DIR}/config/bybit.replay.mvp.yaml)
  set_tests_properties(manual_risk_cycle_test PROPERTIES TIMEOUT 30 LABELS "offline;mvp;risk")
  add_executable(mvp_contract_test tests/test_mvp_contract.cpp)
  target_link_libraries(mvp_contract_test PRIVATE ai_trade_system)
  add_test(NAME mvp_contract_test COMMAND mvp_contract_test
           ${CMAKE_SOURCE_DIR}/config/bybit.replay.mvp.yaml)
  set_tests_properties(mvp_contract_test PROPERTIES TIMEOUT 30 LABELS "offline;mvp")
  # Explicit one-shot synthetic diagnostic, not an economic backtest or a
  # regression test that would bless known strategy-contract gaps.
  add_executable(original_mvp_contract_audit EXCLUDE_FROM_ALL
                 tools/audit_original_mvp_contract.cpp)
  target_link_libraries(original_mvp_contract_audit PRIVATE ai_trade_system)

  add_executable(mvp_history_readiness_audit EXCLUDE_FROM_ALL
                 tools/audit_mvp_history_readiness.cpp)
  target_link_libraries(mvp_history_readiness_audit PRIVATE ai_trade_system)

  add_executable(trade_system_test tests/test_trade_system.cpp)
  target_link_libraries(trade_system_test PRIVATE ai_trade_system)
  add_test(NAME trade_system_test COMMAND trade_system_test ${CMAKE_SOURCE_DIR}/config)

  add_executable(execution_ordering_test tests/test_execution_ordering.cpp)
  target_link_libraries(execution_ordering_test PRIVATE ai_trade_system)
  add_test(NAME execution_ordering_test COMMAND execution_ordering_test)
  set_tests_properties(execution_ordering_test PROPERTIES TIMEOUT 30)

  add_executable(financial_risk_contract_test tests/test_financial_risk_contract.cpp)
  target_link_libraries(financial_risk_contract_test PRIVATE ai_trade_system)
  add_test(NAME financial_risk_contract_test COMMAND financial_risk_contract_test)

  option(AI_TRADE_REQUIRE_PYTHON_TESTS
         "Require Python interpreter for Python unit tests when BUILD_TESTING=ON"
         ON)
  find_package(Python3 COMPONENTS Interpreter QUIET)
  if(NOT Python3_Interpreter_FOUND)
    if(AI_TRADE_REQUIRE_PYTHON_TESTS)
      message(FATAL_ERROR
              "Python3 interpreter not found but BUILD_TESTING=ON and "
              "AI_TRADE_REQUIRE_PYTHON_TESTS=ON. Install Python3 or set "
              "-DAI_TRADE_REQUIRE_PYTHON_TESTS=OFF.")
    else()
      message(WARNING
              "Python3 interpreter not found; Python unit tests are disabled.")
    endif()
  else()
    add_test(NAME validation_stop_gate_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_validation_gate.py)
    add_test(NAME offline_learning_harness_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_verify_offline_learning_loop.py)
    set_tests_properties(offline_learning_harness_test PROPERTIES TIMEOUT 30 LABELS "offline;learning")
    add_test(NAME mvp_reference_pipeline_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_mvp_reference_pipeline.py
                     --binary $<TARGET_FILE:trade_bot>)
    set_tests_properties(mvp_reference_pipeline_test PROPERTIES TIMEOUT 60 LABELS "offline;mvp;reference")
    # These synthetic end-to-end tests validate accounting, not host disk
    # throughput. Keep every real journal write/fsync and the original timeouts,
    # but isolate their disposable fixtures from variable runner disk latency.
    # Durability/error-path tests (manual_risk_cycle, WAL, etc.) are unchanged.
    if(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND IS_DIRECTORY "/dev/shm")
      set_tests_properties(replay_reference_account_test mvp_reference_pipeline_test
                           PROPERTIES ENVIRONMENT "TMPDIR=/dev/shm")
    endif()
    add_test(NAME mvp_history_collector_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_collect_mvp_reference_history.py)
    add_test(NAME mvp_stop_ledger_audit_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_mvp_reference_stop.py)
    add_test(NAME offline_evidence_seal_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_seal_offline_evidence.py)
    set_tests_properties(mvp_history_collector_test mvp_stop_ledger_audit_test offline_evidence_seal_test
                         PROPERTIES TIMEOUT 30 LABELS "offline;mvp;reference")
    # Synthetic fixtures only; never invoke the historical-screen CLI in CI.
    add_test(NAME weekly_momentum_screen_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_screen_weekly_momentum.py)
    set_tests_properties(weekly_momentum_screen_test PROPERTIES TIMEOUT 30 LABELS "offline;research")
    add_test(NAME replay_trace_comparison_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_replay_trace.py)
    add_test(NAME research_closeout_contract_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_research_closeout_contract.py)
    add_test(NAME option_c2_cross_integration_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_option_c2_integration.py)
    add_test(NAME c2_fx_proxy_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_collect_c2_fx_proxy.py)
    add_test(NAME option_exit_evidence_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_option_exit_evidence.py)
    add_test(NAME c2_reference_conformance_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_c2_reference_conformance.py)
    add_test(NAME option_c2_acceptance_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_option_c2_acceptance.py)
    add_test(NAME bybit_c2_depth_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_bybit_c2_depth.py)
    add_test(NAME c2_rest_anchors_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_c2_rest_anchors.py)
    add_test(NAME c2_rest_depth_alignment_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_c2_rest_depth_alignment.py)
    add_test(NAME bybit_c2_market_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_collect_bybit_c2_market.py)
    add_test(NAME bybit_cross_account_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_bybit_cross_account.py)
    add_test(NAME bybit_cross_margin_reference_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_bybit_cross_margin_reference.py)
    add_test(NAME bybit_funding_obligations_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_bybit_funding_obligations.py)
    add_test(NAME bybit_readonly_evidence_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_bybit_readonly_evidence.py)
    add_test(NAME option_public_history_qualification_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_option_public_history.py)
    add_test(NAME option_archive_lifecycle_audit_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_option_archive_lifecycle.py)
    add_test(NAME option_subaccount_ledger_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_option_subaccount_ledger.py)
    add_test(NAME option_lifecycle_subaccount_adapter_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_adapt_option_lifecycle_subaccount_v1.py)
    add_test(NAME option_lifecycle_adapter_summary_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_summarize_option_lifecycle_adapter.py)
    add_test(NAME option_historical_sample_qualification_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_option_historical_sample.py)
    add_test(NAME assess_run_log_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_assess_run_log.py)
    add_test(NAME validate_decision_benchmark_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_validate_decision_benchmark.py)
    add_test(NAME build_decision_benchmark_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_build_decision_benchmark.py)
    add_test(NAME validate_objective_alignment_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_validate_objective_alignment.py)
    add_test(NAME run_paired_evolution_replay_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_run_paired_evolution_replay.py)
    add_test(NAME validate_evolution_uplift_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_validate_evolution_uplift.py)
    add_test(NAME experiment_budget_ledger_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_experiment_budget_ledger.py)
    add_test(NAME record_microstructure_regime_evidence_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_record_microstructure_regime_evidence.py)
    add_test(NAME build_decision_evidence_report_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_build_decision_evidence_report.py)
    add_test(NAME run_replay_validation_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_run_replay_validation.py)
    add_test(NAME compose_consistency_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_compose_consistency.py)
    add_test(NAME ecs_ssh_transport_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_ecs_ssh.py)
    add_test(NAME closed_loop_runner_transaction_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_closed_loop_runner_transaction.py)
    add_test(NAME materialize_release_compose_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_materialize_release_compose.py)
    add_test(NAME validate_deploy_gate_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_validate_deploy_gate.py)
    add_test(NAME release_integrity_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_release_integrity.py)
    add_test(NAME prune_release_storage_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_prune_release_storage.py)
    add_test(NAME write_deployment_diagnostics_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_write_deployment_diagnostics.py)
    add_test(NAME evaluate_smoke_freshness_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_evaluate_smoke_freshness.py)
    add_test(NAME evaluate_demo_incubation_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_evaluate_demo_incubation.py)
    add_test(NAME watchdog_utils_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_watchdog.py)
    add_test(NAME recycle_artifacts_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_recycle_artifacts.py)
    add_test(NAME docker_gc_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_docker_gc.py)
    add_test(NAME validate_reports_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_validate_reports.py)
    add_test(NAME build_closed_loop_report_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_build_closed_loop_report.py)
    add_test(NAME validate_closed_loop_artifact_contract_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_validate_closed_loop_artifact_contract.py)
    add_test(NAME compare_closed_loop_reports_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_compare_closed_loop_reports.py)
    add_test(NAME build_periodic_summary_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_build_periodic_summary.py)
    add_test(NAME data_pipeline_tools_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_data_pipeline_tools.py)
    add_test(NAME run_data_pipeline_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_run_data_pipeline.py)
    add_test(NAME strategy_diagnose_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_strategy_diagnose.py)
    add_test(NAME closed_loop_mechanism_audit_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_closed_loop_mechanism_audit.py)
    add_test(NAME integrator_train_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_integrator_train.py)
    add_test(NAME economic_target_probe_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_economic_target_probe.py)
    add_test(NAME fetch_bybit_derivatives_history_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_fetch_bybit_derivatives_history.py)
    add_test(NAME fetch_market_alpha_history_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_fetch_market_alpha_history.py)
    add_test(NAME fetch_bybit_trade_history_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_fetch_bybit_trade_history.py)
    add_test(NAME collect_bybit_microstructure_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_collect_bybit_microstructure.py)
    add_test(NAME collect_binance_microstructure_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_collect_binance_microstructure.py)
    add_test(NAME binance_microstructure_runtime_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_binance_microstructure_runtime.py)
    add_test(NAME prune_microstructure_capture_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_prune_microstructure_capture.py)
    add_test(NAME cross_venue_information_set_experiment_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_run_cross_venue_information_set_experiment.py)
    add_test(NAME collect_bybit_liquidations_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_collect_bybit_liquidations.py)
    add_test(NAME liquidation_capture_runtime_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_liquidation_capture_runtime.py)
    add_test(NAME liquidation_information_set_experiment_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_run_liquidation_information_set_experiment.py)
    add_test(NAME maker_execution_opportunity_experiment_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_run_maker_execution_opportunity_experiment.py)
    add_test(NAME cross_asset_residual_opportunity_experiment_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_run_cross_asset_residual_opportunity_experiment.py)
    add_test(NAME funding_basis_carry_opportunity_experiment_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_funding_basis_carry_opportunity_experiment.py)
    add_test(NAME cross_venue_funding_differential_experiment_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_cross_venue_funding_differential_experiment.py)
    add_test(NAME account_structural_economics_audit_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_account_structural_economics.py)
    add_test(NAME option_vrp_capture_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_capture_bybit_option_vrp.py)
    add_test(NAME option_vrp_capture_v2_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_capture_bybit_option_vrp_v2.py)
    add_test(NAME option_lifecycle_v3_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_option_lifecycle_v3.py)
    add_test(NAME option_lifecycle_economics_v1_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_option_lifecycle_economics_v1.py)
    add_test(NAME option_candidate_closure_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_option_candidate_closure.py)
    add_test(NAME fixed_engineering_regression_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_fixed_engineering_regression.py)
    add_test(NAME engineering_release_boundary_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_check_engineering_release.py)
    add_test(NAME option_segment_diagnostic_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_diagnose_option_lifecycle_segment.py)
    add_test(NAME option_funding_sensitivity_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_option_funding_sensitivity.py)
    add_test(NAME option_vrp_feasibility_audit_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_option_variance_risk_premium_feasibility.py)
    add_test(NAME option_vrp_sequential_payoff_audit_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_audit_option_vrp_sequential_payoff.py)
    add_test(NAME maker_execution_learnability_experiment_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_run_maker_execution_learnability_experiment.py)
    add_test(NAME maker_subsecond_information_experiment_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_run_maker_subsecond_information_experiment.py)
    add_test(NAME upgrade_microstructure_capture_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_upgrade_microstructure_capture.py)
    add_test(NAME microstructure_runtime_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_microstructure_runtime.py)
    add_test(NAME microstructure_demo_policy_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_microstructure_demo_policy.py)
    add_test(NAME select_alpha_source_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_select_alpha_source.py)
    add_test(NAME verify_microstructure_demo_binding_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_verify_microstructure_demo_binding.py)
    add_test(NAME run_market_alpha_development_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_run_market_alpha_development.py)
    add_test(NAME run_microstructure_alpha_development_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_run_microstructure_alpha_development.py)
    add_test(NAME run_microstructure_alpha_lifecycle_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_run_microstructure_alpha_lifecycle.py)
    add_test(NAME feature_parity_contract_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_feature_parity_contract.py)
    add_test(NAME check_runtime_health_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_check_runtime_health.py)
    add_test(NAME web_report_store_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/apps/ai_trade_web/backend/tests/test_report_store.py)
    add_test(NAME web_governance_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/apps/ai_trade_web/backend/tests/test_governance.py)
    add_test(NAME web_static_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/apps/ai_trade_web/backend/tests/test_web_static.py)
  endif()
endif()
