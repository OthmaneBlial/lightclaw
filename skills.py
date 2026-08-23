"""
Lightweight skill manager for LightClaw.

Features:
- Install skills from ClawHub (`/api/v1`) as zip bundles containing `SKILL.md`
- Create local custom skills
- Activate/deactivate skills per Telegram chat (persisted in JSON)
- Build compact prompt context from active skills
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

from core.fs import atomic_write_json as _atomic_write_json
from core.fs import atomic_write_text as _atomic_write_text
from core.fs import read_json_object

DEFAULT_HUB_BASE_URL = "https://clawhub.ai"
DEFAULT_API_PREFIX = "/api/v1"
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
MAX_SKILL_TEXT_BYTES = 512 * 1024
MAX_SKILL_META_BYTES = 128 * 1024
SKILL_MANIFEST_NAME = "skill.json"
SKILL_MANIFEST_SCHEMA_VERSION = 1
MAX_SKILL_MANIFEST_BYTES = 64 * 1024
SKILL_CAPABILITIES = frozenset(
    {
        "prompt-guidance",
        "workspace-read",
        "workspace-write",
        "network",
        "subprocess",
        "trusted-command",
    }
)
SAFE_PROMPT_CAPABILITIES = frozenset({"prompt-guidance"})

_FRONTMATTER_RE = re.compile(r"^---\s*\n([\s\S]*?)\n---\s*(?:\n|$)")
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_DOMAIN_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_DEPENDENCY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9][A-Za-z0-9.!+_-]*$"
)


class SkillError(RuntimeError):
    """Raised for user-facing skill errors."""


@dataclass
class SkillRecord:
    skill_id: str
    name: str
    description: str
    source: str
    directory: Path
    skill_path: Path
    slug: str | None = None
    version: str | None = None
    owner: str | None = None
    manifest_path: Path | None = None
    manifest: dict[str, Any] | None = None
    content_sha256: str = ""
    validation_errors: tuple[str, ...] = ()
    isolated_only: bool = False


@dataclass
class SkillSearchResult:
    slug: str
    display_name: str
    summary: str
    version: str | None = None
    score: float | None = None


def _sanitize_id(text: str) -> str:
    value = _SAFE_ID_RE.sub("-", text.strip().lower()).strip("-._")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _review_sha256(skill_bytes: bytes, manifest_bytes: bytes) -> str:
    """Bind activation approval to both instructions and declared permissions."""
    digest = hashlib.sha256()
    digest.update(b"lightclaw-skill-review-v1\0")
    digest.update(len(skill_bytes).to_bytes(8, "big"))
    digest.update(skill_bytes)
    digest.update(len(manifest_bytes).to_bytes(8, "big"))
    digest.update(manifest_bytes)
    return digest.hexdigest()


def _safe_relative_path(value: str) -> bool:
    candidate = PurePosixPath(str(value).replace("\\", "/"))
    return bool(
        candidate.as_posix() not in {"", "."}
        and not candidate.is_absolute()
        and not re.match(r"^[A-Za-z]:", candidate.as_posix())
        and ".." not in candidate.parts
    )


def _default_manifest(
    *,
    skill_id: str,
    name: str,
    version: str,
    owner: str,
) -> dict[str, Any]:
    return {
        "schema_version": SKILL_MANIFEST_SCHEMA_VERSION,
        "id": skill_id,
        "name": name,
        "version": version,
        "owner": owner,
        "capabilities": ["prompt-guidance"],
        "network": {"allowed": False, "domains": []},
        "writable_paths": [],
        "dependencies": [],
    }


def validate_skill_manifest(payload: object) -> list[str]:
    """Return stable validation errors for the minimal permission manifest."""
    if not isinstance(payload, dict):
        return ["skill.json must contain a JSON object"]
    errors: list[str] = []
    if payload.get("schema_version") != SKILL_MANIFEST_SCHEMA_VERSION:
        errors.append(f"schema_version must be {SKILL_MANIFEST_SCHEMA_VERSION}")
    skill_id = str(payload.get("id") or "")
    if not skill_id or _sanitize_id(skill_id) != skill_id:
        errors.append("id must be a lowercase safe slug")
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 120:
        errors.append("name must contain 1-120 characters")
    version = str(payload.get("version") or "").strip()
    if not _VERSION_RE.fullmatch(version):
        errors.append("version must be a pinned semantic version")
    owner = str(payload.get("owner") or "").strip()
    if not owner or len(owner) > 200:
        errors.append("owner must contain 1-200 characters")

    raw_capabilities = payload.get("capabilities")
    capabilities = (
        [str(value) for value in raw_capabilities]
        if isinstance(raw_capabilities, list)
        else []
    )
    if not capabilities:
        errors.append("capabilities must be a non-empty list")
    elif len(capabilities) != len(set(capabilities)):
        errors.append("capabilities must not contain duplicates")
    unknown = sorted(set(capabilities) - SKILL_CAPABILITIES)
    if unknown:
        errors.append("unknown capabilities: " + ", ".join(unknown))

    network = payload.get("network")
    if not isinstance(network, dict) or not isinstance(network.get("allowed"), bool):
        errors.append("network must declare boolean allowed and a domains list")
        network_allowed = False
        domains: list[object] = []
    else:
        network_allowed = bool(network.get("allowed"))
        raw_domains = network.get("domains")
        domains = raw_domains if isinstance(raw_domains, list) else []
        if not isinstance(raw_domains, list):
            errors.append("network.domains must be a list")
    for domain in domains:
        if not isinstance(domain, str) or not _DOMAIN_RE.fullmatch(domain):
            errors.append(f"invalid network domain: {domain}")
    if not network_allowed and domains:
        errors.append("network.domains must be empty when network is disabled")
    if network_allowed and ("network" not in capabilities or not domains):
        errors.append("network access requires the network capability and pinned domains")

    raw_paths = payload.get("writable_paths")
    paths = raw_paths if isinstance(raw_paths, list) else []
    if not isinstance(raw_paths, list):
        errors.append("writable_paths must be a list")
    for path in paths:
        if not isinstance(path, str) or not _safe_relative_path(path):
            errors.append(f"invalid writable path: {path}")
    if paths and "workspace-write" not in capabilities:
        errors.append("writable_paths require the workspace-write capability")

    raw_dependencies = payload.get("dependencies")
    dependencies = raw_dependencies if isinstance(raw_dependencies, list) else []
    if not isinstance(raw_dependencies, list):
        errors.append("dependencies must be a list")
    for dependency in dependencies:
        if not isinstance(dependency, str) or not _DEPENDENCY_RE.fullmatch(dependency):
            errors.append(f"dependency must pin an exact version with ==: {dependency}")
    if dependencies and "subprocess" not in capabilities:
        errors.append("executable dependencies require the subprocess capability")
    return errors


def validate_skill_directory(path: str | Path) -> dict[str, Any]:
    """Validate one skill directory without executing or importing its contents."""
    raw_candidate = Path(path).expanduser()
    raw_directory = raw_candidate.parent if raw_candidate.name == "SKILL.md" else raw_candidate
    if raw_candidate.is_symlink() or raw_directory.is_symlink():
        return {
            "valid": False,
            "directory": raw_directory.absolute().as_posix(),
            "errors": ["skill directory is missing or symlinked"],
        }
    candidate = raw_candidate.resolve()
    directory = candidate.parent if candidate.is_file() and candidate.name == "SKILL.md" else candidate
    errors: list[str] = []
    if not directory.is_dir() or directory.is_symlink():
        return {"valid": False, "directory": directory.as_posix(), "errors": ["skill directory is missing or symlinked"]}
    skill_path = directory / "SKILL.md"
    manifest_path = directory / SKILL_MANIFEST_NAME
    if skill_path.is_symlink() or not skill_path.is_file():
        errors.append("SKILL.md is missing or symlinked")
        skill_bytes = b""
    else:
        skill_bytes = skill_path.read_bytes()
        if len(skill_bytes) > MAX_SKILL_TEXT_BYTES:
            errors.append("SKILL.md exceeds the size limit")
    manifest: dict[str, Any] = {}
    manifest_bytes = b""
    if manifest_path.is_symlink() or not manifest_path.is_file():
        errors.append("skill.json is missing or symlinked")
    else:
        manifest_bytes = manifest_path.read_bytes()
        if len(manifest_bytes) > MAX_SKILL_MANIFEST_BYTES:
            errors.append("skill.json exceeds the size limit")
        else:
            try:
                loaded = json.loads(manifest_bytes.decode("utf-8"))
                manifest = loaded if isinstance(loaded, dict) else {}
                errors.extend(validate_skill_manifest(loaded))
            except (UnicodeDecodeError, json.JSONDecodeError):
                errors.append("skill.json is not valid UTF-8 JSON")
    capabilities = set(manifest.get("capabilities", [])) if manifest else set()
    network = manifest.get("network", {}) if manifest else {}
    isolated_only = bool(
        capabilities - SAFE_PROMPT_CAPABILITIES
        or (isinstance(network, dict) and network.get("allowed"))
        or manifest.get("writable_paths")
        or manifest.get("dependencies")
    )
    instructions_sha256 = _sha256_bytes(skill_bytes)
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    content_sha256 = _review_sha256(skill_bytes, manifest_bytes)
    return {
        "valid": not errors,
        "directory": directory.as_posix(),
        "skill_path": skill_path.as_posix(),
        "manifest_path": manifest_path.as_posix(),
        "manifest": manifest,
        "content_sha256": content_sha256,
        "instructions_sha256": instructions_sha256,
        "manifest_sha256": manifest_sha256,
        "activation_token": content_sha256[:12],
        "isolated_only": isolated_only,
        "errors": errors,
    }


def _first_non_empty(*values: str | None) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    raw = match.group(1)
    body = text[match.end() :]
    data: dict[str, Any] = {}

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if value.startswith("{") or value.startswith("["):
            try:
                data[key] = json.loads(value)
                continue
            except json.JSONDecodeError:
                pass

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]

        data[key] = value

    return data, body


def _body_summary(text: str, max_len: int = 180) -> str:
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        return candidate[:max_len]
    return ""


class SkillManager:
    """Manage installed skills, active per-chat skills, and ClawHub installs."""

    def __init__(
        self,
        workspace_path: str,
        skills_state_path: str,
        hub_base_url: str = DEFAULT_HUB_BASE_URL,
    ):
        self.workspace = Path(workspace_path).resolve()
        self.runtime_root = self.workspace.parent if self.workspace.name == "workspace" else self.workspace
        self.skills_root = self.runtime_root / "skills"
        self.legacy_skills_root = (
            self.workspace / "skills"
            if (self.workspace / "skills").resolve() != self.skills_root
            else None
        )
        self.hub_dir = self.skills_root / "hub"
        self.local_dir = self.skills_root / "local"
        self.state_path = Path(skills_state_path).resolve()
        self._lock = threading.RLock()

        hub = (hub_base_url or DEFAULT_HUB_BASE_URL).strip().rstrip("/")
        if hub.endswith(DEFAULT_API_PREFIX):
            self.api_base_url = hub
            self.hub_base_url = hub[: -len(DEFAULT_API_PREFIX)]
        else:
            self.hub_base_url = hub
            self.api_base_url = f"{hub}{DEFAULT_API_PREFIX}"

        self._ensure_dirs()

    def _ensure_dirs(self):
        # Backward compatibility: migrate legacy workspace/skills into runtime-root skills.
        if self.legacy_skills_root and self.legacy_skills_root.exists():
            for src in self.legacy_skills_root.rglob("*"):
                if not src.is_file():
                    continue
                rel = src.relative_to(self.legacy_skills_root)
                dst = self.skills_root / rel
                if dst.exists():
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        self.hub_dir.mkdir(parents=True, exist_ok=True)
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_manifests()

    def _migrate_legacy_manifests(self) -> None:
        """Make old instruction-only skills explicit and prompt-only."""
        for root, source_type in ((self.hub_dir, "hub"), (self.local_dir, "local")):
            for directory in sorted(
                path for path in root.iterdir() if path.is_dir() and not path.is_symlink()
            ):
                skill_path = directory / "SKILL.md"
                manifest_path = directory / SKILL_MANIFEST_NAME
                if not skill_path.is_file() or skill_path.is_symlink() or manifest_path.exists():
                    continue
                source_path = directory / "source.json"
                source: dict[str, Any] = {}
                if source_path.is_file() and not source_path.is_symlink():
                    try:
                        loaded = json.loads(source_path.read_text(encoding="utf-8"))
                        source = loaded if isinstance(loaded, dict) else {}
                    except (OSError, json.JSONDecodeError):
                        source = {}
                content = skill_path.read_text(encoding="utf-8", errors="replace")
                frontmatter, body = _frontmatter(content)
                name = _first_non_empty(
                    str(frontmatter.get("name") or ""),
                    str(source.get("display_name") or ""),
                    directory.name,
                )
                version = str(source.get("version") or "").strip()
                if not _VERSION_RE.fullmatch(version):
                    version = "0.1.0" if source_type == "local" else "0.0.0-legacy"
                owner = _first_non_empty(
                    str(source.get("owner") or ""),
                    "local-owner" if source_type == "local" else "legacy-hub-owner",
                )
                manifest = _default_manifest(
                    skill_id=directory.name,
                    name=name or _body_summary(body) or directory.name,
                    version=version,
                    owner=owner,
                )
                _atomic_write_json(manifest_path, manifest)
                source.update(
                    {
                        "source": source_type,
                        "slug": directory.name,
                        "display_name": name,
                        "owner": owner,
                        "version": version,
                        "instructions_sha256": _sha256_bytes(skill_path.read_bytes()),
                        "content_sha256": _review_sha256(
                            skill_path.read_bytes(),
                            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
                        ),
                        "manifest_sha256": _sha256_bytes(
                            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
                        ),
                        "manifest_migrated": True,
                    }
                )
                _atomic_write_json(source_path, source)

    def _read_state(self) -> dict[str, Any]:
        try:
            data = read_json_object(
                self.state_path,
                default={"active_by_chat": {}, "approved_hashes_by_chat": {}},
                max_bytes=1024 * 1024,
            )
            active = data.get("active_by_chat")
            if not isinstance(active, dict):
                active = {}
            approved = data.get("approved_hashes_by_chat")
            if not isinstance(approved, dict):
                approved = {}
            return {
                "active_by_chat": active,
                "approved_hashes_by_chat": approved,
            }
        except Exception:
            return {"active_by_chat": {}, "approved_hashes_by_chat": {}}

    def _write_state(self, state: dict[str, Any]):
        _atomic_write_json(self.state_path, state)

    @staticmethod
    def _http_get_bytes(url: str, accept: str = "*/*") -> bytes:
        req = Request(
            url,
            headers={
                "Accept": accept,
                "User-Agent": "LightClaw/1.0",
            },
        )

        for attempt in range(3):
            try:
                with urlopen(req, timeout=25) as resp:
                    data = resp.read(MAX_DOWNLOAD_BYTES + 1)
                    if len(data) > MAX_DOWNLOAD_BYTES:
                        raise SkillError("download too large")
                    return data
            except HTTPError as e:
                # ClawHub can briefly rate-limit; quick backoff keeps UX stable.
                if e.code == 429 and attempt < 2:
                    retry_after = e.headers.get("Retry-After", "").strip()
                    try:
                        wait_seconds = float(retry_after) if retry_after else 1.5 * (attempt + 1)
                    except ValueError:
                        wait_seconds = 1.5 * (attempt + 1)
                    time.sleep(min(8.0, max(0.5, wait_seconds)))
                    continue

                detail = ""
                try:
                    detail = e.read(200).decode("utf-8", errors="ignore")
                except Exception:
                    pass
                raise SkillError(f"HTTP {e.code} while fetching skill data. {detail}".strip()) from e
            except URLError as e:
                raise SkillError(f"network error: {e}") from e

        raise SkillError("failed to fetch data from skill hub")

    def _http_get_json(self, url: str) -> dict[str, Any]:
        raw = self._http_get_bytes(url, accept="application/json")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise SkillError("invalid JSON response from hub") from e
        if not isinstance(data, dict):
            raise SkillError("unexpected response from hub")
        return data

    @staticmethod
    def _extract_zip_bundle(
        zip_bytes: bytes,
    ) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile as e:
            raise SkillError("download is not a valid zip bundle") from e

        skill_members: list[zipfile.ZipInfo] = []
        meta_members: list[zipfile.ZipInfo] = []
        manifest_members: list[zipfile.ZipInfo] = []
        for info in zf.infolist():
            leaf = Path(info.filename).name.lower()
            if leaf == "skill.md":
                skill_members.append(info)
            elif leaf == "_meta.json":
                meta_members.append(info)
            elif leaf == SKILL_MANIFEST_NAME:
                manifest_members.append(info)

        if not skill_members:
            raise SkillError("bundle missing SKILL.md")
        if len(skill_members) != 1 or len(meta_members) > 1 or len(manifest_members) > 1:
            raise SkillError("bundle contains ambiguous duplicate skill metadata")
        skill_member = skill_members[0]
        meta_member = meta_members[0] if meta_members else None
        manifest_member = manifest_members[0] if manifest_members else None

        if skill_member.flag_bits & 0x1:
            raise SkillError("encrypted skill bundles are not supported")
        if skill_member.file_size > MAX_SKILL_TEXT_BYTES:
            raise SkillError("SKILL.md exceeds the uncompressed size limit")
        skill_bytes = zf.read(skill_member)
        if len(skill_bytes) > MAX_SKILL_TEXT_BYTES:
            raise SkillError("SKILL.md exceeds the uncompressed size limit")
        skill_text = skill_bytes.decode("utf-8", errors="replace")
        meta = None
        if meta_member:
            if meta_member.file_size > MAX_SKILL_META_BYTES:
                raise SkillError("skill metadata exceeds the uncompressed size limit")
            try:
                meta = json.loads(zf.read(meta_member).decode("utf-8", errors="replace"))
            except Exception:
                meta = None
        manifest = None
        if manifest_member:
            if manifest_member.flag_bits & 0x1:
                raise SkillError("encrypted skill manifests are not supported")
            if manifest_member.file_size > MAX_SKILL_MANIFEST_BYTES:
                raise SkillError("skill.json exceeds the uncompressed size limit")
            try:
                loaded = json.loads(zf.read(manifest_member).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SkillError("skill.json is not valid UTF-8 JSON") from exc
            if not isinstance(loaded, dict):
                raise SkillError("skill.json must contain a JSON object")
            manifest = loaded
        return skill_text, meta, manifest

    @staticmethod
    def _parse_target(target: str) -> tuple[str, str | None]:
        raw = target.strip()
        if not raw:
            raise SkillError("missing skill target")

        version = None
        if "@" in raw and not raw.startswith(("http://", "https://")):
            raw, version = raw.rsplit("@", 1)
            raw = raw.strip()
            version = version.strip() or None

        if raw.startswith(("http://", "https://")):
            parsed = urlparse(raw)
            query = parse_qs(parsed.query)
            q_slug = query.get("slug", [None])[0]
            if q_slug:
                slug = q_slug
            else:
                parts = [p for p in parsed.path.split("/") if p]
                if not parts:
                    raise SkillError("could not parse slug from URL")
                reserved = {
                    "skills",
                    "souls",
                    "u",
                    "upload",
                    "dashboard",
                    "search",
                    "settings",
                    "management",
                    "stars",
                    "admin",
                    "import",
                    "cli",
                    "auth",
                }
                if parts[0] in reserved:
                    if parts[0] == "skills" and len(parts) >= 2:
                        slug = parts[-1]
                    else:
                        raise SkillError("could not parse skill slug from URL")
                else:
                    slug = parts[-1]
            slug = slug.strip()
        else:
            slug = raw.split("/")[-1].strip()

        if not slug:
            raise SkillError("invalid skill target")

        slug = _sanitize_id(slug)
        if not slug:
            raise SkillError("invalid slug format")

        if version:
            version = version.strip()

        return slug, version

    def _build_record(self, directory: Path, source: str, skill_id: str) -> SkillRecord | None:
        if directory.is_symlink():
            return None
        skill_path = directory / "SKILL.md"
        if not skill_path.exists() or skill_path.is_symlink():
            return None

        source_meta_path = directory / "source.json"
        source_meta: dict[str, Any] = {}
        if source_meta_path.exists():
            try:
                source_meta = json.loads(source_meta_path.read_text(encoding="utf-8"))
            except Exception:
                source_meta = {}

        content = skill_path.read_text(encoding="utf-8", errors="replace")
        fm, body = _frontmatter(content)
        validation = validate_skill_directory(directory)
        manifest = (
            validation.get("manifest")
            if isinstance(validation.get("manifest"), dict)
            else {}
        )
        name = _first_non_empty(
            str(manifest.get("name") or ""),
            str(fm.get("name") if fm.get("name") is not None else ""),
            str(source_meta.get("display_name") if source_meta.get("display_name") is not None else ""),
            directory.name,
        )
        description = _first_non_empty(
            str(fm.get("description") if fm.get("description") is not None else ""),
            str(source_meta.get("summary") if source_meta.get("summary") is not None else ""),
            _body_summary(body),
        )

        return SkillRecord(
            skill_id=skill_id,
            name=name or skill_id,
            description=description,
            source=source,
            directory=directory,
            skill_path=skill_path,
            slug=source_meta.get("slug"),
            version=str(manifest.get("version") or source_meta.get("version") or "") or None,
            owner=str(manifest.get("owner") or source_meta.get("owner") or "") or None,
            manifest_path=directory / SKILL_MANIFEST_NAME,
            manifest=manifest,
            content_sha256=str(validation.get("content_sha256") or ""),
            validation_errors=tuple(str(error) for error in validation.get("errors", [])),
            isolated_only=bool(validation.get("isolated_only")),
        )

    def list_skills(self) -> list[SkillRecord]:
        records: list[SkillRecord] = []

        for path in sorted(self.hub_dir.iterdir() if self.hub_dir.exists() else []):
            if not path.is_dir() or path.is_symlink():
                continue
            rec = self._build_record(path, source="hub", skill_id=path.name)
            if rec:
                records.append(rec)

        for path in sorted(self.local_dir.iterdir() if self.local_dir.exists() else []):
            if not path.is_dir() or path.is_symlink():
                continue
            rec = self._build_record(path, source="local", skill_id=f"local/{path.name}")
            if rec:
                records.append(rec)

        records.sort(key=lambda r: r.skill_id.lower())
        return records

    def resolve_skill(self, ref: str) -> SkillRecord | None:
        key = ref.strip().lower()
        if not key:
            return None

        skills = self.list_skills()

        exact = [s for s in skills if s.skill_id.lower() == key]
        if len(exact) == 1:
            return exact[0]

        fuzzy: list[SkillRecord] = []
        for skill in skills:
            tokens = {skill.directory.name.lower(), skill.skill_id.lower()}
            if skill.slug:
                tokens.add(skill.slug.lower())
            if key in tokens:
                fuzzy.append(skill)

        if len(fuzzy) == 1:
            return fuzzy[0]
        return None

    def list_active(self, chat_id: str) -> list[str]:
        with self._lock:
            state = self._read_state()
            active = state.get("active_by_chat", {}).get(chat_id, [])
            if not isinstance(active, list):
                return []
            return [str(item) for item in active if isinstance(item, str) and item.strip()]

    def _set_active(
        self,
        chat_id: str,
        skill_ids: list[str],
        approved_hashes: dict[str, str] | None = None,
    ):
        state = self._read_state()
        active_by_chat = state.setdefault("active_by_chat", {})
        active_by_chat[chat_id] = skill_ids
        approvals_by_chat = state.setdefault("approved_hashes_by_chat", {})
        current = approvals_by_chat.get(chat_id, {})
        if not isinstance(current, dict):
            current = {}
        next_approvals = approved_hashes if approved_hashes is not None else current
        approvals_by_chat[chat_id] = {
            skill_id: str(content_hash)
            for skill_id, content_hash in next_approvals.items()
            if skill_id in skill_ids and str(content_hash)
        }
        self._write_state(state)

    def preview_activation(self, ref: str) -> dict[str, Any]:
        """Return the exact source, permissions, provenance, and hash before activation."""
        record = self.resolve_skill(ref)
        if not record:
            raise SkillError(f"skill not found: {ref}")
        report = validate_skill_directory(record.directory)
        manifest = report.get("manifest") if isinstance(report.get("manifest"), dict) else {}
        source: dict[str, Any] = {}
        source_path = record.directory / "source.json"
        if source_path.is_file() and not source_path.is_symlink():
            try:
                loaded = json.loads(source_path.read_text(encoding="utf-8"))
                source = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError):
                source = {}
        try:
            preview = record.skill_path.read_text(encoding="utf-8", errors="replace")[:1600]
        except OSError:
            preview = ""
        return {
            "skill_id": record.skill_id,
            "name": record.name,
            "source": record.source,
            "source_preview": preview,
            "owner": manifest.get("owner") or source.get("owner"),
            "version": manifest.get("version") or source.get("version"),
            "capabilities": manifest.get("capabilities", []),
            "network": manifest.get("network", {}),
            "writable_paths": manifest.get("writable_paths", []),
            "dependencies": manifest.get("dependencies", []),
            "content_sha256": report.get("content_sha256"),
            "instructions_sha256": report.get("instructions_sha256"),
            "manifest_sha256": report.get("manifest_sha256"),
            "activation_token": report.get("activation_token"),
            "provenance": source.get("provenance", source),
            "valid": report.get("valid"),
            "isolated_only": report.get("isolated_only"),
            "errors": report.get("errors", []),
        }

    def activate(self, chat_id: str, skill_id: str, confirmation: str | None = None):
        preview = self.preview_activation(skill_id)
        if not preview["valid"]:
            raise SkillError("skill validation failed: " + "; ".join(preview["errors"]))
        if preview["isolated_only"]:
            raise SkillError(
                "networked, writable, subprocess, or high-authority skills require an "
                "isolated external runner and cannot enter the core prompt"
            )
        expected = str(preview["activation_token"] or "")
        if not expected or confirmation != expected:
            raise SkillError(
                f"activation requires the reviewed content-hash token: {expected or 'unavailable'}"
            )
        canonical_id = str(preview["skill_id"])
        with self._lock:
            active = self.list_active(chat_id)
            state = self._read_state()
            approvals_by_chat = state.get("approved_hashes_by_chat", {})
            approvals = (
                dict(approvals_by_chat.get(chat_id, {}))
                if isinstance(approvals_by_chat, dict)
                and isinstance(approvals_by_chat.get(chat_id), dict)
                else {}
            )
            approvals[canonical_id] = str(preview["content_sha256"])
            if canonical_id not in active:
                active.append(canonical_id)
            self._set_active(chat_id, active, approvals)

    def deactivate(self, chat_id: str, skill_id: str):
        with self._lock:
            active = [sid for sid in self.list_active(chat_id) if sid != skill_id]
            state = self._read_state()
            approvals_by_chat = state.get("approved_hashes_by_chat", {})
            approvals = (
                dict(approvals_by_chat.get(chat_id, {}))
                if isinstance(approvals_by_chat, dict)
                and isinstance(approvals_by_chat.get(chat_id), dict)
                else {}
            )
            approvals.pop(skill_id, None)
            self._set_active(chat_id, active, approvals)

    def _deactivate_everywhere(self, skill_id: str):
        state = self._read_state()
        active_by_chat = state.get("active_by_chat", {})
        approvals_by_chat = state.get("approved_hashes_by_chat", {})
        changed = False

        for chat_id, active in list(active_by_chat.items()):
            if not isinstance(active, list):
                continue
            filtered = [sid for sid in active if sid != skill_id]
            if len(filtered) != len(active):
                active_by_chat[chat_id] = filtered
                if isinstance(approvals_by_chat, dict):
                    approvals = approvals_by_chat.get(chat_id)
                    if isinstance(approvals, dict):
                        approvals.pop(skill_id, None)
                changed = True

        if changed:
            state["active_by_chat"] = active_by_chat
            state["approved_hashes_by_chat"] = approvals_by_chat
            self._write_state(state)

    def active_records(self, chat_id: str) -> list[SkillRecord]:
        installed = {skill.skill_id: skill for skill in self.list_skills()}
        active_ids = self.list_active(chat_id)
        state = self._read_state()
        approvals_by_chat = state.get("approved_hashes_by_chat", {})
        approvals = (
            approvals_by_chat.get(chat_id, {})
            if isinstance(approvals_by_chat, dict)
            and isinstance(approvals_by_chat.get(chat_id), dict)
            else {}
        )
        active: list[SkillRecord] = []
        missing: list[str] = []

        for sid in active_ids:
            rec = installed.get(sid)
            if (
                rec
                and not rec.validation_errors
                and not rec.isolated_only
                and approvals.get(sid) == rec.content_sha256
            ):
                active.append(rec)
            else:
                missing.append(sid)

        if missing:
            with self._lock:
                cleaned = [sid for sid in active_ids if sid not in missing]
                cleaned_approvals = {
                    sid: str(content_hash)
                    for sid, content_hash in approvals.items()
                    if sid in cleaned
                }
                self._set_active(chat_id, cleaned, cleaned_approvals)

        return active

    def create_local_skill(self, name: str, description: str = "") -> SkillRecord:
        skill_name = name.strip()
        if not skill_name:
            raise SkillError("skill name is required")

        slug = _sanitize_id(skill_name)
        if not slug:
            raise SkillError("invalid skill name")

        directory = self.local_dir / slug
        if directory.exists():
            raise SkillError(f"local skill already exists: {slug}")

        directory.mkdir(parents=True, exist_ok=False)
        skill_path = directory / "SKILL.md"
        source_path = directory / "source.json"
        desc = description.strip() or "Custom local LightClaw skill."

        template = (
            "---\n"
            f"name: {skill_name}\n"
            f"description: {desc}\n"
            "---\n\n"
            f"# {skill_name}\n\n"
            "Purpose\n"
            "- Describe exactly what this skill should do.\n\n"
            "Rules\n"
            "- Add hard constraints and style rules.\n"
            "- Keep guidance concrete and testable.\n\n"
            "Workflow\n"
            "- Step 1\n"
            "- Step 2\n"
            "- Step 3\n"
        )
        _atomic_write_text(skill_path, template)
        manifest = _default_manifest(
            skill_id=slug,
            name=skill_name,
            version="0.1.0",
            owner="local-owner",
        )
        _atomic_write_json(directory / SKILL_MANIFEST_NAME, manifest)
        source = {
            "source": "local",
            "slug": slug,
            "display_name": skill_name,
            "summary": desc,
            "owner": "local-owner",
            "version": "0.1.0",
            "installed_at": int(time.time()),
            "instructions_sha256": _sha256_bytes(skill_path.read_bytes()),
            "content_sha256": _review_sha256(
                skill_path.read_bytes(),
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            ),
            "manifest_sha256": _sha256_bytes(
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
            ),
            "provenance": {
                "kind": "local-create",
                "created_at": int(time.time()),
            },
        }
        _atomic_write_json(source_path, source)

        rec = self._build_record(directory, source="local", skill_id=f"local/{slug}")
        if not rec:
            raise SkillError("failed to create local skill")
        if rec.validation_errors:
            shutil.rmtree(directory, ignore_errors=True)
            raise SkillError("created skill failed validation: " + "; ".join(rec.validation_errors))
        return rec

    def validate_all(self) -> list[dict[str, Any]]:
        return [validate_skill_directory(record.directory) for record in self.list_skills()]

    def remove_skill(self, ref: str) -> SkillRecord:
        rec = self.resolve_skill(ref)
        if not rec:
            raise SkillError(f"skill not found: {ref}")

        if rec.directory.exists():
            shutil.rmtree(rec.directory)
        self._deactivate_everywhere(rec.skill_id)
        return rec

    def install_from_hub(self, target: str, version: str | None = None) -> tuple[SkillRecord, bool]:
        slug, parsed_version = self._parse_target(target)
        if not version:
            version = parsed_version

        meta = self._http_get_json(f"{self.api_base_url}/skills/{quote(slug)}")
        skill_meta = meta.get("skill") or {}
        latest = meta.get("latestVersion") or {}
        owner_meta = meta.get("owner") or {}
        effective_version = (version or latest.get("version") or "").strip() or None

        params = {"slug": slug}
        if effective_version:
            params["version"] = effective_version
        zip_url = f"{self.api_base_url}/download?{urlencode(params)}"
        payload = self._http_get_bytes(zip_url, accept="application/zip")

        skill_text, archive_meta, archive_manifest = self._extract_zip_bundle(payload)
        if not skill_text.strip():
            raise SkillError("downloaded skill is empty")
        if not effective_version or not _VERSION_RE.fullmatch(effective_version):
            raise SkillError("hub skill must resolve to a pinned semantic version")
        display_name = str(skill_meta.get("displayName") or slug)
        owner = str(owner_meta.get("handle") or owner_meta.get("userId") or "hub-owner")
        requested_permissions = archive_manifest or _default_manifest(
            skill_id=slug,
            name=display_name,
            version=effective_version,
            owner=owner,
        )
        manifest = dict(requested_permissions)
        manifest.update(
            {
                "schema_version": SKILL_MANIFEST_SCHEMA_VERSION,
                "id": slug,
                "name": display_name,
                "version": effective_version,
                "owner": owner,
            }
        )
        manifest_errors = validate_skill_manifest(manifest)
        if manifest_errors:
            raise SkillError("hub skill manifest rejected: " + "; ".join(manifest_errors))

        source = {
            "source": "hub",
            "hub_base_url": self.hub_base_url,
            "slug": slug,
            "display_name": display_name,
            "summary": skill_meta.get("summary"),
            "owner": owner,
            "owner_id": owner_meta.get("userId"),
            "version": effective_version,
            "installed_at": int(time.time()),
            "download_sha256": _sha256_bytes(payload),
            "instructions_sha256": _sha256_bytes(skill_text.encode("utf-8")),
            "content_sha256": _review_sha256(
                skill_text.encode("utf-8"),
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            ),
            "manifest_sha256": _sha256_bytes(
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
            ),
            "provenance": {
                "kind": "clawhub",
                "metadata_url": f"{self.api_base_url}/skills/{quote(slug)}",
                "download_url": zip_url,
                "slug": slug,
                "owner": owner,
                "version": effective_version,
                "download_sha256": _sha256_bytes(payload),
            },
        }
        directory = self.hub_dir / slug
        replaced = directory.exists()
        staging = Path(tempfile.mkdtemp(prefix=f".{slug}.staging-", dir=self.hub_dir))
        backup = self.hub_dir / f".{slug}.backup-{time.time_ns()}"
        try:
            _atomic_write_text(staging / "SKILL.md", skill_text)
            _atomic_write_json(staging / SKILL_MANIFEST_NAME, manifest)
            if archive_meta is not None:
                _atomic_write_json(staging / "_meta.json", archive_meta)
            _atomic_write_json(staging / "source.json", source)
            report = validate_skill_directory(staging)
            if not report["valid"]:
                raise SkillError("staged skill validation failed: " + "; ".join(report["errors"]))
            if directory.exists():
                directory.rename(backup)
            staging.rename(directory)
            if backup.exists():
                shutil.rmtree(backup)
        except Exception:
            if not directory.exists() and backup.exists():
                backup.rename(directory)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)
        rec = self._build_record(directory, source="hub", skill_id=slug)
        if not rec:
            raise SkillError("skill installed but could not be loaded")
        return rec, replaced

    def search_hub(self, query: str, limit: int = 8) -> list[SkillSearchResult]:
        q = query.strip()
        if not q:
            raise SkillError("search query is required")

        url = f"{self.api_base_url}/search?{urlencode({'q': q})}"
        payload = self._http_get_json(url)
        rows = payload.get("results", [])
        if not isinstance(rows, list):
            return []

        results: list[SkillSearchResult] = []
        for row in rows[: max(1, limit)]:
            if not isinstance(row, dict):
                continue
            slug = str(row.get("slug", "")).strip()
            if not slug:
                continue
            results.append(
                SkillSearchResult(
                    slug=slug,
                    display_name=str(row.get("displayName", "") or slug),
                    summary=str(row.get("summary", "") or ""),
                    version=str(row.get("version", "") or "") or None,
                    score=float(row.get("score")) if isinstance(row.get("score"), (int, float)) else None,
                )
            )
        return results

    def prompt_context(
        self,
        chat_id: str,
        max_total_chars: int = 22000,
        max_per_skill_chars: int = 6000,
    ) -> str:
        active = self.active_records(chat_id)
        if not active:
            return ""

        parts = [
            "## Active Skills",
            (
                "The user activated these skills for this chat. "
                "Treat each skill as operating guidance and follow it unless it conflicts "
                "with explicit user instructions or safety constraints."
            ),
        ]

        budget = max_total_chars
        for skill in active:
            try:
                text = skill.skill_path.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                continue
            if not text:
                continue

            clipped = False
            if len(text) > max_per_skill_chars:
                text = text[:max_per_skill_chars].rstrip() + "\n...[truncated]"
                clipped = True

            block = (
                f"### {skill.skill_id} ({skill.source}, version {skill.version}, "
                f"sha256 {skill.content_sha256[:12]})\n"
                "Permission boundary: prompt-guidance only; no network, subprocess, "
                "credential, or workspace-write authority is granted by this skill.\n"
                f"{text}"
            )

            if len(block) > budget:
                if budget < 400:
                    parts.append("_Additional active skills omitted due to prompt size._")
                    break
                block = block[:budget].rstrip() + "\n...[truncated]"
                parts.append(block)
                parts.append("_Additional active skills omitted due to prompt size._")
                break

            parts.append(block)
            budget -= len(block)
            if clipped and budget <= 0:
                break

        return "\n\n".join(parts).strip()
