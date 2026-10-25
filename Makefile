# Convenience targets. Every command is also written out in README.md (make may not exist on Windows).
TEST_COMPOSE = docker compose -f docker-compose.yml -f docker-compose.test.yml

.PHONY: up test-up test frontend-test workload workload-40 audit crash trace lint down repro

up:
	docker compose up --build

test-up:
	$(TEST_COMPOSE) up -d --build

test:
	$(TEST_COMPOSE) exec api pytest

frontend-test:
	cd frontend && npm ci && npx vitest run

workload:
	$(TEST_COMPOSE) exec api python scripts/workload.py --out evidence/after/workload.json

workload-40:
	$(TEST_COMPOSE) exec api python scripts/workload.py --video-frames 40 --out evidence/after/workload_40.json

audit:
	$(TEST_COMPOSE) exec api python scripts/audit.py

crash:
	python scripts/crash_test.py

trace:
	sh scripts/trace_outage.sh

lint:
	$(TEST_COMPOSE) exec api ruff check app tests

down:
	docker compose down -v

# Reproduce the incident on the broken baseline in a separate worktree and compose project.
repro:
	docker compose down -v
	git worktree add ../baseline broken-baseline
	cd ../baseline && docker compose -p baseline -f docker-compose.yml -f docker-compose.test.yml up -d --build \
		&& sleep 20 && docker compose -p baseline exec api pytest tests/incident -v; \
		docker compose -p baseline down -v
