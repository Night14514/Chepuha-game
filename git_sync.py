from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx

import config

log = logging.getLogger("chepuha.git")

Status = Literal["no_changes", "pushed", "error"]

GITHUB_OWNER = "Night14514"
GITHUB_REPO = "Chepuha-game"
GITHUB_API_BASE = "https://api.github.com"


@dataclass
class SyncResult:
    status: Status
    detail: str


def _mask(text: str) -> str:
    token = config.GITHUB_TOKEN or ""
    if token and len(token) > 10:
        return (text or "").replace(token, "***")
    return text or ""


def _clip(text: str, limit: int = 300) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _check_token() -> bool:
    token = config.GITHUB_TOKEN or ""
    if not token:
        log.error("GITHUB_TOKEN is empty")
        return False
    if len(token) < 20:
        log.error("GITHUB_TOKEN looks invalid (too short)")
        return False
    return True


async def _sync_file(local_path: Path, repo_filename: str) -> SyncResult:
    if not _check_token():
        return SyncResult(
            "error",
            "Ошибка доступа к репозиторию: проверьте, что GITHUB_TOKEN действителен и имеет права на запись.",
        )

    try:
        local_content = local_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.error("%s not found at %s", repo_filename, local_path)
        return SyncResult(
            "error",
            f"Файл {repo_filename} не найден на сервере, обратитесь к администратору хостинга.",
        )
    except Exception as e:
        log.error("Failed to read %s: %s", repo_filename, e)
        return SyncResult(
            "error",
            f"Не удалось прочитать {repo_filename}: {_clip(str(e))}.",
        )

    api_url = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{repo_filename}"
    headers = {
        "Authorization": f"token {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            log.info("Fetching current %s state from GitHub...", repo_filename)
            try:
                resp = await client.get(api_url, headers=headers)
            except httpx.TimeoutException:
                log.error("Timeout fetching file from GitHub")
                return SyncResult(
                    "error",
                    "Не удалось связаться с GitHub (таймаут). Попробуйте позже.",
                )
            except httpx.RequestError as e:
                log.error("Request error fetching file: %s", e)
                return SyncResult(
                    "error",
                    "Не удалось связаться с GitHub. Попробуйте позже.",
                )

            current_sha = None
            current_content = ""

            if resp.status_code == 200:
                data = resp.json()
                current_sha = data.get("sha")
                try:
                    current_content = base64.b64decode(data.get("content", "")).decode(
                        "utf-8"
                    )
                except Exception as e:
                    log.error("Failed to decode current file content: %s", e)
                    current_content = ""
                if local_content == current_content:
                    log.info("%s hasn't changed", repo_filename)
                    return SyncResult(
                        "no_changes",
                        f"Изменений в {repo_filename} нет, пушить нечего.",
                    )
            elif resp.status_code == 404:
                log.info("%s doesn't exist in repo yet, will create", repo_filename)
                current_sha = None
            elif resp.status_code in (401, 403):
                log.error("GitHub auth failed: %s", resp.status_code)
                return SyncResult(
                    "error",
                    "Ошибка доступа к репозиторию: проверьте, что GITHUB_TOKEN действителен и имеет права на запись.",
                )
            else:
                log.error("GitHub API error: %s %s", resp.status_code, _mask(resp.text))
                return SyncResult(
                    "error",
                    f"Не удалось проверить {repo_filename} на GitHub: {resp.status_code}.",
                )

            new_content_b64 = base64.b64encode(local_content.encode("utf-8")).decode(
                "utf-8"
            )
            stamp = datetime.now(ZoneInfo(config.TIMEZONE)).isoformat(timespec="seconds")
            payload = {
                "message": f"{repo_filename}: автосохранение {stamp}",
                "content": new_content_b64,
                "branch": "master",
            }
            if current_sha:
                payload["sha"] = current_sha

            log.info("Pushing %s to GitHub...", repo_filename)
            try:
                resp = await client.put(api_url, headers=headers, json=payload)
            except httpx.TimeoutException:
                log.error("Timeout pushing to GitHub")
                return SyncResult(
                    "error",
                    "Не удалось связаться с GitHub (таймаут). Попробуйте позже.",
                )
            except httpx.RequestError as e:
                log.error("Request error pushing: %s", e)
                return SyncResult(
                    "error",
                    "Не удалось связаться с GitHub. Попробуйте позже.",
                )

            if resp.status_code in (200, 201):
                try:
                    data = resp.json()
                    commit_sha = data.get("commit", {}).get("sha", "")[:7]
                    log.info("Successfully pushed %s (%s)", repo_filename, commit_sha)
                    extra = f" ({commit_sha})" if commit_sha else ""
                    return SyncResult(
                        "pushed",
                        f"{repo_filename} сохранён и отправлен в GitHub.{extra}",
                    )
                except Exception as e:
                    log.error("Failed to parse push response: %s", e)
                    return SyncResult(
                        "pushed",
                        f"{repo_filename} сохранён и отправлен в GitHub.",
                    )

            if resp.status_code in (401, 403):
                log.error("GitHub auth failed on push: %s", resp.status_code)
                return SyncResult(
                    "error",
                    "Ошибка доступа к репозиторию: проверьте, что GITHUB_TOKEN действителен и имеет права на запись.",
                )

            if resp.status_code == 422:
                log.error("GitHub push rejected: %s", resp.status_code)
                try:
                    error_data = resp.json()
                    message = error_data.get("message", "")
                    if "reference update refused" in message.lower() or "non-fast" in message.lower():
                        return SyncResult(
                            "error",
                            "Локальная копия отстала от GitHub, автоматический пуш отменён "
                            "во избежание конфликта. Изменения сохранены локальным коммитом, "
                            "нужна ручная синхронизация.",
                        )
                except Exception:
                    pass
                return SyncResult(
                    "error",
                    f"Не удалось сохранить {repo_filename} в GitHub: конфликт версий.",
                )

            log.error(
                "GitHub API error on push: %s %s",
                resp.status_code,
                _mask(resp.text),
            )
            reason = _clip(_mask(resp.text)) or "неизвестная ошибка"
            return SyncResult(
                "error",
                f"Не удалось сохранить {repo_filename} в GitHub: {reason}.",
            )

    except Exception as e:
        log.error("Unexpected error in _sync_file: %s", e)
        return SyncResult(
            "error",
            f"Неожиданная ошибка: {_clip(str(e))}.",
        )


async def sync_users_file() -> SyncResult:
    return await _sync_file(config.USERS_FILE, "users.txt")


async def sync_admins_file() -> SyncResult:
    return await _sync_file(config.ADMINS_FILE, "admins.txt")
