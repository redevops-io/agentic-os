.PHONY: install test run modules permissions-key permissions-check
# The control plane needs a persistent grant-store key. Create it once, on purpose: `make permissions-key` writes
# PERMISSIONS_KEY to a 0600 env file OUTSIDE the repository (default ~/.config/agentic-os/, or $XDG_CONFIG_HOME): never printed, never
# overwritten, never in a Docker build context. There is no default key.
PERMISSIONS_ENV ?= $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME),$(HOME)/.config)/agentic-os/permissions.env
PERMISSIONS_DIR ?= .local/permissions

install: ; uv sync
test:    ; uv run pytest -q
permissions-key:
	uv run agentic-os permissions key --env-file $(PERMISSIONS_ENV)
permissions-check:
	@if [ -n "$$PERMISSIONS_KEY" ] || [ -n "$$PERMISSIONS_KEY_FILE" ] || [ -f "$(PERMISSIONS_ENV)" ]; then exit 0; fi; \
	echo "error: no PERMISSIONS_KEY. Create one once with 'make permissions-key' (writes $(PERMISSIONS_ENV), mode 0600), or export PERMISSIONS_KEY / PERMISSIONS_KEY_FILE." >&2; exit 1
run: permissions-check
	@f="$(PERMISSIONS_ENV)"; case "$$f" in /*) ;; *) f="./$$f";; esac; \
	set -a; if [ -z "$$PERMISSIONS_KEY" ] && [ -f "$$f" ]; then . "$$f"; fi; set +a; \
	PERMISSIONS_DIR="$${PERMISSIONS_DIR:-$(PERMISSIONS_DIR)}" uv run uvicorn agentic_os.control_plane:app --reload --port 8080
modules: ; uv run agentic-os modules
