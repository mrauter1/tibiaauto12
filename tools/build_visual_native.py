"""Explicitly build the optional native visual filter; normal startup never builds.

Windows: run from an x64 Native Tools shell for MSVC/clang-cl, or use MinGW/Clang.
Linux: GCC/Clang. The generated library and intermediate files stay in .work.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import native_visual_guard as native  # noqa: E402


def compiler_command(compiler, source, output, platform_name):
    executable = str(compiler).replace('\\', '/').rsplit('/', 1)[-1].lower()
    if platform_name == 'win32':
        if executable.removesuffix('.exe') in ('cl', 'clang-cl'):
            return [str(compiler), '/nologo', '/O2', '/LD', '/std:c11', '/TC', str(source),
                    '/link', f'/OUT:{output}']
        return [str(compiler), '-O3', '-shared', '-std=c11', str(source), '-o', str(output)]
    if platform_name == 'darwin':
        return [str(compiler), '-O3', '-dynamiclib', '-fPIC', '-std=c11', str(source),
                '-o', str(output)]
    if platform_name.startswith('linux'):
        return [str(compiler), '-O3', '-shared', '-fPIC', '-std=c11', str(source),
                '-o', str(output)]
    raise RuntimeError(f'Native visual build unsupported on {platform_name}')


def find_compiler(platform_name, which=None):
    which = which or shutil.which
    choices = ('cl', 'clang-cl', 'gcc', 'clang') if platform_name == 'win32' else (
        'cc', 'gcc', 'clang')
    for name in choices:
        compiler = which(name)
        if compiler is not None:
            return compiler
    raise RuntimeError('No C compiler found. Use GCC/Clang or a Windows Native Tools developer shell.')


def build(compiler=None):
    compiler = compiler or find_compiler(sys.platform)
    destination = native.library_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        native._open_backend(destination, native.source_id())
        native._load_backend.cache_clear()
        return destination
    with tempfile.TemporaryDirectory(prefix='visual-guard-build-', dir=destination.parent) as temp:
        directory = Path(temp)
        source = directory / native.SOURCE.name
        source.write_bytes(native.SOURCE.read_bytes())
        (directory / 'native_visual_build.h').write_text(
            f'#define VISUAL_GUARD_SOURCE_ID "{native.source_id()}"\n', encoding='ascii')
        output = directory / ('visual_guard' + destination.suffix)
        command = compiler_command(compiler, source, output, sys.platform)
        try:
            subprocess.run(command, cwd=directory, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or str(error))[-2000:]
            raise RuntimeError(f'Native visual compilation failed: {detail}') from error
        output.replace(destination)
    # Validate only after publishing the build. Windows keeps a loaded DLL open,
    # so trying to rename an already-loaded temporary DLL would fail there.
    native._open_backend(destination, native.source_id())
    native._load_backend.cache_clear()
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler', help='C compiler executable; otherwise detect the platform toolchain')
    args = parser.parse_args(argv)
    try:
        print(build(args.compiler))
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f'{error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
