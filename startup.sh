#!/bin/bash
# Azure App Service startup command for SentinelSwarm
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000
