"""Test-only subprocess: terminate at a chosen mutation without running finally."""
import os
from pathlib import Path
import signal
import sys
from unittest.mock import patch

from manga_kit.config import Settings
from manga_kit.conversion import ConversionService
from manga_kit.operations import OperationService


def main():
    root, engine, action, operation_id, confirmation, boundary = sys.argv[1:]
    root = Path(root)
    settings = Settings(demo=False, library_root=root / 'library', data_root=root / 'state', execution_enabled=True)
    service = (ConversionService if engine == 'conversion' else OperationService)(settings)
    actual_link, actual_unlink, actual_write, actual_save = os.link, os.unlink, os.write, OperationService._save
    def terminate():
        os.kill(os.getpid(), signal.SIGKILL)
    def link(*args, **kwargs):
        result = actual_link(*args, **kwargs)
        temporary = str(args[0]).startswith('.manga-kit-restore-')
        if 'src_dir_fd' in kwargs and ((boundary == 'link' and not temporary) or (boundary == 'publish' and temporary)):
            terminate()
        return result
    def unlink(*args, **kwargs):
        result = actual_unlink(*args, **kwargs)
        if boundary == 'unlink' and not str(args[0]).startswith('.manga-kit-restore-'):
            terminate()
        return result
    def write(fd, data):
        if boundary == 'copy' and Path(os.readlink(f'/proc/self/fd/{fd}')).name.startswith('.manga-kit-restore-'):
            actual_write(fd, data[:max(1, len(data) // 2)])
            terminate()
        return actual_write(fd, data)
    def save(self, operation):
        if boundary == 'edits' and operation['status'] == 'completed':
            terminate()
        result = actual_save(self, operation)
        if boundary == 'temporary' and operation.get('temporary_files'):
            terminate()
        return result
    with patch('os.link', side_effect=link), patch('os.unlink', side_effect=unlink), patch('os.write', side_effect=write), patch.object(OperationService, '_save', new=save):
        getattr(service, action)(operation_id, confirmation)
    raise AssertionError('The selected crash boundary was not reached')


if __name__ == '__main__':
    main()
