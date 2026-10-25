"""Verification-only loaded-library audit, inherited by bundled Python children.

No third-party packages. Isolated bootstrap imports the in-bundle probe, then
byte-validates the external evidence-directory sitecustomize inherited by its
Python children. The native OCR adapter also records live Windows worker
snapshots; other non-Python executables retain their separate static audits.
"""
from __future__ import annotations

import atexit
import ctypes
import json
import ntpath
import os
from pathlib import Path
import platform
import posixpath
import re
import sys
import struct
import threading


def _normal(path: str, system: str) -> str:
    """Pure target-OS lexical normalization; never apply host Path semantics."""
    if system == 'Windows':
        if path[:8].lower() == '\\\\?\\unc\\':
            path = '\\\\' + path[8:]
        elif path.startswith('\\\\?\\'):
            path = path[4:]
        return ntpath.normcase(ntpath.normpath(path))
    return posixpath.normpath(path)


def _identity(path: str, system: str) -> str:
    """Resolve aliases only on the matching native OS, then compare lexically.

    Windows Path.resolve uses the native final-path API, handling drive case,
    junctions and existing 8.3 short names. POSIX resolve handles /var aliases.
    A target-OS path on another host must not acquire that host's cwd/drive.
    """
    native = {'win32': 'Windows', 'darwin': 'Darwin', 'linux': 'Linux'}.get(sys.platform)
    mod = ntpath if system == 'Windows' else posixpath
    if system == native and mod.isabs(path):
        path = str(Path(path).resolve())
    return _normal(path, system)


def _inside(path: str, root: str, system: str) -> bool:
    mod = ntpath if system == 'Windows' else posixpath
    path, root = _identity(path, system), _identity(root, system)
    try:
        return mod.commonpath([path, root]) == root
    except ValueError:
        return False


# Deliberately explicit: a third-party DLL in System32 is not an OS dependency.
WINDOWS_OS_DLLS = set("""
ntdll kernel32 kernelbase advapi32 sechost rpcrt4 user32 win32u gdi32 gdi32full
msimg32 msvcrt ucrtbase msvcp_win combase ole32 oleaut32 comdlg32 comctl32 shell32 shlwapi
shcore propsys imm32 version setupapi cfgmgr32 bcrypt bcryptprimitives crypt32
cryptbase cryptsp rsaenh ncrypt ntasn1 wintrust winhttp wininet ws2_32 mswsock
nsi dnsapi iphlpapi netapi32 netutils wkscli srvcli userenv profapi psapi
powrprof umpdc uxtheme dwmapi dwrite d2d1 d3d9 d3d11 d3d12 dxgi dxcore
d3dcompiler_47 opengl32 glu32 winmm winmmbase avrt msacm32 mf mfplat mfreadwrite
mfuuid mfcore evr ksuser audioses mmdevapi devenum quartz qdvd windowscodecs
msctf textinputframework coremessaging coreuicomponents inputhost wintypes
windows.storage windows.ui windows.ui.immersive windows.globalization
wtsapi32 secur32 sspicli authz clbcatq imagehlp dbghelp dbgcore dhcpcsvc dhcpcsvc6
winnsi rasapi32 rasman rtutils sensapi wldap32 winspool.drv cryptnet cryptui
cryptdll samlib apphelp edputil policymanager vaultcli bcp47langs oleacc
atlthunk cabinet urlmon iertutil winrnr pnrpnsp nlansp_c wshbth napinsp
hid winusb normaliz rstrtmgr slc sppc windows.staterepositoryclient
windows.staterepositorycore windows.staterepositoryps portcls wdmaud.drv
msacm32.drv midimap explorerframe dataexchange twinapi.appcore twinapi avicap32
mpr wsock32 mscms uiautomationcore icuuc icuin icu
rtworkq amsi devobj kernel.appcore msasn1 pdh gdiplus windows.graphics
""".split())
# avicap32 is the Windows Video for Windows capture API, not a vendor runtime:
# https://learn.microsoft.com/en-us/windows/win32/api/vfw/nf-vfw-capcreatecapturewindowa
# Actual Windows wheel imports: Qt6Core (PySide6 6.12.0) -> mpr/icuuc,
# qwindows -> mscms/uiautomationcore; cv2 (4.10.0.84) -> wsock32.
# https://learn.microsoft.com/en-us/windows/win32/api/winnetwk/nf-winnetwk-multinetgetconnectionperformancew
# https://learn.microsoft.com/en-us/windows/win32/api/icm/nf-icm-getcolorprofileheader
# https://learn.microsoft.com/en-us/windows/win32/api/uiautomationcoreapi/nf-uiautomationcoreapi-uiaraiseautomationevent
# https://learn.microsoft.com/en-us/troubleshoot/windows-client/application-management/add-remove-programs-displays-programs-incorrectly
# Windows 10 1703 supplies icuuc/icuin; 1903 adds combined icu, with the old
# names retained as forwarders. Only these exact unversioned OS names qualify;
# versioned/vendor ICU DLLs still must be inside the application bundle.
# https://learn.microsoft.com/en-us/windows/win32/intl/international-components-for-unicode--icu-
# https://github.com/microsoft/win32metadata/issues/1956
# Additional exact OS modules observed by Server 2022/2025 runtime audits:
# https://learn.microsoft.com/en-us/windows/win32/api/rtworkq/nf-rtworkq-rtwqshutdown
# https://learn.microsoft.com/en-us/windows/win32/api/amsi/nf-amsi-amsiinitialize
# https://learn.microsoft.com/en-us/windows/win32/api/pdh/nf-pdh-pdhsetdefaultrealtimedatasource
# https://learn.microsoft.com/en-us/windows/win32/api/gdiplusinit/nf-gdiplusinit-gdiplusstartup
# https://learn.microsoft.com/en-us/security-updates/securitybulletins/2004/ms04-007
# devobj, kernel.appcore and Windows.Graphics are named in Microsoft OS update
# file inventories (not vendor/third-party DLL databases):
# https://support.microsoft.com/en-us/topic/driver-installation-fails-even-when-a-compatible-driver-is-found-in-the-local-driver-store-in-windows-7-and-in-windows-server-2008-r2-8e317dec-645c-c68b-59bd-4c350eb72b19
# https://support.microsoft.com/en-us/servicing/os/windows/safeos-du/2026/02/kb5076122-safe-os-dynamic-update-for-windows-server-2025-february-10-2026
# https://support.microsoft.com/en-us/topic/windows-rt-8-1-windows-8-1-and-windows-server-2012-r2-update-rollup-january-2014-afd36561-dddb-8466-8fb4-a06bde36fb4e


LINUX_BASE = re.compile(r"^(?:ld-linux[^/]*\.so(?:\.\d+)*|lib(?:c|m|dl|pthread|rt|util|resolv|anl|BrokenLocale|nss_(?:files|dns|compat|hesiod))\.so(?:\.\d+)*)$")


# Apple OS Cryptex overlays can supply frameworks such as WebKit. Only the
# corresponding OS library subtrees are trusted, never the whole Preboot
# volume, Cryptexes directory, or even the OS mount (which has other content).
MACOS_OS_LIBRARY_ROOTS = (
    '/System/Library', '/usr/lib',
    '/System/Volumes/Preboot/Cryptexes/OS/System/Library',
    '/System/Volumes/Preboot/Cryptexes/OS/usr/lib',
)


def _macos_os_library(path: str) -> bool:
    # Keep the original spelling until _inside resolves symlinks on macOS:
    # normalizing away '..' first could hide a symlink escape. On other hosts,
    # _inside uses target-POSIX lexical containment, not host path semantics.
    return any(_inside(path, base, 'Darwin') for base in MACOS_OS_LIBRARY_ROOTS)


def library_violation(path: str, root: str, system: str, windows_root: str = r'C:\Windows') -> str | None:
    """Classify only ACTUALLY loaded images; optional unused plugins do not count."""
    if _inside(path, root, system):
        return None
    normalized = _normal(path, system)
    if system == 'Windows':
        name = ntpath.basename(normalized)
        # These are redistributable toolchain libraries, NOT baseline Windows.
        if re.match(r'(?:vcruntime|msvcr|msvcp|concrt|vcomp|mfc|mfcm)\d', name) or name.startswith(('python', 'libpython')):
            return f'Non-OS runtime loaded outside bundle: {path}'
        if any(_inside(path, ntpath.join(windows_root, part), system) for part in ('System32', 'SysWOW64', 'WinSxS')):
            stem = name[:-4] if name.endswith('.dll') else name
            if stem in WINDOWS_OS_DLLS or name.startswith(('api-ms-win-', 'ext-ms-win-')):
                return None
    elif system == 'Darwin':
        if _macos_os_library(path):
            return None
    elif system == 'Linux':
        if LINUX_BASE.fullmatch(posixpath.basename(normalized)) and any(_inside(normalized, base, system) for base in ('/lib', '/lib64', '/usr/lib', '/usr/lib64')):
            return None
    return f'External native library loaded: {path}'


def loaded_images() -> list[str]:
    if sys.platform == 'win32':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        process = kernel.GetCurrentProcess
        process.restype = wintypes.HANDLE
        enum = kernel.K32EnumProcessModules
        enum.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        enum.restype = wintypes.BOOL
        filename = kernel.K32GetModuleFileNameExW
        filename.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
        filename.restype = wintypes.DWORD
        capacity = 256
        while True:
            modules = (wintypes.HMODULE * capacity)()
            needed = wintypes.DWORD()
            if not enum(process(), modules, ctypes.sizeof(modules), ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            if needed.value <= ctypes.sizeof(modules):
                break
            capacity = needed.value // ctypes.sizeof(wintypes.HMODULE) + 32
        result = []
        for module in modules[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
            buffer = ctypes.create_unicode_buffer(32768)
            length = filename(process(), module, buffer, len(buffer))
            if not length or length >= len(buffer):
                raise RuntimeError('Cannot enumerate loaded DLL filename')
            result.append(buffer.value)
        return result
    if sys.platform == 'darwin':
        lib = ctypes.CDLL(None)
        count = lib._dyld_image_count
        count.restype = ctypes.c_uint32
        name = lib._dyld_get_image_name
        name.argtypes = [ctypes.c_uint32]
        name.restype = ctypes.c_char_p
        result = []
        for index in range(count()):
            value = name(index)
            if value:
                result.append(os.fsdecode(value))
        return result
    if sys.platform.startswith('linux'):
        return sorted({line.split(maxsplit=5)[5].strip() for line in Path('/proc/self/maps').read_text().splitlines()
                       if len(line.split(maxsplit=5)) == 6 and 'x' in line.split()[1] and line.split(maxsplit=5)[5].startswith('/')})
    raise RuntimeError(f'Unsupported runtime audit platform: {sys.platform}')


HOOK_STARTUP = b'from standalone_runtime_probe import install_probe\ninstall_probe()\n'
_NATIVE_WORKER_AUDIT = None


def record_native_worker(pid: int, executable: Path, phase: str) -> None:
    """Record actual Windows native children only during authenticated verification.

    Capture after the first successful inference and again before orderly close,
    not per frame. Every spawned worker leaves a pending record, so a missing
    inference/snapshot cannot silently masquerade as a Python-only audit pass.
    """
    context = _NATIVE_WORKER_AUDIT
    if context is None or context['system'] != 'Windows':
        return
    from scripts.windows_native_worker_evidence import collect_native_worker_evidence
    expected = context['root'] / 'native-ocr/native-ocr.exe'
    if (type(pid) is not int or pid <= 0 or phase not in ('start', 'inference', 'close')
            or _identity(str(executable), 'Windows') != _identity(str(expected), 'Windows')):
        raise RuntimeError('Invalid native worker audit PID, phase or bundled executable')
    with context['lock']:
        records = context['records']
        if phase == 'start':
            if pid in records:
                raise RuntimeError('Duplicate native worker PID in audit')
            records[pid] = {'schema': 1, 'parent_pid': os.getpid(), 'worker_pid': pid,
                            'parent_argv': list(sys.argv), 'executable': str(expected.resolve()),
                            'successful_requests': 0, 'snapshots': [], 'errors': []}
        if pid not in records:
            raise RuntimeError('Native worker inference was not preceded by an audited launch')
        record = records[pid]
        if phase == 'inference':
            record['successful_requests'] += 1
        if ((phase == 'inference' and record['successful_requests'] == 1)
                or (phase == 'close' and record['successful_requests'] > 0)):
            evidence = collect_native_worker_evidence(pid, expected)
            record['snapshots'].append({'phase': phase, 'native_runtime': evidence})
            if evidence.get('available') is not True or evidence.get('passed') is not True:
                record['errors'].extend(evidence.get('errors') or ['Missing native worker module evidence'])
        record['phase'] = phase
        record['passed'] = bool(record['successful_requests'] and record['snapshots'] and not record['errors'])
        destination = context['directory'] / f'native-worker-{os.getpid()}-{pid}.json'
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(destination)
        if record['errors']:
            raise RuntimeError('Windows native worker audit failed: ' + '; '.join(record['errors']))


def _summarize_native_workers(bundle_root, report_dir, processes, required):
    errors, records = [], []
    paths = sorted(report_dir.glob('native-worker-*.json'))
    if required and not paths:
        errors.append('No Windows native worker PID/module evidence produced after inference')
    if not paths:
        return records, errors
    # The source packager imports this module directly from scripts/, while
    # the isolated runtime imports scripts.standalone_runtime_probe. Preserve
    # both contexts without adding an application/host directory to sys.path.
    if __package__:
        from .windows_native_worker_evidence import validate_native_worker_evidence
    else:
        from windows_native_worker_evidence import validate_native_worker_evidence
    parents = {row['pid'] for row in processes if row['system'] == 'Windows'}
    expected = bundle_root.resolve() / 'native-ocr/native-ocr.exe'
    seen = set()
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            parent, pid = record.get('parent_pid'), record.get('worker_pid')
            if (record.get('schema') != 1 or type(parent) is not int or parent not in parents
                    or type(pid) is not int or pid <= 0 or pid == parent or (parent, pid) in seen
                    or path.name != f'native-worker-{parent}-{pid}.json'
                    or _identity(str(record.get('executable', '')), 'Windows') != _identity(str(expected), 'Windows')
                    or record.get('passed') is not True or record.get('errors') != []
                    or type(record.get('successful_requests')) is not int or record['successful_requests'] <= 0):
                raise ValueError('Missing/failed native inference, invalid worker/parent identity or executable')
            snapshots = record.get('snapshots', [])
            if (not isinstance(snapshots, list) or not 1 <= len(snapshots) <= 2
                    or snapshots[0].get('phase') != 'inference'
                    or (len(snapshots) == 2 and snapshots[1].get('phase') != 'close')):
                raise ValueError('Missing or invalid native worker inference snapshots')
            for snapshot in snapshots:
                evidence = snapshot.get('native_runtime', {})
                if evidence.get('pid') != pid:
                    raise ValueError('Native worker snapshot PID mismatch')
                validated = validate_native_worker_evidence(evidence, expected)
                if validated.get('passed') is not True:
                    raise ValueError('; '.join(validated.get('errors') or ['Invalid native worker evidence']))
            seen.add((parent, pid))
            records.append({'file': path.name, 'parent_pid': parent, 'worker_pid': pid,
                            'successful_requests': record['successful_requests'], 'snapshots': len(snapshots)})
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            errors.append(f'Invalid Windows native worker evidence {path.name}: {error}')
    return records, errors


def _trusted_hooks(bundle_root: Path, report_dir: Path) -> dict[str, tuple[Path, bytes]]:
    return {
        'standalone_runtime_probe': (report_dir / 'hook/standalone_runtime_probe.py',
                                     (bundle_root / 'scripts/standalone_runtime_probe.py').read_bytes()),
        'sitecustomize': (report_dir / 'hook/sitecustomize.py', HOOK_STARTUP),
    }


def audit_environment(bundle_root: Path, report_dir: Path) -> dict[str, str]:
    """Return overrides for a sanitized subprocess environment, not host PATH."""
    bundle_root, report_dir = bundle_root.resolve(), report_dir.resolve()
    trusted = _trusted_hooks(bundle_root, report_dir)
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if list(report_dir.glob('process-*.json')) or list(report_dir.glob('native-worker-*.json')):
        raise RuntimeError('Runtime audit requires a fresh report directory (stale process reports found)')
    hooks = report_dir / 'hook'
    hooks.mkdir(mode=0o700)  # Never reuse potentially stale/symlinked hook files.
    for path, contents in trusted.values():
        path.write_bytes(contents)
    return {'PYTHONPATH': str(hooks), 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
            'VSE_RUNTIME_AUDIT_ROOT': str(bundle_root), 'VSE_RUNTIME_AUDIT_REPORTS': str(report_dir)}


def install_probe() -> None:
    """Snapshot repeatedly: multiprocessing workers may exit without atexit."""
    root = os.environ['VSE_RUNTIME_AUDIT_ROOT']
    directory = Path(os.environ['VSE_RUNTIME_AUDIT_REPORTS'])
    system = platform.system()
    global _NATIVE_WORKER_AUDIT
    _NATIVE_WORKER_AUDIT = {'root': Path(root).resolve(), 'directory': directory.resolve(),
                            'system': system, 'lock': threading.Lock(), 'records': {}}
    lock = threading.Lock()
    images: set[str] = set()
    modules: dict[str, str] = {}
    raw_modules: dict[str, str] = {}
    errors: set[str] = set()
    snapshots = 0

    def snapshot(force: bool = False) -> None:
        nonlocal snapshots
        with lock:
            previous = (len(images), len(modules), len(errors))
            module_changed = False
            try:
                images.update(str(Path(path).resolve()) for path in loaded_images())
                for name, module in list(sys.modules.items()):
                    filename = getattr(module, '__file__', None)
                    if filename and not filename.startswith('<') and raw_modules.get(name) != filename:
                        modules[name] = str(Path(filename).resolve())
                        raw_modules[name] = filename
                        module_changed = True
                snapshots += 1
            except Exception as exc:
                errors.add(f'{type(exc).__name__}: {exc}')
            if not force and not module_changed and snapshots > 1 and previous == (len(images), len(modules), len(errors)):
                return
            report = {'pid': os.getpid(), 'system': system, 'executable': str(Path(sys.executable).resolve()),
                      'prefix': str(Path(sys.prefix).resolve()), 'bundle_root': root,
                      'windows_root': os.environ.get('SystemRoot', r'C:\Windows'),
                      'argv': sys.argv, 'images': sorted(images), 'modules': modules,
                      'isolated': sys.flags.isolated, 'ignore_environment': sys.flags.ignore_environment,
                      'sys_path': sys.path, 'no_user_site': sys.flags.no_user_site, 'utf8_mode': sys.flags.utf8_mode,
                      'user_site_enabled': __import__('site').ENABLE_USER_SITE,
                      'environment': {key: os.environ[key] for key in (
                          'PATH', 'PYTHONHOME', 'PYTHONPATH', 'PYTHONUSERBASE', 'PYTHONUTF8', 'PYTHONIOENCODING', 'CONDA_PREFIX',
                          'VIRTUAL_ENV', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH',
                          'QML2_IMPORT_PATH', 'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH',
                          'OPENCV_VIDEOIO_PLUGIN_PATH', 'OPENCV_CORE_PLUGIN_PATH', 'OPENCV_DNN_PLUGIN_PATH',
                          'OPENCV_UI_PLUGIN_PATH', 'OPENCV_OPENCL_RUNTIME', 'OPENCV_FFMPEG_DLL_DIR', 'OPENCV_DNN_IE_EXTRA_PLUGIN_PATH',
                          'OPENCV_VIDEOIO_PLUGIN_FFMPEG', 'OPENCV_CORE_PARALLEL_PLUGIN_TBB', 'VSE_VSF_BINARY', 'VSE_VSF_DIR', 'IMAGEIO_FFMPEG_EXE',
                          'VSE_CONFIG_FILE', 'VSE_WORK_DIR', 'VSE_OCR_BACKEND', 'VSE_NATIVE_OCR_ROOT', 'VSE_DIRECTML_MODEL_ROOT',
                          'CUDA_PATH', 'CUDA_HOME', 'CUDNN_PATH', 'CUDNN_HOME',
                          *(name for name in os.environ if name.lower().startswith('flags_') and name.lower().endswith('_dir'))) if key in os.environ},
                      'snapshots': snapshots, 'probe_errors': sorted(errors)}
            runtime = sys.modules.get('scripts.windows_app_local_runtime')
            if runtime is not None:
                report['windows_app_local_runtime'] = dict(runtime.PRELOAD_EVIDENCE)
            destination = directory / f'process-{os.getpid()}.json'
            temporary = destination.with_suffix('.tmp')
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(destination)

    stopped = threading.Event()
    def poll() -> None:
        while not stopped.wait(0.2):
            snapshot()
    def finish() -> None:
        stopped.set()
        snapshot(force=True)
    snapshot()
    threading.Thread(target=poll, name='standalone-runtime-audit', daemon=True).start()
    atexit.register(finish)


def summarize_reports(bundle_root: Path, report_dir: Path, required_modules: tuple[str, ...] = (),
                      forbidden_modules: tuple[str, ...] = (), *, require_isolated: bool = False,
                      require_native_workers: bool = False) -> dict:
    """Write evidence and fail closed on missing reports or probe failures."""
    root = str(bundle_root.resolve())
    errors: list[str] = []
    reports = [json.loads(file.read_text(encoding='utf-8')) for file in sorted(report_dir.glob('process-*.json'))]
    seen_modules: set[str] = set()
    component_proofs = {}
    observed_components = []
    # Defender injection is not an application dependency. Likewise, two
    # undocumented exact OS names require actual signed-OS evidence, not a
    # filename-only exemption. Collect once after workloads (never per poll),
    # retain raw proof, and keep the static PE allowlist unchanged.
    try:
        from .windows_os_evidence import CANDIDATE_NAMES, collect_windows_os_evidence, classify_loaded_windows_component
    except ImportError:
        from windows_os_evidence import CANDIDATE_NAMES, collect_windows_os_evidence, classify_loaded_windows_component
    candidates = {}
    for report in reports:
        if report['system'] != 'Windows':
            continue
        for path in report['images']:
            key = _identity(path, 'Windows')
            if (ntpath.basename(key) in CANDIDATE_NAMES
                    and library_violation(path, root, 'Windows', report['windows_root'])):
                candidates.setdefault(key, path)
    if candidates:
        try:
            proof = collect_windows_os_evidence(list(candidates.values()))
        except (OSError, ValueError, RuntimeError) as error:
            proof = {'schema': 1, 'errors': [f'{type(error).__name__}: {error}'], 'files': []}
        (report_dir / 'windows-os-security-evidence.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        component_proofs = {key: classify_loaded_windows_component(path, proof) for key, path in candidates.items()}
    app_local_expected = None
    if require_isolated and (bundle_root / 'runtime/windows-app-local-runtime.json').is_file():
        try:
            from .windows_app_local_runtime import checked_runtime_metadata
        except ImportError:
            from windows_app_local_runtime import checked_runtime_metadata
        candidate, metadata = checked_runtime_metadata(bundle_root)
        app_local_expected = (str(candidate), metadata['sha256'])
    trusted_hooks = _trusted_hooks(bundle_root.resolve(), report_dir.resolve())
    valid_hooks = {}
    for name, (path, expected) in trusted_hooks.items():
        if path.is_file() and not path.is_symlink() and path.read_bytes() == expected:
            valid_hooks[name] = str(path.resolve())
        else:
            errors.append(f'Missing or modified verification hook: {path}')
    for report in reports:
        system = report['system']
        if system == 'Windows' and app_local_expected is not None:
            expected_path, expected_hash = app_local_expected
            preload = report.get('windows_app_local_runtime', {})
            paths = preload.get('loaded_paths', [])
            if (preload.get('sha256') != expected_hash or preload.get('retained_handle') is not True
                    or _identity(str(preload.get('path', '')), system) != _identity(expected_path, system)
                    or not paths or any(_identity(path, system) != _identity(expected_path, system) for path in paths)
                    or not any(_identity(path, system) == _identity(expected_path, system) for path in report['images'])):
                errors.append(f"PID {report['pid']} missing authenticated app-local VCOMP startup evidence")
        if require_isolated:
            if report.get('isolated') != 1 or report.get('ignore_environment') != 1 or report.get('no_user_site') != 1:
                errors.append(f"PID {report['pid']} did not start with isolated/user-site-safe path configuration")
            for path in report.get('sys_path', []):
                if not path or not _inside(path, root, system):
                    errors.append(f"PID {report['pid']} external Python search path: {path}")
            if not report.get('sys_path'):
                errors.append(f"PID {report['pid']} missing isolated Python search-path evidence")
        for field in ('executable', 'prefix'):
            if not _inside(report[field], root, system):
                errors.append(f"PID {report['pid']} external {field}: {report[field]}")
        errors.extend(report['probe_errors'])
        if not report['images'] or not report['snapshots']:
            errors.append(f"PID {report['pid']} has no loaded-library evidence")
        for path in report['images']:
            violation = library_violation(path, root, system, report['windows_root'])
            if violation:
                component = component_proofs.get(_identity(path, system), {}) if system == 'Windows' else {}
                if component.get('allowed') is True:
                    observed_components.append(dict(component, pid=report['pid'], argv=report.get('argv', [])))
                else:
                    errors.append(violation)
                    if component:
                        errors.append(f"Unverified OS/security component {path}: {component.get('reason', 'missing evidence')}")
        for name, path in report['modules'].items():
            seen_modules.add(name)
            trusted_hook = name in valid_hooks and _identity(path, system) == _identity(valid_hooks[name], system)
            if not _inside(path, root, system) and not trusted_hook:
                errors.append(f'External Python module {name}: {path}')
    if not reports:
        errors.append('No Python process audit reports produced; inherited hook did not execute')
    for module in required_modules:
        if not any(name == module or name.startswith(module + '.') for name in seen_modules):
            errors.append(f'Required workload was not observed: {module}')
    for module in forbidden_modules:
        if any(name == module or name.startswith(module + '.') for name in seen_modules):
            errors.append(f'Forbidden inference framework loaded: {module}')
    native_workers, native_errors = _summarize_native_workers(bundle_root, report_dir, reports, require_native_workers)
    errors.extend(native_errors)
    summary = {'process_count': len(reports), 'modules_observed': sorted(seen_modules),
               'native_worker_count': len(native_workers), 'native_workers': native_workers,
               'errors': sorted(set(errors)), 'passed': not errors,
               'observed_os_security_components': observed_components,
               'unverified_os_security_components': [entry for entry in component_proofs.values() if not entry.get('allowed')],
               'os_security_evidence_file': 'windows-os-security-evidence.json' if candidates else None,
               'scope': ('Loaded Python process libraries plus Windows native OCR worker first-inference/pre-close snapshots and on-disk hashes; not process-memory or lifetime certification'
                         if native_workers else 'Loaded Python process libraries; non-Python executables require separate native audit')}
    (report_dir / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    if errors:
        raise RuntimeError('Standalone runtime audit failed:\n' + '\n'.join(summary['errors']))
    return summary


def pe_imports(path: Path) -> list[str]:
    """Read mandatory PE imports, not optional delay-load/plugin dependencies."""
    data = path.read_bytes()
    def number(offset: int, fmt: str = '<I') -> int:
        if offset < 0 or offset + struct.calcsize(fmt) > len(data):
            raise ValueError(f'Truncated PE binary: {path}')
        return struct.unpack_from(fmt, data, offset)[0]
    pe = number(0x3c)
    if data[:2] != b'MZ' or data[pe:pe + 4] != b'PE\0\0':
        raise ValueError(f'Not a PE executable: {path}')
    sections, optional_size = number(pe + 6, '<H'), number(pe + 20, '<H')
    optional = pe + 24
    magic = number(optional, '<H')
    if magic not in (0x10b, 0x20b):
        raise ValueError(f'Unsupported PE optional header: {path}')
    directory = optional + (96 if magic == 0x10b else 112)
    if directory + 16 > optional + optional_size:
        raise ValueError(f'Missing PE import directory: {path}')
    import_rva = number(directory + 8)
    if not import_rva:
        return []
    section_table = optional + optional_size
    def offset(rva: int) -> int:
        for index in range(sections):
            section = section_table + index * 40
            virtual_size, address = number(section + 8), number(section + 12)
            raw_size, raw = number(section + 16), number(section + 20)
            if address <= rva < address + max(virtual_size, raw_size):
                delta = rva - address
                if delta >= raw_size or raw + delta >= len(data):
                    break
                return raw + delta
        raise ValueError(f'Invalid PE import RVA in {path}: {rva}')
    descriptor = offset(import_rva)
    result = []
    for index in range(65536):
        entry = descriptor + index * 20
        fields = [number(entry + part * 4) for part in range(5)]
        if not any(fields):
            return result
        start = offset(fields[3])
        end = data.find(b'\0', start, start + 4096)
        if end < 0:
            raise ValueError(f'Unterminated PE import name: {path}')
        name = data[start:end].decode('ascii')
        if not name or ntpath.basename(name) != name or ':' in name:
            raise ValueError(f'Unsafe PE import name in {path}: {name}')
        result.append(name)
    raise ValueError(f'Unbounded PE import table: {path}')


def audit_windows_executables(bundle_root: Path, runtime_python: Path, report_dir: Path,
                              executables: list[Path] | None = None) -> dict:
    """Audit mandatory VSF/ffmpeg imports in reachable package DLL locations.

    Search only the EXE directory, package working directory and restricted PATH
    interpreter directory. Do not accept DLLs merely present elsewhere in bundle.
    Native Python loaded-module audit complements this static subprocess gate.
    """
    root = bundle_root.resolve()
    if executables is None:
        vsf = sorted((root / 'backend/subfinder/windows').rglob('VideoSubFinder*.exe'))
        ffmpeg = sorted((root / 'runtime').glob('**/imageio_ffmpeg/binaries/*.exe'))
        if not vsf or not ffmpeg:
            raise RuntimeError('Cannot audit Windows native executables: bundled VSF/ffmpeg missing')
        executables = vsf + ffmpeg + list((root / "native-ocr").glob("native-ocr.exe"))
    errors, graphs = [], {}
    for executable in executables:
        executable = executable.resolve()
        directories = [executable.parent, root, runtime_python.resolve().parent]
        if any(not directory.is_relative_to(root) for directory in directories):
            raise RuntimeError('Windows native audit search path escapes bundle')
        candidates = [{p.name.lower(): p for p in directory.iterdir() if p.is_file()} for directory in directories]
        pending, visited, graph = [executable], set(), {}
        while pending:
            binary = pending.pop()
            if binary in visited:
                continue
            visited.add(binary)
            dependencies = {}
            for name in pe_imports(binary):
                # Baseline DLLs are explicitly named, never every DLL in System32.
                if library_violation('C:\\Windows\\System32\\' + name, str(root), 'Windows') is None:
                    dependencies[name] = 'Windows base OS'
                    continue
                located = next((items[name.lower()] for items in candidates if name.lower() in items), None)
                if located is None or not located.resolve().is_relative_to(root):
                    errors.append(f'{binary.relative_to(root)} requires unbundled/unreachable DLL: {name}')
                    continue
                dependencies[name] = located.relative_to(root).as_posix()
                pending.append(located)
            graph[binary.relative_to(root).as_posix()] = dependencies
        graphs[executable.relative_to(root).as_posix()] = graph
    report = {'passed': not errors, 'errors': sorted(set(errors)), 'executables': graphs,
              'scope': 'Mandatory PE imports recursively; optional delay-load/plugins covered only when used by runtime tests'}
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / 'windows-native-imports.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    if errors:
        raise RuntimeError('Windows native dependency audit failed:\n' + '\n'.join(report['errors']))
    return report


def macho_load_commands(path: Path) -> tuple[list[str], list[str]]:
    """Build-host otool only; no Xcode or command-line tools needed by customers."""
    import subprocess
    output = subprocess.check_output(['/usr/bin/otool', '-arch', platform.machine(), '-l', str(path)], text=True)
    command, dependencies, rpaths = '', [], []
    for line in output.splitlines():
        fields = line.strip().split()
        if len(fields) == 2 and fields[0] == 'cmd':
            command = fields[1]
        elif line.strip().startswith('name ') and command in ('LC_LOAD_DYLIB', 'LC_REEXPORT_DYLIB', 'LC_LOAD_UPWARD_DYLIB'):
            dependencies.append(line.strip()[5:].rsplit(' (offset ', 1)[0])
        elif line.strip().startswith('path ') and command == 'LC_RPATH':
            rpaths.append(line.strip()[5:].rsplit(' (offset ', 1)[0])
    return dependencies, rpaths


def audit_macos_executables(bundle_root: Path, report_dir: Path, executables: list[Path] | None = None) -> dict:
    """Recursively audit required VSF/ffmpeg Mach-O loads using build-host otool.

    Weak loads are optional. Resolve runpaths in their loader ancestry, never
    permit Homebrew, build-machine paths, or a dependency elsewhere in the .app
    unless its actual loader path reaches that file.
    """
    root = bundle_root.resolve()
    if executables is None:
        vsf = sorted((root / 'backend/subfinder/macos').rglob('VideoSubFinder*'))
        ffmpeg = sorted((root / 'runtime').glob('**/imageio_ffmpeg/binaries/ffmpeg-macos*'))
        executables = [path for path in vsf + ffmpeg + list((root / "native-ocr").glob("native-ocr")) if path.is_file()]
        if not vsf or not ffmpeg:
            raise RuntimeError('Cannot audit macOS native executables: bundled VSF/ffmpeg missing')
    errors, graphs = [], {}
    for executable in executables:
        executable = executable.resolve()
        if not executable.is_relative_to(root):
            raise RuntimeError('macOS native executable escapes bundle')
        pending, visited, graph = [(executable, ())], set(), {}
        while pending:
            binary, inherited_rpaths = pending.pop()
            context = (binary, inherited_rpaths)
            if context in visited:
                continue
            visited.add(context)
            dependencies, declared_rpaths = macho_load_commands(binary)
            def expand(value: str) -> Path:
                if value == '@loader_path' or value.startswith('@loader_path/'):
                    return binary.parent / value.removeprefix('@loader_path').lstrip('/')
                if value == '@executable_path' or value.startswith('@executable_path/'):
                    return executable.parent / value.removeprefix('@executable_path').lstrip('/')
                return Path(value)
            # Keep ancestors' already-expanded paths; resolving them relative to
            # each child would incorrectly accept misplaced libraries.
            rpaths = tuple(dict.fromkeys([str(expand(path)) for path in declared_rpaths] + list(inherited_rpaths)))
            mapped = {}
            for dependency in dependencies:
                # Mach-O load names use POSIX syntax even in cross-OS fixture
                # inspection. Shared-cache OS dylibs may have no disk file.
                if posixpath.isabs(dependency) and _macos_os_library(dependency):
                    mapped[dependency] = 'macOS base OS'
                    continue
                if posixpath.isabs(dependency) and os.name == 'nt':
                    errors.append(f'{binary.relative_to(root)} requires external/missing dependency: {dependency}')
                    continue
                if dependency.startswith('@rpath/'):
                    suffix = dependency.removeprefix('@rpath/')
                    candidates = [Path(path) / suffix for path in rpaths]
                    located = next((path for path in candidates if path.is_file()), None)
                else:
                    located = expand(dependency)
                if located is None or not located.is_absolute():
                    errors.append(f'{binary.relative_to(root)} has unresolved native dependency: {dependency}')
                    continue
                resolved = located.resolve()
                if _macos_os_library(str(resolved)):
                    mapped[dependency] = 'macOS base OS'
                elif resolved.is_relative_to(root) and resolved.is_file():
                    mapped[dependency] = resolved.relative_to(root).as_posix()
                    pending.append((resolved, rpaths))
                else:
                    errors.append(f'{binary.relative_to(root)} requires external/missing dependency: {dependency} -> {resolved}')
            graph[binary.relative_to(root).as_posix()] = mapped
        graphs[executable.relative_to(root).as_posix()] = graph
    report = {'passed': not errors, 'errors': sorted(set(errors)), 'executables': graphs,
              'scope': 'Required Mach-O loads recursively; weak loads/plugins covered only when used by runtime tests'}
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / 'macos-native-imports.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    if errors:
        raise RuntimeError('macOS native dependency audit failed:\n' + '\n'.join(report['errors']))
    return report
