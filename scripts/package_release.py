"""Create the versioned assets consumed by RenderDesk's GitHub updater."""
import argparse
import hashlib
from pathlib import Path
import re
import shutil
import zipfile


def main():
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, default=repo / 'dist/BlenderRenderDesk')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    version = (repo / 'VERSION').read_text(encoding='utf8').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid VERSION')
    module = (repo / 'renderdesk/version.py').read_text(encoding='utf8')
    if f"VERSION = '{version}'" not in module:
        raise ValueError('VERSION and renderdesk/version.py must match')
    binary = args.bundle.resolve()
    if not (binary / 'BlenderRenderDesk.exe').is_file() or not (binary / '_internal').is_dir():
        raise ValueError('Build the complete Windows application first')
    notes = repo / 'docs/releases' / ('v' + version + '.md')
    if not notes.is_file():
        raise ValueError('Missing release notes')
    output = (args.output or repo / 'dist/releases' / ('v' + version)).resolve()
    if output == binary or binary in output.parents:
        raise ValueError('Release output must be outside the Windows bundle')
    output.mkdir(parents=True, exist_ok=True)
    for name in ('README.md', 'VERSION', 'LICENSE'):
        shutil.copy2(repo / name, binary / name)
    for name in ('docs', 'licenses'):
        shutil.copytree(repo / name, binary / name, dirs_exist_ok=True)
    shutil.copy2(notes, output / '发布说明.md')
    ignored = {'.git', '__pycache__', '.venv', 'build', 'dist'}
    sources = [p for p in repo.rglob('*') if p.is_file() and not any(x in ignored for x in p.relative_to(repo).parts)
               and p.suffix != '.pyc' and output not in p.parents and binary not in p.parents]
    sums = []
    for suffix, base, files in (('Windows', binary, [p for p in binary.rglob('*') if p.is_file()]), ('source', repo, sources)):
        target = output / f'BlenderRenderDesk-v{version}-{suffix}.zip'
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in files:
                archive.write(path, 'BlenderRenderDesk/' + path.relative_to(base).as_posix())
        with zipfile.ZipFile(target) as archive:
            if archive.testzip():
                raise ValueError('ZIP verification failed')
            for path in files:
                if archive.read('BlenderRenderDesk/' + path.relative_to(base).as_posix()) != path.read_bytes():
                    raise ValueError('Archive differs from source: ' + str(path))
        with target.open('rb') as stream:
            hasher = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                hasher.update(block)
        sums.append(hasher.hexdigest() + '  ' + target.name)
        print('Verified:', target.name, target.stat().st_size, 'bytes', flush=True)
    (output / f'SHA256SUMS-v{version}.txt').write_text('\n'.join(sums) + '\n', encoding='utf8')
    print(output, flush=True)


if __name__ == '__main__':
    main()
