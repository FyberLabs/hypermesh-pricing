#!/usr/bin/env bash
# Start-up checks for a locally built pricing image.
# Generates a throwaway bearer token in memory. Does not print it.
set -euo pipefail

image="${1:?usage: scripts/smoke-image.sh <image>}"
name="pricing-smoke-$$"
refuse="${name}-refuse"

docker rm -f "$refuse" >/dev/null 2>&1 || true
set +e
timeout 20 docker run --rm --name "$refuse" "$image"
status=$?
set -e
docker rm -f "$refuse" >/dev/null 2>&1 || true
if [ "$status" -eq 0 ] || [ "$status" -eq 124 ]; then
  echo "container kept running without PRICING_SERVICE_TOKEN" >&2
  exit 1
fi

token="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
envfile="$(mktemp)"
chmod 600 "$envfile"
printf 'PRICING_SERVICE_TOKEN=%s\n' "$token" > "$envfile"
unset token
trap 'docker rm -f "$name" >/dev/null 2>&1 || true; rm -f "$envfile"' EXIT
docker rm -f "$name" >/dev/null 2>&1 || true
docker run -d --name "$name" --publish 127.0.0.1::8080 \
  --env-file "$envfile" \
  "$image" >/dev/null

mapping="$(docker port "$name" 8080/tcp)"
port="${mapping##*:}"
port="${port%%$'\n'*}"
if [ -z "$port" ]; then
  echo "container did not publish port 8080" >&2
  docker logs "$name" >&2 || true
  exit 1
fi

health=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if health="$(curl -fsS "http://127.0.0.1:${port}/healthz" 2>/dev/null)"; then
    break
  fi
  health=""
  sleep 1
done
if [ -z "$health" ]; then
  echo "GET /healthz did not succeed" >&2
  docker logs "$name" >&2 || true
  exit 1
fi
python3 -c 'import json,sys; body=json.loads(sys.argv[1]); assert body.get("status")=="ok", body' "$health"

code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${port}/v1/floors" \
  -H 'content-type: application/json' -d '{}')"
if [ "$code" != "401" ]; then
  echo "expected 401 from /v1/floors without a bearer token, got ${code}" >&2
  exit 1
fi

echo "smoke ok: refused to start without PRICING_SERVICE_TOKEN; /healthz ok; /v1/floors 401"
