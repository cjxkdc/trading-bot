"""Offline regression tests in a disposable project, independent of user settings.

Run: python -X utf8 tools/test_project.py. Broker network/order calls use fixtures.
The fixed portfolio fixture is the original technology-strategy benchmark;
MOM8 has separate explicit strategy and portfolio-context tests.
"""
import os
import logging
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not (ROOT / "tests/fixtures/strategy_samples").is_dir():
        raise SystemExit("Run regression tests from the full source checkout; release packages omit test strategy fixtures.")
    with tempfile.TemporaryDirectory(prefix='trading-bot-tests-', ignore_cleanup_errors=True) as directory:
        # Windows CI can expose TEMP through an 8.3 alias (RUNNER~1).
        # Use one canonical root for cwd, imports, and account path comparisons.
        project = (Path(directory) / 'project').resolve()
        project.mkdir()
        for name in ('app', 'backtests', 'brokers', 'configs', 'data', 'framework', 'strategies', 'platforms', 'portal', 'tests', 'tools', 'ui', 'deploy', 'docs', 'requirements'):
            if (ROOT / name).is_dir():
                shutil.copytree(ROOT / name, project / name, ignore=shutil.ignore_patterns(
                    '__pycache__', 'node_modules', '*.pyc', 'execution_policy.json'))
        for name in ('bot.py', 'requirements.txt', 'README.md', '.gitignore'):
            shutil.copyfile(ROOT / name, project / name)
        shutil.copyfile(project / 'tests/fixtures/portfolio_default.json', project / 'configs/portfolio_config.json')
        shutil.copyfile(project / 'tests/fixtures/broker_profiles.json', project / 'configs/broker_profiles.json')
        samples = project / 'tests/fixtures/strategy_samples'
        for folder in ('strategies', 'configs'):
            shutil.copytree(samples / folder, project / folder, dirs_exist_ok=True)
        os.environ['APPDATA'] = str(Path(directory) / 'appdata')
        os.chdir(project)
        sys.path.insert(0, str(project))
        sys.path.insert(0, str(project / 'tests'))
        suite = unittest.defaultTestLoader.discover(str(project / 'tests'))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        logging.shutdown()
        return int(not result.wasSuccessful())


if __name__ == '__main__':
    raise SystemExit(main())
