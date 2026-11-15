"""Shared test setup: makes the repository root importable (pictochat.py, custom_components)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
