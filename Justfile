# Default recipe
default: start

# Start OpenTelemetry services
start:
	docker compose up -d

# Stop Docker services
stop:
	docker compose stop

# Run development server
dev:
	uv run uvicorn main:app --reload
