from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import (
    DEFAULT_ORDER,
    DEFAULT_QUESTIONS,
    HISTORY_DIR,
    HISTORY_INDEX_FILE,
    QUESTIONS_FILE,
    ROOMS_FILE,
    STATUS_FINISHED,
    STATUS_LOBBY,
    STATUS_PLAYING,
    STATUS_REVEALING,
    TIMEZONE,
    UNKNOWN_USERNAME,
    USERS_FILE,
    ADMINS_FILE,
)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
    tmp.replace(path)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _field(value: str | None) -> str:
    return (value or "").replace("\r", " ").replace("\n", " ")


def ensure_files() -> None:
    if not USERS_FILE.exists():
        USERS_FILE.write_text("", encoding="utf-8")
    if not ADMINS_FILE.exists():
        ADMINS_FILE.write_text("", encoding="utf-8")
    if not ROOMS_FILE.exists():
        _write_json(ROOMS_FILE, {})
    if not QUESTIONS_FILE.exists():
        _write_json(
            QUESTIONS_FILE,
            {"order": list(DEFAULT_ORDER), "texts": dict(DEFAULT_QUESTIONS)},
        )
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    if not HISTORY_INDEX_FILE.exists():
        _write_json(HISTORY_INDEX_FILE, [])


def today_str() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%d.%m.%Y")


def format_user_label(user_id: int, username: str | None) -> str:
    if username and username not in ("", UNKNOWN_USERNAME):
        name = username.lstrip("@")
        return f"@{name}"
    return f"id: {user_id} (username отсутствует)"


def format_list_line(
    user_id: int,
    username: str | None,
    is_owner: bool = False,
    is_admin: bool = False,
) -> str:
    uname = username.lstrip("@") if username and username not in ("", UNKNOWN_USERNAME) else None
    if uname:
        line = f"@{uname} (id: {user_id})"
    else:
        line = f"id: {user_id} (username отсутствует)"
    if is_owner:
        line += " (создатель)"
    elif is_admin:
        line += " (админ)"
    return line


# --- users.txt ---


def load_users() -> list[dict[str, str]]:
    ensure_files()
    rows: list[dict[str, str]] = []
    text = USERS_FILE.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(";", 2)
        user_id = parts[0].strip()
        username = parts[1].strip() if len(parts) > 1 else UNKNOWN_USERNAME
        first_name = parts[2] if len(parts) > 2 else ""
        rows.append({"user_id": user_id, "username": username, "first_name": first_name})
    return rows


def save_users(rows: list[dict[str, str]]) -> None:
    lines = [
        f"{_field(r['user_id'])};{_field(r.get('username') or UNKNOWN_USERNAME)};{_field(r.get('first_name'))}"
        for r in rows
    ]
    _write_text(USERS_FILE, "\n".join(lines) + ("\n" if lines else ""))


def is_authorized(user_id: int, owner_id: int) -> bool:
    if user_id == owner_id:
        return True
    if is_admin(user_id):
        return True
    return any(int(r["user_id"]) == user_id for r in load_users())


def add_user(user_id: int, username: str = UNKNOWN_USERNAME, first_name: str = "") -> bool:
    rows = load_users()
    if any(int(r["user_id"]) == user_id for r in rows):
        return False
    rows.append(
        {
            "user_id": str(user_id),
            "username": username or UNKNOWN_USERNAME,
            "first_name": first_name or "",
        }
    )
    save_users(rows)
    return True


def delete_user(query: str) -> bool:
    rows = load_users()
    q = query.strip().lstrip("@")
    remaining: list[dict[str, str]] = []
    found = False
    for r in rows:
        if q.isdigit() and r["user_id"] == q:
            found = True
            continue
        stored = (r.get("username") or "").lstrip("@")
        if not q.isdigit() and stored.lower() == q.lower() and stored not in ("", UNKNOWN_USERNAME):
            found = True
            continue
        remaining.append(r)
    if found:
        save_users(remaining)
    return found


def touch_user(user_id: int, username: str | None, first_name: str | None) -> None:
    rows = load_users()
    changed = False
    new_username = username or UNKNOWN_USERNAME
    new_first = first_name or ""
    for r in rows:
        if int(r["user_id"]) == user_id:
            if r.get("username") != new_username or r.get("first_name") != new_first:
                r["username"] = new_username
                r["first_name"] = new_first
                changed = True
            break
    if changed:
        save_users(rows)


def find_user_row(query: str) -> dict[str, str] | None:
    q = query.strip().lstrip("@")
    for r in load_users():
        if q.isdigit() and r["user_id"] == q:
            return r
        stored = (r.get("username") or "").lstrip("@")
        if not q.isdigit() and stored.lower() == q.lower() and stored not in ("", UNKNOWN_USERNAME):
            return r
    return None


# --- admins.txt ---


def load_admins() -> list[dict[str, str]]:
    ensure_files()
    rows: list[dict[str, str]] = []
    text = ADMINS_FILE.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(";", 2)
        user_id = parts[0].strip()
        username = parts[1].strip() if len(parts) > 1 else UNKNOWN_USERNAME
        first_name = parts[2] if len(parts) > 2 else ""
        rows.append({"user_id": user_id, "username": username, "first_name": first_name})
    return rows


def save_admins(rows: list[dict[str, str]]) -> None:
    lines = [
        f"{_field(r['user_id'])};{_field(r.get('username') or UNKNOWN_USERNAME)};{_field(r.get('first_name'))}"
        for r in rows
    ]
    _write_text(ADMINS_FILE, "\n".join(lines) + ("\n" if lines else ""))


def is_admin(user_id: int) -> bool:
    return any(int(r["user_id"]) == user_id for r in load_admins())


def add_admin(user_id: int, username: str = UNKNOWN_USERNAME, first_name: str = "") -> bool:
    rows = load_admins()
    if any(int(r["user_id"]) == user_id for r in rows):
        return False
    rows.append(
        {
            "user_id": str(user_id),
            "username": username or UNKNOWN_USERNAME,
            "first_name": first_name or "",
        }
    )
    save_admins(rows)
    return True


def remove_admin(query: str) -> bool:
    rows = load_admins()
    q = query.strip().lstrip("@")
    remaining: list[dict[str, str]] = []
    found = False
    for r in rows:
        if q.isdigit() and r["user_id"] == q:
            found = True
            continue
        stored = (r.get("username") or "").lstrip("@")
        if not q.isdigit() and stored.lower() == q.lower() and stored not in ("", UNKNOWN_USERNAME):
            found = True
            continue
        remaining.append(r)
    if found:
        save_admins(remaining)
    return found


def touch_admin(user_id: int, username: str | None, first_name: str | None) -> None:
    rows = load_admins()
    changed = False
    new_username = username or UNKNOWN_USERNAME
    new_first = first_name or ""
    for r in rows:
        if int(r["user_id"]) == user_id:
            if r.get("username") != new_username or r.get("first_name") != new_first:
                r["username"] = new_username
                r["first_name"] = new_first
                changed = True
            break
    if changed:
        save_admins(rows)


def find_admin_row(query: str) -> dict[str, str] | None:
    q = query.strip().lstrip("@")
    for r in load_admins():
        if q.isdigit() and r["user_id"] == q:
            return r
        stored = (r.get("username") or "").lstrip("@")
        if not q.isdigit() and stored.lower() == q.lower() and stored not in ("", UNKNOWN_USERNAME):
            return r
    return None


# --- questions ---


def load_questions() -> dict[str, Any]:
    ensure_files()
    data = _read_json(QUESTIONS_FILE, {"order": list(DEFAULT_ORDER), "texts": dict(DEFAULT_QUESTIONS)})
    if "order" not in data or "texts" not in data:
        data = {"order": list(DEFAULT_ORDER), "texts": dict(DEFAULT_QUESTIONS)}
    return data


def save_questions(data: dict[str, Any]) -> None:
    _write_json(QUESTIONS_FILE, data)


def snapshot_questions() -> list[dict[str, Any]]:
    cfg = load_questions()
    texts = cfg["texts"]
    result = []
    for num in cfg["order"]:
        key = str(num)
        result.append({"id": int(num), "text": texts.get(key, DEFAULT_QUESTIONS.get(key, key))})
    return result


# --- rooms ---


def load_rooms() -> dict[str, Any]:
    ensure_files()
    data = _read_json(ROOMS_FILE, {})
    if not isinstance(data, dict):
        return {}
    return data


def save_rooms(rooms: dict[str, Any]) -> None:
    _write_json(ROOMS_FILE, rooms)


def get_active_room(rooms: dict[str, Any] | None = None) -> dict[str, Any] | None:
    rooms = rooms if rooms is not None else load_rooms()
    for room in rooms.values():
        if room.get("status") in (STATUS_LOBBY, STATUS_PLAYING, STATUS_REVEALING):
            return room
    return None


def find_player_room(user_id: int, rooms: dict[str, Any] | None = None) -> dict[str, Any] | None:
    rooms = rooms if rooms is not None else load_rooms()
    for room in rooms.values():
        if room.get("status") not in (STATUS_LOBBY, STATUS_PLAYING, STATUS_REVEALING):
            continue
        for p in room.get("players", []):
            if int(p["user_id"]) == user_id:
                return room
    return None


def player_in_room(room: dict[str, Any], user_id: int) -> bool:
    return any(int(p["user_id"]) == user_id for p in room.get("players", []))


def find_player(room: dict[str, Any], query: str) -> dict[str, Any] | None:
    q = query.strip().lstrip("@")
    for p in room.get("players", []):
        if q.isdigit() and str(p["user_id"]) == q:
            return p
        stored = (p.get("username") or "").lstrip("@")
        if not q.isdigit() and stored.lower() == q.lower() and stored not in ("", UNKNOWN_USERNAME):
            return p
    return None


# --- history ---


def load_history_index() -> list[dict[str, Any]]:
    ensure_files()
    data = _read_json(HISTORY_INDEX_FILE, [])
    if not isinstance(data, list):
        return []
    return data


def save_history_index(index: list[dict[str, Any]]) -> None:
    _write_json(HISTORY_INDEX_FILE, index)


def append_history(
    room_id: str,
    date: str,
    players: list[dict[str, Any]],
    file_text: str,
    raw: dict[str, Any] | None = None,
    mode: str = "V1",
) -> Path:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"history_{room_id}_{date}.txt"
    path.write_text(file_text, encoding="utf-8")
    if raw is not None:
        _write_json(HISTORY_DIR / f"{room_id}_{date}_raw.json", raw)
    index = load_history_index()
    index.append(
        {
            "room_id": str(room_id),
            "date": date,
            "mode": mode,
            "players": [
                {"user_id": int(p["user_id"]), "username": p.get("username") or ""}
                for p in players
            ],
            "file": str(path.name),
        }
    )
    save_history_index(index)
    return path


def history_for_user(user_id: int, owner_id: int) -> list[dict[str, Any]]:
    index = load_history_index()
    if user_id == owner_id:
        return index
    result = []
    for item in index:
        ids = [int(p["user_id"]) for p in item.get("players", [])]
        if user_id in ids:
            result.append(item)
    return result


def get_history_entry(room_id: str) -> dict[str, Any] | None:
    for item in load_history_index():
        if str(item.get("room_id")) == str(room_id):
            return item
    return None


def history_file_path(entry: dict[str, Any]) -> Path | None:
    name = entry.get("file")
    if name:
        path = HISTORY_DIR / name
        if path.exists():
            return path
    room_id = entry.get("room_id")
    date = entry.get("date")
    if room_id and date:
        path = HISTORY_DIR / f"history_{room_id}_{date}.txt"
        if path.exists():
            return path
    return None


def load_history_raw(entry: dict[str, Any]) -> dict[str, Any] | None:
    room_id = entry.get("room_id")
    date = entry.get("date")
    if not room_id or not date:
        return None
    path = HISTORY_DIR / f"{room_id}_{date}_raw.json"
    if not path.exists():
        return None
    data = _read_json(path, None)
    if not isinstance(data, dict):
        return None
    return data
