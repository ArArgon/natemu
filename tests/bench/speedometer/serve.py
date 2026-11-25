# The Speedometer cases' guest web server (tests/bench/run_speedometer.py): serves /sp on 127.0.0.1:8080 and prints each
# POST of hook.js as one console line, "SP <path> <body>"; exits after /done (the results or an error).
import functools, http.server, os


class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0))).decode(errors='replace')
        self.send_response(204); self.end_headers()
        print(f'SP {self.path} {body}', flush=True)
        if self.path == '/done': os._exit(0)


http.server.ThreadingHTTPServer(('127.0.0.1', 8080), functools.partial(H, directory='/sp')).serve_forever()
