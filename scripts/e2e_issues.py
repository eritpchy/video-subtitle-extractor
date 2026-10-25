"""Run issue-targeted real-media regressions with retained per-case evidence."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for name in ('multiline', 'av1', 'timeline'):
        directory = output / name
        directory.mkdir(exist_ok=True)
        try:
            with (directory / 'runner.log').open('w', encoding='utf-8') as log:
                subprocess.run([sys.executable, str(ROOT / 'scripts' / f'e2e_{name}.py'),
                                '--output', str(directory)], cwd=ROOT, stdout=log,
                               stderr=subprocess.STDOUT, check=True, timeout=1200)
            results.append({'case': name, 'result': 'passed'})
        except (OSError, subprocess.SubprocessError) as error:
            results.append({'case': name, 'result': 'failed', 'error': str(error)})
        (output / 'report.json').write_text(json.dumps(results, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(results, indent=2))
    return int(any(item['result'] != 'passed' for item in results))


if __name__ == '__main__':
    raise SystemExit(main())
