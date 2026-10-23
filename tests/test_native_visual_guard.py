"""Optional production native filter: parity, fallback, and safe ABI boundaries."""
from pathlib import Path

import cv2
import numpy as np
import pytest

from core import native_visual_guard as native
from core.perception import Frame
from tools import resource_panel_guard as guard
from tools.build_visual_native import compiler_command, find_compiler
from tools.resource_reader import CurrentResourceReader
from test_resource_panel_guard import (
    add_duplicate, example, replace_health_with_current_capacity, signature,
)


@pytest.fixture
def backend():
    function = native._load_backend()
    if function is None:
        pytest.skip('Optional native backend not built; run python tools/build_visual_native.py')
    return function


def scalar_candidates(pixels, samples, width, height):
    points = []
    for y in range(height):
        for x in range(width):
            bright = [all(int(v) >= 130 for v in pixels[y+dy, x+dx]) == coarse
                      for dy, dx, _, coarse in samples[:4]]
            ink = [(min(map(int, pixels[y+dy, x+dx])) >= 130
                    and max(map(int, pixels[y+dy, x+dx]))-min(map(int, pixels[y+dy, x+dx])) < 35)
                   == expected for dy, dx, expected, _ in samples]
            if all(bright) and all(ink):
                points.append((y, x))
    return points[:129]


@pytest.mark.parametrize('seed', [2, 19, 67])
def test_native_and_numpy_proposals_match_scalar_oracle_in_order(backend, monkeypatch, seed):
    rng = np.random.default_rng(seed)
    pixels = rng.choice(np.array([0, 129, 130, 164, 165, 220, 255], np.uint8), size=(29, 41, 3))
    samples = ((0, 0, True, True), (1, 1, False, False), (2, 0, False, True),
               (0, 3, False, True), (3, 4, True, True))
    for x, y in [(3, 4), (20, 15)]:
        for dy, dx, expected, bright in samples:
            pixels[y+dy, x+dx] = (200, 200, 200) if expected else (
                (130, 200, 130) if bright else (42, 42, 42))
    expected = scalar_candidates(pixels, samples, 37, 26)
    assert len(expected) >= 2
    ys, xs = native.try_candidates(pixels, samples, 37, 26)
    assert list(zip(ys, xs)) == expected
    sentinels = [((dy, dx), foreground) for dy, dx, foreground, _ in samples]
    coarse = np.zeros((4, 5), dtype=bool)
    for dy, dx, _, bright in samples:
        coarse[dy, dx] = bright
    monkeypatch.setattr(native, '_load_backend', lambda: None)
    ys, xs = guard._candidate_coordinates(pixels, sentinels, coarse, 26, 37)
    assert list(zip(ys, xs)) == expected


def test_native_cap_preserves_fail_closed_threshold(backend, monkeypatch):
    pixels = np.full((32, 32, 3), 200, np.uint8)
    samples = ((0, 0, True, True),)
    ys, xs = native.try_candidates(pixels, samples, 32, 32)
    assert len(xs) == 129
    assert list(zip(ys, xs)) == [(y, x) for y in range(32) for x in range(32)][:129]
    monkeypatch.setattr(native, '_load_backend', lambda: None)
    other_y, other_x = guard._candidate_coordinates(pixels, [((0, 0), True)], np.array([[True]]), 32, 32)
    assert np.array_equal(ys, other_y) and np.array_equal(xs, other_x)


def test_missing_backend_runs_numpy_without_compiler_or_process(example, monkeypatch):
    import subprocess

    panel, frame, layout = example
    monkeypatch.setattr(native, '_load_backend', lambda: None)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('Runtime tried to compile'))
    model = CurrentResourceReader()
    first = model.read(frame, layout)
    assert all(reading.value is not None for reading in first.values())
    duplicate = model.read(add_duplicate(panel, frame), layout)
    assert all(reading.value is None for reading in duplicate.values())
    assert signature(model.read(frame, layout)) == signature(first)


def test_native_and_fallback_preserve_changed_values_and_duplicate_recovery(backend, example, monkeypatch):
    panel, frame, layout = example
    changed = replace_health_with_current_capacity(frame, layout)
    frames = [frame, changed, add_duplicate(panel, changed), changed, Frame(changed.pixels)]
    native_reader = CurrentResourceReader()
    expected = [signature(native_reader.read(current, layout)) for current in frames]
    monkeypatch.setattr(native, '_load_backend', lambda: None)
    fallback_reader = CurrentResourceReader()
    assert [signature(fallback_reader.read(current, layout)) for current in frames] == expected


@pytest.mark.parametrize('failure', ['exception', 'negative_count', 'oversized_count'])
def test_backend_failure_is_not_interpreted_as_absent_panel(example, monkeypatch, failure):
    panel, frame, layout = example

    def broken(*args):
        if failure == 'exception':
            raise RuntimeError('native failure')
        return -1 if failure == 'negative_count' else 130

    monkeypatch.setattr(native, '_load_backend', lambda: broken)
    model = CurrentResourceReader()
    readings = model.read(add_duplicate(panel, frame), layout)
    assert all(reading.value is None for reading in readings.values())


@pytest.mark.parametrize('pixels,samples,width,height', [
    (np.zeros((8, 8, 3), np.uint8), ((0, -1, True, True),), 8, 8),
    (np.zeros((8, 8, 3), np.uint8), ((1, 0, True, True),), 8, 8),
    (np.zeros((8, 8, 3), np.uint8), ((0, 0, True, True),), 9, 8),
    (np.zeros((8, 8, 3), np.uint8), (), 8, 8),
    (np.zeros((8, 8, 3), np.uint8), ((0, 0, True, True),)*21, 8, 8),
    (np.zeros((8, 8, 3), np.float32), ((0, 0, True, True),), 8, 8),
    (np.zeros((8, 8, 3), np.uint8)[::2], ((0, 0, True, True),), 8, 4),
    (np.zeros((8, 8, 3), np.uint8), ((0, 0, True, True),), 7.5, 8),
])
def test_invalid_input_never_reaches_native_pointer_call(monkeypatch, pixels, samples, width, height):
    monkeypatch.setattr(native, '_load_backend', lambda: lambda *args: pytest.fail('Native call'))
    assert native.try_candidates(pixels, samples, width, height) is None


class Function:
    def __init__(self, value):
        self.value = value

    def __call__(self, *args):
        return self.value


@pytest.mark.parametrize('version,identity', [(2, b'expected'), (1, b'old-source')])
def test_loader_rejects_incompatible_abi_or_source(monkeypatch, version, identity):
    class Library:
        visual_guard_abi = Function(version)
        visual_guard_source_id = Function(identity)
        visual_guard_candidates = Function(0)

    monkeypatch.setattr(native.ctypes, 'CDLL', lambda path: Library())
    with pytest.raises(ValueError, match='source and ABI'):
        native._open_backend(Path('ignored-library'), 'expected')


def test_loader_treats_invalid_library_as_optional_fallback(monkeypatch, tmp_path):
    bogus = tmp_path / 'guard.so'
    bogus.write_bytes(b'not a library')
    native._load_backend.cache_clear()
    monkeypatch.setattr(native, 'library_path', lambda: bogus)
    try:
        assert native._load_backend() is None
    finally:
        native._load_backend.cache_clear()


@pytest.mark.parametrize('compiler', [r'C:\Program Files\MSVC\cl.exe', 'clang-cl'])
def test_msvc_command_keeps_paths_literal_and_uses_shared_c11(compiler):
    source, output = r'C:\Project files\guard.c', r'C:\Build files\guard.dll'
    assert compiler_command(compiler, source, output, 'win32') == [
        compiler, '/nologo', '/O2', '/LD', '/std:c11', '/TC', source, '/link', '/OUT:'+output]


@pytest.mark.parametrize('platform_name,flag', [('linux', '-shared'), ('darwin', '-dynamiclib')])
def test_gnu_command_uses_platform_shared_library_flag(platform_name, flag):
    assert compiler_command('clang', 'input path.c', 'output path.so', platform_name) == [
        'clang', '-O3', flag, '-fPIC', '-std=c11', 'input path.c', '-o', 'output path.so']


def test_missing_explicit_build_compiler_has_actionable_error():
    with pytest.raises(RuntimeError, match='Native Tools developer shell'):
        find_compiler('win32', lambda name: None)


def test_missing_library_never_attempts_dynamic_load(monkeypatch, tmp_path):
    native._load_backend.cache_clear()
    monkeypatch.setattr(native, 'library_path', lambda: tmp_path / 'absent.so')
    monkeypatch.setattr(native.ctypes, 'CDLL', lambda path: pytest.fail('Unexpected dynamic load'))
    try:
        assert native._load_backend() is None
    finally:
        native._load_backend.cache_clear()


def test_native_boundary_rejects_unsafe_offsets_and_short_output(backend):
    pixels = np.full((8, 8, 3), 200, np.uint8)
    dy = np.array([0], dtype=np.int32)
    expected = np.array([1], dtype=np.uint8)
    output = np.empty((129, 2), dtype=np.int32)
    for offset, capacity in [(-1, 129), (1, 129), (np.iinfo(np.int32).max, 129), (0, 128)]:
        dx = np.array([offset], dtype=np.int32)
        assert backend(pixels.ctypes.data_as(native._BYTE_POINTER), 8, 8,
            dy.ctypes.data_as(native._INT_POINTER), dx.ctypes.data_as(native._INT_POINTER),
            expected.ctypes.data_as(native._BYTE_POINTER), expected.ctypes.data_as(native._BYTE_POINTER),
            1, 8, 8, output.ctypes.data_as(native._INT_POINTER), capacity) == -1


def test_explicit_existing_build_refreshes_cached_unavailable_state(monkeypatch, tmp_path):
    from tools.build_visual_native import build

    path = tmp_path / 'built-by-another-process.so'
    function = lambda *args: 0
    native._load_backend.cache_clear()
    monkeypatch.setattr(native, 'library_path', lambda: path)
    monkeypatch.setattr(native, '_open_backend', lambda *args: function)
    try:
        assert not native.available()
        path.write_bytes(b'compatible external build; loader mocked for this lifecycle test')
        assert not native.available()  # This process still caches absence.
        assert build(compiler='unused-existing-build') == path
        assert native.available()
    finally:
        native._load_backend.cache_clear()
