@echo off
REM Probiotic price tracking: scrape -> store -> process -> dashboard.
REM
REM   run.bat            collect Lazada, rebuild everything, open the dashboard
REM   run.bat offline    rebuild from the snapshots already on disk (no network)
REM   run.bat rebuild    re-parse today's saved JSON payloads, then rebuild
REM   run.bat dashboard  just open the dashboard
REM   run.bat browser    force the browser path, skipping the HTTP attempt
REM   run.bat headed     force the browser path with the window visible (solve a captcha)
REM   run.bat test       run the pipeline self-checks (touches nothing in data\)
REM
REM Collection also runs unattended in GitHub Actions, daily at 08:00 Bangkok. This file is
REM the local path to the same work. KEEP THE TWO IN SYNC: the workflow calls the same two
REM entry points in the same order, collect_lazada.py then pipeline.py. If you add a stage
REM here, add it to .github/workflows/daily-collect.yml as well.
cd /d "%~dp0"
set PY=py
where py >nul 2>nul || set PY=python

if /i "%~1"=="dashboard" goto dashboardonly
if /i "%~1"=="test"      goto test

REM Only this entry point pulls. After a LOCAL collection the freshest data is already
REM on disk and pulling could collide with it; when you just want to look at the numbers,
REM the freshest data is whatever the daily runner committed.
goto skip_pull
:dashboardonly
echo Fetching the data the daily run committed...
pushd "%~dp0.."
git pull --rebase --autostash
if errorlevel 1 echo       (could not pull - showing whatever is already on disk)
popd
goto dashboard
:skip_pull
if /i "%~1"=="offline"   goto process
if /i "%~1"=="rebuild"   goto rebuild

set LAZ_FAILED=
set LAZARGS=
if /i "%~1"=="browser" set LAZARGS=--browser
if /i "%~1"=="headed"  set LAZARGS=--browser --headed

echo [1/3] Collecting Lazada listings...
echo       (3 to 8 minutes - deliberately slow so we stay a polite visitor)
%PY% collect_lazada.py %LAZARGS%
if errorlevel 1 set LAZ_FAILED=1
goto process

:rebuild
echo [1/3] Re-parsing today's saved payloads (no network)...
%PY% collect_lazada.py --rebuild
if errorlevel 1 (
  echo.
  echo Re-parse failed - existing data was left untouched.
  pause & exit /b 1
)

:process
echo.
echo [2/3] Merging snapshots and building the dashboard data...
%PY% pipeline.py
if errorlevel 1 (
  echo.
  echo Processing failed - the previous dashboard data was left untouched.
  echo If the error mentions a conflict copy, delete the file it names and re-run.
  pause & exit /b 1
)

if /i "%~1"=="collect" goto done

:dashboard
echo.
echo [3/3] Opening the dashboard...
REM Opened as a file:// URL with a changing query string. Without it the browser can serve
REM a cached index.html and you keep seeing the previous version after an update.
REM Each of these must be its own line: %CD% is expanded when the line is parsed, so
REM "pushd ... && set DASHDIR=%CD%" would capture the directory from BEFORE the pushd.
pushd "%~dp0..\dashboard"
set "DASHDIR=%CD%"
popd
set "DASHURL=file:///%DASHDIR:\=/%/index.html?t=%RANDOM%%RANDOM%"
start "" "%DASHURL%"
goto done

:test
echo Running pipeline self-checks...
%PY% test_pipeline.py
if errorlevel 1 (
  echo.
  echo Self-checks FAILED - do not trust the numbers until this is fixed.
  pause & exit /b 1
)
goto done

:done
echo.
if defined LAZ_FAILED (
  echo ============================================================
  echo   LAZADA COLLECTION FAILED - NO NEW LAZADA DATA
  echo ============================================================
  echo   Scroll up for the reason. If the anti-bot wall is up,
  echo   retrying will not help. Double-click  run-headed.bat
  echo   and solve the challenge once by hand.
  echo ============================================================
) else (
  echo Done.
)
pause
