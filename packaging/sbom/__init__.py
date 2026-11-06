"""Shipped-component SBOM tooling for the Agentic Apps native build.

generate_sbom.py  builds a CycloneDX 1.5 SBOM from the BUILT artifacts (PyInstaller archive, launcher
                  crate graph, UI bundle), not from a lock file
verify_sbom.py    cross-checks any SBOM against the archive and fails on unlisted components

Licences are recorded as DECLARED by the package metadata, never as cleared. Nothing here asserts
legal conclusions.
"""
