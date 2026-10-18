"""Test-only subprocess: kill immediately before/after a durable external patch."""
import json
import os
from pathlib import Path
import signal
import sys
from unittest.mock import patch

from manga_kit.config import PathMapping, Settings
from manga_kit.komga_metadata import KomgaMetadataService
from manga_kit.operations import OperationService
from test_komga_metadata import FakeMetadataKomga

root, operation_id, confirmation, action, boundary = sys.argv[1:]
root = Path(root)
remote = root / 'fake-remote.json'
client = FakeMetadataKomga(json.loads(remote.read_text()))
settings = Settings(demo=False, library_root=root / 'library', data_root=root / 'state', execution_enabled=True,
    komga_url='http://127.0.0.1:25601', path_mappings=(PathMapping('/data', root / 'library'),))
service = KomgaMetadataService(settings, client=client)
actual_patch, actual_save = service._patch, OperationService._save


def send(plan, values):
    actual_patch(plan, values)
    with remote.open('w') as output:
        json.dump(client.data, output)
        output.flush()
        os.fsync(output.fileno())
    if boundary == 'after':
        os.kill(os.getpid(), signal.SIGKILL)


def save(self, operation):
    actual_save(self, operation)
    if boundary == 'before' and operation['status'] == 'running':
        os.kill(os.getpid(), signal.SIGKILL)


with patch.object(service, '_patch', side_effect=send), patch.object(OperationService, '_save', new=save):
    getattr(service, action)(operation_id, confirmation)
raise AssertionError('Crash boundary was not reached')
