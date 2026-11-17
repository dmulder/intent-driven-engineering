"""Progress must remain visible while a worker is blocked and preserve machine output."""
import io
import os
import re
import threading
import unittest
from unittest.mock import patch

from trueup_support import TrueUpTest
from intentlib.trueup.progress import Progress


class RecordingStream(io.StringIO):
    def __init__(self, terminal=False):
        super().__init__()
        self.terminal = terminal
        self.heartbeat = threading.Event()
        self.writes = 0

    def isatty(self):
        return self.terminal

    def write(self, text):
        result = super().write(text)
        self.writes += 1
        if self.writes >= 2:
            self.heartbeat.set()
        return result


class ProgressTests(unittest.TestCase):
    def test_heartbeat_without_completed_work_and_cleanup(self):
        stream = RecordingStream()
        progress = Progress(811, stream, interval=0.01)
        progress.waiting('waiting for abc123')
        with progress:
            self.assertTrue(stream.heartbeat.wait(2))
            self.assertIn('0/811 processed', stream.getvalue())
            self.assertIn('waiting for abc123', stream.getvalue())
        self.assertFalse(progress.thread.is_alive())
        self.assertNotIn('\033', stream.getvalue())
        self.assertIn('run finished', stream.getvalue())

    def test_outcomes_color_and_no_color(self):
        for no_color in (False, True):
            stream = RecordingStream(terminal=True)
            with patch.dict('os.environ', {'TERM': 'xterm'}, clear=True):
                if no_color:
                    os.environ['NO_COLOR'] = '1'
                with Progress(3, stream) as progress:
                    progress.completed({'analysis': {'status': 'analyzed'}})
                    progress.completed({'link': {'status': 'failed'}})
                    progress.completed({'analysis': {'status': 'partial'}})
            text = stream.getvalue()
            self.assertIn('3/3', text)
            self.assertIn('ok:1 fail:1 part:1', text)
            self.assertEqual('\033[31m' in text, not no_color)
            self.assertTrue(text.endswith('\n'))

    def test_terminal_frames_fit_narrow_widths_with_and_without_color(self):
        for columns in (1, 2, 5, 8, 12, 20, 40, 80, 120):
            for no_color in (False, True):
                with self.subTest(columns=columns, no_color=no_color):
                    stream = RecordingStream(terminal=True)
                    env = {'TERM': 'xterm', **({'NO_COLOR': '1'} if no_color else {})}
                    with patch.dict('os.environ', env, clear=True), patch(
                            'intentlib.trueup.progress.shutil.get_terminal_size',
                            return_value=os.terminal_size((columns, 24))):
                        progress = Progress(811, stream)
                        progress.waiting('waiting for abc123 (provider or local work)')
                        progress.render()
                        progress.counts.update(success=500, failed=200, partial=111)
                        progress.render(final=True)
                    text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', stream.getvalue())
                    for line in text.splitlines():
                        self.assertLessEqual(len(line), max(0, columns - 1))
                    if columns == 120:
                        self.assertIn('ok:500 fail:200 part:111', text)

    def test_exception_closes_display(self):
        stream = RecordingStream()
        with self.assertRaises(RuntimeError):
            with Progress(0, stream) as progress:
                raise RuntimeError('test')
        self.assertFalse(progress.thread.is_alive())
        self.assertIn('stopped', stream.getvalue())


class ProgressIntegration(TrueUpTest):
    def test_run_reports_outcomes_on_stderr_with_valid_json_stdout(self):
        self.small_history()
        self.init()
        code, out, err = self.tu('run', '--no-ai', '--json')
        import json
        json.loads(out)
        self.assertIn('processed', err)
        self.assertIn('succeeded', err)
        self.assertNotIn('\033', err)
