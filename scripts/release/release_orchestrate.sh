#!/bin/bash -p
# release_orchestrate.sh: the one release command for 0.6.0. Default is a read-only dry run.
#   scripts/release/release_orchestrate.sh                         dry run (read-only)
#   scripts/release/release_orchestrate.sh --simulate-authorization --report FILE
#   scripts/release/release_orchestrate.sh --run --workspace DIR   at the owner's terminal, after authorize_release.sh (stops at the first FAIL or BLOCKED stage)
# See scripts/release/release_orchestrate.py for the stages. Python runs isolated (-I): no PYTHONPATH, no user site.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec /usr/bin/env -u PYTHONPATH -u PYTHONHOME -u PYTHONSTARTUP python3 -I "$HERE/release_orchestrate.py" "$@"
