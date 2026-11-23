@echo off
title Almacen de Alimentos CLAP - Ejercito Bolivariano (Local Offline)
cd /d "%~dp0"
echo Iniciando servidor local 100%% offline en http://127.0.0.1:5000 ...
start "" http://127.0.0.1:5000
python app.py
pause
