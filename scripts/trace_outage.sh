#!/usr/bin/env sh
# Follow one media item through a provider outage and recovery by grepping its ID across the
# api, worker and provider logs. Stack: docker compose -f docker-compose.yml -f docker-compose.test.yml
#   sh scripts/trace_outage.sh > evidence/after/sample_trace.log
set -eu
H1="X-User-Id: member_u"; H2="X-Workspace-Id: ws_unverified"; CT="Content-Type: application/json"
curl -s -X POST localhost:8001/_control -H "$CT" -d '{"mode":"error"}' >/dev/null
ID=$(curl -s -X POST localhost:8000/media -H "$H1" -H "$H2" -H "$CT" \
  -d '{"type":"image","source":"upload","frames":[{"explicit":0.02}]}' | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
sleep 12
curl -s -X POST localhost:8001/_control -H "$CT" -d '{"mode":"ok"}' >/dev/null
for _ in $(seq 1 60); do
  S=$(curl -s localhost:8000/media/$ID -H "$H1" -H "$H2" | python -c "import sys,json;print(json.load(sys.stdin)['status'])")
  [ "$S" = published ] && break
  sleep 1
done
echo "# media $ID (ws_unverified): provider mode=error for 12 s, then ok. Final status: $S"
docker compose logs --no-log-prefix api worker provider 2>/dev/null | grep "$ID" | python -c "
import sys, json
rows = []
for line in sys.stdin:
    try: rows.append(json.loads(line))
    except ValueError: pass
for r in sorted(rows, key=lambda r: r.get('ts', '')):
    print(json.dumps(r))"
