#!/usr/bin/env bash
# Live proof of the egress boundary, on a throwaway network (no compose project, nothing else is touched):
#   * a client on an INTERNAL network cannot reach anything directly (not the origin, not the internet);
#   * through the proxy it reaches only what the allowlist names, and the proxy refuses the rest and the metadata address;
#   * every decision is on the proxy's log.
# Needs a running Docker daemon and builds one small image. Cleans up after itself. Usage: deploy/egress/selftest.sh
set -u
cd "$(dirname "$0")"
T=egress-selftest-$$
INT=$T-int; OUT=$T-out; IMG=$T-img
pass=0; fail=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }
cleanup() { docker rm -f $T-proxy $T-origin >/dev/null 2>&1; docker network rm $INT $OUT >/dev/null 2>&1; docker rmi $IMG >/dev/null 2>&1; rm -f "$ALLOW"; }
trap cleanup EXIT
ALLOW=$(mktemp)
printf 'origin:8080 private\n' > "$ALLOW"

docker build -q -t $IMG . >/dev/null || { echo "build failed"; exit 2; }
docker network create --internal $INT >/dev/null && docker network create $OUT >/dev/null || exit 2
docker run -d --name $T-origin --network $OUT --network-alias origin --entrypoint python $IMG -m http.server 8080 >/dev/null
docker run -d --name $T-proxy --network $INT --network-alias egress -v "$ALLOW":/etc/egress/allowlist.txt:ro $IMG >/dev/null
docker network connect $OUT $T-proxy
sleep 2
ORIGIN_IP=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$OUT\").IPAddress}}" $T-origin)

client() { docker run --rm --network $INT --entrypoint python -e HTTP_PROXY=http://egress:3128 -e http_proxy=http://egress:3128 $IMG -c "$1" 2>&1; }

echo "egress boundary self-test"
out=$(client "
import socket
for host,port in (('$ORIGIN_IP',8080),('1.1.1.1',443),('origin',8080)):
    s=socket.socket(); s.settimeout(3)
    try: s.connect((host,port)); print('REACHED',host)
    except Exception as e: print('blocked',host)
")
echo "$out" | grep -q REACHED && bad "a process that ignores the proxy reached something directly: $out" || ok "direct sockets (origin IP, the internet, the origin by name) are all unreachable"

r=$(client "import urllib.request as u; print(u.urlopen('http://origin:8080/', timeout=5).status)")
[ "$r" = "200" ] && ok "an allowlisted destination is reachable through the proxy" || bad "allowlisted destination failed: $r"

r=$(client "
import urllib.request as u, urllib.error as e
try: u.urlopen('http://blocked.example.com/', timeout=5); print('REACHED')
except e.HTTPError as x: print(x.code)
")
[ "$r" = "403" ] && ok "a destination off the allowlist is refused (403)" || bad "off-list destination: $r"

r=$(client "
import urllib.request as u, urllib.error as e
try: u.urlopen('http://169.254.169.254/latest/meta-data/', timeout=5); print('REACHED')
except e.HTTPError as x: print(x.code)
")
[ "$r" = "403" ] && ok "the cloud metadata address is refused (403)" || bad "metadata address: $r"

logs=$(docker logs $T-proxy 2>&1)
echo "$logs" | grep -q '"allow": true' && echo "$logs" | grep -q '"allow": false' && echo "$logs" | grep -q 'blocked.example.com' \
  && ok "every decision is on the proxy log" || bad "decision log incomplete"

echo "$pass passed, $fail failed"
[ $fail -eq 0 ]
