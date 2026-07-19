# Local development.
#
#   make up      rabbitmq + minio
#   make api     the producer (uvicorn, with reload)
#   make worker  a consumer — run several to add capacity
#   make test    the test suite
#
# The database is remote (Turso); only the broker and object storage run here.

ifneq (,$(wildcard .env))
-include .env
export
endif

PY   := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
PORT ?= 8000

.PHONY: up down api worker test docker

up:
	docker compose up -d

down:
	docker compose down

api:
	$(PY) -m uvicorn api.index:app --reload --host 0.0.0.0 --port $(PORT)

worker:
	$(PY) -m worker

test:
	$(PY) -m pytest -q

docker:
	docker buildx create --use --name multi-arch-builder || true
	docker buildx build \
		--platform linux/amd64 \
		--tag sirily11/markitdown-server:latest \
		--tag sirily11/markitdown-server:$$(git describe --tags --always) \
		--build-arg VERSION=$$(git describe --tags --always) \
		--build-arg BUILD_TIME=$$(date -u +'%Y-%m-%d_%H:%M:%S') \
		--build-arg COMMIT_HASH=$$(git rev-parse --short HEAD) \
		--push \
		.
