"""Loopback-only integration for bounded notification HTTP delivery; no agents."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kindex.config import Config
from kindex.notify import dispatch


@pytest.fixture
def endpoint():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, payload, self.headers['Content-Type']))
            if self.path == '/slow':
                # Every byte arrives inside the socket inactivity timeout, but
                # the overall request must still end at output_timeout.
                try:
                    for byte in b'HTTP/1.1 200 OK\r\nX-Slow: endless\r\n\r\n':
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.1)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            status = int(self.path.split('?', 1)[0].strip('/'))
            self.send_response(status)
            if status == 302:
                self.send_header('Location', '/200')
            self.send_header('Content-Length', '0')
            self.end_headers()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', calls
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.parametrize('status,success', [(200, True), (204, True), (302, False), (503, False)])
def test_post_wire_status_and_redirect_refusal(endpoint, tmp_path, status, success):
    base, calls = endpoint
    cfg = Config(data_dir=str(tmp_path))
    cfg.reminders.output_destination = f'{base}/{status}?token=synthetic-local-token'
    result = dispatch({'id': 'r1', 'title': 'Synthetic build notice', 'next_due': '2099-01-01'}, cfg)[0]
    assert result.success is success
    assert len(calls) == 1  # No redirect, retry, or duplicate effect.
    path, payload, content_type = calls[0]
    assert path.endswith('?token=synthetic-local-token')
    assert content_type == 'application/json'
    assert payload == {'id': 'r1', 'text': 'Reminder due: Synthetic build notice\nReminder ID: r1',
                       'priority': 'normal', 'due': '2099-01-01'}
    assert 'synthetic-local-token' not in result.message


def test_trickling_headers_cannot_extend_total_deadline(endpoint, tmp_path):
    base, calls = endpoint
    cfg = Config(data_dir=str(tmp_path))
    cfg.reminders.output_destination = base + '/slow'
    cfg.reminders.output_timeout = 0.4
    start = time.monotonic()
    result = dispatch({'title': 'Synthetic timeout notice'}, cfg)[0]
    elapsed = time.monotonic() - start
    assert not result.success
    assert 'unconfirmed' in result.message
    assert 0.3 <= elapsed < 2
    assert len(calls) == 1
