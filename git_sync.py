from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import config

log = logging.getLogger("chepuha.git")

Status = Literal["no_changes", "pushed", "error"]

USERS_REL = "users.txt"
PUSH_REMOTE = "https://github.com/Night14514/Chepuha-game.git"


@dataclass
class SyncResult:
    status: Status
    detail: str


def _mask(text: str) -> str:
    token = config.GITHUB_TOKEN or ""
    if token:
        return (text or "").replace(token, "***")
    return text or ""


def _clip(text: str, limit: int = 300) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _repo() -> Path:
    return Path(config.GIT_REPO_PATH)


def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _classify_push_error(stderr: str) -> str:
    low = (stderr or "").lower()
    if any(
        marker in low
        for marker in (
            "401",
            "403",
            "permission denied",
            "authentication failed",
            "invalid username",
            "could not read username",
        )
    ):
        return (
            "Ошибка доступа к репозиторию: проверьте, что GITHUB_TOKEN "
            "действителен и имеет права на запись."
        )
    if any(
        marker in low
        for marker in (
            "non-fast-forward",
            "failed to push some refs",
            "updates were rejected",
            "[rejected]",
        )
    ):
        return (
            "Локальная копия отстала от GitHub, автоматический пуш отменён "
            "во избежание конфликта. Изменения сохранены локальным коммитом, "
            "нужна ручная синхронизация."
        )
    reason = _clip(_mask(stderr)) or "неизвестная ошибка"
    return f"Не удалось сохранить users.txt в GitHub: {reason}."


def sync_users_file() -> SyncResult:
    repo = _repo()
    try:
        inside = _run(
            ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
            timeout=15,
        )
    except FileNotFoundError:
        log.error("Git binary not found")
        return SyncResult(
            "error",
            "Репозиторий не найден на сервере, обратитесь к администратору хостинга.",
        )
    except subprocess.TimeoutExpired:
        log.error("git rev-parse timed out")
        return SyncResult(
            "error",
            "Не удалось связаться с GitHub (таймаут). Попробуйте позже.",
        )
    if inside.returncode != 0 or (inside.stdout or "").strip() != "true":
        log.error("Not a git repo: %s", _mask(inside.stderr))
        return SyncResult(
            "error",
            "Репозиторий не найден на сервере, обратитесь к администратору хостинга.",
        )

    try:
        status = _run(
            ["git", "-C", str(repo), "status", "--porcelain", "--", USERS_REL],
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        log.error("git status timed out")
        return SyncResult(
            "error",
            "Не удалось связаться с GitHub (таймаут). Попробуйте позже.",
        )
    if status.returncode != 0:
        log.error("git status failed: %s", _mask(status.stderr))
        return SyncResult(
            "error",
            f"Не удалось сохранить users.txt в GitHub: {_clip(_mask(status.stderr)) or 'ошибка git status'}.",
        )
    if not (status.stdout or "").strip():
        return SyncResult("no_changes", "Изменений в users.txt нет, пушить нечего.")

    try:
        added = _run(["git", "-C", str(repo), "add", USERS_REL], timeout=15)
    except subprocess.TimeoutExpired:
        log.error("git add timed out")
        return SyncResult(
            "error",
            "Не удалось связаться с GitHub (таймаут). Попробуйте позже.",
        )
    if added.returncode != 0:
        log.error("git add failed: %s", _mask(added.stderr))
        return SyncResult(
            "error",
            f"Не удалось сохранить users.txt в GitHub: {_clip(_mask(added.stderr)) or 'ошибка git add'}.",
        )

    stamp = datetime.now(ZoneInfo(config.TIMEZONE)).isoformat(timespec="seconds")
    message = f"users.txt: автосохранение {stamp}"
    try:
        committed = _run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                f"user.name={config.GIT_AUTHOR_NAME}",
                "-c",
                f"user.email={config.GIT_AUTHOR_EMAIL}",
                "commit",
                "-m",
                message,
            ],
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        log.error("git commit timed out")
        return SyncResult(
            "error",
            "Не удалось связаться с GitHub (таймаут). Попробуйте позже.",
        )
    if committed.returncode != 0:
        log.error("git commit failed: %s", _mask(committed.stderr or committed.stdout))
        return SyncResult(
            "error",
            f"Не удалось сохранить users.txt в GitHub: {_clip(_mask(committed.stderr or committed.stdout)) or 'ошибка git commit'}.",
        )

    try:
        branch_p = _run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=15,
        )
        short_p = _run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        log.error("git rev-parse timed out after commit")
        return SyncResult(
            "error",
            "Не удалось связаться с GitHub (таймаут). Попробуйте позже.",
        )
    branch = (branch_p.stdout or "").strip()
    short_hash = (short_p.stdout or "").strip()
    if not branch or branch == "HEAD":
        log.error("Could not determine branch: %s", _mask(branch_p.stderr))
        return SyncResult(
            "error",
            "Не удалось сохранить users.txt в GitHub: не удалось определить текущую ветку.",
        )

    token = config.GITHUB_TOKEN or ""
    if not token:
        log.error("GITHUB_TOKEN is empty")
        return SyncResult(
            "error",
            "Ошибка доступа к репозиторию: проверьте, что GITHUB_TOKEN действителен и имеет права на запись.",
        )

    push_url = f"https://{token}@{PUSH_REMOTE.removeprefix('https://')}"
    try:
        pushed = _run(
            ["git", "-C", str(repo), "push", push_url, branch],
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        log.error("Git push failed: timeout")
        return SyncResult(
            "error",
            "Не удалось связаться с GitHub (таймаут). Попробуйте позже.",
        )
    if pushed.returncode != 0:
        stderr_safe = _mask(pushed.stderr or pushed.stdout)
        log.error("Git push failed: %s", stderr_safe)
        return SyncResult("error", _classify_push_error(pushed.stderr or pushed.stdout))

    extra = f" ({short_hash})" if short_hash else ""
    return SyncResult("pushed", f"users.txt сохранён и отправлен в GitHub.{extra}")
