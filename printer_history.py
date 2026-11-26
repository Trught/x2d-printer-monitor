"""Bounded, indexable history without copying every sample on eviction."""
from __future__ import annotations

from collections.abc import Sequence
from itertools import chain, islice


class RingSeries(Sequence):
    """O(1) append and indexing; bisect and ordinary sequence slices work."""

    def __init__(self, capacity: int, values=()):
        if capacity <= 0:
            raise ValueError("History capacity must be positive")
        self.capacity = capacity
        self._values = [None] * capacity
        self._start = self._size = 0
        for value in values:
            self.append(value)

    def __len__(self):
        return self._size

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self._size))]
        if index < 0:
            index += self._size
        if not 0 <= index < self._size:
            raise IndexError("History index out of range")
        return self._values[(self._start + index) % self.capacity]

    def __iter__(self):
        return self.iter_window(0, self._size)

    def iter_window(self, start, stop):
        start, stop, _ = slice(start, stop).indices(self._size)
        count = max(0, stop - start)
        physical = (self._start + start) % self.capacity
        first = min(count, self.capacity - physical)
        return chain(islice(self._values, physical, physical + first),
                     islice(self._values, 0, count - first))

    def append(self, value):
        self._values[(self._start + self._size) % self.capacity] = value
        if self._size < self.capacity:
            self._size += 1
        else:
            self._start = (self._start + 1) % self.capacity


def iter_window(series, start, stop):
    """Iterate only a visible interval without repeated modular indexing."""
    if isinstance(series, RingSeries):
        return series.iter_window(start, stop)
    return islice(series, start, stop)
