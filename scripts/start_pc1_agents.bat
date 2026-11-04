@echo off
title Khoi dong Agents PC1 (PM & DevOps)
echo ======================================================================
echo  KHOI DONG PM AGENT (PORT 8001) VA DEVOPS AGENT (PORT 8002)
echo ======================================================================
start "PM Agent (8001)" cmd /k "python -m uvicorn agents.pm.api:app --host 0.0.0.0 --port 8001"
start "DevOps Agent (8002)" cmd /k "python -m uvicorn agents.devops.api:app --host 0.0.0.0 --port 8002"
echo [+] Da khoi dong ca 2 Agent tren PC1 thanh cong.
pause
