"""Crash boundaries for per-book author writes; uses only a JSON fake server."""
import json
import os
from pathlib import Path
import signal
import sys
from unittest.mock import patch
from manga_kit.config import PathMapping, Settings
from manga_kit.komga_authors import KomgaAuthorsService
from test_komga_authors import FakeAuthorsKomga

root,operation_id,confirmation,action,boundary=sys.argv[1:]
root=Path(root)
remote=root/'fake-authors.json'
client=FakeAuthorsKomga(json.loads(remote.read_text()))
settings=Settings(demo=False,library_root=root/'library',data_root=root/'state',execution_enabled=True,
    komga_url='http://127.0.0.1:25601',path_mappings=(PathMapping('/data',root/'library'),))
service=KomgaAuthorsService(settings,client=client)
actual_patch,actual_save=service._patch_book,service._save
count=0

def send(book,values):
    global count
    actual_patch(book,values)
    count+=1
    with remote.open('w') as out:
        json.dump(client.data,out)
        out.flush()
        os.fsync(out.fileno())
    if boundary=='after-first' and count==1 or boundary=='after-second' and count==2:
        os.kill(os.getpid(),signal.SIGKILL)

def save(operation):
    actual_save(operation)
    if boundary=='before' and operation.get('intent'):
        os.kill(os.getpid(),signal.SIGKILL)

with patch.object(service,'_patch_book',side_effect=send),patch.object(service,'_save',side_effect=save):
    getattr(service,action)(operation_id,confirmation)
raise AssertionError('Crash boundary was not reached')
