"""Thread-safe bounded FIFO for passing gesture events core0 -> core1.

Fixed-size ring buffer guarded by a lock. When full, the OLDEST entry is dropped
(so the freshest button activity always survives) and a dropped-counter increments
for observability. Preallocated storage -> no per-event allocation in the hot path.

Pure w.r.t. hardware: the lock is injected. On device pass
`_thread.allocate_lock()`; tests pass a dummy lock.
"""


class _NullLock:
    """Context-manager no-op lock for single-threaded host tests."""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def acquire(self):
        return True

    def release(self):
        pass


class EventQueue:
    def __init__(self, size, lock=None):
        if size < 1:
            size = 1
        self._buf = [None] * size
        self._size = size
        self._head = 0          # next write index
        self._count = 0
        self._dropped = 0
        self._lock = lock if lock is not None else _NullLock()

    def put(self, item):
        """Enqueue. Returns True if stored cleanly, False if it displaced an item."""
        with self._lock:
            displaced = False
            if self._count == self._size:
                # full: advance tail by overwriting oldest
                self._dropped += 1
                displaced = True
                self._buf[self._head] = item
                self._head = (self._head + 1) % self._size
                # count stays == size; tail implicitly moved
            else:
                self._buf[self._head] = item
                self._head = (self._head + 1) % self._size
                self._count += 1
            return not displaced

    def get(self):
        """Dequeue oldest, or None if empty."""
        with self._lock:
            if self._count == 0:
                return None
            tail = (self._head - self._count) % self._size
            item = self._buf[tail]
            self._buf[tail] = None
            self._count -= 1
            return item

    def __len__(self):
        with self._lock:
            return self._count

    @property
    def dropped(self):
        with self._lock:
            return self._dropped
