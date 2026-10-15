#!/usr/bin/env python3
"""
kiln tui -- the status board and the everyday verbs as a full-screen
terminal app, with an interactive benchmark.

    tui.py      open it; installs Textual the first time

This file only makes sure Textual is there and hands over to
tui_app.py, so it runs on any Python kiln supports and can explain
itself when something is missing.

WHY TEXTUAL
-----------
A full-screen terminal app needs raw keys, redraws and resizing on
three platforms. The standard library's answer is curses, which
Windows does not ship, and doing it by hand with escape codes means
re-solving cmd.exe, Windows Terminal and every macOS terminal
separately. Textual already has: it is the Python counterpart of what
Claude Code (Ink) and Codex (Ratatui) are built on, and it works over
SSH, which a browser page on 127.0.0.1 would not.

WHY ITS OWN VENV
----------------
Textual is the one thing kiln needs that is not in the standard
library, so it goes into .kiln/venv-<platform> inside this checkout,
not into your Python. Debian, Ubuntu and Fedora refuse a plain
`pip install` into the system interpreter (PEP 668), and a private venv
cannot break anything else you run. One per platform, because a
checkout shared between Windows and WSL would otherwise have both
fighting over one. Delete .kiln/ to remove it; the next `kiln tui`
asks again.
"""

import os
import shutil
import signal
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True

WINDOWS = os.name == "nt"
REQUIREMENT = "textual>=8.2,<9"
MAJOR = 8
VENV = os.path.join(ROOT, ".kiln", "venv-" + sys.platform)


def venv_python():
    if WINDOWS:
        return os.path.join(VENV, "Scripts", "python.exe")
    return os.path.join(VENV, "bin", "python")


def usable_here():
    """Textual of the major version this was written against, in the
    Python running now."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        return int(version("textual").split(".")[0]) == MAJOR
    except (ImportError, ValueError, PackageNotFoundError):
        return False


def quiet_call(argv):
    """Exit status of argv, or None when it cannot be started at all."""
    try:
        return subprocess.call(argv, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    except OSError:
        return None


def venv_ready():
    py = venv_python()
    if not os.path.isfile(py):
        return False
    probe = ("from importlib.metadata import version; import sys; "
             "sys.exit(int(version('textual').split('.')[0]) != %d)" % MAJOR)
    return quiet_call([py, "-c", probe]) == 0


def running_in_venv():
    norm = lambda p: os.path.normcase(os.path.realpath(p))  # noqa: E731
    return norm(sys.prefix) == norm(VENV)


def install():
    rel = os.path.relpath(VENV, ROOT)
    print()
    print(" kiln tui needs Textual, a library for terminal apps.")
    print(" It installs once, into %s inside this folder, and" % rel)
    print(" nowhere else. Delete that folder to remove it.")
    print()
    if not sys.stdin.isatty():
        print(" Run `kiln tui` from a terminal to install it.")
        return False
    try:
        answer = input(" Install it now? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if answer not in ("", "y", "yes"):
        return False

    # A venv keeps the Python it was made from. If that Python was
    # upgraded or removed, or a creation was interrupted, its pip can
    # never run, and reusing the venv fails the same way on every run.
    if os.path.lexists(VENV) and quiet_call(
            [venv_python(), "-m", "pip", "--version"]) != 0:
        print(" %s is broken, so it is being rebuilt ..." % rel)
        shutil.rmtree(VENV, ignore_errors=True)

    if not os.path.isfile(venv_python()):
        print(" Creating %s ..." % rel)
        if subprocess.call([sys.executable, "-m", "venv", VENV]) != 0:
            # Debian's venv makes bin/python before it finds ensurepip
            # missing. Left there, the next run, after the apt install
            # below, would skip straight to a pip that is not in it.
            shutil.rmtree(VENV, ignore_errors=True)
            print()
            print(" Python could not create a virtual environment.")
            if not WINDOWS and sys.platform != "darwin":
                print(" On Debian or Ubuntu that needs the venv package:")
                print("     sudo apt install python3-venv")
                print(" then run kiln tui again.")
            return False
    print(" Installing Textual ...")
    code = subprocess.call([venv_python(), "-m", "pip", "install", "-q",
                            "--disable-pip-version-check", REQUIREMENT])
    if code != 0:
        print()
        print(" pip could not install Textual. Check the connection and run")
        print(" kiln tui again.")
        return False
    return True


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if sys.version_info < (3, 9):
        print(" kiln tui needs Python 3.9 or newer; this is %d.%d."
              % sys.version_info[:2])
        print(" Everything else in kiln still works on this Python.")
        return 1

    if usable_here():
        import tui_app
        return tui_app.main(argv)

    if running_in_venv() or not venv_ready():
        if not install():
            return 1

    # Hand over to the venv's Python. Ignore Ctrl+C here: the app
    # handles its own keys, and a parent that died on one would leave
    # the child drawing over the shell.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    return subprocess.call([venv_python(), os.path.abspath(__file__)] + argv)


if __name__ == "__main__":
    sys.exit(main())
