"""Small authenticated HTTP control service and board. Default bind is loopback."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import db as DB
from . import team


class Handler(BaseHTTPRequestHandler):
    server_version = 'Plat'

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        pass  # never log bearer credentials or request bodies

    def reply(self, code, value, content_type='application/json'):
        body = value.encode() if isinstance(value, str) else json.dumps(value, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')

    def handle_request(self, method):
        url = urlsplit(self.path)
        if method == 'GET' and url.path in ('/', '/board.js', '/board.css'):
            file = {'/': 'board.html', '/board.js': 'board.js', '/board.css': 'board.css'}[url.path]
            content = {'/': 'text/html; charset=utf-8', '/board.js': 'application/javascript', '/board.css': 'text/css'}[url.path]
            self.reply(200, (Path(__file__).parent / 'web' / file).read_text(), content)
            return
        try:
            auth = self.headers.get('Authorization', '')
            body = None
            if method == 'POST':
                size = int(self.headers.get('Content-Length', '0'))
                if size <= 0 or size > 65536:
                    raise ValueError('request body must be 1–65536 bytes')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError('command must be an object')
            with DB.session() as db:
                joining = method == 'POST' and url.path == '/api/join'
                member = None if joining else team.authenticate(db, auth[7:] if auth.startswith('Bearer ') else '')
                if joining:
                    result = team.join(db, body.get('code'), body.get('token'))
                elif method == 'POST' and url.path == '/api/invitations':
                    result = team.invite(db, member, body.get('name'), body.get('role', 'engineer'), body.get('hours', 24))
                elif method == 'POST' and url.path == '/api/invitations/revoke':
                    result = team.revoke_invitation(db, member, body.get('id'))
                elif method == 'GET' and url.path == '/api/board':
                    result = {**team.board(db), 'member': {'name': member.name, 'role': member.role}}
                elif method == 'GET' and url.path == '/api/events':
                    after = int(parse_qs(url.query).get('after', ['0'])[0])
                    if after < 0:
                        raise ValueError('cursor must be nonnegative')
                    result = team.events(db, after)
                elif method == 'POST' and url.path == '/api/commands':
                    result = team.command(db, member, body)
                else:
                    self.reply(404, {'error': 'route not found'})
                    return
            self.reply(200, result)  # only acknowledge after transaction commit
        except team.Forbidden as exc:
            self.reply(403, {'error': str(exc)})
        except team.Conflict as exc:
            self.reply(409, {'error': str(exc)})
        except (ValueError, KeyError) as exc:
            self.reply(400, {'error': str(exc)})
        except Exception:
            self.reply(500, {'error': 'operation failed; no successful transition was acknowledged'})


def serve(host='127.0.0.1', port=7318):
    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()
