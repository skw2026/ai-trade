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
    add_test(NAME compose_consistency_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_compose_consistency.py)
    add_test(NAME watchdog_utils_test
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_watchdog.py)
  endif()
endif()
