"""Build a standalone Windows installer with the local Python runtime and Inno Setup.

    python build_installer.py
    python build_installer.py --version 1.0.1
    python build_installer.py --check

No PyInstaller, credentials, existing projects, or user media are bundled.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent
from app_info import APP_NAME, VERSION
APP_ID = '{{DBF27CDD-9313-4C68-B02E-171E432388C4}'
SOURCES = (
    'app_info.py', 'shorts_studio.py', 'shorts_core.py', 'studio_store.py', 'datetime_picker.py',
    'model_picker.py', 'media_versions.py', 'router_retry.py', 'scene_generation.py',
    'youtube_actions.py', 'clip_costs.py', 'upload_auto.py',
)
BOOTSTRAP = '''"""Installed application entry point. Isolated from the system Python."""
import os
from pathlib import Path
import runpy
import sys
app = Path(__file__).resolve().parent
sys.path.insert(0, str(app))
os.environ['SHORTS_STUDIO_INSTALLED'] = '1'
os.environ['PATH'] = str(app.parent / 'bin') + os.pathsep + os.environ.get('PATH', '')
runpy.run_path(str(app / 'shorts_studio.py'), run_name='__main__')
'''
PROBE = '''import json, sys, sysconfig, struct
import tkinter, googleapiclient.discovery, google_auth_oauthlib.flow, tzdata
print(json.dumps({'base': sys.base_prefix, 'site': sysconfig.get_path('purelib'),
 'major': sys.version_info.major, 'minor': sys.version_info.minor,
 'bits': struct.calcsize('P') * 8}))
'''


def run(command, **kwargs):
    print('>', subprocess.list2cmdline(list(map(str, command))), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def find_iscc(override=None):
    candidates = [Path(override)] if override else []
    if not override:
        if shutil.which('ISCC.exe'):
            candidates.append(Path(shutil.which('ISCC.exe')))
        for variable in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'):
            base = os.environ.get(variable)
            if base:
                directory = Path(base)
                candidates.extend(sorted(directory.glob('Inno Setup */ISCC.exe'), reverse=True))
                candidates.extend(sorted((directory / 'Programs').glob('Inno Setup */ISCC.exe'), reverse=True))
        if os.name == 'nt':
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                    try:
                        key = winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1', 0, winreg.KEY_READ | view)
                        with key:
                            directory = winreg.QueryValueEx(key, 'InstallLocation')[0]
                        candidates.append(Path(directory) / 'ISCC.exe')
                    except OSError:
                        pass
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise RuntimeError('Inno Setup compiler not found. Pass --iscc "C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe"')


def find_python(override=None):
    candidates = [Path(override)] if override else [ROOT / '.venv/Scripts/python.exe', ROOT / 'venv/Scripts/python.exe', Path(sys.executable)]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise RuntimeError('Python not found. Pass --python PATH_TO_PYTHON_EXE')


def find_media_tool(name, directory=None):
    path = Path(directory) / (name + '.exe') if directory else Path(shutil.which(name + '.exe') or '')
    if not path.is_file():
        raise RuntimeError(f'{name}.exe not found. Install FFmpeg or pass --ffmpeg-dir PATH')
    # Resolve WinGet symlinks so the payload contains the actual binary.
    return path.resolve()


def probe_python(python):
    result = subprocess.run([str(python), '-I', '-c', PROBE], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Build Python lacks project dependencies. Use --install-deps or select the project venv with --python.\n' + result.stderr[-2000:])
    info = json.loads(result.stdout)
    if info['major'] != 3 or info['minor'] < 11 or info['bits'] != 64:
        raise RuntimeError('This installer requires a 64-bit Python 3.11+ build environment.')
    return info


def ignored(directory, names):
    omit = {'__pycache__', 'test', 'tests', 'ensurepip', 'idlelib', 'turtledemo', 'site-packages'}
    return [name for name in names if name in omit or name.endswith(('.pyc', '.pyo'))]


def copy_runtime(info, destination):
    base = Path(info['base'])
    destination.mkdir(parents=True)
    for pattern in ('python*.exe', 'python*.dll', 'vcruntime*.dll', 'LICENSE*'):
        for path in base.glob(pattern):
            shutil.copy2(path, destination / path.name)
    for name in ('DLLs', 'Lib', 'tcl', 'abi3t-compat'):
        if (base / name).is_dir():
            shutil.copytree(base / name, destination / name, ignore=ignored)
    packages = destination / 'Lib/site-packages'
    packages.mkdir(parents=True, exist_ok=True)
    for path in Path(info['site']).iterdir():
        if path.name == '__pycache__' or path.name == 'pip' or path.name.startswith('pip-') or path.suffix in ('.pyc', '.pyo'):
            continue
        if path.is_dir():
            shutil.copytree(path, packages / path.name, ignore=ignored, dirs_exist_ok=True)
        else:
            shutil.copy2(path, packages / path.name)
    # Fully isolated search paths: no dependency on Python registry entries or PYTHONPATH.
    (destination / f'python{info["major"]}{info["minor"]}._pth').write_text(
        '.\nDLLs\nLib\nLib\\site-packages\nimport site\n', encoding='utf-8')
    if not (destination / 'pythonw.exe').is_file():
        raise RuntimeError('The selected Python installation does not provide pythonw.exe')


def iss_string(value):
    value = str(value)
    if any(c in value for c in ('\n', '\r', '"')):
        raise ValueError('Installer paths cannot contain double quotes or newlines')
    return value.replace('{', '{{')


def installer_script(stage, output, version):
    return rf'''; Generated by build_installer.py. Credentials and user projects are excluded.
[Setup]
AppId={APP_ID}
AppName={APP_NAME}
AppVersion={version}
AppPublisher=Uncle Engineer
DefaultDirName={{localappdata}}\Programs\UncleShortsGenerator
DefaultGroupName=Uncle Engineer\Uncle Shorts Generator
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir={iss_string(output)}
OutputBaseFilename=UncleShortsGenerator-Setup-{version}-x64
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Uncle Shorts Generator {version}
UninstallDisplayIcon={{app}}\app\assets\uncle.ico
SetupIconFile={iss_string(stage / "app" / "assets" / "uncle.ico")}
LicenseFile={iss_string(stage / "app" / "LICENSE")}
AppPublisherURL=https://uncle-engineer.com/
AppSupportURL=https://github.com/UncleLabsBlacksmith/UncleShortsGenerator/issues
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "{{cm:CreateDesktopIcon}}"; GroupDescription: "{{cm:AdditionalIcons}}"; Flags: unchecked

[Files]
Source: "{iss_string(stage)}\*"; DestDir: "{{app}}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{{group}}\Uncle Shorts Generator"; Filename: "{{app}}\runtime\pythonw.exe"; Parameters: "-I ""{{app}}\app\launch_studio.py"""; WorkingDir: "{{app}}\app"; IconFilename: "{{app}}\app\assets\uncle.ico"
Name: "{{autodesktop}}\Uncle Shorts Generator"; Filename: "{{app}}\runtime\pythonw.exe"; Parameters: "-I ""{{app}}\app\launch_studio.py"""; WorkingDir: "{{app}}\app"; IconFilename: "{{app}}\app\assets\uncle.ico"; Tasks: desktopicon

[Run]
Filename: "{{app}}\runtime\pythonw.exe"; Parameters: "-I ""{{app}}\app\launch_studio.py"""; Description: "Launch Uncle Shorts Generator"; WorkingDir: "{{app}}\app"; Flags: nowait postinstall skipifsilent
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--version', default=VERSION, help='Installer version, e.g. 1.0.1')
    parser.add_argument('--iscc', help='Path to installed ISCC.exe')
    parser.add_argument('--python', help='Build interpreter with project dependencies (defaults to the project venv)')
    parser.add_argument('--ffmpeg-dir', help='Directory containing ffmpeg.exe and ffprobe.exe')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist/installer')
    parser.add_argument('--check', action='store_true', help='Only check local tools and dependencies')
    parser.add_argument('--prepare-only', action='store_true', help='Stage and smoke-test the app and write .iss; do not compile')
    parser.add_argument('--install-deps', action='store_true', help='Create an isolated build venv and install requirements (internet needed)')
    args = parser.parse_args(argv)
    if os.name != 'nt':
        parser.error('Build this Windows installer on Windows.')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:\.\d+)?', args.version) or any(int(n) > 65535 for n in args.version.split('.')):
        parser.error('--version must have 3 or 4 numeric components, each <= 65535')
    build = ROOT / '.build/installer'
    compiler, python = find_iscc(args.iscc), find_python(args.python)
    media = [find_media_tool(name, args.ffmpeg_dir) for name in ('ffmpeg', 'ffprobe')]
    if args.install_deps and not args.check:
        environment = build / 'venv'
        if not (environment / 'Scripts/python.exe').is_file():
            run([python, '-m', 'venv', environment])
        python = environment / 'Scripts/python.exe'
        run([python, '-m', 'pip', 'install', '-r', ROOT / 'requirements.txt'])
    info = probe_python(python)
    print(json.dumps({'inno_setup': str(compiler), 'python': str(python), 'ffmpeg': list(map(str, media)), 'runtime': info}, indent=2), flush=True)
    if args.check:
        return 0
    missing = [name for name in SOURCES if not (ROOT / name).is_file()]
    if missing:
        raise RuntimeError('Missing application files: ' + ', '.join(missing))
    # Use a fresh allowlisted staging directory; never copy the repository wholesale.
    stage = build / ('payload-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    app, runtime = stage / 'app', stage / 'runtime'
    app.mkdir(parents=True)
    for name in SOURCES:
        shutil.copy2(ROOT / name, app / name)
    (app / 'launch_studio.py').write_text(BOOTSTRAP, encoding='utf-8')
    for name in ('README.md', 'USER_GUIDE.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copy2(ROOT / name, app / name)
    shutil.copytree(ROOT / 'assets', app / 'assets')
    print('Copying local Python runtime and dependencies...', flush=True)
    copy_runtime(info, runtime)
    binaries = stage / 'bin'
    binaries.mkdir()
    for path in media:
        shutil.copy2(path, binaries / path.name)
    licenses = stage / 'licenses'
    licenses.mkdir()
    result = subprocess.run([str(media[0]), '-L'], capture_output=True, text=True, check=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    (licenses / 'FFmpeg.txt').write_text(result.stdout + '\n' + result.stderr + '\nhttps://ffmpeg.org/legal.html\nhttps://www.gyan.dev/ffmpeg/builds/\n', encoding='utf-8')
    (stage / 'BUNDLE_INFO.json').write_text(json.dumps({'application': APP_NAME, 'version': args.version,
        'python': f'{info["major"]}.{info["minor"]}', 'user_data': '%LOCALAPPDATA%/UncleEngineer/UncleShortsGenerator (legacy ShortsStudio reused)' ,
        'bundled_app_sources': SOURCES}, indent=2), encoding='utf-8')
    report = stage.parent / (stage.name + '-self-test.json')
    environment = dict(os.environ, SHORTS_STUDIO_DATA_DIR=str(build / 'smoke-data'))
    print('Testing the bundled runtime without system Python or network access...', flush=True)
    run([runtime / 'python.exe', '-I', app / 'launch_studio.py', 'self-test', '--output', report],
        env=environment, cwd=stage, timeout=120)
    checks = json.loads(report.read_text(encoding='utf-8'))
    if not checks.get('ok') or not checks.get('installed'):
        raise RuntimeError('Bundled runtime self-test failed; see ' + str(report))
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    script = build / 'UncleShortsGenerator.iss'
    script.write_text(installer_script(stage, output, args.version), encoding='utf-8-sig')
    print('Inno Setup script:', script, flush=True)
    if args.prepare_only:
        return 0
    run([compiler, '/Qp', script], cwd=ROOT)
    installer = output / f'UncleShortsGenerator-Setup-{args.version}-x64.exe'
    if not installer.is_file():
        raise RuntimeError('Compiler exited without producing the expected installer')
    print('\nInstaller ready:', installer, flush=True)
    print('User data is stored separately and is not included in the installer.', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print('Build failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
