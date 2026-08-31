#!/bin/bash
set -euo pipefail

uv venv --allow-existing
uv sync --all-extras --dev
