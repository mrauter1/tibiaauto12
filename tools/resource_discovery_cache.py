"""Bounded exact-evidence reuse for the production label searches.

Only complete label-search results are retained. A hit requires the same full
binary search image, frame/box/field selection, search mode, configuration and
relevant template-bank bytes. The ordinary discovery path still reconstructs
current geometry, decides uniqueness, and decodes current resource pixels.

This deliberately keeps all font families and scales in the search contract.
It is useful for repeated discovery on unchanged label evidence, not a shortcut
that treats a previously selected panel as globally unique on another frame.
"""
from __future__ import annotations

from collections import OrderedDict
from hashlib import sha256

from tibia_client.adaptive import _crop, _ink


_BANKS = {
    "primary": ("_templates", "_raw_templates"),
    "native": ("_native_templates",),
    "alternate": ("_alternate_templates", "_alternate_raw_templates"),
}


class ExactDiscoveryEvidenceCacheMixin:
    """Compose before the existing reader; zero in either limit disables reuse.

    The byte limit accounts for retained image and template payloads. Small key
    and anchor metadata is separately bounded by the entry limit. No Frame or
    mutable image buffer is retained. Cache access follows the reader's existing
    single-owner convention; discovery and current reads use separate instances.
    """

    def __init__(self, *args, discovery_cache_entries=24,
                 discovery_cache_bytes=32 * 1024 * 1024, **kwargs):
        for name, value in (("discovery_cache_entries", discovery_cache_entries),
                            ("discovery_cache_bytes", discovery_cache_bytes)):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        self._discovery_cache_entries = discovery_cache_entries
        self._discovery_cache_limit = discovery_cache_bytes
        self._discovery_search_cache = OrderedDict()
        self.discovery_cache_stats = dict(hits=0, misses=0, evictions=0,
                                          bypasses=0, bytes=0)
        super().__init__(*args, **kwargs)

    def clear_discovery_cache(self):
        """Discard retained evidence; cumulative hit/miss counters remain useful."""
        self._discovery_search_cache.clear()
        self.discovery_cache_stats["bytes"] = 0

    def _discovery_configuration(self, family):
        # Arrays can be replaced or edited in place by custom reader setup. An
        # exact snapshot invalidates both cases; object identity is insufficient.
        banks = tuple((name, tuple((key, image.shape, image.dtype.str, image.tobytes())
                                  for key, image in getattr(self, name).items()))
                      for name in _BANKS[family])
        configuration = (tuple(sorted(self.features)), tuple(self.label_scales))
        payload_size = sum(len(entry[3]) for _, entries in banks for entry in entries)
        return (configuration, banks), payload_size

    def _cached_label_search(self, family, method, frame, box, checkpoint, fields, exhaustive):
        checkpoint()
        if (not self._discovery_cache_entries or not self._discovery_cache_limit
                or (box[2] - box[0]) * (box[3] - box[1]) > self._discovery_cache_limit):
            self.discovery_cache_stats["bypasses"] += 1
            return method(frame, box, checkpoint, fields=fields, exhaustive=exhaustive)

        evidence = _ink(_crop(frame, box)).tobytes()
        configuration, bank_bytes = self._discovery_configuration(family)
        payload_size = len(evidence) + bank_bytes
        if payload_size > self._discovery_cache_limit:
            self.discovery_cache_stats["bypasses"] += 1
            return method(frame, box, checkpoint, fields=fields, exhaustive=exhaustive)
        key = (family, frame.size, tuple(box),
               tuple(sorted(fields)) if fields is not None else None,
               bool(exhaustive), configuration, method.__func__, sha256(evidence).digest())
        cached = self._discovery_search_cache.get(key)
        if cached is not None and cached[0] == evidence:
            # Hashes index entries only. Equality of the retained complete bytes
            # is required before returning an immutable search result's copy.
            checkpoint()
            self.discovery_cache_stats["hits"] += 1
            self._discovery_search_cache.move_to_end(key)
            return list(cached[1])

        self.discovery_cache_stats["misses"] += 1
        # Materialization and the final checkpoint must both succeed. A reader
        # raising partway through its search cannot install a partial result.
        result = tuple(method(frame, box, checkpoint, fields=fields, exhaustive=exhaustive))
        checkpoint()
        current_configuration, _ = self._discovery_configuration(family)
        if current_configuration != configuration:
            return list(result)
        old = self._discovery_search_cache.pop(key, None)
        if old is not None:
            self.discovery_cache_stats["bytes"] -= old[2]
        self._discovery_search_cache[key] = (evidence, result, payload_size)
        self.discovery_cache_stats["bytes"] += payload_size
        while (len(self._discovery_search_cache) > self._discovery_cache_entries
               or self.discovery_cache_stats["bytes"] > self._discovery_cache_limit):
            _, (_, _, removed_size) = self._discovery_search_cache.popitem(last=False)
            self.discovery_cache_stats["bytes"] -= removed_size
            self.discovery_cache_stats["evictions"] += 1
        return list(result)

    def _anchors(self, frame, box, checkpoint, *, fields=None, exhaustive=False):
        return self._cached_label_search("primary", super()._anchors, frame, box,
                                         checkpoint, fields, exhaustive)

    def _native_anchors(self, frame, box, checkpoint, *, fields=None, exhaustive=False):
        return self._cached_label_search("native", super()._native_anchors, frame, box,
                                         checkpoint, fields, exhaustive)

    def _alternate_anchors(self, frame, box, checkpoint, *, fields=None, exhaustive=False):
        return self._cached_label_search("alternate", super()._alternate_anchors, frame, box,
                                         checkpoint, fields, exhaustive)
