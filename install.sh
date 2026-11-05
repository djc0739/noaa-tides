#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ -x ".venv/bin/pip3" ]; then
    .venv/bin/pip3 install -r requirements.txt
else
    pip3 install -r requirements.txt --user
fi
