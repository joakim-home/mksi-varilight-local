#!/bin/sh
set -eu

CONFIG=${1:-/etc/mksi-varilightd/config.json}

systemctl is-active --quiet mksi-varilightd.service

BIND=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["bind"])' "$CONFIG")
PORT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port",2016))' "$CONFIG")

if ! ss -ltn | grep -Fq "$BIND:$PORT"; then
  echo "FAIL listener missing: $BIND:$PORT" >&2
  exit 1
fi

echo "OK service=active listener=$BIND:$PORT"
