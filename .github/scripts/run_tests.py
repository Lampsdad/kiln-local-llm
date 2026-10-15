#!/usr/bin/env python3
"""Run every test_*.py next to this file, one after another.

CI calls this instead of naming each file, so a new test file runs as
soon as it is committed. Each file gets a header and its own process,
and runs through unittest, so its tests run whether or not the file
ends with a __main__ block. A failing file does not stop the rest; the
exit status is 1 if any file failed, and the summary lists them.
"""
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    if not files:
        print("FAIL no test_*.py files found in %s" % HERE)
        return 1

    failed = []
    for path in files:
        name = os.path.basename(path)
        print("=" * 70, flush=True)
        print("== %s" % name, flush=True)
        print("=" * 70, flush=True)
        start = time.time()
        # Run from this directory so the file imports as a module named
        # test_x, not as a path that unittest cannot turn into one.
        code = subprocess.call([sys.executable, "-m", "unittest", "-v", name[:-3]],
                               cwd=HERE)
        took = time.time() - start
        if code == 0:
            print("== %s: ok (%.0fs)" % (name, took), flush=True)
        else:
            print("== %s: FAILED with exit %d (%.0fs)" % (name, code, took), flush=True)
            failed.append(name)

    print()
    print("%d test files, %d failed" % (len(files), len(failed)))
    for name in failed:
        print("  FAIL %s" % name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
