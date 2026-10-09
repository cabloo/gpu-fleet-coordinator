"""Is `coord-api` answering? Asked over its unix socket, from inside its own container.

There is deliberately no network health route (remote-submit inv. 3): the service has no network at
all, and an unauthenticated route that touched the registry would be the one hole in a deny-by-default
surface. So the healthcheck speaks the same socket the proxy does.
"""

import socket
import sys

SOCKET = "/run/coord-api/api.sock"


def main() -> int:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(SOCKET)
        s.sendall(b"GET /v1/ping HTTP/1.1\r\nHost: healthcheck\r\nContent-Length: 0\r\n\r\n")
        head = s.recv(256)
    except OSError as e:
        print(f"coord-api unreachable on {SOCKET}: {e}", file=sys.stderr)
        return 1
    finally:
        s.close()
    if b" 200 " not in head:
        print(f"coord-api answered: {head[:80]!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
