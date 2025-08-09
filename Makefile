.PHONY: all docker dev

all: 
	otel dev

otel:
	docker compose up -d
dev:
	uv run uvicorn main:app --reload

stop:
	docker compose stop