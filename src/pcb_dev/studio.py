"""Small, authenticated HTTP surface for the explicitly invoked project studio.

The browser never receives provider credentials. Model calls start only after a
human POST, run outside the HTTP request, and expose detached cached snapshots.
This server is intended for loopback or an explicitly configured HTTPS proxy.
"""

from __future__ import annotations

from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import re
import socket
import stat
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit


MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_MESSAGE_CHARS = 16_000
WEB_DIRECTORY = Path(__file__).with_name("web")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
       "form-action 'self'")


def _origin(value: str) -> str:
    if type(value) is not str or any(char.isspace() for char in value):
        raise ValueError("public_origin must be an explicit HTTPS origin")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid public_origin port") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("public_origin must be an explicit HTTPS origin without a path")
    hostname = parsed.hostname.lower()
    authority = f"[{hostname}]" if ":" in hostname else hostname
    return "https://" + authority + (f":{port}" if port not in (None, 443) else "")


def _json_bytes(value: Any) -> bytes:
    result = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(result) > MAX_RESPONSE_BYTES:
        raise ValueError("Studio snapshot exceeds its response limit")
    return result


def _strict_json(raw: bytes) -> Any:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value

    def invalid_constant(_value):
        raise ValueError("Nonfinite JSON number")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)


class StudioServer(ThreadingHTTPServer):
    """HTTP server with a single admitted project mutation and cached state."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, manager, auth_token, public_origin, loopback):
        self.manager = manager
        self.auth_token = auth_token
        self.public_origin = public_origin
        self.loopback = loopback
        self._state_lock = threading.Lock()
        self._busy = False
        self._error = None
        initial = manager.snapshot()
        if not isinstance(initial, dict):
            raise ValueError("ProjectManager.snapshot must return a JSON object")
        self._snapshot = json.loads(_json_bytes(initial))
        super().__init__(address, handler)
        self.timeout = 0.5
        port = self.server_address[1]
        if public_origin:
            self.allowed_hosts = {urlsplit(public_origin).netloc}
            self.allowed_origins = {public_origin}
        else:
            suffix = "" if port == 80 else f":{port}"
            self.allowed_hosts = {name + suffix for name in ("localhost", "127.0.0.1", "[::1]")}
            self.allowed_origins = {"http://" + host for host in self.allowed_hosts}

    def detached_state(self) -> dict:
        with self._state_lock:
            result = deepcopy(self._snapshot)
            result["studio"] = {"busy": self._busy, "error": self._error}
        return result

    def start_action(self, method: str, value: Any) -> bool:
        with self._state_lock:
            if self._busy:
                return False
            self._busy, self._error = True, None

        def run():
            try:
                action = getattr(self.manager, method)
                if method == "approve_brief":
                    result = action(expected_hash=value)
                elif method == "reject_patch":
                    result = action(**value)
                else:
                    result = action() if value is None else action(value)
                if not isinstance(result, dict):
                    raise ValueError("Project result must be a snapshot")
                detached = json.loads(_json_bytes(result))
                with self._state_lock:
                    self._snapshot = detached
            except Exception:
                # Provider exceptions can contain URLs, credentials or prompts.
                # Detailed diagnostics belong to the private project trace.
                try:
                    failed_state = json.loads(_json_bytes(self.manager.snapshot()))
                except Exception:
                    failed_state = None
                with self._state_lock:
                    if isinstance(failed_state, dict):
                        self._snapshot = failed_state
                    self._snapshot["status"] = "error"
                    self._error = ("Cette version n’a pas pu être vérifiée. Actualisez le projet avant de réessayer."
                                   if method in ("accept_patch", "reject_patch", "approve_brief") else
                                   "Cette action n’a pas abouti. Consultez le journal privé du projet.")
            finally:
                with self._state_lock:
                    self._busy = False

        threading.Thread(target=run, name="pcb-dev-studio-action", daemon=True).start()
        return True

    def artifact(self, relative_path: str) -> Path:
        """Resolve only a snapshot-listed file beneath the project directory."""
        if (type(relative_path) is not str or not relative_path or "\\" in relative_path
                or "\x00" in relative_path or relative_path.startswith("/")
                or any(part in ("", ".", "..") for part in relative_path.split("/"))):
            raise ValueError("Invalid artifact path")
        path = PurePosixPath(relative_path)
        if path.is_absolute():
            raise ValueError("Absolute artifact paths are forbidden")
        state = self.detached_state()
        listed = {item.get("relative_path") for item in state.get("artifacts", []) if isinstance(item, dict)}
        if relative_path not in listed:
            raise ValueError("Artifact is not present in the project manifest")
        # ProjectManager owns this directory; never take a root path from HTTP.
        directory = Path(self.manager.directory)
        if directory.is_symlink():
            raise ValueError("Linked project directories are forbidden")
        root = directory.resolve(strict=True)
        authorized = Path(self.manager.artifact_path(relative_path))
        candidate = root.joinpath(*path.parts)
        if authorized != candidate:
            raise ValueError("Artifact resolver returned a different project path")
        current = root
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Linked artifacts are forbidden")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError("Artifact is not a contained project file")
        if resolved.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("Artifact is too large")
        return resolved


    def artifact_bytes(self, relative_path: str) -> bytes:
        path = self.artifact(relative_path)
        root = Path(self.manager.directory).resolve(strict=True)
        # Hold each directory open while walking on POSIX, so a concurrent
        # rename/symlink swap cannot redirect a manifest-listed download.
        descriptors = []
        try:
            if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
                parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                descriptors.append(parent)
                pieces = path.relative_to(root).parts
                for part in pieces[:-1]:
                    parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    descriptors.append(parent)
                descriptor = os.open(pieces[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            else:
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("Artifact must be a regular file")
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                descriptors.append(descriptor)
                actual = os.fstat(descriptor)
                if ((actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino)
                        or path.resolve(strict=True) != path or path.is_symlink()):
                    raise ValueError("Artifact changed while opening")
                descriptors.pop()
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
                    raise ValueError("Artifact is not a bounded regular file")
                result = stream.read(MAX_ARTIFACT_BYTES + 1)
                if len(result) > MAX_ARTIFACT_BYTES:
                    raise ValueError("Artifact is too large")
                return result
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


class _IPv6StudioServer(StudioServer):
    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    server: StudioServer
    server_version = "PCBStudio"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, _format, *_args):
        # In particular, never log an Authorization header or a request URL.
        return

    def _send(self, status: int, content: bytes, content_type: str = "application/json; charset=utf-8",
              *, filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if filename is not None:
            # A fixed download name avoids reflecting arbitrary filenames.
            self.send_header("Content-Disposition", 'attachment; filename="pcb-project-artifact"')
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _error(self, status: int, message: str) -> None:
        self._send(status, _json_bytes({"error": message}))

    def _guard(self, *, mutation: bool = False, authenticate: bool = True) -> bool:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0].lower() not in self.server.allowed_hosts:
            self._error(HTTPStatus.FORBIDDEN, "Adresse de serveur non autorisée.")
            return False
        origins = self.headers.get_all("Origin", [])
        if (len(origins) > 1 or (mutation and not origins)
                or (origins and origins[0] not in self.server.allowed_origins)
                or self.headers.get("Sec-Fetch-Site", "") == "cross-site"):
            self._error(HTTPStatus.FORBIDDEN, "Origine de requête non autorisée.")
            return False
        if authenticate and self.server.auth_token is not None:
            values = self.headers.get_all("Authorization", [])
            expected = "Bearer " + self.server.auth_token
            if len(values) != 1 or not values[0].isascii() or not secrets.compare_digest(values[0], expected):
                self._error(HTTPStatus.UNAUTHORIZED, "Une clé d’accès à cette session est nécessaire.")
                return False
        return True

    def _request_path(self):
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError("Invalid request URL")
        return parsed

    def do_GET(self):
        try:
            parsed = self._request_path()
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "Adresse invalide.")
            return
        is_api = parsed.path.startswith("/api/")
        if not self._guard(authenticate=is_api):
            return
        if parsed.path == "/api/state" and not parsed.query:
            self._send(HTTPStatus.OK, _json_bytes(self.server.detached_state()))
        elif parsed.path == "/api/versions/compare":
            try:
                query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
                if set(query) != {"before", "after"} or any(len(values) != 1 for values in query.values()):
                    raise ValueError("Invalid comparison query")
                before, after = query["before"][0], query["after"][0]
                if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (before, after)):
                    raise ValueError("Invalid comparison hashes")
                result = self.server.manager.compare_versions(before, after)
                self._send(HTTPStatus.OK, _json_bytes(result))
            except (ValueError, OSError, KeyError, AttributeError):
                self._error(HTTPStatus.CONFLICT, "Ces versions ne peuvent pas être comparées. Leur intégrité doit être vérifiée.")
        elif parsed.path == "/api/artifact":
            try:
                query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
                if set(query) != {"path"} or len(query["path"]) != 1:
                    raise ValueError("Invalid artifact query")
                content = self.server.artifact_bytes(query["path"][0])
                self._send(HTTPStatus.OK, content, "application/octet-stream", filename="artifact")
            except (ValueError, OSError, KeyError, AttributeError):
                self._error(HTTPStatus.NOT_FOUND, "Ce fichier n’est pas disponible dans ce projet.")
        elif parsed.path in ("/", "/studio.js", "/studio.css") and not parsed.query:
            name = "studio.html" if parsed.path == "/" else parsed.path[1:]
            try:
                content = (WEB_DIRECTORY / name).read_bytes()
                content_type = {"studio.html": "text/html; charset=utf-8", "studio.js": "text/javascript; charset=utf-8",
                                "studio.css": "text/css; charset=utf-8"}[name]
                self._send(HTTPStatus.OK, content, content_type)
            except OSError:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "L’interface est indisponible.")
        else:
            self._error(HTTPStatus.NOT_FOUND, "Ressource inconnue.")

    def do_POST(self):
        if not self._guard(mutation=True):
            return
        try:
            parsed = self._request_path()
            actions = {"/api/message": ("send_message", "text"),
                       "/api/approve": ("approve_brief", "brief_hash"),
                       "/api/claude-review": ("request_claude_review", "question"),
                       "/api/patch/accept": ("accept_patch", "proposal_hash"),
                       "/api/patch/reject": ("reject_patch", "rejection")}
            if parsed.query or parsed.path not in actions:
                self._error(HTTPStatus.NOT_FOUND, "Action inconnue.")
                return
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                self._error(HTTPStatus.LENGTH_REQUIRED, "Une longueur de requête valide est nécessaire.")
                return
            length = int(lengths[0])
            if length > MAX_REQUEST_BYTES:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Le message est trop volumineux.")
                return
            content_types = self.headers.get_all("Content-Type", [])
            content_type = content_types[0].lower().replace(" ", "") if len(content_types) == 1 else ""
            if content_type not in ("application/json", "application/json;charset=utf-8"):
                self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Le format JSON est nécessaire.")
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete request")
            data = _strict_json(raw)
            method, field = actions[parsed.path]
            expected = {"proposal_hash", "reason"} if field == "rejection" else {field} if field else set()
            if type(data) is not dict or set(data) != expected:
                raise ValueError("Unexpected fields")
            value = data if field == "rejection" else data[field] if field else None
            if field == "rejection":
                if (type(data["reason"]) is not str or not data["reason"].strip() or len(data["reason"]) > 4000
                        or type(data["proposal_hash"]) is not str or re.fullmatch(r"[0-9a-f]{64}", data["proposal_hash"]) is None):
                    raise ValueError("Invalid human rejection")
            if field != "rejection" and field and (type(value) is not str or not value.strip() or len(value) > MAX_MESSAGE_CHARS):
                raise ValueError("Invalid message")
            if field in ("brief_hash", "proposal_hash") and re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("Invalid displayed brief hash")
            if not self.server.start_action(method, value):
                self._error(HTTPStatus.CONFLICT, "Une action est déjà en cours. Attendez sa réponse.")
                return
            self._send(HTTPStatus.ACCEPTED, _json_bytes(self.server.detached_state()))
        except (ValueError, UnicodeError, json.JSONDecodeError, RecursionError):
            self._error(HTTPStatus.BAD_REQUEST, "La requête est invalide.")
        except (TimeoutError, socket.timeout):
            self._error(HTTPStatus.REQUEST_TIMEOUT, "Le message n’a pas été reçu entièrement.")

    def do_OPTIONS(self):
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Cette méthode n’est pas autorisée.")


def make_server(manager, *, host: str = "127.0.0.1", port: int = 8788,
                auth_token: str | None = None, public_origin: str | None = None) -> StudioServer:
    """Construct without starting a model call; port=0 is useful for tests."""
    if host == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("Studio host must be a literal IP address or localhost") from exc
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Studio port must be an integer from 0 to 65535")
    if auth_token is not None and (type(auth_token) is not str or len(auth_token) < 24
            or not auth_token.isascii() or any(not 33 <= ord(char) <= 126 for char in auth_token)):
        raise ValueError("Studio access token must contain at least 24 non-whitespace ASCII characters")
    origin = _origin(public_origin) if public_origin is not None else None
    if not address.is_loopback and (auth_token is None or origin is None):
        raise ValueError("Non-loopback studio requires an access token and an explicit HTTPS public_origin")
    server_type = _IPv6StudioServer if address.version == 6 else StudioServer
    return server_type((host, port), _Handler, manager=manager, auth_token=auth_token,
                       public_origin=origin, loopback=address.is_loopback)


def serve_studio(manager, *, host: str = "127.0.0.1", port: int = 8788,
                 auth_token: str | None = None, public_origin: str | None = None) -> None:
    """Serve until interrupted. The caller controls display of the private URL."""
    server = make_server(manager, host=host, port=port, auth_token=auth_token, public_origin=public_origin)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
