"""Write evidence archives and verify every member before publication."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
from .evidence import digest

INDEX = '_archive_index.json'

def verify(path):
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)):
            raise ValueError('duplicate archive member')
        for member in members:
            name = Path(member.name)
            if not member.isfile() or name.is_absolute() or '..' in name.parts:
                raise ValueError('unsafe archive member')
        index = json.load(archive.extractfile(INDEX))
        if set(names) != set(index) | {INDEX}:
            raise ValueError('archive member coverage changed')
        for name, expected in index.items():
            stream = archive.extractfile(name)
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != expected:
                raise ValueError('archive member hash changed: ' + name)
    return {'verified_files': len(index), 'sha256': digest(path)}

def create(directory, output):
    directory = directory.resolve(); output = output.resolve()
    if output.is_relative_to(directory):
        raise ValueError('archive output must be outside the study')
    if output.exists():
        raise ValueError('archive output already exists')
    paths = sorted(directory.rglob('*'))
    if any(p.is_symlink() for p in paths):
        raise ValueError('archive cannot contain symbolic links')
    files = [p for p in paths if p.is_file()]
    index = {str(p.relative_to(directory)): digest(p) for p in files}
    if INDEX in index:
        raise ValueError('reserved archive index name')
    partial = output.with_name(output.name + '.partial')
    with partial.open('xb') as stream:
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for p in files:
                archive.add(p, arcname=str(p.relative_to(directory)), recursive=False)
            data = json.dumps(index, sort_keys=True).encode()
            info = tarfile.TarInfo(INDEX); info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    receipt = verify(partial)
    partial.rename(output)
    return {'archive': str(output), **receipt}
