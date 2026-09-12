#!/usr/bin/env python3
"""Backward-compatible entry point; use legal_case_os.py in new instructions."""

from legal_case_os import main


if __name__ == "__main__":
    raise SystemExit(main())
