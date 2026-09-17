#!/bin/bash
cd "$(dirname "$0")/.."
exec .venv/bin/python tools/funding_dispersion_collector.py
