#!/bin/bash
# Deliberately no "cd /home/site/wwwroot" here. With Oryx's compressed-destination-dir
# build (which this plan uses unconditionally), the real app content is decompressed by
# the platform into a runtime-local path, not literally left at /home/site/wwwroot -
# and the startup command is expected to run from wherever it already placed us. Forcing
# a cd back to /home/site/wwwroot pointed at the still-compressed, mostly-empty copy.

# azure_sdk_vendor is a last-resort Linux fallback only (see azure_conn/connector.py);
# it must never be prepended ahead of the real antenv site-packages that Oryx installs
# from requirements.txt, or it silently shadows the correctly installed SDK.
export PYTHONPATH="$PWD:$PYTHONPATH:$PWD/azure_sdk_vendor"

if [ -d "antenv" ]; then
    source antenv/bin/activate 2>/dev/null || true
fi

echo "[INFO] Starting Streamlit from $(pwd)..."
python3 -m streamlit run app.py --server.port 8000 --server.address 0.0.0.0 --server.headless true
