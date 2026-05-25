#!/usr/bin/env bash
# One-time setup for Streamlit Cloud (and local) — installs Playwright's bundled Chromium.
python -m playwright install chromium
python -m playwright install-deps chromium || true
