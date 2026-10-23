"""Production discovery evidence reuse keeps complete current-image semantics."""
from dataclasses import replace

import numpy as np
import pytest

from core.perception import Frame
from tibia_client.adaptive import Anchor
from tools.resource_discovery_cache import ExactDiscoveryEvidenceCacheMixin
from tools.resource_numeric_vector import VectorNumberReader
from tools.resource_panel_guard import PanelGuardMixin
from tools.resource_reader_experiments import ExperimentalResourceReader, VARIANTS
from tools.resource_row_geometry import StructuralRowMixin
from test_resource_panel_guard import (  # noqa: F401
    example, add_duplicate, replace_health_with_current_capacity, signature,
)


class OriginalReader(PanelGuardMixin, StructuralRowMixin, ExperimentalResourceReader):
    def __init__(self, **kwargs):
        features = VARIANTS['accelerated_robust']
        super().__init__(features, number_reader=VectorNumberReader(features), **kwargs)


class CachedReader(ExactDiscoveryEvidenceCacheMixin, OriginalReader):
    pass


class ProbeSearches:
    def __init__(self):
        self.features = frozenset(('labels',))
        self.label_scales = (1.0,)
        self.calls = []
        self.fail = False
        self.partial = False
        self.mutate = None
        for name in ('_templates', '_raw_templates', '_native_templates',
                     '_alternate_templates', '_alternate_raw_templates'):
            setattr(self, name, {'resource_health': np.eye(3, dtype=np.uint8) * 255})

    def _search(self, name, frame, box, checkpoint, *, fields, exhaustive):
        self.calls.append((name, frame.size, box, fields, exhaustive))
        if self.fail:
            raise RuntimeError('incomplete search')
        if self.mutate is not None:
            self.mutate()
        anchor = Anchor('resource_health', (1, 1, 4, 4), .9, 1., name)
        if self.partial:
            def unfinished():
                yield anchor
                raise RuntimeError('partially yielded search')
            return unfinished()
        return [anchor]

    def _anchors(self, frame, box, checkpoint, *, fields=None, exhaustive=False):
        return self._search('primary', frame, box, checkpoint,
                            fields=fields, exhaustive=exhaustive)

    def _native_anchors(self, frame, box, checkpoint, *, fields=None, exhaustive=False):
        return self._search('native', frame, box, checkpoint,
                            fields=fields, exhaustive=exhaustive)

    def _alternate_anchors(self, frame, box, checkpoint, *, fields=None, exhaustive=False):
        return self._search('alternate', frame, box, checkpoint,
                            fields=fields, exhaustive=exhaustive)


class Probe(ExactDiscoveryEvidenceCacheMixin, ProbeSearches):
    pass


def blank(size=(20, 30), value=42):
    return Frame(np.full((*size, 3), value, np.uint8))


def search(reader, frame=None, **kwargs):
    frame = frame or blank()
    return reader._anchors(frame, (0, 0, frame.width, frame.height), lambda: None, **kwargs)


def test_full_binary_evidence_allows_only_equivalent_pixels():
    reader = Probe()
    first = search(reader)
    assert search(reader, blank(value=43)) == first
    assert len(reader.calls) == 1
    pixels = blank().pixels.copy()
    pixels[-1, -1] = 255  # Outside the selected label but inside full search evidence.
    search(reader, Frame(pixels))
    assert len(reader.calls) == 2
    assert reader.discovery_cache_stats['hits'] == 1


def test_search_identity_includes_frame_box_fields_mode_and_family():
    reader = Probe()
    frame = blank()
    box = (0, 0, frame.width, frame.height)
    search(reader, frame)
    search(reader, frame, fields=frozenset(('resource_health',)))
    search(reader, frame, fields=frozenset(('resource_health',)), exhaustive=True)
    reader._anchors(frame, (0, 0, 29, 20), lambda: None)
    bigger = blank((21, 30))
    reader._anchors(bigger, box, lambda: None)  # Same box/evidence, different frame.
    reader._native_anchors(frame, box, lambda: None)
    reader._alternate_anchors(frame, box, lambda: None)
    assert len(reader.calls) == 7
    search(reader, frame)
    assert len(reader.calls) == 7


@pytest.mark.parametrize('bank', ['_templates', '_raw_templates'])
def test_primary_template_replacement_and_mutation_invalidate(bank):
    reader = Probe()
    search(reader)
    getattr(reader, bank)['resource_health'][0, 0] ^= 255
    search(reader)
    getattr(reader, bank)['resource_health'] = np.zeros((4, 4), dtype=np.uint8)
    search(reader)
    assert len(reader.calls) == 3


@pytest.mark.parametrize('method,bank', [('_native_anchors', '_native_templates'),
    ('_alternate_anchors', '_alternate_templates'),
    ('_alternate_anchors', '_alternate_raw_templates')])
def test_other_template_banks_are_in_search_identity(method, bank):
    reader = Probe()
    frame = blank()
    invoke = getattr(reader, method)
    invoke(frame, (0, 0, 30, 20), lambda: None)
    getattr(reader, bank)['resource_health'][0, 0] ^= 255
    invoke(frame, (0, 0, 30, 20), lambda: None)
    assert len(reader.calls) == 2


def test_features_and_scale_bank_changes_invalidate():
    reader = Probe()
    search(reader)
    reader.features = frozenset(('other',))
    search(reader)
    reader.label_scales = (.5, 1.)
    search(reader)
    assert len(reader.calls) == 3


def test_hash_collision_cannot_reuse_different_evidence(monkeypatch):
    class SameDigest:
        def digest(self):
            return b'collision'
    monkeypatch.setattr('tools.resource_discovery_cache.sha256', lambda data: SameDigest())
    reader = Probe()
    search(reader)
    search(reader, blank(value=255))
    assert len(reader.calls) == 2
    assert reader.discovery_cache_stats['bytes'] == 600 + 18
    assert len(reader._discovery_search_cache) == 1


def test_entry_and_payload_byte_budgets_evict_and_skip_oversized_images():
    reader = Probe(discovery_cache_entries=2, discovery_cache_bytes=1000)
    search(reader)  # 600 evidence bytes plus 18 bank bytes.
    search(reader, blank(value=255))
    assert len(reader._discovery_search_cache) == 1
    assert reader.discovery_cache_stats['bytes'] == 618
    assert reader.discovery_cache_stats['evictions'] == 1
    search(reader, blank((40, 40)))
    assert len(reader._discovery_search_cache) == 1
    assert reader.discovery_cache_stats['bypasses'] == 1
    bounded = Probe(discovery_cache_entries=1, discovery_cache_bytes=5000)
    search(bounded)
    search(bounded, blank(value=255))
    assert len(bounded._discovery_search_cache) == 1
    assert bounded.discovery_cache_stats['evictions'] == 1
    bounded.clear_discovery_cache()
    assert not bounded._discovery_search_cache
    assert bounded.discovery_cache_stats['bytes'] == 0


def test_mutating_diagnostic_snapshots_cannot_change_cache_accounting():
    reader = Probe(discovery_cache_entries=2, discovery_cache_bytes=1000)
    search(reader)
    snapshot = reader.discovery_cache_stats
    snapshot['bytes'] = 0
    reader.discovery_cache_stats.clear()
    with pytest.raises(AttributeError):
        reader.discovery_cache_stats = {}
    search(reader, blank(value=255))
    assert len(reader._discovery_search_cache) == 1
    assert sum(entry[2] for entry in reader._discovery_search_cache.values()) == 618
    assert reader.discovery_cache_stats['bytes'] == 618
    assert reader.discovery_cache_stats['evictions'] == 1
    search(reader, blank(value=255))
    assert reader.discovery_cache_stats['hits'] == 1
    assert len(reader.calls) == 2


@pytest.mark.parametrize('kwargs', [{'discovery_cache_entries': 0}, {'discovery_cache_bytes': 0}])
def test_zero_limit_is_an_uncached_comparator_path(kwargs):
    reader = Probe(**kwargs)
    search(reader)
    search(reader)
    assert len(reader.calls) == 2
    assert not reader._discovery_search_cache
    assert reader.discovery_cache_stats['bypasses'] == 2


@pytest.mark.parametrize('bad', [-1, True, 1.5])
def test_invalid_limits_rejected(bad):
    with pytest.raises(ValueError):
        Probe(discovery_cache_entries=bad)
    with pytest.raises(ValueError):
        Probe(discovery_cache_bytes=bad)


def test_returned_list_mutation_cannot_poison_cached_result():
    reader = Probe()
    result = search(reader)
    expected = result[:]
    result.clear()
    cached = search(reader)
    assert cached == expected
    cached.append(replace(cached[0], field='other'))
    assert search(reader) == expected


@pytest.mark.parametrize('failure', ['fail', 'partial'])
def test_failed_or_partially_yielded_search_is_never_stored(failure):
    reader = Probe()
    setattr(reader, failure, True)
    with pytest.raises(RuntimeError):
        search(reader)
    assert not reader._discovery_search_cache
    setattr(reader, failure, False)
    search(reader)
    assert len(reader.calls) == 2


def test_cancellation_at_return_prevents_install_and_hit_checks_deadline():
    reader = Probe()
    calls = 0
    def cancelled_after_search():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError('deadline')
    with pytest.raises(TimeoutError):
        reader._anchors(blank(), (0, 0, 30, 20), cancelled_after_search)
    assert not reader._discovery_search_cache
    search(reader)
    def cancel():
        raise TimeoutError('deadline')
    with pytest.raises(TimeoutError):
        reader._anchors(blank(), (0, 0, 30, 20), cancel)
    assert reader.discovery_cache_stats['hits'] == 0


def test_configuration_mutated_during_search_is_not_cached():
    reader = Probe()
    reader.mutate = lambda: setattr(reader, 'label_scales', (.5,))
    search(reader)
    assert not reader._discovery_search_cache


def test_real_repeat_reuses_labels_but_rechecks_current_geometry(example, monkeypatch):
    _, frame, _ = example
    baseline = OriginalReader()
    original = baseline.discover(frame)
    reader = CachedReader()
    first = reader.discover(frame)
    assert first.layout == original.layout
    calls = []
    layout = reader._layout
    def counted(current, anchors):
        calls.append(current)
        return layout(current, anchors)
    monkeypatch.setattr(reader, '_layout', counted)
    second = reader.discover(Frame(frame.pixels))
    assert calls
    assert reader.discovery_cache_stats['hits'] > 0
    assert second.layout == original.layout
    assert signature(reader.read(frame, second.layout)) == signature(baseline.read(frame, original.layout))


def test_real_remote_duplicate_and_changed_digits_never_reuse_stale_values(example):
    panel, frame, _ = example
    reader = CachedReader()
    first = reader.discover(frame)
    changed = replace_health_with_current_capacity(frame, first.layout)
    update = reader.discover(changed)
    assert update.layout is not None
    assert reader.read(changed, update.layout)['resource_health'].value['count'] == 192
    duplicate = reader.discover(add_duplicate(panel, frame))
    assert duplicate.layout is None and duplicate.candidates


def test_current_geometry_can_reject_even_when_label_search_hits(example, monkeypatch):
    _, frame, _ = example
    reader = CachedReader()
    assert reader.discover(frame).layout is not None
    monkeypatch.setattr(reader, '_layout', lambda *args: None)
    result = reader.discover(Frame(frame.pixels))
    assert reader.discovery_cache_stats['hits'] > 0
    assert result.layout is None


def test_template_iteration_order_changes_invalidate_tie_order():
    reader = Probe()
    reader._templates['resource_mana'] = np.eye(3, dtype=np.uint8) * 255
    search(reader)
    reader._templates = dict(reversed(list(reader._templates.items())))
    search(reader)
    assert len(reader.calls) == 2
