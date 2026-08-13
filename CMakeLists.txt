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
  src/universe/universe_selector.cpp
  src/strategy/strategy_engine.cpp
  src/strategy/integrator_shadow.cpp
  src/strategy/microstructure_demo_overlay.cpp
  src/regime/regime_engine.cpp
  src/risk/risk_engine.cpp
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
  add_executable(trade_system_test tests/test_trade_system.cpp)
  target_link_libraries(trade_system_test PRIVATE ai_trade_system)
  add_test(NAME trade_system_test COMMAND trade_system_test)

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
