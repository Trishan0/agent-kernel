#!/bin/bash
set -eo pipefail
#
# Builds the three Lambda packages and runs terraform. Docker must be running (the agent runner
# deploys as a container image because agentkernel[aws,openai,cron] exceeds Lambda's 250 MB
# unzipped zip limit). Pass `local` as $1 to install agentkernel from ../../../ak-py/dist.
#
# Generated requirements.txt / dist_* are removed / gitignored - never commit them.

LOCAL_BUILD=${1-}

# $1 extra, $2 entrypoint, $3 dist dir, $4 local-extras
zip_package() {
  local extra="$1" entry="$2" dist="$3" local_extras="$4"
  echo "Building $dist (zip, extra: $extra)..."
  pushd ../ >/dev/null
  rm -rf "$dist" "$dist.zip"
  mkdir -p "$dist"
  uv export --extra "$extra" --no-hashes >requirements.txt
  if [[ $LOCAL_BUILD != "local" ]]; then
    uv pip install -r requirements.txt --target="$dist"
  else
    uv pip install --force-reinstall --target="$dist" --find-links ../../../ak-py/dist "agentkernel[$local_extras]" --no-cache-dir
  fi
  cp lambda.py lambda_response_handler.py agent.py tool.py sites.py detectors.py solar_model.py \
     investigate.py llm.py state_dynamodb.py "$dist/"
  cp deploy/config.yaml "$dist/config.yaml"
  cp data/sites.yaml "$dist/" 2>/dev/null || true
  mkdir -p "$dist/data" && cp data/sites.yaml "$dist/data/"
  cd "$dist" && zip -rq "../$dist.zip" .
  popd >/dev/null
}

# $1 extra, $2 dist dir, $3 local-extras
image_package() {
  local extra="$1" dist="$2" local_extras="$3"
  echo "Building $dist (image)..."
  pushd ../ >/dev/null
  rm -rf "$dist"
  mkdir -p "$dist/data"
  uv export --extra "$extra" --no-hashes >requirements.txt
  if [[ $LOCAL_BUILD != "local" ]]; then
    uv pip install -r requirements.txt --target="$dist/data"
  else
    uv pip install --force-reinstall --target="$dist/data" --find-links ../../../ak-py/dist "agentkernel[$local_extras]" --no-cache-dir
  fi
  cp lambda_agent_runner.py agent.py tool.py sites.py detectors.py solar_model.py \
     investigate.py llm.py state_dynamodb.py "$dist/data/"
  cp deploy/config.yaml "$dist/data/config.yaml"
  mkdir -p "$dist/data/data" && cp data/sites.yaml "$dist/data/data/"
  popd >/dev/null
  cp Dockerfile.agent_runner "../$dist/Dockerfile"
}

zip_package request_handler  lambda.py                  dist_request_handler  "aws,cron"
image_package agent_runner    dist_agent_runner          "aws,openai,cron"
zip_package response_handler  lambda_response_handler.py dist_response_handler "aws"

rm -f ../requirements.txt

terraform init
terraform apply
