"""Subprocess-only test guard, enabled explicitly by native integration tests."""
import os
if os.environ.get('VSE_NATIVE_TEST_IMPORT_GUARD') == '1':
    import atexit
    import builtins
    import json
    import sys
    from pathlib import Path
    _forbidden = {'paddle', 'paddlex', 'paddleocr', 'onnxruntime'}
    _directory = Path(os.environ['VSE_NATIVE_TEST_IMPORT_LOG'])
    _directory.mkdir(parents=True, exist_ok=True)
    _path = _directory / f'{os.getpid()}.jsonl'
    def _record(event, **kwargs):
        with _path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'event': event, 'pid': os.getpid(), **kwargs}) + '\n')
    _original_import = builtins.__import__
    def _guarded_import(name, *args, **kwargs):
        if name.split('.')[0] in _forbidden:
            _record('forbidden_import', module=name)
            raise ImportError(f'Native profile must not import {name}')
        return _original_import(name, *args, **kwargs)
    class _GuardFinder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split('.')[0] in _forbidden:
                _record('forbidden_import', module=fullname)
                raise ImportError(f'Native profile must not import {fullname}')
            return None
    sys.meta_path.insert(0, _GuardFinder())
    builtins.__import__ = _guarded_import
    _record('guard_active', argv=sys.argv)
    def _verify_exit():
        modules = sorted(name for name in sys.modules if name.split('.')[0] in _forbidden)
        _record('exit_checked', forbidden_loaded=modules)
    atexit.register(_verify_exit)
