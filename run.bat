@echo off
REM StoryLiver launcher (Windows)
where py >nul 2>nul && (py run.py %*) || (python run.py %*)
