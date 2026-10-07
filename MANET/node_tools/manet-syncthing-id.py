#!/usr/bin/env python3
"""Standalone fallback for reading Syncthing's certificate-derived device ID."""
from manet_runtime import Helpers

if __name__ == '__main__':
    print(Helpers().syncthing_id())
