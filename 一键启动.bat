@echo off
chcp 65001 >nul
title Music Downloader Web
cd /d "%~dp0"
python -u selftest.py
if errorlevel 1 pause
