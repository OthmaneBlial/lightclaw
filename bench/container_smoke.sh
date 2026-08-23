#!/usr/bin/env bash
set -euo pipefail

docker_config_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$docker_config_dir"
}
trap cleanup EXIT

docker --config "$docker_config_dir" build --tag lightclaw:bench .
docker --config "$docker_config_dir" run --rm lightclaw:bench --help >/dev/null
docker --config "$docker_config_dir" run \
  --rm \
  --memory=256m \
  --cpus=0.5 \
  lightclaw:bench \
  demo --scenario repo-task --output /tmp/lightclaw-demo --json
