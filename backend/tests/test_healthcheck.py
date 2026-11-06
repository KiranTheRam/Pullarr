import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import Mock

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "docker" / "healthcheck.py"
spec = importlib.util.spec_from_file_location("healthcheck", SCRIPT)
healthcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(healthcheck)


@pytest.mark.parametrize("host,target", [
    ("", "127.0.0.1"), ("0.0.0.0", "127.0.0.1"), ("::", "::1"),
    ("0:0:0:0:0:0:0:0", "::1"), ("127.0.0.2", "127.0.0.2"),
    ("::1", "::1"), ("192.0.2.2", "192.0.2.2"), ("listener", "listener"),
])
def test_probe_uses_listener_and_only_trusted_host(monkeypatch, host, target):
    monkeypatch.setenv("PULLARR_HOST", host)
    monkeypatch.setenv("PULLARR_PORT", "8123")
    factory = Mock()
    connection = factory.return_value
    connection.getresponse.return_value.status = 200
    connection.getresponse.return_value.read.return_value = b'{"status":"OK"}'
    monkeypatch.setattr(healthcheck.http.client, "HTTPConnection", factory)
    assert healthcheck.check() == 0
    factory.assert_called_once_with(target, 8123, timeout=4)
    connection.request.assert_called_once_with("GET", "/ping", headers={"Host": "localhost"})
    connection.close.assert_called_once()


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        status = 200
        body = b'{"status":"OK"}'
        requests = []

        def do_GET(self):
            self.requests.append((self.path, self.headers["Host"], self.headers.get("X-Api-Key")))
            self.send_response(self.status)
            if self.status == 302:
                self.send_header("Location", "/login")
            self.end_headers()
            self.wfile.write(self.body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, Handler
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def run_probe(port, **env):
    return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT)], capture_output=True, timeout=6,
                          env={**os.environ, "PULLARR_HOST": "127.0.0.1", "PULLARR_PORT": str(port), **env})


def test_real_probe_bypasses_all_proxy_environment_and_does_not_send_secrets(server):
    httpd, handler = server
    env = {name: "http://127.0.0.1:1" for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")}
    result = run_probe(httpd.server_port, NO_PROXY="", no_proxy="", PULLARR_API_KEY="never-send-this", **env)
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""
    assert handler.requests == [("/ping", "localhost", None)]


@pytest.mark.parametrize("status,body", [(302, b'{"status":"OK"}'), (401, b"secret-error"), (500, b"secret-error"),
                                         (200, b"<html>login</html>"), (200, b'{"status":"DOWN"}'),
                                         (200, b'{"status":"OK","secret":"no"}'), (200, b" " * 70 + b'{"status":"OK"}'),
                                         (200, b'{"status":"OK"}' + b" " * 70)])
def test_failed_responses_and_redirects_are_silent_failures(server, status, body):
    httpd, handler = server
    handler.status, handler.body = status, body
    result = run_probe(httpd.server_port)
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""
    assert len(handler.requests) == 1


@pytest.mark.parametrize("port", ["0", "65536", "-1", "not-a-port"])
def test_invalid_configuration_is_silent_failure(port):
    result = run_probe(port)
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""


def test_connection_failure_is_silent(server):
    httpd, _ = server
    port = httpd.server_port
    httpd.shutdown()
    httpd.server_close()
    result = run_probe(port)
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""
