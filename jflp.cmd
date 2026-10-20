@echo off
rem Lancador do JFLP Studio para o cmd: delega ao jflp.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0jflp.ps1" %*
exit /b %ERRORLEVEL%
