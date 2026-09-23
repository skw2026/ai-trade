#!/usr/bin/env python3
"""Make an explicit, writable source-only validation copy, never live data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(root, output, include):
    root, output = root.resolve(), output.resolve()
    if output.exists():
        raise ValueError('output must be new; preserve previous workspaces')
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
    names = sorted(set(filter(None, names)) | set(include))
    # Ignore user/runtime data even if accidentally tracked in a later revision.
    blocked = {'.git', '.artifacts', '.codex', '.agents', 'build', 'data'}
    selected = []
    for name in names:
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('unsafe path')
        if rel.parts[0] in blocked:
            continue
        source = root / rel
        if source.is_symlink() or not source.is_file():
            raise ValueError('source must be a regular file: ' + name)
        selected.append((name, source))
    output.mkdir(parents=True)
    hashes = {}
    for name, source in selected:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        hashes[name] = digest(source)
        if digest(target) != hashes[name]:
            raise ValueError('source-copy mismatch')
    (output / 'data/models').mkdir(parents=True)
    (output.parent / (output.name + '-source-manifest.json')).write_text(
        json.dumps(hashes, sort_keys=True, indent=2) + '\n')
    print(json.dumps({'source_files': len(hashes), 'live_data_copied': False}))


def preflight(root, test_temp):
    if test_temp is None:
        raise ValueError('--test-temp must name the CTestCustom scratch directory')
    test_temp.mkdir(parents=True, exist_ok=True)
    for binary in ('cmake', 'ninja', 'c++', 'python3', 'bash', 'git', 'flock'):
        if not shutil.which(binary):
            raise ValueError('missing dependency: ' + binary)
    for directory in (root, root / 'data/models', Path(tempfile.gettempdir())):
        if shutil.disk_usage(directory).free < 400 * 1024 * 1024:
            raise ValueError('insufficient free bytes: ' + str(directory))
        with tempfile.NamedTemporaryFile(dir=directory) as stream:
            stream.write(b'validation preflight\n')
            stream.flush()
            os.fsync(stream.fileno())
            subprocess.run(['flock', '-n', stream.name, 'true'], check=True)
    with tempfile.TemporaryDirectory(dir=test_temp) as temporary:
        standin = Path(temporary) / 'standin'
        standin.write_text('#!/bin/sh\nexit 0\n')
        standin.chmod(0o700)
        subprocess.run([str(standin)], check=True)
    print(json.dumps({'dependencies': 'PASS', 'writable_lock_fsync': 'PASS',
                      'temporary_executable': 'PASS',
                      'free_bytes_minimum': 400 * 1024 * 1024}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'preflight'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--include', action='append', default=[])
    parser.add_argument('--test-temp', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        if args.output is None:
            parser.error('--output required')
        prepare(args.root, args.output, args.include)
    else:
        preflight(args.root, args.test_temp)
