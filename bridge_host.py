#!/usr/bin/env python3
"""Native Messaging host launched by the Vivaldi extension."""

import sys

import bookmark_bridge


if __name__ == "__main__":
    raise SystemExit(bookmark_bridge.main_native(sys.argv[1] if len(sys.argv) > 1 else ""))
