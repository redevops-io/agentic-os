# Convenience targets for the portable apps stack. Secrets are generated first (idempotent).
SECRETS ?= $(or $(REDEVOPS_SECRETS_FILE),$(HOME)/.config/redevops/secrets.env)

secrets:
	./bootstrap-secrets.sh

up: secrets
	docker compose --env-file .env --env-file $(SECRETS) up -d

.PHONY: secrets up
