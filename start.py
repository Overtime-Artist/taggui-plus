#!/usr/bin/env python3
"""Cross-platform launcher for TagGUI.

Runs the app on Windows, macOS, and Linux with a single command:

    python start.py

If a virtual environment named ``venv`` or ``.venv`` exists next to this
file, its Python interpreter is used automatically (no need to activate it
first). If no virtual environment is found, the Python you launched this
script with is used instead, so it also works for a plain system install.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
APP_ENTRY_POINT = REPO_ROOT / 'taggui' / 'run_gui.py'

# Exit code the app returns when it wants to be relaunched (for example after
# importing settings and confirming a restart). When we see this code we start
# a fresh copy of the app in this same console window, instead of the app
# opening a second console window of its own. Keep this value in sync with the
# matching constant in taggui/run_gui.py.
RESTART_EXIT_CODE = 1010


def find_virtual_environment_python() -> Path | None:
    """Return the Python executable inside a local venv, or None if absent."""
    for environment_name in ('venv', '.venv'):
        environment_directory = REPO_ROOT / environment_name
        # Windows keeps the interpreter in Scripts\; macOS/Linux use bin/.
        candidates = [
            environment_directory / 'Scripts' / 'python.exe',
            environment_directory / 'bin' / 'python',
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return None


def main() -> int:
    if not APP_ENTRY_POINT.is_file():
        print(f'Error: could not find {APP_ENTRY_POINT}.', file=sys.stderr)
        return 1

    virtual_environment_python = find_virtual_environment_python()
    if virtual_environment_python is not None:
        python_executable = str(virtual_environment_python)
        print(f'Using virtual environment: {virtual_environment_python.parent.parent}')
    else:
        # No venv found: fall back to the interpreter running this script.
        python_executable = sys.executable
        print('No virtual environment found; using the current Python.')

    # Let the app know it was started by this managed launcher. That way, when
    # it needs to restart itself, it can simply exit with RESTART_EXIT_CODE and
    # let us relaunch it in this same console window, rather than opening a
    # second console window of its own.
    os.environ['TAGGUI_MANAGED_LAUNCHER'] = '1'

    # Run the app entry point directly so its folder is on the import path,
    # exactly as the original start.bat did. If the app asks to restart (by
    # exiting with RESTART_EXIT_CODE), loop and start a fresh copy in this same
    # window; otherwise return its exit code to the caller.
    while True:
        result = subprocess.run([python_executable, str(APP_ENTRY_POINT)])
        if result.returncode != RESTART_EXIT_CODE:
            return result.returncode
        print('Restarting TagGUI...')


if __name__ == '__main__':
    sys.exit(main())
