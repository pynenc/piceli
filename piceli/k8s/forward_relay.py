"""A loopback relay in front of a supervised ``kubectl port-forward`` (0.19.0).

``kubectl port-forward`` carries every connection over one stream through
the API server: over a slow link a large download saturates it, and a
health probe through the same forward times out although the forward is
fine. With a relay, Piceli itself holds the declared local port and passes
each connection on to ``kubectl`` listening on an internal port, counting
the bytes. The supervisor then knows when a forward is moving bytes (it is
healthy, whatever a probe says), probes ``kubectl`` directly on a separate
connection, and keeps the local port bound while it restarts ``kubectl``.

Importing this module is side-effect free.
"""

from __future__ import annotations

import socket
import threading
import time

#: A forward that moved bytes within this many seconds is busy, not broken.
BUSY_SECONDS = 15.0
_CHUNK = 64 * 1024


class PortRelay:
    """Relay ``127.0.0.1:port`` to ``127.0.0.1:target`` (set later, changeable).

    :raises OSError: when the port cannot be bound (another process has it).
    """

    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self.port = port
        self.host = host
        self.target: int | None = None
        self.active = 0
        self.bytes = 0
        self.connections = 0
        self.last_byte: float | None = None
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = set()
        self._closed = threading.Event()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((host, port))
            listener.listen(128)
        except OSError:
            listener.close()
            raise
        self._listener = listener
        self._thread = threading.Thread(
            target=self._accept, name=f"piceli-relay-{port}", daemon=True
        )

    def start(self) -> PortRelay:
        self._thread.start()
        return self

    def busy(self, window: float = BUSY_SECONDS) -> bool:
        """Whether a connection is open and bytes moved within ``window`` seconds."""
        last = self.last_byte
        return self.active > 0 and last is not None and time.monotonic() - last < window

    def stats(self) -> dict[str, int | bool]:
        return {
            "active_connections": self.active,
            "bytes": self.bytes,
            "busy": self.busy(),
        }

    def close(self) -> None:
        self._closed.set()
        try:
            # On Linux, close() alone leaves a thread blocked in accept()
            # holding the socket (the port stays bound); shutdown wakes it.
            self._listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # macOS: a listening socket is "not connected"
        try:
            self._listener.close()
        except OSError:
            pass
        with self._lock:
            sockets = list(self._sockets)
        for item in sockets:
            try:
                item.close()
            except OSError:
                pass
        self._thread.join(timeout=2)

    # ------------------------------------------------------------ internals
    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _track(self, item: socket.socket, add: bool) -> None:
        with self._lock:
            (self._sockets.add if add else self._sockets.discard)(item)

    def _serve(self, client: socket.socket) -> None:
        target = self.target
        if target is None:
            client.close()
            return
        try:
            upstream = socket.create_connection(("127.0.0.1", target), timeout=10)
        except OSError:
            client.close()  # like kubectl: accept locally, drop when it is down
            return
        upstream.settimeout(None)
        for item in (client, upstream):
            self._track(item, True)
        with self._lock:
            self.active += 1
            self.connections += 1

        def pump(source: socket.socket, sink: socket.socket) -> None:
            how = socket.SHUT_WR  # a clean end: pass the half-close on
            try:
                while True:
                    data = source.recv(_CHUNK)
                    if not data:
                        break
                    # Counted when it arrives, before it is passed on: whoever
                    # gets the bytes on the other side sees them counted.
                    with self._lock:
                        self.bytes += len(data)
                        self.last_byte = time.monotonic()
                    sink.sendall(data)
            except OSError:
                how = socket.SHUT_RDWR  # broken: end both directions
            finally:
                try:
                    sink.shutdown(how)
                except OSError:
                    pass
                if how == socket.SHUT_RDWR:
                    try:
                        source.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        other = threading.Thread(target=pump, args=(upstream, client), daemon=True)
        other.start()
        pump(client, upstream)
        other.join()
        with self._lock:
            self.active -= 1
        for item in (client, upstream):
            self._track(item, False)
            try:
                item.close()
            except OSError:
                pass
