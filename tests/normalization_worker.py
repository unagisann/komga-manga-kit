"""Kill at the in-place replacement gap to verify durable original recovery."""
import os
from pathlib import Path
import signal
import sys
from unittest.mock import patch

from manga_kit.config import Settings
from manga_kit.normalization import NormalizationOperations

root = Path(sys.argv[1])
service = NormalizationOperations(Settings(demo=False, library_root=root / 'library', data_root=root / 'state', execution_enabled=True))
original = service._remove
def remove(*args, **kwargs):
    original(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
with patch.object(service, '_remove', side_effect=remove):
    service.execute(sys.argv[2], sys.argv[3])
