.PHONY: setup build up down logs shell-admin build-proxy restart-admin

setup:
	@bash setup.sh

build:
	docker compose build

build-proxy:
	docker build --no-cache -t smtp-proxy-tenant:latest ./proxy

up: build build-proxy
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f admin

shell-admin:
	docker exec -it smtp_proxy_admin bash

restart-admin:
	docker compose restart admin
