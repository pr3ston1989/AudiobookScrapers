"""Persist restart checkpoints and recover invalid JSON without losing queues."""

import json
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path

log = logging.getLogger(__name__)


def _state(path):
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict) or any(not isinstance(item, dict) for item in value.values()):
        raise ValueError('Nieprawidłowa struktura punktu wznowienia')
    return value


def _atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path, value):
    """Flush JSON to disk before replacement; retain a valid previous checkpoint."""
    path = Path(path)
    if path.name in ('run.json', 'state.json') and path.exists():
        try:
            previous = _state(path)
        except (ValueError, UnicodeError):
            pass
        else:
            _atomic(path.with_suffix(path.suffix + '.bak'), previous)
    _atomic(path, value)


def read_state(path, fallback=None):
    """Restore a corrupt checkpoint from its backup or independent parent state."""
    path = Path(path)
    fallback = json.loads(json.dumps(fallback if fallback is not None else {}))
    try:
        return _state(path)
    except FileNotFoundError:
        return fallback
    except (ValueError, UnicodeError) as exc:
        archive = path.with_name(path.name + '.corrupt.' + uuid.uuid4().hex)
        shutil.copy2(path, archive)
        backup = path.with_suffix(path.suffix + '.bak')
        try:
            restored = _state(backup)
        except (FileNotFoundError, ValueError, UnicodeError):
            restored = fallback
        write_json(path, restored)
        log.warning('Odtworzono uszkodzony punkt wznowienia %s; kopia uszkodzenia: %s (%s)', path, archive, exc)
        return restored
