#!/usr/bin/env python3
"""Private first-segment transport only: no network, replay, research or orders."""
import functools
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile

SCHEMA = "forward_first_segment_backup_v1"
FILE_LIMITS = {
    "capture/started.json": 1024**2,
    "code/forward_evidence.py": 256 * 1024,
    "code/collect_bybit_microstructure.py": 256 * 1024,
    "capture/segment-000/receipt.json": 1024**2,
    "capture/segment-000/receive.jsonl.gz": 129 * 1024**2,
}
MANIFEST = "manifest.json"
MAX_PLAIN = sum(FILE_LIMITS.values()) + 1024**2
MAX_CIPHER = MAX_PLAIN + 1024**2


class BackupError(ValueError):
    """Fixed public error codes; never include OpenSSL stderr or plaintext."""


def require(condition, code):
    if not condition:
        raise BackupError(code)


def bounded_errors(code):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except BackupError:
                raise
            except Exception:
                raise BackupError(code) from None
        return wrapped
    return decorate


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def directory_fd(path):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "BACKUP_UNSAFE_PATH")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_regular(path, limit, private=False):
    path = Path(path)
    parent = directory_fd(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "BACKUP_NOT_SINGLE_REGULAR_FILE")
        require(before.st_size <= limit, "BACKUP_FILE_SIZE_LIMIT")
        require(not private or stat.S_IMODE(before.st_mode) & 0o077 == 0, "BACKUP_PRIVATE_KEY_PERMISSIONS")
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
        require(all(getattr(before, k) == getattr(after, k) for k in fields)
                and len(data) == before.st_size, "BACKUP_SOURCE_CHANGED")
        return data


def archive_bytes(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(files.items()):
            item = tarfile.TarInfo(name)
            item.size, item.mode, item.mtime = len(data), 0o600, 0
            archive.addfile(item, io.BytesIO(data))
    result = output.getvalue()
    require(len(result) <= MAX_PLAIN, "BACKUP_ARCHIVE_SIZE_LIMIT")
    return result


@bounded_errors("BACKUP_PACK_FAILED")
def pack(batch):
    """Read five fixed original files without following links; return canonical tar."""
    batch = Path(batch)
    files = {name: read_regular(batch / name, limit) for name, limit in FILE_LIMITS.items()}
    manifest = {"schema_version": SCHEMA,
                "files": {name: {"sha256": sha(data), "bytes": len(data)} for name, data in files.items()}}
    files[MANIFEST] = canonical(manifest)
    return archive_bytes(files)


def openssl(arguments, data):
    try:
        result = subprocess.run(["openssl"] + arguments, input=data, capture_output=True,
                                timeout=120, check=False)
    except Exception:
        raise BackupError("BACKUP_CRYPTO_FAILED") from None
    require(result.returncode == 0, "BACKUP_CRYPTO_FAILED")
    return result.stdout


def check_certificate(certificate):
    require(isinstance(certificate, bytes) and 0 < len(certificate) <= 64 * 1024
            and certificate.count(b"-----BEGIN CERTIFICATE-----") == 1
            and b"PRIVATE KEY" not in certificate, "BACKUP_CERTIFICATE_INVALID")
    public = openssl(["x509", "-pubkey", "-noout"], certificate)
    modulus = openssl(["rsa", "-pubin", "-modulus", "-noout"], public).strip()
    require(re.fullmatch(b"Modulus=[0-9A-Fa-f]+", modulus) is not None
            and int(modulus.split(b"=", 1)[1], 16).bit_length() >= 2048,
            "BACKUP_RSA_CERTIFICATE_REQUIRED")


@bounded_errors("BACKUP_ENCRYPT_FAILED")
def encrypt(plaintext, certificate_pem):
    require(isinstance(plaintext, bytes) and 0 < len(plaintext) <= MAX_PLAIN, "BACKUP_PLAINTEXT_SIZE_LIMIT")
    check_certificate(certificate_pem)
    with tempfile.TemporaryDirectory(prefix="forward-public-cert-") as temporary:
        certificate = Path(temporary) / "recipient.pem"
        certificate.write_bytes(certificate_pem)
        encrypted = openssl(["cms", "-encrypt", "-binary", "-outform", "DER", "-aes-256-cbc",
                             "-recip", str(certificate), "-keyopt", "rsa_padding_mode:oaep",
                             "-keyopt", "rsa_oaep_md:sha256"], plaintext)
    require(0 < len(encrypted) <= MAX_CIPHER, "BACKUP_CIPHERTEXT_SIZE_LIMIT")
    return encrypted


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "BACKUP_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def validate_archive(plaintext, expected):
    require(isinstance(expected, dict) and set(expected) == set(FILE_LIMITS)
            and all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in expected.values()),
            "BACKUP_EXPECTED_HASHES_INVALID")
    require(0 < len(plaintext) <= MAX_PLAIN, "BACKUP_PLAINTEXT_SIZE_LIMIT")
    files = {}
    limits = dict(FILE_LIMITS, **{MANIFEST: 64 * 1024})
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:") as archive:
        for item in archive:
            require(item.name in limits and item.name not in files, "BACKUP_ARCHIVE_MEMBER_INVALID")
            require(item.type == tarfile.REGTYPE and not item.linkname and not item.pax_headers,
                    "BACKUP_ARCHIVE_NOT_REGULAR")
            require(0 <= item.size <= limits[item.name], "BACKUP_ARCHIVE_MEMBER_SIZE_LIMIT")
            stream = archive.extractfile(item)
            require(stream is not None, "BACKUP_ARCHIVE_MEMBER_MISSING")
            with stream:
                data = stream.read(limits[item.name] + 1)
            require(len(data) == item.size, "BACKUP_ARCHIVE_MEMBER_TRUNCATED")
            files[item.name] = data
    require(set(files) == set(limits), "BACKUP_ARCHIVE_FILES_MISSING")
    # Reject hidden extension headers, trailing archives/data and noncanonical metadata.
    require(archive_bytes(files) == plaintext, "BACKUP_ARCHIVE_NONCANONICAL")
    manifest = json.loads(files[MANIFEST], object_pairs_hook=no_duplicate_keys)
    require(isinstance(manifest, dict) and set(manifest) == {"schema_version", "files"}
            and manifest["schema_version"] == SCHEMA and isinstance(manifest["files"], dict)
            and set(manifest["files"]) == set(FILE_LIMITS), "BACKUP_MANIFEST_INVALID")
    for name in FILE_LIMITS:
        actual = {"sha256": sha(files[name]), "bytes": len(files[name])}
        require(manifest["files"][name] == actual and expected[name] == actual["sha256"],
                "BACKUP_ORIGINAL_HASH_MISMATCH")
    return files, manifest


def write_exclusive(destination, files):
    destination = Path(destination)
    require(destination.name not in ("", ".", ".."), "BACKUP_DESTINATION_INVALID")
    parent = directory_fd(destination.parent)
    try:
        os.mkdir(destination.name, 0o700, dir_fd=parent)
        root = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        for name in ("code", "capture", "capture/segment-000"):
            os.mkdir(name, 0o700, dir_fd=root)
        for name, data in files.items():
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        for name in ("capture/segment-000", "capture", "code"):
            directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        os.fsync(root)
    finally:
        os.close(root)


@bounded_errors("BACKUP_RESTORE_FAILED")
def restore(ciphertext, key_path, certificate_path, destination, expected_file_hashes):
    """Decrypt locally, bind originals to external hashes, create a private new tree."""
    require(isinstance(ciphertext, bytes) and 0 < len(ciphertext) <= MAX_CIPHER,
            "BACKUP_CIPHERTEXT_SIZE_LIMIT")
    # Copy checked inputs into a private temp directory to avoid key-path link races.
    key = read_regular(key_path, 64 * 1024, private=True)
    certificate = read_regular(certificate_path, 64 * 1024)
    check_certificate(certificate)
    with tempfile.TemporaryDirectory(prefix="forward-private-restore-") as temporary:
        private = Path(temporary) / "key.pem"
        recipient = Path(temporary) / "recipient.pem"
        private.write_bytes(key)
        private.chmod(0o600)
        recipient.write_bytes(certificate)
        plaintext = openssl(["cms", "-decrypt", "-binary", "-inform", "DER", "-recip", str(recipient),
                             "-inkey", str(private), "-passin", "pass:"], ciphertext)
    files, manifest = validate_archive(plaintext, expected_file_hashes)
    write_exclusive(destination, files)
    return {"schema_version": SCHEMA, "status": "RESTORED_HASH_BOUND_NOT_REPLAYED",
            "files": manifest["files"], "manifest_sha256": sha(files[MANIFEST]),
            "raw_replay_performed": False, "economic_evidence": False, "research_admitted": False}
