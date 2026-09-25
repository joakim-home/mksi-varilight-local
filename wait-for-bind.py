#!/usr/bin/env python3
import errno
import json
import socket
import sys
import time
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: wait-for-bind.py CONFIG", file=sys.stderr)
        return 2
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    address = str(cfg["bind"])
    deadline = time.monotonic() + 25.0

    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((address, 0))
            return 0
        except OSError as exc:
            if exc.errno not in (errno.EADDRNOTAVAIL, errno.EACCES):
                print(f"bind readiness check failed: {exc}", file=sys.stderr)
                return 1
        finally:
            sock.close()
        time.sleep(0.25)

    print(f"bind address did not become available: {address}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
