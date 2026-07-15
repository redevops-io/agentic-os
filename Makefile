.PHONY: install test run modules
install: ; uv sync
test:    ; uv run pytest -q
run:     ; uv run uvicorn agentic_os.control_plane:app --reload --port 8080
modules: ; uv run agentic-os modules
