"""
Lightweight skill manager for LightClaw.

Features:
- Install skills from ClawHub (`/api/v1`) as zip bundles containing `SKILL.md`
- Create local custom skills
- Activate/deactivate skills per Telegram chat (persisted in JSON)
- Build compact prompt context from active skills
"""

from __future__ import annotations

import io
import json
import re
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen


DEFAULT_HUB_BASE_URL = "https://clawhub.ai"
DEFAULT_API_PREFIX = "/api/v1"
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024

_FRONTMATTER_RE = re.compile(r"^---\s*\n([\s\S]*?)\n---\s*(?:\n|$)")
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9._-]+")


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
        self.skills_root = self.workspace / "skills"
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
        self.hub_dir.mkdir(parents=True, exist_ok=True)
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"active_by_chat": {}}

        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            active = data.get("active_by_chat")
            if not isinstance(active, dict):
                return {"active_by_chat": {}}
            return {"active_by_chat": active}
        except Exception:
            return {"active_by_chat": {}}

    def _write_state(self, state: dict[str, Any]):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_path)

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
    def _extract_zip_bundle(zip_bytes: bytes) -> tuple[str, dict[str, Any] | None]:
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile as e:
            raise SkillError("download is not a valid zip bundle") from e

        skill_member = None
        meta_member = None
        for name in zf.namelist():
            leaf = Path(name).name.lower()
            if leaf == "skill.md" and skill_member is None:
                skill_member = name
            elif leaf == "_meta.json" and meta_member is None:
                meta_member = name

        if not skill_member:
            raise SkillError("bundle missing SKILL.md")

        skill_text = zf.read(skill_member).decode("utf-8", errors="replace")
        meta = None
        if meta_member:
            try:
                meta = json.loads(zf.read(meta_member).decode("utf-8", errors="replace"))
            except Exception:
                meta = None
        return skill_text, meta

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
        skill_path = directory / "SKILL.md"
        if not skill_path.exists():
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
        name = _first_non_empty(
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
            version=source_meta.get("version"),
            owner=source_meta.get("owner"),
        )

    def list_skills(self) -> list[SkillRecord]:
        records: list[SkillRecord] = []

        for path in sorted(self.hub_dir.iterdir() if self.hub_dir.exists() else []):
            if not path.is_dir():
                continue
            rec = self._build_record(path, source="hub", skill_id=path.name)
            if rec:
                records.append(rec)

        for path in sorted(self.local_dir.iterdir() if self.local_dir.exists() else []):
            if not path.is_dir():
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

    def _set_active(self, chat_id: str, skill_ids: list[str]):
        state = self._read_state()
        active_by_chat = state.setdefault("active_by_chat", {})
        active_by_chat[chat_id] = skill_ids
        self._write_state(state)

    def activate(self, chat_id: str, skill_id: str):
        with self._lock:
            active = self.list_active(chat_id)
            if skill_id not in active:
                active.append(skill_id)
                self._set_active(chat_id, active)

    def deactivate(self, chat_id: str, skill_id: str):
        with self._lock:
            active = [sid for sid in self.list_active(chat_id) if sid != skill_id]
            self._set_active(chat_id, active)

    def _deactivate_everywhere(self, skill_id: str):
        state = self._read_state()
        active_by_chat = state.get("active_by_chat", {})
        changed = False

        for chat_id, active in list(active_by_chat.items()):
            if not isinstance(active, list):
                continue
            filtered = [sid for sid in active if sid != skill_id]
            if len(filtered) != len(active):
                active_by_chat[chat_id] = filtered
                changed = True

        if changed:
            state["active_by_chat"] = active_by_chat
            self._write_state(state)

    def active_records(self, chat_id: str) -> list[SkillRecord]:
        installed = {skill.skill_id: skill for skill in self.list_skills()}
        active_ids = self.list_active(chat_id)
        active: list[SkillRecord] = []
        missing: list[str] = []

        for sid in active_ids:
            rec = installed.get(sid)
            if rec:
                active.append(rec)
            else:
                missing.append(sid)

        if missing:
            with self._lock:
                cleaned = [sid for sid in active_ids if sid not in missing]
                self._set_active(chat_id, cleaned)

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
        skill_path.write_text(template, encoding="utf-8")
        source = {
            "source": "local",
            "slug": slug,
            "display_name": skill_name,
            "summary": desc,
            "version": "local",
            "installed_at": int(time.time()),
        }
        source_path.write_text(json.dumps(source, indent=2, sort_keys=True), encoding="utf-8")

        rec = self._build_record(directory, source="local", skill_id=f"local/{slug}")
        if not rec:
            raise SkillError("failed to create local skill")
        return rec

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

        skill_text, archive_meta = self._extract_zip_bundle(payload)
        if not skill_text.strip():
            raise SkillError("downloaded skill is empty")

        directory = self.hub_dir / slug
        replaced = directory.exists()
        directory.mkdir(parents=True, exist_ok=True)

        (directory / "SKILL.md").write_text(skill_text, encoding="utf-8")
        if archive_meta is not None:
            (directory / "_meta.json").write_text(
                json.dumps(archive_meta, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        source = {
            "source": "hub",
            "hub_base_url": self.hub_base_url,
            "slug": slug,
            "display_name": skill_meta.get("displayName"),
            "summary": skill_meta.get("summary"),
            "owner": owner_meta.get("handle"),
            "owner_id": owner_meta.get("userId"),
            "version": effective_version or latest.get("version"),
            "installed_at": int(time.time()),
        }
        (directory / "source.json").write_text(
            json.dumps(source, indent=2, sort_keys=True),
            encoding="utf-8",
        )

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
                f"### {skill.skill_id} ({skill.source})\n"
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
