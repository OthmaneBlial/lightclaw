"""Shared datatypes for LightClaw."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FileOperationResult:
    action: str
    path: str
    detail: str = ""
    diff: str = ""
