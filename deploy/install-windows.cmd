@echo off
rem Double-click to install Freqon. Windows asks for administrator rights.
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -NoExit -File ""%~dp0install-windows.ps1""'"
