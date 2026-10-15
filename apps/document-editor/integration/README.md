# Integration tests against a real stock ONLYOFFICE Document Server

Marked `integration`; skipped unless `DOCEDITOR_IT_ONLYOFFICE_URL` and `DOCEDITOR_IT_ONLYOFFICE_JWT_SECRET` are set, so CI on
Ubuntu/macOS/Windows still runs the unit tests only (`-m "not integration"`).

```
python apps/document-editor/integration/run_throwaway.py        # starts, tests, ALWAYS removes the container + volumes
```

## Pinned image

`PINNED_IMAGE`: `onlyoffice/documentserver@sha256:3ab6ebc7c605e5a32b7ae3ff19daed4925090245acc8100ce2230bd766c88212`
(tag `9.4.0.1`, latest stable 9.x on Docker Hub at 2026-10-15; server build `9.4.0.129`, source tag `v9.4.0.129`).
Resolved with the registry API (`GET /v2/onlyoffice/documentserver/manifests/9.4.0.1`, header `Docker-Content-Digest` of the
OCI image index). To bump: resolve the new index digest, edit `PINNED_IMAGE`, rerun, update `docs/document-editor-connector/EDIT_API.md`.

## Container hygiene

`docker run -d --memory 6g -p 127.0.0.1::80` (loopback, random port, never published), `JWT_ENABLED=true` with a random
throwaway secret, `ALLOW_PRIVATE_IP_ADDRESS=true` (the editor must fetch signed bytes from this workstation's bridge address,
`DOCEDITOR_IT_HOST_ADDR`, default `172.17.0.1`). `docker rm -f -v` runs in a `finally`. Never point it at a production host.

## What it proves

See the table at the end of `docs/document-editor-connector/EDIT_API.md`. The `open` proof drives headless Chrome through a
tiny stdlib CDP client (`cdp.py`) and waits for the editor's `onDocumentReady`; without Chrome that single test skips and `open`
would be unproven on that machine.

## Last run

2026-10-15, Linux x86_64, fresh container from the pinned digest: **8 passed**.
