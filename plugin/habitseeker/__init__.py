import json
import threading
import uuid

from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pynicotine.core import Core
from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin
from pynicotine.slskmessages import FileSearchResponse
from pynicotine.search import Search
from typing import Any

SERVER_IP = "127.0.0.1"
SERVER_PORT = 8765

# Helper: extracts file information from a FileSearchResponse message. Distinguishes between
# changes made after v3.4 of Nicotine+.
def _extract_files(msg: FileSearchResponse) -> list[tuple[str, int]]:
    # Distinguish between v3.4+ and v3.3- since file handling is different between versions.
    files: Any = getattr(msg, "public_files", None)  # v3.4+
    if files is None:
        files = getattr(msg, "list", None) or []     # v3.3-

    results: list[tuple[str, int]] = []
    for file in files:
        if isinstance(file, tuple):
            _code, path, size, *rest = file
        else:
            path: str = file.path
            size: int = file.size
            results.append((path, size))
    return results

@dataclass
class SearchEntry:
    query: str
    token: int | None = None
    results: list[dict[str, Any]] = field(default_factory=list)

class Handler(BaseHTTPRequestHandler):
    # Send JSON message with an HTTP code through HTTP.
    def _send_json(self, http_code, obj):
        # Craft the response body and send responses + headers
        body = json.dumps(obj).encode()
        self.send_response(http_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        # Write the response.
        self.wfile.write(body)

    # Handle GET requests.
    def do_GET(self):
        if self.path == "/api/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not found"})

    # Handle POST requests.
    def do_POST(self):
        # Check that the path is on /api/search.
        if self.path != "/api/search":
            self._send_json(404, {"error": "not found"})
            return

        # Check that the content type is application/json.
        if self.headers.get_content_type() != "application/json":
            self._send_json(415, {"error": "Content-Type must be application/json"})
            return

        # Read the content-length and load JSON.
        try:
            length = int(self.headers.get("Content-Length", 0))  # Determine length of message
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return

        # Read the JSON payload...
        raw = self.rfile.read(length)

        # ...and attempt to decode JSON payload.
        try:
            data: dict[Any, Any] = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON"})
            return

        # Execute a search on the HTTPServer thread, given that "track" and "artist" are both in the payload.
        if not isinstance(data, dict):
            self._send_json(400, {"error": "data response must be a dict"})
            return
        elif "track" not in data and "artist" not in data:
            self._send_json(400, {"error": "'track' and/or 'artist' must be specified in data JSON"})
            return
        elif not isinstance(data["track"], str):
            self._send_json(400, {"error": "'track' must be of type str"})
            return
        elif not isinstance(data["artist"], str):
            self._send_json(400, {"error": "'artist' must be of type str"})
            return
        elif data["track"].strip() == "":
            self._send_json(400, {"error": "'track' is required"})
            return
        elif data["artist"].strip() == "":
            self._send_json(400, {"error": "'artist' is required"})
            return

        # Generate the query
        query = f"{data["track"]} {data["artist"]}"

        # Start a search from the main thread
        search_id = self.server.plugin.start_search(query)  # pyright: ignore[reportAttributeAccessIssue]
        self._send_json(200, {"search_id": search_id})


    def log_message(self, format, *args):
        pass


class Plugin(BasePlugin):
    core: Core

    def init(self) -> None:
        self.lock: threading.Lock = threading.Lock()    # Lock
        self._pending_search_id: str | None = None      # The ID of the ongoing pending search of this thread
        self.searches: dict[str, SearchEntry] = {}      # Dictionary of search_id -> SearchEntries
        self.server: ThreadingHTTPServer | None = None  # HTTP server
        self.token_to_id: dict[int, str] = {}           # Dictionary mapping token to search_id

        # Connect to the file-search-response and add-search events queue.
        events.connect("file-search-response", self._on_search_response)
        events.connect("add-search", self._on_add_search)

    def loaded_notification(self):
        self.log("HabitSeeker successfully loaded.")

        try:
            # Initialize the HTTP server.
            self.server = ThreadingHTTPServer((SERVER_IP, SERVER_PORT), Handler)
            self.server.plugin = self  # type: ignore[attr-defined]

            # Create thread for HTTP server, serve forever as a daemon, and start it.
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
        except OSError as e:
            self.log(f"{e}: Error starting HTTP server on {SERVER_IP}:{SERVER_PORT}, please check settings.")

    def disable(self):
        self.log("HabitSeeker disabled.")

        if self.server:
            self.server.shutdown()      # Stops serve_forever func.
            self.server.server_close()  # Free up the port on SERVER_PORT.

        self.server = None
        
        # Disconnect from the file-search-response events queue.
        events.disconnect("file-search-response", self._on_search_response)
        events.disconnect("add-search", self._on_add_search)

    # Lock-gated functions
    def _add_to_search(self, search_id: str, query: str):
        with self.lock:
            self.searches[search_id] = SearchEntry(query=query, token=None, results=[])

    def _register_token(self, search_id: str, token: int):
        with self.lock:
            self.searches[search_id].token = token
            self.token_to_id[token] = search_id

    def _get_search_id_from_token(self, token: int):
        with self.lock:
            return self.token_to_id.get(token)

    def start_search(self, query: str):
        search_id = uuid.uuid4().hex   # Construct a unique search identifier
        self._add_to_search(search_id, query)  # While under lock, add the search_id and query to the map of searches.
        events.invoke_main_thread(self._run_search, search_id, query)  # Queue the search on the main thread.
        return search_id
    
    def _run_search(self, search_id: str, query: str):
        self._pending_search_id = search_id
        search: Search | None = self.core.search
        if search is None:
            self.log("Search component not available")
            return

        try:
            search.do_search(query, "global", switch_page=False)
        finally:
            self._pending_search_id = None

    def _on_search_response(self, msg: FileSearchResponse) -> None:
        if msg.token is None:
            return

        search_id = self._get_search_id_from_token(msg.token)

        if search_id:
            files: list[tuple[str, int]] = _extract_files(msg)
            user: Any | None = msg.search_username

            with self.lock:
                for file in files:
                    # file[0] == file name/path
                    # file[1] == file size
                    file_map = {"filename": file[0], "size": file[1], "user": user}
                    self.searches[search_id].results.append(file_map)

            self.log(f"Search results: {len(files)} results added.")

    def _on_add_search(self, token, search, *args) -> None:
        if not self._pending_search_id:
            return
        self._register_token(self._pending_search_id, token)