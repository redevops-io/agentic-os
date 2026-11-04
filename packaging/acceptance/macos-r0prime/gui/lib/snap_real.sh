#!/bin/bash
# READ-ONLY snapshot of the REAL profile locations the app could touch: names and modification times only, never contents.
# It uses only `find` (with -exec stat) and `stat`; it writes nothing but its standard output and changes nothing.
# REAL_HOME must be given by the caller (absolute path of the real user's home); the harness' own HOME is a disposable directory,
# so $HOME is deliberately NOT used. No user name or scratch path is hard-coded here.
: "${REAL_HOME:?set REAL_HOME to the real user home directory (absolute path); this script only reads names and mtimes}"
case "$REAL_HOME" in /*) ;; *) echo "REAL_HOME must be absolute" >&2; exit 2;; esac
for d in "Application Support" Caches Preferences Logs WebKit HTTPStorages Cookies "Saved Application State" "Group Containers" "Containers"; do
  p="$REAL_HOME/Library/$d"
  [ -d "$p" ] || { echo "ABSENT $d"; continue; }
  /usr/bin/find "$p" -maxdepth 1 \( -iname '*redevops*' -o -iname '*agentic*' -o -iname '*io.redevops*' \) -exec /usr/bin/stat -f '%N %m %Sm' {} \; 2>/dev/null
  echo "DIR $d mtime=$(/usr/bin/stat -f '%m' "$p")"
done
for p in "$REAL_HOME/.agentic-os" "$REAL_HOME/.cache" "$REAL_HOME/.config" "$REAL_HOME/Library/LaunchAgents"; do [ -e "$p" ] && echo "DIR $p mtime=$(/usr/bin/stat -f '%m' "$p")"; done
/usr/bin/find "$REAL_HOME/Library/Caches" "$REAL_HOME/Library/WebKit" "$REAL_HOME/Library/HTTPStorages" "$REAL_HOME/Library/Saved Application State" -maxdepth 2 \( -iname '*redevops*' -o -iname '*agentic*' \) 2>/dev/null -exec /usr/bin/stat -f '%N %m' {} \;
