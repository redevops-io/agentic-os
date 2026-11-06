#!/bin/bash -p
# check-literals-against-sources.sh SOURCES_ROOT
# A signer MUST run this with the real source root before the first real signing run (SIGNING-PROCEDURE-R0PRIME.md, gate G7): it verifies every
# digest and fingerprint listed in packaging/signing/LITERALS-PROVENANCE.json against its source and FAILS when a source is missing.
# The unit test without a source root is a self-consistency check only.
HERE="${BASH_SOURCE[0]%/*}"
[[ "$HERE" == "${BASH_SOURCE[0]}" ]] && HERE="."
exec /usr/bin/env -i PATH=/usr/bin:/bin /usr/bin/python3 -I "$HERE/check_literals_against_sources.py" "$@"
