SHELL := /bin/bash

.PHONY: help configure build test qa compose-check report-contract closed-loop-assess web-backend-run

help:
	@echo "Targets:"
	@echo "  configure         - cmake configure"
	@echo "  build             - cmake build"
	@echo "  test              - ctest all"
	@echo "  qa                - full engineering quality gate"
	@echo "  compose-check     - validate compose files"
	@echo "  report-contract   - validate closed-loop report contracts"
	@echo "  closed-loop-assess- run assess workflow locally via script"
	@echo "  web-backend-run   - run ai-trade-web backend locally"

configure:
	cmake -S . -B build

build:
	cmake --build build -j$$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)

test:
	ctest --test-dir build --output-on-failure

qa:
	tools/quality_gate.sh

compose-check:
	docker compose -f docker-compose.yml config >/tmp/ai_trade_compose_dev.txt
	AI_TRADE_IMAGE=dummy AI_TRADE_RESEARCH_IMAGE=dummy docker compose -f docker-compose.prod.yml --profile research config >/tmp/ai_trade_compose_prod.txt

report-contract:
	python3 tools/validate_reports.py --reports-root ./data/reports/closed_loop --allow-missing

closed-loop-assess:
	tools/closed_loop_runner.sh assess --compose-file docker-compose.yml --output-root ./data/reports/closed_loop --stage S3 --since 1h

web-backend-run:
	cd apps/ai_trade_web/backend && python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
