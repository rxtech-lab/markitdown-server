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