"""Release compatibility and fail-closed assembly, using synthetic artifacts."""
import importlib.util
import io
import json
from pathlib import Path

import pytest
import ytdl_tray as app

spec = importlib.util.spec_from_file_location('release_prep', Path(__file__).resolve().parents[1] / 'prepare-release.py')
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)


def test_checksums_readable_by_existing_updater(monkeypatch):
    expected = 'a' * 64
    text = prep.checksum_text((name, expected) for name in (*prep.WINDOWS, *prep.LINUX))
    monkeypatch.setattr(app, '_fetch', lambda url: io.BytesIO(text.encode('ascii')))
    release = {'assets': [{'name': 'checksums.txt', 'browser_download_url': 'https://example.test/checksums.txt'}]}
    for name in (*prep.WINDOWS, *prep.LINUX):
        assert app._published_sha(release, name) == expected
    assert app._published_sha(release, 'missing.exe') == ''


@pytest.mark.parametrize('records', [
    [('Portable/Aevum.exe', 'a' * 64)],
    [('Aevum.exe', 'a' * 64), ('Aevum.exe', 'b' * 64)],
    [('Aevum.exe', 'not-a-hash')],
])
def test_invalid_checksum_names_hashes_and_duplicates(records):
    with pytest.raises(ValueError):
        prep.checksum_text(records)


@pytest.fixture
def candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(prep, 'ROOT', tmp_path)
    source = {'version': '1.2.7', 'commit': 'test-commit', 'files': {'ytdl_tray.py': 'a' * 64}}
    monkeypatch.setattr(prep, 'provenance', lambda: source)
    (tmp_path / 'release-notes.md').write_text('# Aevum 1.2.7\n', encoding='utf-8')
    win, linux = tmp_path / 'windows', tmp_path / 'linux'
    for directory, names, manifest in ((win, prep.WINDOWS, 'build-manifest.json'),
                                       (linux, prep.LINUX, 'linux-build-manifest.json')):
        directory.mkdir()
        for name in names:
            (directory / name).write_bytes(('synthetic-' + name).encode())
        prep.write_json(directory / manifest, {'source': source, 'ytDlp': 'same-version',
                        'artifacts': [prep.record(directory / name) for name in names]})
    return win, linux, tmp_path / 'releases/candidate'


def test_assemble_flat_assets_and_never_overwrite(candidates):
    win, linux, output = candidates
    prep.assemble(win, linux, output)
    assert all((output / name).is_file() for name in (*prep.WINDOWS, *prep.LINUX))
    lines = (output / 'checksums.txt').read_text().splitlines()
    assert len(lines) == 4
    for line in lines:
        name, sha = line.split()
        assert prep.digest(output / name) == sha
    assert json.loads((output / 'release-manifest.json').read_text())['published'] is False
    assert '## Checksums' in (output / 'RELEASE-NOTES.md').read_text()
    with pytest.raises(ValueError, match='already exists'):
        prep.assemble(win, linux, output)


@pytest.mark.parametrize('problem', ['hash', 'missing', 'commit', 'version'])
def test_assembly_rejects_mixed_or_damaged_builds(candidates, problem):
    win, linux, output = candidates
    if problem == 'hash':
        (win / 'Aevum.exe').write_bytes(b'changed')
    elif problem == 'missing':
        (linux / prep.LINUX[0]).unlink()
    else:
        path = linux / 'linux-build-manifest.json'
        manifest = json.loads(path.read_text())
        if problem == 'commit':
            manifest['source']['commit'] = 'different-commit'
        else:
            manifest['ytDlp'] = 'different-version'
        prep.write_json(path, manifest)
    with pytest.raises(ValueError):
        prep.assemble(win, linux, output)
    assert not output.exists()


def test_asset_cannot_escape_manifest_directory(tmp_path):
    folder = tmp_path / 'assets'
    folder.mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'synthetic')
    with pytest.raises(ValueError, match='unsafe'):
        prep.checked_asset(folder, prep.record(outside, '../outside'))
