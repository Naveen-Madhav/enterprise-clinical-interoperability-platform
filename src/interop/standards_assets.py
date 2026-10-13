"""Fetch checksum-pinned HL7 validation assets into ignored local runtime storage."""

import hashlib
import json
from importlib.resources import files
from pathlib import Path

import httpx

from .errors import InterfaceError


def prepare_assets(directory=Path("runtime/standards")) -> Path:
    manifest = json.loads(files("interop").joinpath("standards-assets.json").read_text())
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
        for relative, item in manifest.items():
            target = directory / relative
            if target.exists():
                content = target.read_bytes()
            else:
                try:
                    response = client.get(item["url"])
                    response.raise_for_status()
                    content = response.content
                except httpx.HTTPError as exc:
                    raise InterfaceError("standards.asset-download") from exc
            if (
                len(content) > 70 * 1024 * 1024
                or hashlib.sha256(content).hexdigest() != item["sha256"]
            ):
                raise InterfaceError("standards.asset-digest")
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
    return directory
