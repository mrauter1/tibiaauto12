"""Harness isolation and parity contracts; no expensive discovery benchmark."""
from types import SimpleNamespace
import json
import sys

import pytest

from tools import benchmark_visual_production as benchmark


def test_launches_supplied_checkout_and_interpreter_in_thread_limited_subprocess(tmp_path, monkeypatch):
    checkout = tmp_path / 'archived checkout'
    (checkout / 'tools').mkdir(parents=True)
    (checkout / 'tools/benchmark_resource_reader.py').write_text('')
    calls = []

    def launch(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=7)
    monkeypatch.setattr(benchmark.subprocess, 'run', launch)
    out = tmp_path / 'result.json'
    assert benchmark.main(['--checkout', str(checkout), '--python', '/python with spaces',
                           '--ref', 'base-ref', '--mode', 'correctness', '--out', str(out)]) == 7
    command, options = calls[0]
    assert command[0] == '/python with spaces' and '--child' in command
    assert str(checkout) in command and str(out) in command
    assert options['cwd'] == checkout
    assert options['env']['PYTHONPATH'] == str(checkout)
    assert all(options['env'][name] == '1' for name in benchmark.THREAD_ENV)


def test_discrete_signature_retains_regions_and_unknown_quality():
    row = {'panel_status': 'found', 'fields': {
        'health': {'actual': 710, 'quality': 'known', 'detected_region': [1, 2, 3, 4], 'score': .9},
        'mana': {'actual': None, 'quality': 'unknown', 'detected_region': None},
    }}
    result = benchmark.discrete_outputs(row)
    assert result['fields']['health']['detected_region'] == [1, 2, 3, 4]
    assert result['fields']['mana'] == row['fields']['mana']
    assert 'score' not in result['fields']['health']


def test_archive_without_git_does_not_inherit_outer_repository_revision(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('archive should not query an enclosing repository')
    monkeypatch.setattr(benchmark.subprocess, 'run', unexpected)
    assert benchmark.checkout_revision(tmp_path) is None


def test_timing_selection_remains_eight_predeclared_originals():
    assert len(benchmark.TIMING_IDS) == len(set(benchmark.TIMING_IDS)) == 8
    assert benchmark.TIMING_IDS[0] == 'user-paulistinha-20261013'


@pytest.mark.parametrize('mode,expected_count,warm', [('correctness', 81, 0), ('timing', 8, 3)])
def test_child_protocol_uses_cold_all_originals_or_fixed_timing_subset(
        tmp_path, monkeypatch, mode, expected_count, warm):
    from tools import benchmark_resource_reader, compare_resource_readers, screenshot_split

    cases = [SimpleNamespace(id=name) for name in benchmark.TIMING_IDS]
    cases.extend(SimpleNamespace(id=f'control-{index}') for index in range(73))
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.setattr(benchmark, 'native_metadata', lambda root: {'available': False})
    monkeypatch.setattr(benchmark, 'fingerprints', lambda *args: {'frozen': 'hash'})
    monkeypatch.setattr(benchmark, 'checkout_revision', lambda root: None)
    monkeypatch.setattr(compare_resource_readers, 'load_cases',
                        lambda *args, **kwargs: ({'screenshots': []}, cases))
    monkeypatch.setattr(compare_resource_readers, 'summarize', lambda rows: {'cases': len(rows)})
    monkeypatch.setattr(compare_resource_readers, 'environment', lambda: {'test': True})
    monkeypatch.setattr(screenshot_split, 'load_split',
                        lambda *args: ({}, {case.id: 'test' for case in cases}))
    # Restore this module setting after evaluate changes the probe's loop count.
    monkeypatch.setattr(benchmark_resource_reader, 'BACKGROUND_READS', 100)
    calls = []

    def probe(case, warm_reads):
        calls.append((case.id, warm_reads, benchmark_resource_reader.BACKGROUND_READS))
        return {'case_id': case.id, 'panel_status': 'missing', 'fields': {}}
    monkeypatch.setattr(benchmark_resource_reader, 'probe', probe)
    out = tmp_path / 'output.json'
    args = SimpleNamespace(checkout=tmp_path, out=out, mode=mode, samples=3, ref='archive-ref')
    assert benchmark.evaluate(args) == 0
    result = json.loads(out.read_text())
    assert len(calls) == len(result['cases']) == expected_count
    assert all(count == background == warm for _, count, background in calls)
    assert result['timing_valid'] == (mode == 'timing')
    assert result['supplied_ref'] == 'archive-ref' and result['revision'] is None
    assert len(out.with_suffix('.jsonl').read_text().splitlines()) == expected_count
