"""flashgate: hardware-in-the-loop verification gate for coding agents.

The agent may not claim firmware work is done until the board itself
says so: build -> flash -> boot banner over serial, with exit codes
that an agent harness (Stop hook) can enforce.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("flashgate")   # pyproject.toml is the single source
except PackageNotFoundError:                  # running from a raw source tree
    __version__ = "0.0.0"
