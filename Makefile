CRON_SCHEDULE := 7 2 * * *
CRON_CMD      := /Users/rk/github/jerry/bin/gate.sh

.PHONY: test lint type check cron help

help:
	@echo "Available targets:"
	@echo "  test    Run all tests"
	@echo "  lint    Run ruff linter and formatter"
	@echo "  type    Run type checking with ty"
	@echo "  check   Run both type checking and linting"
	@echo "  cron    Install (or replace) the nightly jerry cron entry"

test:
	uv run pytest

lint:
	uv run ruff check . --fix
	uv run ruff format .

type:
	uv run ty check

check: type lint

cron:
	( crontab -l 2>/dev/null | grep -v "jerry/bin/gate.sh"; \
	  echo "$(CRON_SCHEDULE) $(CRON_CMD)" ) | crontab -
	@echo "Cron installed: $(CRON_SCHEDULE) $(CRON_CMD)"
