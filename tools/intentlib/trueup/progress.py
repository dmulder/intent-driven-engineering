"""Human progress on stderr, including heartbeats while provider calls block."""
import os
import sys
import shutil
import threading
import time


class Progress:
    def __init__(self, total, stream=None, interval=None):
        self.stream = stream if stream is not None else sys.stderr
        self.terminal = self.stream.isatty() and os.environ.get('TERM') != 'dumb'
        self.color = self.terminal and 'NO_COLOR' not in os.environ
        self.interval = interval if interval is not None else (1 if self.terminal else 10)
        self.total = total
        self.counts = dict(success=0, failed=0, partial=0)
        self.phase = 'preparing'
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def __enter__(self):
        self.render()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()
        return self

    def _heartbeat(self):
        while not self.stop.wait(self.interval):
            self.render()

    def waiting(self, phase):
        with self.lock:
            self.phase = phase

    def completed(self, rec):
        statuses = [rec.get(s, {}).get('status') for s in ('inspection', 'external', 'analysis', 'link')]
        if rec.get('analysis', {}).get('status') in ('analyzed', 'partial') and rec.get('link', {}).get('status') == 'pending':
            statuses.append('partial')
        outcome = 'failed' if 'failed' in statuses else 'partial' if 'partial' in statuses or 'stale' in statuses else 'success'
        with self.lock:
            self.counts[outcome] += 1
        self.render()

    def render(self, final=False):
        with self.lock:
            c = self.counts
            done = sum(c.values())
            columns = shutil.get_terminal_size((80, 24)).columns
            width = min(24, max(4, columns // 6)) if self.terminal else 24
            sizes = {k: max(1, c[k] * width // max(1, self.total)) if c[k] else 0 for k in c}
            # Reserve a visible mark even for a single failure in a large run.
            while sum(sizes.values()) > width:
                sizes[max(sizes, key=sizes.get)] -= 1
            bar = ''
            for key, mark, color in (('success', '+', 32), ('failed', 'x', 31), ('partial', '~', 33)):
                marks = mark * sizes[key]
                bar += f'\033[{color}m{marks}\033[0m' if self.color and marks else marks
            occupied = sum(sizes.values())
            bar += '.' * (width - occupied)
            elapsed = int(time.monotonic() - self.started)
            message = (f'[{bar}] {done}/{self.total} processed | '
                       f'{c["success"]} succeeded, {c["failed"]} failed, {c["partial"]} partial | '
                       f'{self.total - done} remaining | {elapsed}s | {self.phase}')
            if self.terminal:
                summary = f'{done}/{self.total} ok:{c["success"]} fail:{c["failed"]} part:{c["partial"]} {elapsed}s'
                room = max(0, columns - width - len(summary) - 5)
                message = f'[{bar}] {summary} {self.phase[:room]}'
            prefix = '\r\033[2K' if self.terminal else ''
            self.stream.write(prefix + message + ('\n' if final or not self.terminal else ''))
            self.stream.flush()

    def __exit__(self, exc_type, exc, tb):
        self.stop.set()
        self.thread.join()
        self.waiting('stopped' if exc_type else 'run finished')
        self.render(final=True)
