import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

loader = unittest.TestLoader()
names = sys.argv[1:]
suite = (
    loader.loadTestsFromNames(names)
    if names
    else loader.discover(os.path.join(ROOT, "tests"))
)
result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
sys.stdout.flush()
sys.stderr.flush()
os._exit(0 if result.wasSuccessful() else 1)
