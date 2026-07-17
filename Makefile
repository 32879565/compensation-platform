.PHONY: ci backend-ci frontend-ci dev-db

ci: backend-ci frontend-ci

backend-ci:
	cd backend && ruff check app tests && black --check app tests && mypy app && pytest

frontend-ci:
	cd frontend && npm run lint && npm run typecheck && npm test -- --run && npm run build

dev-db:
	docker compose -f deploy/docker-compose.yml up -d postgres
