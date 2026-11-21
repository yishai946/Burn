@echo off
rem Builds dist\Burner-Setup.exe: PyInstaller (app + ffmpeg) then Inno Setup (installer).
rem   build.bat           build only
rem   build.bat release   build, then publish a GitHub release tagged v<core.VERSION> (needs gh, logged in)
rem Needs: regular Python 3.11 with pyinstaller + the app's packages, ffmpeg, Inno Setup 6.
rem PY, FF (ffmpeg.exe) and ISCC can be set beforehand; otherwise the local defaults below are used
rem (the GitHub release workflow sets all three).
cd /d "%~dp0"
if not defined PY set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined ISCC set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
for /f "delims=" %%F in ('where ffmpeg') do if not defined FF set "FF=%%F"
if not defined FF (echo ffmpeg not found: put it on PATH or set FF & exit /b 1)
for /f "delims=" %%V in ('call "%PY%" -c "import core; print(core.VERSION)"') do set VER=%%V
if not defined VER (echo could not read core.VERSION & exit /b 1)
echo Building Burner %VER%

"%PY%" -m PyInstaller --noconfirm --clean --windowed --onedir --name Burner --icon "%CD%\icon.ico" ^
  --add-data "%CD%\index.html;." --add-data "%CD%\icon.ico;." --add-data "%CD%\vendor;vendor" --add-binary "%FF%;." ^
  --collect-submodules yt_dlp --collect-submodules windows_toasts --collect-submodules winrt --workpath "%TEMP%\burner-build" --specpath "%TEMP%\burner-build" --distpath "%CD%\dist" "%CD%\app.py" || exit /b 1

rem build check: song search, YouTube lookup and a real download with the bundled ffmpeg
dist\Burner\Burner.exe --selftest

"%ISCC%" /Q /DAppVersion=%VER% installer\burner.iss || exit /b 1
echo Built dist\Burner-Setup.exe (%VER%)

if /i "%1"=="release" (
  gh release create v%VER% dist\Burner-Setup.exe --title "Burner %VER%" --generate-notes || exit /b 1
  echo Published v%VER%: installed copies will offer the update on their next start.
)
