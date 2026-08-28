"""STM32Cube bundle discovery: toolchain + CubeProgrammer live under
%LOCALAPPDATA%/stm32cube/bundles/<tool>/<version>/bin on Windows when the
STM32Cube CLT / VSCode extension installed them."""

from __future__ import annotations

import os
import re
from pathlib import Path


def _bundle_root() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "stm32cube" / "bundles"


def bundle_bin_dirs() -> list[Path]:
    """All bundle bin dirs (cmake/ninja/gcc/programmer...), newest version
    of each tool first so PATH lookups win with the newest."""
    root = _bundle_root()
    if not root.is_dir():
        return []

    def version_key(p: Path) -> tuple:
        raw = p.parent.name  # the <version> directory
        return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[+.-]", raw))

    dirs = [p for p in root.glob("*/*/bin") if p.is_dir()]
    return sorted(dirs, key=lambda p: (p.parts[-3], tuple(-x for x in version_key(p))))


def augmented_env() -> dict[str, str]:
    """subprocess env with all ST bundle bins prepended to PATH."""
    env = os.environ.copy()
    dirs = [str(p) for p in bundle_bin_dirs()]
    if dirs:
        env["PATH"] = os.pathsep.join(dirs + [env.get("PATH", "")])
    return env


def find_cubeprogrammer() -> Path | None:
    """Newest STM32_Programmer_CLI.exe under the bundles, or PATH lookup."""
    candidates = list((_bundle_root() / "programmer").glob("*/bin/STM32_Programmer_CLI.exe"))

    def version_key(p: Path) -> tuple:
        raw = p.parent.parent.name
        return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[+.-]", raw))

    if candidates:
        return max(candidates, key=version_key)

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        guess = Path(entry) / "STM32_Programmer_CLI.exe"
        if guess.is_file():
            return guess
    return None
