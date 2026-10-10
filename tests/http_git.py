"""测试用 HTTP Git 服务端：wsgiref 经 CGI 调用 git http-backend，要求 Basic 认证。"""

import base64
import os
import subprocess
import threading
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

USER, PASSWORD = "tester", "s3cret-pw-9f2"
_EXPECTED = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


class _Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class _Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def serve(root: Path) -> tuple[_Server, str]:
    def app(environ, start_response):
        if environ.get("HTTP_AUTHORIZATION") != _EXPECTED:
            start_response("401 Unauthorized", [("WWW-Authenticate", 'Basic realm="t"')])
            return [b""]
        body = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")} | {
            "GIT_PROJECT_ROOT": str(root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REMOTE_USER": USER,
            "PATH_INFO": environ["PATH_INFO"],
            "QUERY_STRING": environ.get("QUERY_STRING", ""),
            "REQUEST_METHOD": environ["REQUEST_METHOD"],
            "CONTENT_TYPE": environ.get("CONTENT_TYPE", ""),
            "CONTENT_LENGTH": str(len(body)),
            "HTTP_CONTENT_ENCODING": environ.get("HTTP_CONTENT_ENCODING", ""),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.receivepack",
            "GIT_CONFIG_VALUE_0": "true",
        }
        out = subprocess.run(["git", "http-backend"], input=body, capture_output=True, env=env)
        head, _, payload = out.stdout.partition(b"\r\n\r\n")
        headers = [tuple(h.split(": ", 1)) for h in head.decode().split("\r\n")]
        status = dict(headers).get("Status", "200 OK")
        start_response(status, [h for h in headers if h[0] != "Status"])
        return [payload]

    server = make_server("127.0.0.1", 0, app, server_class=_Server, handler_class=_Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"
