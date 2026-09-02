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
GITHUB_FILE = "users.txt"
GITHUB_API_BASE = "https://api.github.com"


@dataclass
class SyncResult:
    status: Status
    detail: str


def _mask(text: str) -> str:
    """Маскирует GitHub токен в текстах логов"""
    token = config.GITHUB_TOKEN or ""
    if token and len(token) > 10:
        return (text or "").replace(token, "***")
    return text or ""


def _clip(text: str, limit: int = 300) -> str:
    """Обрезает текст до лимита"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _check_token() -> bool:
    """Проверяет наличие и валидность токена"""
    token = config.GITHUB_TOKEN or ""
    if not token:
        log.error("GITHUB_TOKEN is empty")
        return False
    if len(token) < 20:  # PAT обычно длинный
        log.error("GITHUB_TOKEN looks invalid (too short)")
        return False
    return True


async def sync_users_file() -> SyncResult:
    """
    Синхронизирует users.txt с GitHub через API.
    Не требует git binary, работает чисто через HTTP.
    """

    if not _check_token():
        return SyncResult(
            "error",
            "Ошибка доступа к репозиторию: проверьте, что GITHUB_TOKEN действителен и имеет права на запись.",
        )

    # Прочитаем локальный файл
    users_file_path = config.USERS_FILE
    try:
        with open(users_file_path, "r", encoding="utf-8") as f:
            local_content = f.read()
    except FileNotFoundError:
        log.error(f"users.txt not found at {users_file_path}")
        return SyncResult(
            "error",
            "Файл users.txt не найден на сервере, обратитесь к администратору хостинга.",
        )
    except Exception as e:
        log.error(f"Failed to read users.txt: {e}")
        return SyncResult(
            "error",
            f"Не удалось прочитать users.txt: {_clip(str(e))}.",
        )

    # GitHub API endpoint для файла
    api_url = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{GITHUB_FILE}"

    headers = {
        "Authorization": f"token {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # 1. Получаем текущее состояние файла в репо
            log.info("Fetching current file state from GitHub...")
            try:
                resp = await client.get(api_url, headers=headers)
            except httpx.TimeoutException:
                log.error("Timeout fetching file from GitHub")
                return SyncResult(
                    "error",
                    "Не удалось связаться с GitHub (таймаут). Попробуйте позже.",
                )
            except httpx.RequestError as e:
                log.error(f"Request error fetching file: {e}")
                return SyncResult(
                    "error",
                    "Не удалось связаться с GitHub. Попробуйте позже.",
                )

            current_sha = None
            current_content = ""

            if resp.status_code == 200:
                # Файл существует в репо
                data = resp.json()
                current_sha = data.get("sha")
                # GitHub возвращает content в base64
                try:
                    current_content = base64.b64decode(data.get("content", "")).decode(
                        "utf-8"
                    )
                except Exception as e:
                    log.error(f"Failed to decode current file content: {e}")
                    current_content = ""

                # Проверяем, изменился ли файл
                if local_content == current_content:
                    log.info("users.txt hasn't changed")
                    return SyncResult(
                        "no_changes", "Изменений в users.txt нет, пушить нечего."
                    )
            elif resp.status_code == 404:
                # Файла ещё нет в репо — создадим его
                log.info("users.txt doesn't exist in repo yet, will create")
                current_sha = None
            elif resp.status_code == 401:
                log.error(f"GitHub auth failed: {resp.status_code}")
                return SyncResult(
                    "error",
                    "Ошибка доступа к репозиторию: проверьте, что GITHUB_TOKEN действителен и имеет права на запись.",
                )
            elif resp.status_code == 403:
                log.error(f"GitHub forbidden: {resp.status_code}")
                return SyncResult(
                    "error",
                    "Ошибка доступа к репозиторию: проверьте, что GITHUB_TOKEN действителен и имеет права на запись.",
                )
            else:
                log.error(f"GitHub API error: {resp.status_code} {_mask(resp.text)}")
                return SyncResult(
                    "error",
                    f"Не удалось проверить users.txt на GitHub: {resp.status_code}.",
                )

            # 2. Кодируем новое содержимое в base64
            new_content_b64 = base64.b64encode(
                local_content.encode("utf-8")
            ).decode("utf-8")

            # 3. Формируем сообщение коммита
            stamp = datetime.now(ZoneInfo(config.TIMEZONE)).isoformat(
                timespec="seconds"
            )
            commit_message = f"users.txt: автосохранение {stamp}"

            # 4. Подготавливаем payload для PUT запроса
            payload = {
                "message": commit_message,
                "content": new_content_b64,
                "branch": "main",
            }
            if current_sha:
                payload["sha"] = current_sha

            # 5. Отправляем обновление
            log.info("Pushing users.txt to GitHub...")
            try:
                resp = await client.put(api_url, headers=headers, json=payload)
            except httpx.TimeoutException:
                log.error("Timeout pushing to GitHub")
                return SyncResult(
                    "error",
                    "Не удалось связаться с GitHub (таймаут). Попробуйте позже.",
                )
            except httpx.RequestError as e:
                log.error(f"Request error pushing: {e}")
                return SyncResult(
                    "error",
                    "Не удалось связаться с GitHub. Попробуйте позже.",
                )

            if resp.status_code in (200, 201):
                # Успех
                try:
                    data = resp.json()
                    commit_sha = data.get("commit", {}).get("sha", "")[:7]
                    log.info(f"Successfully pushed users.txt ({commit_sha})")
                    return SyncResult(
                        "pushed",
                        f"users.txt сохранён и отправлен в GitHub. ({commit_sha})",
                    )
                except Exception as e:
                    log.error(f"Failed to parse push response: {e}")
                    return SyncResult(
                        "pushed", "users.txt сохранён и отправлен в GitHub."
                    )

            elif resp.status_code == 401:
                log.error(f"GitHub auth failed on push: {resp.status_code}")
                return SyncResult(
                    "error",
                    "Ошибка доступа к репозиторию: проверьте, что GITHUB_TOKEN действителен и имеет права на запись.",
                )

            elif resp.status_code == 403:
                log.error(f"GitHub forbidden on push: {resp.status_code}")
                return SyncResult(
                    "error",
                    "Ошибка доступа к репозиторию: проверьте, что GITHUB_TOKEN действителен и имеет права на запись.",
                )

            elif resp.status_code == 422:
                # Обычно конфликт (non-fast-forward или другое)
                log.error(f"GitHub push rejected: {resp.status_code}")
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
                except:
                    pass
                return SyncResult(
                    "error",
                    "Не удалось сохранить users.txt в GitHub: конфликт версий.",
                )

            else:
                log.error(
                    f"GitHub API error on push: {resp.status_code} {_mask(resp.text)}"
                )
                reason = _clip(_mask(resp.text)) or "неизвестная ошибка"
                return SyncResult(
                    "error",
                    f"Не удалось сохранить users.txt в GitHub: {reason}.",
                )

    except Exception as e:
        log.error(f"Unexpected error in sync_users_file: {e}")
        return SyncResult(
            "error",
            f"Неожиданная ошибка: {_clip(str(e))}.",
    )
            
