"""Run synthetic tests against this checkout, without touching a user's data."""
from pathlib import Path
import sys
import unittest


def main():
    system = Path(__file__).resolve().parent
    sys.path.insert(0, str(system))
    suite = unittest.defaultTestLoader.discover(str(system / 'tests'), pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
