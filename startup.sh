#!/bin/bash
# Azure Web App startup script
exec uvicorn app:app --host 0.0.0.0 --port 8000 --workers 2
