#!/bin/bash
# names + mtimes only (never contents) of the real profile locations the app could touch
for d in "Application Support" Caches Preferences Logs WebKit HTTPStorages Cookies "Saved Application State" "Group Containers" "Containers"; do
  p="/Users/arybach/Library/$d"
  [ -d "$p" ] || { echo "ABSENT $d"; continue; }
  /usr/bin/find "$p" -maxdepth 1 \( -iname '*redevops*' -o -iname '*agentic*' -o -iname '*io.redevops*' \) -exec /usr/bin/stat -f '%N %m %Sm' {} \; 2>/dev/null
  echo "DIR $d mtime=$(/usr/bin/stat -f '%m' "$p")"
done
for p in /Users/arybach/.agentic-os /Users/arybach/.cache /Users/arybach/.config /Users/arybach/Library/LaunchAgents; do [ -e "$p" ] && echo "DIR $p mtime=$(/usr/bin/stat -f '%m' "$p")"; done
/usr/bin/find /Users/arybach/Library/Caches /Users/arybach/Library/WebKit /Users/arybach/Library/HTTPStorages "/Users/arybach/Library/Saved Application State" -maxdepth 2 \( -iname '*redevops*' -o -iname '*agentic*' \) 2>/dev/null -exec /usr/bin/stat -f '%N %m' {} \;
