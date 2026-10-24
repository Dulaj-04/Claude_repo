"""Runtime configuration from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def safe_name(value: str, what: str = "name") -> str:
    """Validate an identifier used in file paths (project, floor, ...)."""
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise ValueError(
            f"Invalid {what} {value!r}: use 1-64 letters, digits, '_' or '-' (no paths)."
        )
    return value


@dataclass
class Config:
    workspace: Path
    backend: str = "dxf"  # "dxf" (offline, ezdxf) or "autocad" (live COM, Windows)
    allow_commands: bool = False
    require_plan: bool = False

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            workspace=Path(os.environ.get("ACAD_MCP_WORKSPACE", "acad_mcp_workspace")).resolve(),
            backend=os.environ.get("ACAD_MCP_BACKEND", "dxf").lower(),
            allow_commands=os.environ.get("ACAD_MCP_ALLOW_COMMANDS", "") == "1",
            require_plan=os.environ.get("ACAD_MCP_REQUIRE_PLAN", "") == "1",
        )
