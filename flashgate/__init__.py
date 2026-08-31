"""flashgate: hardware-in-the-loop verification gate for coding agents.

The agent may not claim firmware work is done until the board itself
says so: build -> flash -> boot banner over serial, with exit codes
that an agent harness (Stop hook) can enforce.
"""

__version__ = "0.3.0"
