#!/usr/bin/env bash
# StoryLiver launcher
set -e
cd "$(dirname "$0")"
exec python3 run.py "$@"
