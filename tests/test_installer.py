"""Exercise installer orchestration without downloads or account/system mutations."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix" or not shutil.which("bash"), reason="Bash installer")


@pytest.fixture
def installation(tmp_path):
    repo = tmp_path / 'repo space'
    repo.mkdir()
    script = repo / 'install.sh'
    shutil.copyfile(Path(__file__).parents[1] / 'install.sh', script)
    home = tmp_path / 'home'
    home.mkdir()
    tools = tmp_path / 'tools'
    tools.mkdir()
    log = tmp_path / 'operations.jsonl'
    # Fixture interpreters implement venv/bootstrap transitions, never execute pip.
    stub = f'''#!{sys.executable}
import json, os, pathlib, shutil, sys
args=sys.argv[1:]
name=pathlib.Path(sys.argv[0]).name
with open(os.environ['INSTALL_LOG'],'a') as out: out.write(json.dumps([name,args])+'\\n')
if name=='aios-client': sys.exit(0)
if name=='uv':
    if args[:2]==['python','find']: print(pathlib.Path(os.environ['INSTALL_TOOLS'])/'modern-python')
    sys.exit(0)
if args[:1]==['-c']:
    sys.exit(0 if name=='modern-python' or pathlib.Path(sys.argv[0]).parent.parent.name=='.venv' else 1)
if args[:2]==['-m','venv']:
    target=pathlib.Path(args[2])/'bin'
    target.mkdir(parents=True,exist_ok=True)
    for executable in ['python','uv','aios-client']:
        shutil.copyfile(sys.argv[0],target/executable)
        (target/executable).chmod(0o755)
'''
    for name in ('python3', 'python3.11', 'python3.12', 'modern-python'):
        file = tools / name
        file.write_text(stub)
        file.chmod(0o755)
    identity = tools / 'id'
    identity.write_text('#!/bin/sh\nif [ "$1" = "-u" ]; then echo 1001; else echo aiosagent; fi\n')
    identity.chmod(0o755)
    env = {**os.environ, 'HOME': str(home), 'PATH': str(tools)+os.pathsep+os.environ['PATH'],
           'INSTALL_LOG': str(log), 'INSTALL_TOOLS': str(tools)}
    return repo, home, tools, log, env


def test_installer_bootstraps_old_python_and_reruns_without_deleting(installation):
    repo, home, tools, log, env = installation
    command = ['bash', str(repo/'install.sh'), '--yes', '--local-only']
    first = subprocess.run(command, env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (home/'workspace/test-project').is_dir()
    launcher = home/'.local/bin/aios-client'
    assert launcher.is_file()
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert ['uv', ['python', 'install', '3.12']] in calls
    assert any(c[1][:2] == ['-m','pip'] for c in calls)
    assert any(c[0]=='aios-client' and 'configure-ui' in c[1] for c in calls)
    sentinel = repo/'.venv/keep-this'
    sentinel.write_text('preserve')
    second = subprocess.run(command, env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stdout + second.stderr
    assert sentinel.read_text() == 'preserve'
    # Launcher supports spaces and passes arguments without shell re-parsing.
    launched = subprocess.run([str(launcher), 'doctor', '--agent', 'Shego Playground'], env=env)
    assert launched.returncode == 0
    assert json.loads(log.read_text().splitlines()[-1]) == ['aios-client', ['doctor', '--agent', 'Shego Playground']]


def test_installer_preserves_incompatible_venv(installation):
    repo, home, tools, log, env = installation
    old = repo/'.venv/bin'
    old.mkdir(parents=True)
    python = old/'python'
    python.write_text('#!/bin/sh\nexit 1\n')
    python.chmod(0o755)
    result = subprocess.run(['bash', str(repo/'install.sh'), '--yes', '--local-only', '--python', str(tools/'modern-python')], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'incompatible' in result.stderr
    assert python.exists()


def test_installer_remote_and_local_options_conflict(installation):
    repo, _, _, _, env = installation
    result = subprocess.run(['bash', str(repo/'install.sh'), '--yes', '--local-only', '--remote-url', 'http://192.0.2.10:8765'], env=env, capture_output=True, text=True)
    assert result.returncode == 2
