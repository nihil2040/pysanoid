"""Jobs spread over forked workers. A worker inherits the parent's memory as it is at the fork, so `work` can read
what the parent set up; its results come back as JSON through a file."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any


def fan_out(jobs: list[Any], work: Callable[[Any], Any], workers: int | None = None) -> list[Any]:
    """[work(job) for job in jobs], computed by up to `workers` processes (the machine's cores, at most 8)."""
    n = max(1, min(workers or os.cpu_count() or 1, 8, len(jobs)))
    scratch = Path(tempfile.mkdtemp(prefix="fan_out_"))
    sys.stdout.flush()
    pids = []
    for k in range(n):
        pid = os.fork()
        if pid == 0:
            status = 1
            try:
                _ = (scratch / f"{k}.json").write_text(json.dumps([work(job) for job in jobs[k::n]]))
                status = 0
            except BaseException:  # noqa: BLE001 - shown here, and the parent raises
                traceback.print_exc(file=sys.__stderr__)
            finally:
                os._exit(status)
        pids.append(pid)
    failed = [k for k, pid in enumerate(pids) if os.waitpid(pid, 0)[1] != 0]
    try:
        if failed:
            raise RuntimeError(f"{len(failed)} of {n} workers failed")
        results: list[Any] = [None] * len(jobs)
        for k in range(n):
            results[k::n] = json.loads((scratch / f"{k}.json").read_text())
        return results
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
