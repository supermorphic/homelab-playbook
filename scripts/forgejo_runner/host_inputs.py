"""Stream checksum-verified public assets into the owned offline image staging tree."""

import hashlib
import os
from pathlib import PurePosixPath
import re


def validate_assets(assets, maximum):
    if not isinstance(assets, dict) or not assets or len(assets) > 16:
        raise ValueError('Invalid offline asset manifest')
    total = 0
    for name, descriptor in assets.items():
        path = PurePosixPath(name)
        if (str(path) != name or len(path.parts) != 3 or path.parts[:2] != ('work', 'input')
                or re.fullmatch(r'[a-z][a-z0-9-]*\.(?:tar|oci)', path.name) is None
                or not isinstance(descriptor, dict) or set(descriptor) != {'size', 'sha256'}
                or type(descriptor['size']) is not int or descriptor['size'] <= 0
                or not isinstance(descriptor['sha256'], str)
                or re.fullmatch(r'[0-9a-f]{64}', descriptor['sha256']) is None):
            raise ValueError('Invalid offline asset manifest')
        total += descriptor['size']
    if type(maximum) is not int or maximum <= 0 or total > maximum:
        raise ValueError('Offline assets exceed their storage budget')
    return total


def receive_asset(stream, path, descriptor):
    """Read one framed asset; never extract its archive under host authority."""
    validate_assets({'work/input/asset.tar': descriptor}, descriptor['size'])
    digest = hashlib.sha256()
    remaining = descriptor['size']
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        original = os.fstat(output.fileno())
        try:
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError('Offline asset transfer was truncated')
                remaining -= len(chunk)
                digest.update(chunk)
                output.write(chunk)
            if digest.hexdigest() != descriptor['sha256']:
                raise ValueError('Offline asset checksum differs from its manifest')
            os.fchmod(output.fileno(), 0o444)
        except BaseException:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                path.unlink()
            raise
