"""Verified public network helper for the disposable native worker image."""

import gzip
import hashlib
import io
import os
from pathlib import Path
import stat
import struct
import subprocess
import tarfile
import urllib.request


NETAVARK = {
    'version': '1.16.1', 'architecture': 'amd64',
    'url': 'https://github.com/containers/netavark/releases/download/v1.16.1/netavark.gz',
    'compressed_size': 6233051,
    'compressed_sha256': '24aeeb787e25ba9728edc4ec2f8ed5566a5ac56795c63b2d501fd902e8d14634',
    'size': 17708896,
    'sha256': '8df169b63a3ce6e04236d965d711a2e8cefe320ab5a5e979d26d44880f427ffc',
}


def verify_version():
    result = subprocess.run(['/etc/runner-tools/netavark', '--version'],
                            capture_output=True, text=True, check=True, timeout=10)
    if result.stdout.strip() != 'netavark ' + NETAVARK['version']:
        raise ValueError('Native network helper version does not match')


def verify_binary(data):
    if (len(data) != NETAVARK['size']
            or hashlib.sha256(data).hexdigest() != NETAVARK['sha256']
            or data[:6] != b'\x7fELF\x02\x01' or len(data) < 64
            or struct.unpack_from('<H', data, 18)[0] != 62):
        raise ValueError('Native network helper identity or ABI does not match')


def prepare_archive(directory: Path, architecture: str) -> Path:
    if architecture != NETAVARK['architecture']:
        raise ValueError('Native network helper requires amd64')
    with urllib.request.urlopen(NETAVARK['url'], timeout=120) as stream:
        compressed = stream.read(NETAVARK['compressed_size'] + 1)
    if (len(compressed) != NETAVARK['compressed_size']
            or hashlib.sha256(compressed).hexdigest() != NETAVARK['compressed_sha256']):
        raise ValueError('Native network helper download identity does not match')
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        data = stream.read(NETAVARK['size'] + 1)
    verify_binary(data)
    path = directory / 'netavark.tar'
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        with tarfile.open(fileobj=stream, mode='w') as archive:
            member = tarfile.TarInfo('netavark')
            member.size = len(data)
            member.mode = 0o555
            archive.addfile(member, io.BytesIO(data))
        stream.flush()
        os.fsync(stream.fileno())
    return path


def install_archive(archive_path: Path, destination: Path):
    fd = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_size > NETAVARK['size'] + 10240):
            raise ValueError('Native network helper archive is not a bounded regular file')
        with tarfile.open(fileobj=stream, mode='r:') as archive:
            member = archive.next()
            if (member is None or member.name != 'netavark' or not member.isfile()
                    or member.size != NETAVARK['size']):
                raise ValueError('Native network helper archive member does not match')
            with archive.extractfile(member) as source:
                data = source.read(NETAVARK['size'] + 1)
            if archive.next() is not None:
                raise ValueError('Native network helper archive contains extra members')
    verify_binary(data)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o555)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fchmod(stream.fileno(), 0o555)
        os.fsync(stream.fileno())
