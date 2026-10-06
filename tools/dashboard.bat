@echo off
rem CopyTrade Bot dashboard — read-only view of data\bot.db.
rem Runs from its own venv (.venv-dash) so its dependencies can never
rem conflict with the trading bot's. Opens http://localhost:8501.
cd /d "%~dp0.."
".venv-dash\Scripts\python.exe" -m streamlit run src\dashboard\app.py
