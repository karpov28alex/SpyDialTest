.PHONY: up down logs test lint migrate webhook backup
up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200 api worker

test:
	pytest -q

lint:
	ruff check .

migrate:
	alembic upgrade head

webhook:
	python scripts/set_webhook.py

backup:
	bash scripts/backup.sh
