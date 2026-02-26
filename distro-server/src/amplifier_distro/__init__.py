"""Amplifier Distro - The Amplifier Experience Server.

Public API Surface
------------------
These are the modules and symbols that the server, apps, and CLI
are allowed to import. This surface defines the extraction boundary:
when the server becomes its own package, these are its dependencies.
"""

__version__ = "0.2.0"

# --- Constants (immutable, zero-cost) ---
from amplifier_distro import conventions

# --- Utilities ---
from amplifier_distro.fileutil import atomic_write

__all__ = [
    "__version__",
    "atomic_write",
    "conventions",
]
