@echo off
REM Double-click this when Lazada asks for a slider verification.
REM
REM It opens a VISIBLE browser. If a slider appears, solve it yourself and the
REM collection carries on by itself. The profile is remembered, so this should
REM be a one-off, and the ordinary run.bat works again afterwards.
cd /d "%~dp0"
call "%~dp0run.bat" headed
