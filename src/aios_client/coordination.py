"""Cooperative host/workspace locks, shared by profiles and client processes.

These locks coordinate clients under the same OS account, not arbitrary shell
programs. Windows conservatively serializes all work because msvcrt has no
equivalent of POSIX shared flock locks.
"""
from __future__ import annotations

import hashlib
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import UUID, uuid4


def coordination_dir():
    # Independent of the profile/config directory, so two UI/CLI installations
    # running as the same user coordinate the same host.
    base = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) if os.name == 'nt' else Path.home() / '.cache'
    path = base / 'aios-client' / 'coordination'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@contextmanager
def _lock(path, *, shared=False, blocking=False):
    stream = path.open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(stream, flags | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        stream.close()


def environment_id():
    directory = coordination_dir()
    with _lock(directory / 'identity.lock', blocking=True):
        path = directory / 'environment-id'
        if not path.exists():
            path.write_text(str(uuid4()), encoding='ascii')
        return str(UUID(path.read_text(encoding='ascii').strip()))


@contextmanager
def execution_slot(root, administration=False):
    """Try before claiming a remote lease. Never wait holding a claimed run."""
    directory = coordination_dir()
    root = Path(os.path.normcase(str(Path(root).expanduser().resolve(strict=True))))
    with ExitStack() as stack:
        try:
            stack.enter_context(_lock(directory / 'host.lock', shared=not administration))
            if os.name != 'nt':
                # Shared ancestor locks prevent parent/child workspaces racing,
                # while independent sibling workspaces can execute concurrently.
                for path in [*reversed(root.parents), root]:
                    key = hashlib.sha256(str(path).encode()).hexdigest()
                    stack.enter_context(_lock(directory / (key + '.lock'), shared=path != root))
        except BlockingIOError:
            stack.close()
            yield False
            return
        except OSError as exc:
            # Windows reports lock contention as EACCES/EDEADLK.
            import errno
            if os.name != 'nt' or exc.errno not in (errno.EACCES, errno.EDEADLK, errno.EAGAIN):
                raise
            stack.close()
            yield False
            return
        yield True
