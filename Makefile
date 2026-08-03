.PHONY: install run run-dev agents test lint docker-up docker-down

HOST ?= 0.0.0.0
PORT ?= 8787

install:
	python3 -m venv .venv
	.venv/bin/pip install -U pip
	.venv/bin/pip install -r requirements.txt

run:
	.venv/bin/uvicorn main:app --host $(HOST) --port $(PORT)

run-dev:
	.venv/bin/uvicorn main:app --reload --host 127.0.0.1 --port $(PORT)

agents:
	.venv/bin/python -m forge agents

greenfield:
	.venv/bin/python -m forge run greenfield --workers 4

test:
	.venv/bin/python -m pytest tests -q

test-unit:
	.venv/bin/python -m pytest tests/unit -q

test-integration:
	.venv/bin/python -m pytest tests/integration -q

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down
