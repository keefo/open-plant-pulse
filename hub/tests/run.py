#!/usr/bin/env python3
"""Run the hub's tests, one process per file, all at once.

Serially this suite took just under three minutes, which is long enough
that it stops being run. Almost all of that was waiting rather than work:
every test that started a web server paid half a second in teardown to
`shutdown()`, because `serve_forever` polls twice a second by default, and
the browser tests slept a fixed three to five seconds wherever they needed
the page to have caught up.

With those fixed the suite takes about half a minute; spread over the
cores already in the machine it takes about a quarter of that. The files
are independent -- each builds its own store and its own server -- so
there is nothing to coordinate beyond collecting the results.

    python3 hub/tests/run.py            every file
    python3 hub/tests/run.py plants     only files matching "plants"
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

TESTS = Path(__file__).parent
ROOT = TESTS.parents[1]


def main() -> int:
    wanted = sys.argv[1:]
    files = sorted(p.stem for p in TESTS.glob("test_*.py"))
    if wanted:
        files = [f for f in files if any(w in f for w in wanted)]
    if not files:
        print(f"no test files match {wanted}")
        return 1

    environment = {**os.environ, "PYTHONPATH": str(ROOT / "hub" / "src")}
    started = time.time()
    running = {
        name: subprocess.Popen(
            [sys.executable, "-m", "unittest", "discover",
             "-s", str(TESTS), "-p", f"{name}.py"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=ROOT, env=environment, text=True,
        )
        for name in files
    }

    failed = []
    for name, process in running.items():
        output, _ = process.communicate()
        if process.returncode != 0:
            failed.append((name, output))

    for name, output in failed:
        print(f"\n{'=' * 70}\n{name}\n{'=' * 70}\n{output.rstrip()}")

    elapsed = time.time() - started
    if failed:
        print(f"\n{len(failed)} of {len(files)} files failed in {elapsed:.1f}s: "
              f"{', '.join(name for name, _ in failed)}")
        return 1
    print(f"all {len(files)} test files passed in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
