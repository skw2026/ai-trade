#!/usr/bin/env python3
"""Exact TEST_ONLY learning/registration file transfer, without promotion authority.

Prepare a trusted manifest outside the upload root. After downloading the unique
artifact, compare against that original manifest, never a downloaded replacement.
This proves byte transport, not market evidence or live model activation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

SCOPE = 'TEST_ONLY_EVIDENCE_TRANSPORT'
AUTHORITY = ('production_promotion_authority', 'market_economic_evidence',
             'demo_trading_authority', 'full_mechanism_valid')
LOCKS = {'registration/' + case + '/registry/.index.lock'
         for case in ('registry_empty', 'registry_with_previous')}
MAX_FILES, MAX_BYTES = 512, 64 * 1024 * 1024


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')


def safe_name(name):
    require(isinstance(name, str) and name and len(name) < 512, 'UNSAFE_PATH')
    p = PurePosixPath(name)
    require(not p.is_absolute() and p.as_posix() == name and '\\' not in name
            and ':' not in name and all(s not in ('.', '..', '') for s in name.split('/')),
            'UNSAFE_PATH')
    require(p.parts[0] in ('acceptance', 'registration'), 'UNEXPECTED_UPLOAD_ROOT')
    require(all(not part.startswith('.') for part in p.parts) or name in LOCKS,
            'UNEXPECTED_HIDDEN_FILE')
    require(p.suffix in ('.json', '.log', '.csv', '.cbm') or name in LOCKS,
            'UNEXPECTED_FILE_TYPE')
    return name


def inventory(root):
    require(root.is_dir() and not root.is_symlink(), 'INVALID_ROOT')
    root = root.resolve()
    paths, total = [], 0
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(), 'SYMLINK_FORBIDDEN')
        require(path.resolve().is_relative_to(root), 'PATH_ESCAPE')
        if path.is_dir():
            continue
        require(path.is_file(), 'SPECIAL_FILE_FORBIDDEN')
        name = safe_name(path.relative_to(root).as_posix())
        total += path.stat().st_size
        paths.append((name, path))
        require(len(paths) <= MAX_FILES and total <= MAX_BYTES, 'BYTE_OR_FILE_BUDGET')
    return {name: digest(path) for name, path in paths}


def validate_reports(root, files, expected_sha):
    require(bool(expected_sha), 'EXPECTED_SHA_REQUIRED')
    expected = {}
    reports = {}
    for folder, filename, scope in (
            ('acceptance', 'result.json', 'TEST_ONLY_OFFLINE_COMPONENT_INTEGRATION'),
            ('registration', 'registration-result.json', 'TEST_ONLY_REGISTRATION_ISOLATION')):
        name = folder + '/' + filename
        require(name in files, 'MISSING_REPORT:' + name)
        report = read(root / name)
        require(report['status'] == 'PASS' and report['scope'] == scope
                and report['verification_git_sha'] == expected_sha, 'REPORT_IDENTITY:' + name)
        require(all(report[k] is False for k in AUTHORITY), 'FORBIDDEN_AUTHORITY')
        reports[folder] = report
        expected[name] = files[name]
        groups = [report['artifact_sha256']]
        if folder == 'acceptance':
            groups.append(report['training_artifact_sha256'])
        for group in groups:
            require(isinstance(group, dict) and bool(group), 'EMPTY_MANIFEST')
            for relative, checksum in group.items():
                name = safe_name(folder + '/' + relative)
                require(re.fullmatch('[0-9a-f]{64}', checksum) is not None, 'INVALID_HASH')
                require(name not in expected or expected[name] == checksum, 'CONFLICTING_HASH')
                expected[name] = checksum
    learning, registration = reports['acceptance'], reports['registration']
    require(registration['learning_verification_git_sha'] == expected_sha
            and registration['learning_report_sha256'] == files['acceptance/result.json'],
            'LEARNING_REGISTRATION_LINK')
    require(registration['production_registry_written'] is False
            and registration['original_inputs_unchanged'] is True, 'REGISTRY_NOT_ISOLATED')
    require(registration['input_sha256'] == learning['training_artifact_sha256'], 'TRAINING_INPUT_LINK')
    require(LOCKS <= expected.keys(), 'LOCK_MANIFEST_MISSING')
    require(expected == files, 'INCOMPLETE_OR_CHANGED_FILE_SET')
    metadata = read(root / 'acceptance/learned.json')
    require(metadata['test_only'] is True and metadata['production_promotion_authority'] is False
            and metadata['governance']['pass'] is False
            and metadata['data']['source_venue'] == 'synthetic'
            and metadata['data']['source_category'] == 'test_only', 'SOURCE_NOT_TEST_ONLY')


def prepare(root, manifest_path, expected_sha):
    root = Path(root)
    require(not manifest_path.resolve().is_relative_to(root.resolve()), 'MANIFEST_INSIDE_UPLOAD')
    require(not manifest_path.exists(), 'MANIFEST_ALREADY_EXISTS')
    files = inventory(root)
    validate_reports(root, files, expected_sha)
    manifest = {'schema_version': 'evidence_transport_manifest_v1', 'scope': SCOPE,
                'verification_git_sha': expected_sha, 'files': files,
                **{k: False for k in AUTHORITY}}
    write_new(manifest_path, manifest)
    return manifest


def verify(root, manifest_path, expected_sha, artifact_id=0, run_id=0, run_attempt=0):
    manifest = read(manifest_path)
    require(manifest['schema_version'] == 'evidence_transport_manifest_v1'
            and manifest['scope'] == SCOPE and manifest['verification_git_sha'] == expected_sha
            and all(manifest[k] is False for k in AUTHORITY), 'MANIFEST_IDENTITY')
    files = inventory(root)
    require(files == manifest['files'], 'TRANSFER_FILE_SET_MISMATCH')
    validate_reports(root, files, expected_sha)
    return {'schema_version': 'evidence_transport_receipt_v1', 'status': 'PASS', 'scope': SCOPE,
            'verification_git_sha': expected_sha, 'manifest_sha256': digest(manifest_path),
            'file_count': len(files), 'hidden_lock_count': len(LOCKS),
            'artifact_id': artifact_id, 'run_id': run_id, 'run_attempt': run_attempt,
            **{k: False for k in AUTHORITY}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'verify'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--output', type=Path)
    for field in ('artifact-id', 'run-id', 'run-attempt'):
        parser.add_argument('--' + field, type=int, default=0)
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            result = prepare(args.root, args.manifest, args.expected_sha)
            print(json.dumps({'scope': SCOPE, 'prepared_files': len(result['files'])}))
        else:
            if args.output:
                require(not args.output.exists() and
                        not args.output.resolve().is_relative_to(args.root.resolve()), 'UNSAFE_RECEIPT_OUTPUT')
            result = verify(args.root, args.manifest, args.expected_sha,
                            args.artifact_id, args.run_id, args.run_attempt)
            if args.output:
                write_new(args.output, result)
            print(json.dumps(result))
    except (ValueError, KeyError, TypeError, OSError) as error:
        print('EVIDENCE_TRANSPORT_REJECTED: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
