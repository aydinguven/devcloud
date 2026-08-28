"""Dependency-free health probe used by the controller OCI image."""

from __future__ import annotations

import sys
from urllib.request import urlopen


def main() -> int:
    try:
        with urlopen("http://127.0.0.1:8000/readyz", timeout=4) as response:
            return 0 if response.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())

