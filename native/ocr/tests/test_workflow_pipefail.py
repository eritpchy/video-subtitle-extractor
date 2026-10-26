"""Exercise tee gates under GitHub's default bash -e, not assumed shell flags."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]


def pipeline_prefixes():
    records = []
    for path in sorted((ROOT / '.github/workflows').glob('*.yml')):
        lines = path.read_text(encoding='utf-8').splitlines()
        index = 0
        while index < len(lines):
            if lines[index].strip() != 'run: |':
                index += 1
                continue
            indent = len(lines[index]) - len(lines[index].lstrip())
            index += 1
            body = []
            while index < len(lines) and (not lines[index].strip() or len(lines[index]) - len(lines[index].lstrip()) > indent):
                body.append(lines[index][indent + 2:])
                index += 1
            for number, line in enumerate(body):
                if '| tee ' not in line:
                    continue
                start = number
                while start and body[start - 1].rstrip().endswith('\\'):
                    start -= 1
                records.append((path.name, '\n'.join(body[:start])))
    return records


PIPELINES = pipeline_prefixes()


def test_all_logged_shell_gates_have_explicit_pipefail_before_the_command():
    assert len(PIPELINES) >= 5
    assert {'macos-coreml.yml', 'macos-accessibility.yml', 'ci.yml'} <= {name for name, _ in PIPELINES}
    for name, prefix in PIPELINES:
        assert any(line.strip().startswith('set ') and 'pipefail' in line for line in prefix.splitlines()), name


@pytest.mark.skipif(os.name != 'posix' or not shutil.which('bash'), reason='Actual Mac/Linux bash pipeline semantics')
@pytest.mark.parametrize('name,prefix', PIPELINES)
def test_logged_producer_failure_is_not_hidden_by_successful_tee(tmp_path, name, prefix):
    script = tmp_path / 'workflow-step.sh'
    producer = shlex.quote(sys.executable) + " -c 'print(\"intentional producer failure\"); raise SystemExit(23)'"
    tail = producer + ' 2>&1 | tee pipeline.log\nprintf false-success > false-success-marker\n'
    script.write_text(prefix + '\n' + tail, encoding='utf-8')
    result = subprocess.run([shutil.which('bash'), '-e', str(script)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 23, (name, result.stdout, result.stderr)
    assert 'intentional producer failure' in (tmp_path / 'pipeline.log').read_text()
    assert not (tmp_path / 'false-success-marker').exists()
    # Reproduce the exact original defect rather than testing only strings:
    # bash -e alone reports success when tee, the last command, succeeds.
    script.write_text(tail, encoding='utf-8')
    control = subprocess.run([shutil.which('bash'), '-e', str(script)], cwd=tmp_path, capture_output=True, text=True)
    assert control.returncode == 0
    assert (tmp_path / 'false-success-marker').read_text() == 'false-success'
