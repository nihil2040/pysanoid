"""pysanoid.main as a program, its clock frozen at $FAKE_NOW (lib/world.py's Clock): what dev/diff_whole_run.py runs
beside upstream's whole script under lib/FakeTime.pm.

    python3 dev/lib/run_frozen.py [pysanoid's options]
"""
import os
import sys
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(DEV_DIR), str(DEV_DIR.parent)]

import pysanoid as ps
from lib.world import Clock, set_clock

set_clock(Clock(float(os.environ["FAKE_NOW"])))
sys.exit(ps.main(sys.argv[1:]))
