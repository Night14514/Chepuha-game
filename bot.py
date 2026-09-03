#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import html
import logging
import random
import sys
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, FSInputFile, Message, ReactionTypeEmoji, TelegramObject
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

import config
import git_sync
import storage as st

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("chepuha")

MSG_DENIED = (
    "Вы не добавлены в список участников игры, для добавления пишите в лс @enotdev"
)
MSG_OWNER_ONLY = "Эта команда доступна только создателю."
MSG_ADMIN_ONLY = "Эта команда доступна только создателю или администраторам."
MSG_USAGE_HELP = "Список команд: /help"

OWNER_COMMANDS = {
    "list",
    "rooms",
    "edit",
    "edit_qu",
    "save",
    "grand",
    "unadmin",
    "admins",
}

ADMIN_COMMANDS = {
    "add",
    "delete",
    "create",
    "exc",
    "game",
    "stop",
    "players",
}

HELP_TEXT_USER = """Команды:

Для всех:
/start — доступ к боту
/join <id> — войти в комнату
/exit — выйти из комнаты (только до начала игры)
/rules — правила
/result — ваши завершённые игры
/get <id> — получить файл истории игры
/finish — подтвердить, что дочитал историю (только во время чтения)
/help — этот список
/cancel — отменить текущий диалог"""

HELP_TEXT_ADMIN = HELP_TEXT_USER + """

Команды администратора:
/add <user_id> — добавить пользователя
/delete <username или user_id> — удалить из списка
/create — создать комнату
/exc <username или user_id> — исключить из комнаты (только лобби)
/game — начать игру
/stop — остановить игру или отменить комнату
/players — игроки в текущей комнате"""

HELP_TEXT_OWNER = HELP_TEXT_USER + """

Только для создателя:
/add <user_id> — добавить пользователя
/delete <username или user_id> — удалить из списка
/list — список авторизованных пользователей
/rooms — состояние активной комнаты
/create — создать комнату
/exc <username или user_id> — исключить из комнаты (только лобби)
/edit — изменить порядок вопросов
/edit_qu — изменить текст вопросов
/game — начать игру
/stop — остановить игру или отменить комнату
/players — игроки в текущей комнате
/save — сохранить users.txt в GitHub
/grand <user_id или username> — выдать права админа
/unadmin <user_id или username> — снять права админа
/admins — список администраторов"""

RULES_TEXT = """Правила игры «Чепуха»

Каждый игрок по очереди отвечает на 9 вопросов. Ответы разных игроков
на каждый вопрос перемешиваются между собой. В итоге каждому достаётся
история, составленная из чужих ответов.

Ни один игрок не видит ни своего, ни чужого ответа до финального
зачитывания историй.

Игра идёт только в личных сообщениях с ботом.

Вопросы по умолчанию:
1. Кто?
2. Когда?
3. С кем?
4. Где?
5. Почему?
6. Что они там делали?
7. Кто к ним пришёл?
8. Зачем?
9. Чем всё закончилось?

Порядок и тексты вопросов может менять создатель до начала партии.
Минимум игроков для старта: 2."""


class EditOrder(StatesGroup):
    waiting_order = State()


class EditQuestion(StatesGroup):
    waiting_number = State()
    waiting_text = State()


class NotInPlayingGame(Filter):
    """FSM-редактирование не должно перехватывать ответы, пока идёт партия."""

    async def __call__(self, message: Message) -> bool:
        user = message.from_user
        if user is None:
            return True
        room = st.find_player_room(user.id)
        return not (
            room
            and room.get("status") in (config.STATUS_PLAYING, config.STATUS_REVEALING)
        )


room_lock = asyncio.Lock()
router = Router()
router.message.filter(F.chat.type == "private")
router.edited_message.filter(F.chat.type == "private")

CHUGUN_PHRASES = [
    "Опять чугун... Почему не алюминий?",
    "Чугун? Иди потрогай траву",
    "А я тут причем?",
    "Чугун? Почему не урановая шахта?",
    "Ну ты и любитель чугуна",
]
_chugun_bag: list[str] = []


def _cmd_name(message: Message) -> str | None:
    text = (message.text or message.caption or "").strip()
    if not text.startswith("/"):
        return None
    first = text.split()[0]
    name = first[1:].split("@", 1)[0].lower()
    return name or None


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]):
        if not isinstance(event, Message) or event.from_user is None:
            return await handler(event, data)
        user = event.from_user
        st.touch_user(user.id, user.username, user.first_name or "")
        st.touch_admin(user.id, user.username, user.first_name or "")
        cmd = _cmd_name(event)
        if cmd and not st.is_authorized(user.id, config.OWNER_ID):
            await event.answer(MSG_DENIED)
            return None
        if cmd in OWNER_COMMANDS and user.id != config.OWNER_ID:
            await event.answer(MSG_OWNER_ONLY)
            return None
        if cmd in ADMIN_COMMANDS and user.id != config.OWNER_ID and not st.is_admin(user.id):
            await event.answer(MSG_ADMIN_ONLY)
            return None
        return await handler(event, data)


async def _safe_send(bot: Bot, user_id: int, text: str) -> None:
    limit = 4000
    chunks = [text[i : i + limit] for i in range(0, max(len(text), 1), limit)] or [text]
    for chunk in chunks:
        try:
            await bot.send_message(user_id, chunk)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            log.warning("Не удалось отправить сообщение %s: %s", user_id, e)
            return


async def _broadcast(bot: Bot, user_ids: list[int], text: str) -> None:
    for uid in user_ids:
        await _safe_send(bot, uid, text)


def _player_ids(room: dict[str, Any]) -> list[int]:
    return [int(p["user_id"]) for p in room.get("players", [])]


def _status_human(room: dict[str, Any]) -> str:
    status = room.get("status")
    if status == config.STATUS_LOBBY:
        return "сбор игроков"
    if status == config.STATUS_PLAYING:
        n = int(room.get("current_question_index", 0)) + 1
        total = len(room.get("questions") or []) or 9
        return f"игра идёт, вопрос №{n} из {total}"
    if status == config.STATUS_REVEALING:
        n = int(room.get("reveal_index", 0)) + 1
        total = len(room.get("reveal_order") or [])
        return f"чтение историй ({n} из {total})"
    if status == config.STATUS_FINISHED:
        return "завершена"
    if status == config.STATUS_CANCELLED:
        return "остановлена без результата"
    return str(status)


def _usage(cmd: str, example: str) -> str:
    return f"Использование: {cmd} {example}\n{MSG_USAGE_HELP}"


def _generate_room_id(rooms: dict[str, Any]) -> str:
    taken = set(rooms.keys())
    for item in st.load_history_index():
        taken.add(str(item.get("room_id")))
    for _ in range(200):
        rid = str(random.randint(10000, 99999))
        if rid not in taken:
            return rid
    raise RuntimeError("Не удалось подобрать уникальный id комнаты")


def _parse_order(text: str) -> list[int] | None:
    parts = [p.strip() for p in text.replace(" ", "").split(",") if p.strip()]
    if len(parts) != 9:
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if sorted(nums) != list(range(1, 10)):
        return None
    return nums


def _format_questions_list() -> str:
    snap = st.snapshot_questions()
    lines = []
    for i, q in enumerate(snap, start=1):
        lines.append(f"{i}. [{q['id']}] {q['text']}")
    return "\n".join(lines)


def _format_questions_by_id() -> str:
    cfg = st.load_questions()
    lines = []
    for num in range(1, 10):
        text = cfg["texts"].get(str(num), "")
        lines.append(f"{num}. {text}")
    return "\n".join(lines)


def _round_answer_text(val: Any) -> str:
    if isinstance(val, dict):
        return str(val.get("text") or "")
    return str(val)


def _has_chugun(text: str) -> bool:
    return "чугун" in (text or "").lower()


def _next_chugun_phrase() -> str:
    global _chugun_bag
    if not _chugun_bag:
        _chugun_bag = list(CHUGUN_PHRASES)
        random.shuffle(_chugun_bag)
    return _chugun_bag.pop()


def _ack_for_answer(text: str) -> str:
    if _has_chugun(text):
        return _next_chugun_phrase()
    return "Ответ засчитан, ожидайте ответа других игроков..."


def _pair_uid(pair: Any) -> int:
    return int(pair[0])


def _pair_text(pair: Any) -> str:
    return str(pair[1])


def _assign_round(pairs: list[list[Any]], stories: list[list[Any]]) -> list[list[Any]]:
    n = len(pairs)
    used = [set(_pair_uid(item) for item in slot) for slot in stories]
    first_round = all(len(s) == 0 for s in used)
    if first_round or n <= 1:
        result = list(pairs)
        random.shuffle(result)
        return result
    tries = 30
    best_penalty: int | None = None
    best: list[list[list[Any]]] = []
    for _ in range(tries):
        cand = list(pairs)
        random.shuffle(cand)
        penalty = sum(1 for i, p in enumerate(cand) if _pair_uid(p) in used[i])
        if best_penalty is None or penalty < best_penalty:
            best_penalty = penalty
            best = [cand]
        elif penalty == best_penalty:
            best.append(cand)
    return random.choice(best)


def _build_history_text(
    room: dict[str, Any],
    date: str,
    assignment: list[tuple[dict, list]],
) -> str:
    players_labels = [
        st.format_user_label(int(p["user_id"]), p.get("username"))
        for p in room.get("players", [])
    ]
    questions = room.get("questions") or st.snapshot_questions()
    chunks = [
        f"Игра #{room['id']}",
        f"Дата: {date}",
        "Участники: " + ", ".join(players_labels),
        "",
    ]
    for i, (player, answers) in enumerate(assignment, start=1):
        reader = st.format_user_label(int(player["user_id"]), player.get("username"))
        chunks.append(f"История {i} (читал: {reader})")
        for q, ans in zip(questions, answers):
            chunks.append(q["text"])
            chunks.append(_pair_text(ans))
        chunks.append("")
    return "\n".join(chunks).rstrip() + "\n"


def _build_history_raw(
    room: dict[str, Any],
    date: str,
    assignment: list[tuple[dict, list]],
) -> dict[str, Any]:
    players_by_id = {int(p["user_id"]): p for p in room.get("players", [])}
    questions = room.get("questions") or st.snapshot_questions()
    stories_out = []
    for player, answers in assignment:
        items = []
        for q, ans in zip(questions, answers):
            author_id = _pair_uid(ans)
            author = players_by_id.get(author_id, {})
            items.append(
                {
                    "question": q["text"],
                    "text": _pair_text(ans),
                    "user_id": author_id,
                    "username": author.get("username") or "",
                }
            )
        stories_out.append(
            {
                "reader_user_id": int(player["user_id"]),
                "reader_username": player.get("username") or "",
                "answers": items,
            }
        )
    return {
        "room_id": str(room["id"]),
        "date": date,
        "stories": stories_out,
    }


def _history_text_from_raw(raw: dict[str, Any]) -> str:
    chunks = [
        f"Игра #{raw.get('room_id')}",
        f"Дата: {raw.get('date')}",
        "",
    ]
    for i, story in enumerate(raw.get("stories") or [], start=1):
        reader = st.format_user_label(
            int(story.get("reader_user_id") or 0),
            story.get("reader_username"),
        )
        chunks.append(f"История {i} (читал: {reader})")
        for item in story.get("answers") or []:
            chunks.append(str(item.get("question") or ""))
            mark = st.format_list_line(int(item.get("user_id") or 0), item.get("username"))
            chunks.append(f"{item.get('text') or ''} (ответил: {mark})")
        chunks.append("")
    return "\n".join(chunks).rstrip() + "\n"


def _story_text(answers: list) -> str:
    return "\n".join(_pair_text(a) for a in answers)


# --- commands ---


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    user = message.from_user
    if user and st.find_user_row(str(user.id)):
        try:
            await bot.set_message_reaction(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji="⚡")],
            )
        except Exception as e:
            log.warning("Не удалось поставить реакцию на /start: %s", e)
    await message.answer(
        "Добро пожаловать в игру «Чепуха».\n"
        "Правила: /rules\n"
        "Список команд: /help"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    user = message.from_user
    if user and user.id == config.OWNER_ID:
        await message.answer(HELP_TEXT_OWNER)
        return
    if user and st.is_admin(user.id):
        await message.answer(HELP_TEXT_ADMIN)
        return
    await message.answer(HELP_TEXT_USER)


@router.message(Command("rules"))
async def cmd_rules(message: Message) -> None:
    await message.answer(
        f"<blockquote>{html.escape(RULES_TEXT)}</blockquote>",
        parse_mode="HTML",
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    if current is None:
        await message.answer("Нечего отменять.")
        return
    await message.answer("Режим редактирования завершён.")


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip()
    if not args or not args.isdigit():
        await message.answer(_usage("/add", "<user_id>"))
        return
    user_id = int(args)
    if not st.add_user(user_id):
        await message.answer("Этот пользователь уже добавлен.")
        return
    await message.answer(f"Пользователь {user_id} добавлен.")


@router.message(Command("delete"))
async def cmd_delete(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip()
    if not args:
        await message.answer(_usage("/delete", "<username или user_id>"))
        return
    row = st.find_user_row(args)
    if row and int(row["user_id"]) == config.OWNER_ID:
        await message.answer("Нельзя удалить владельца из списка.")
        return
    if not st.delete_user(args):
        await message.answer("Пользователь не найден в списке.")
        return
    await message.answer("Пользователь удалён из списка.")


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    rows = st.load_users()
    admin_ids = {int(a["user_id"]) for a in st.load_admins()}
    lines: list[str] = []
    owner_in_list = False
    for r in rows:
        uid = int(r["user_id"])
        is_owner = uid == config.OWNER_ID
        if is_owner:
            owner_in_list = True
        lines.append(
            st.format_list_line(
                uid,
                r.get("username"),
                is_owner=is_owner,
                is_admin=(uid in admin_ids and not is_owner),
            )
        )
    if not owner_in_list and config.OWNER_ID:
        lines.insert(0, st.format_list_line(config.OWNER_ID, None, is_owner=True))
    if not lines:
        await message.answer("Список пуст. Создатель всегда имеет доступ.")
        return
    numbered = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, start=1))
    await message.answer(numbered)


@router.message(Command("admins"))
async def cmd_admins(message: Message) -> None:
    rows = st.load_admins()
    if not rows:
        await message.answer("Список администраторов пуст.")
        return
    lines = []
    for i, r in enumerate(rows, start=1):
        lines.append(
            f"{i}. {st.format_list_line(int(r['user_id']), r.get('username'), is_admin=True)}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("grand"))
async def cmd_grand(message: Message, command: CommandObject, bot: Bot) -> None:
    args = (command.args or "").strip()
    if not args:
        await message.answer(_usage("/grand", "<user_id или username>"))
        return
    q = args.lstrip("@")
    if q.isdigit():
        user_id = int(q)
        row = st.find_user_row(q) or st.find_admin_row(q)
        username = (row.get("username") if row else "") or config.UNKNOWN_USERNAME
        first_name = (row.get("first_name") if row else "") or ""
    else:
        row = st.find_user_row(args) or st.find_admin_row(args)
        if not row:
            await message.answer(
                "Пользователь с таким username не найден. "
                "Сначала добавьте его через /add, либо укажите числовой user_id."
            )
            return
        user_id = int(row["user_id"])
        username = row.get("username") or config.UNKNOWN_USERNAME
        first_name = row.get("first_name") or ""
    if user_id == config.OWNER_ID:
        await message.answer("Владелец не может быть админом — он и так выше по правам.")
        return
    if st.is_admin(user_id):
        await message.answer("Пользователь уже является админом.")
        return
    st.add_admin(user_id, username, first_name or "")
    label = st.format_user_label(user_id, username)
    await message.answer(f"Пользователь {label} назначен админом.")
    await _safe_send(
        bot,
        user_id,
        "Вы назначены админом, напишите /help чтобы увидеть список доступных команд",
    )


@router.message(Command("unadmin"))
async def cmd_unadmin(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip()
    if not args:
        await message.answer(_usage("/unadmin", "<user_id или username>"))
        return
    row = st.find_admin_row(args)
    if not row:
        await message.answer("Этот пользователь не является админом.")
        return
    st.remove_admin(args)
    label = st.format_user_label(int(row["user_id"]), row.get("username"))
    await message.answer(f"Права админа сняты с {label}.")


@router.message(Command("rooms"))
async def cmd_rooms(message: Message) -> None:
    room = st.get_active_room()
    if not room:
        await message.answer("Активных комнат нет. Создайте комнату командой /create.")
        return
    players = room.get("players", [])
    player_lines = []
    for i, p in enumerate(players, start=1):
        player_lines.append(
            f"{i}. {st.format_list_line(int(p['user_id']), p.get('username'), is_owner=int(p['user_id']) == config.OWNER_ID)}"
        )
    body = "\n".join(player_lines) if player_lines else "пока никого"
    await message.answer(
        f"Комната {room['id']}\n"
        f"Статус: {_status_human(room)}\n"
        f"Участники:\n{body}"
    )


@router.message(Command("create"))
async def cmd_create(message: Message) -> None:
    async with room_lock:
        rooms = st.load_rooms()
        if st.get_active_room(rooms):
            await message.answer(
                "У вас уже есть активная комната. Завершите игру или "
                "остановите её командой /stop перед созданием новой."
            )
            return
        room_id = _generate_room_id(rooms)
        rooms[room_id] = {
            "id": room_id,
            "status": config.STATUS_LOBBY,
            "players": [],
            "current_question_index": 0,
            "answers_this_round": {},
            "stories": [],
            "questions": [],
        }
        st.save_rooms(rooms)
    await message.answer(f"Комната успешно создана! Id комнаты: {room_id}")


@router.message(Command("join"))
async def cmd_join(message: Message, command: CommandObject, bot: Bot) -> None:
    args = (command.args or "").strip()
    if not args:
        await message.answer(_usage("/join", "<room_id>"))
        return
    user = message.from_user
    assert user is not None
    async with room_lock:
        rooms = st.load_rooms()
        room = rooms.get(args)
        if not room:
            await message.answer("Комната с таким id не найдена.")
            return
        if room.get("status") != config.STATUS_LOBBY:
            await message.answer(
                "К этой комнате нельзя присоединиться: игра уже началась или завершена."
            )
            return
        other = st.find_player_room(user.id, rooms)
        if other:
            if other["id"] == room["id"]:
                await message.answer("Вы уже в этой комнате.")
                return
            await message.answer(
                "Вы уже состоите в другой комнате. Сначала покиньте её командой /exit."
            )
            return
        if len(room.get("players", [])) >= config.MAX_PLAYERS:
            await message.answer(f"Комната заполнена. Максимум игроков: {config.MAX_PLAYERS}.")
            return
        room.setdefault("players", []).append(
            {
                "user_id": user.id,
                "username": user.username or "",
                "first_name": user.first_name or "",
            }
        )
        st.save_rooms(rooms)
        label = st.format_user_label(user.id, user.username)
        room_id = room["id"]
    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji="❤️‍🔥")],
        )
    except Exception as e:
        log.warning("Не удалось поставить реакцию на /join: %s", e)
    await message.answer(
        f"Вы успешно присоединились к комнате {room_id}! Ожидайте начало игры..."
    )
    if config.OWNER_ID and config.OWNER_ID != user.id:
        await _safe_send(
            bot,
            config.OWNER_ID,
            f"Игрок {label} успешно присоединился к комнате {room_id}!",
        )


@router.message(Command("exit"))
async def cmd_exit(message: Message) -> None:
    user = message.from_user
    assert user is not None
    async with room_lock:
        rooms = st.load_rooms()
        room = st.find_player_room(user.id, rooms)
        if not room:
            return
        if room.get("status") != config.STATUS_LOBBY:
            await message.answer("Нельзя покинуть комнату во время активной игры.")
            return
        room["players"] = [p for p in room["players"] if int(p["user_id"]) != user.id]
        st.save_rooms(rooms)
    await message.answer("Вы покинули комнату.")


@router.message(Command("exc"))
async def cmd_exc(message: Message, command: CommandObject, bot: Bot) -> None:
    args = (command.args or "").strip()
    if not args:
        await message.answer(_usage("/exc", "<username или user_id>"))
        return
    async with room_lock:
        rooms = st.load_rooms()
        room = st.get_active_room(rooms)
        if not room:
            await message.answer("Активных комнат нет. Создайте комнату командой /create.")
            return
        if room.get("status") != config.STATUS_LOBBY:
            await message.answer("Нельзя исключить игрока во время активной игры.")
            return
        player = st.find_player(room, args)
        if not player:
            await message.answer("Игрок не найден в комнате.")
            return
        uid = int(player["user_id"])
        if uid == config.OWNER_ID:
            await message.answer("Нельзя исключить владельца из комнаты.")
            return
        room["players"] = [p for p in room["players"] if int(p["user_id"]) != uid]
        st.save_rooms(rooms)
        room_id = room["id"]
    await message.answer("Игрок исключён из комнаты.")
    await _safe_send(bot, uid, f"Вас исключили из комнаты {room_id}.")


@router.message(Command("players"))
async def cmd_players(message: Message) -> None:
    room = st.get_active_room()
    if not room:
        await message.answer("Активных комнат нет. Создайте комнату командой /create.")
        return
    players = room.get("players", [])
    if not players:
        await message.answer("В комнате пока нет игроков.")
        return
    lines = []
    for i, p in enumerate(players, start=1):
        lines.append(
            f"{i}. {st.format_list_line(int(p['user_id']), p.get('username'), is_owner=int(p['user_id']) == config.OWNER_ID)}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("save"))
async def cmd_save(message: Message) -> None:
    users_result = await git_sync.sync_users_file()
    admins_result = await git_sync.sync_admins_file()
    await message.answer(
        f"users.txt: {users_result.detail}\nadmins.txt: {admins_result.detail}"
    )


@router.message(Command("edit"))
async def cmd_edit(message: Message, state: FSMContext) -> None:
    room = st.get_active_room()
    if room and room.get("status") in (config.STATUS_PLAYING, config.STATUS_REVEALING):
        await message.answer("Нельзя менять вопросы во время активной игры.")
        return
    listing = _format_questions_list()
    await state.set_state(EditOrder.waiting_order)
    await message.answer(
        "Текущий порядок вопросов (позиция. [номер] текст):\n"
        f"{listing}\n\n"
        "Пришлите новую последовательность номеров через запятую, "
        "например: 3,1,2,4,5,6,7,8,9\n"
        "Отмена: /cancel"
    )


@router.message(
    StateFilter(EditOrder.waiting_order),
    NotInPlayingGame(),
    F.text,
    ~F.text.startswith("/"),
)
async def edit_order_input(message: Message, state: FSMContext) -> None:
    nums = _parse_order(message.text or "")
    if not nums:
        await message.answer(
            "Нужна перестановка чисел 1–9 без пропусков и повторов, "
            "например: 3,1,2,4,5,6,7,8,9\n"
            "Повторите ввод или /cancel"
        )
        return
    cfg = st.load_questions()
    cfg["order"] = nums
    st.save_questions(cfg)
    await state.clear()
    listing = _format_questions_list()
    await message.answer(f"Порядок вопросов сохранён:\n{listing}")


@router.message(Command("edit_qu"))
async def cmd_edit_qu(message: Message, state: FSMContext) -> None:
    room = st.get_active_room()
    if room and room.get("status") in (config.STATUS_PLAYING, config.STATUS_REVEALING):
        await message.answer("Нельзя менять вопросы во время активной игры.")
        return
    await state.set_state(EditQuestion.waiting_number)
    await message.answer(
        "Текущие вопросы:\n"
        f"{_format_questions_by_id()}\n\n"
        "Пришлите номер вопроса (1–9), который хотите изменить.\n"
        "Отмена: /cancel"
    )


@router.message(
    StateFilter(EditQuestion.waiting_number),
    NotInPlayingGame(),
    F.text,
    ~F.text.startswith("/"),
)
async def edit_qu_number(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) not in range(1, 10):
        await message.answer("Нужен номер от 1 до 9. Повторите ввод или /cancel")
        return
    await state.update_data(question_id=int(raw))
    await state.set_state(EditQuestion.waiting_text)
    await message.answer("Пришлите новый текст вопроса.")


@router.message(
    StateFilter(EditQuestion.waiting_text),
    NotInPlayingGame(),
    F.text,
    ~F.text.startswith("/"),
)
async def edit_qu_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст не должен быть пустым. Повторите ввод или /cancel")
        return
    data = await state.get_data()
    qid = int(data["question_id"])
    cfg = st.load_questions()
    cfg["texts"][str(qid)] = text
    st.save_questions(cfg)
    await state.set_state(EditQuestion.waiting_number)
    await message.answer(
        f"Вопрос {qid} сохранён: {text}\n\n"
        "Можно изменить ещё один: пришлите номер (1–9).\n"
        "Завершить: /cancel\n\n"
        f"{_format_questions_by_id()}"
    )


@router.message(Command("game"))
async def cmd_game(message: Message, bot: Bot, state: FSMContext) -> None:
    async with room_lock:
        rooms = st.load_rooms()
        room = st.get_active_room(rooms)
        if not room:
            await message.answer("Активных комнат нет. Создайте комнату командой /create.")
            return
        if room.get("status") == config.STATUS_PLAYING:
            await state.clear()
            await message.answer("Игра уже идёт.")
            return
        if room.get("status") != config.STATUS_LOBBY:
            await message.answer("Эту комнату нельзя запустить.")
            return
        n = len(room.get("players", []))
        if n < config.MIN_PLAYERS:
            await message.answer("Недостаточно игроков для начала игры. Минимум: 2.")
            return
        questions = st.snapshot_questions()
        room["status"] = config.STATUS_PLAYING
        room["current_question_index"] = 0
        room["answers_this_round"] = {}
        room["stories"] = [[] for _ in range(n)]
        room["questions"] = questions
        st.save_rooms(rooms)
        q_text = questions[0]["text"]
        ids = _player_ids(room)
    await state.clear()
    await _broadcast(bot, ids, "Игра началась!")
    await _broadcast(bot, ids, q_text)
    if message.from_user and message.from_user.id not in ids:
        await message.answer("Игра запущена.")


@router.message(Command("stop"))
async def cmd_stop(message: Message, bot: Bot) -> None:
    async with room_lock:
        rooms = st.load_rooms()
        room = st.get_active_room(rooms)
        if not room:
            await message.answer("Активных комнат нет.")
            return
        ids = _player_ids(room)
        room["status"] = config.STATUS_CANCELLED
        room["answers_this_round"] = {}
        st.save_rooms(rooms)
    await _broadcast(bot, ids, "Игра была остановлена создателем.")
    await message.answer("Комната остановлена.")


@router.message(Command("result"))
async def cmd_result(message: Message) -> None:
    user = message.from_user
    assert user is not None
    items = st.history_for_user(user.id, config.OWNER_ID)
    if not items:
        await message.answer("У вас пока нет завершённых игр.")
        return
    lines = ["Завершённые игры:"]
    for item in items:
        lines.append(f"{item['room_id']} — {item['date']}")
    lines.append("Чтобы получить файл, напишите /get <id>")
    await message.answer("\n".join(lines))


@router.message(Command("get"))
async def cmd_get(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip()
    if not args:
        await message.answer(_usage("/get", "<room_id>"))
        return
    user = message.from_user
    assert user is not None
    entry = st.get_history_entry(args)
    allowed = False
    if entry:
        if user.id == config.OWNER_ID or st.is_admin(user.id):
            allowed = True
        else:
            ids = [int(p["user_id"]) for p in entry.get("players", [])]
            allowed = user.id in ids
    path = st.history_file_path(entry) if entry and allowed else None
    if not entry or not allowed or path is None:
        await message.answer("История игры не найдена.")
        return
    if user.id == config.OWNER_ID or st.is_admin(user.id):
        raw = st.load_history_raw(entry)
        if raw:
            text = _history_text_from_raw(raw)
            await message.answer_document(
                BufferedInputFile(text.encode("utf-8"), filename=path.name)
            )
            return
    await message.answer_document(FSInputFile(path))


@router.message(Command("finish"))
async def cmd_finish(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None:
        return
    error: str | None = None
    next_room: dict[str, Any] | None = None
    finished_ids: list[int] | None = None
    async with room_lock:
        rooms = st.load_rooms()
        room = _current_reader_room(rooms, user.id)
        if not room:
            error = "Сейчас не ваша очередь читать историю."
        else:
            room["reveal_index"] = int(room.get("reveal_index", 0)) + 1
            order = room.get("reveal_order") or []
            if room["reveal_index"] < len(order):
                st.save_rooms(rooms)
                next_room = room
            else:
                finished_ids = _player_ids(room)
                room["status"] = config.STATUS_FINISHED
                room.pop("reveal_order", None)
                room.pop("reveal_stories", None)
                room.pop("reveal_index", None)
                st.save_rooms(rooms)
    if error:
        await message.answer(error)
        return
    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji="❤️")],
        )
    except Exception as e:
        log.warning("Не удалось поставить реакцию на /finish: %s", e)
    if next_room:
        await _reveal_current(bot, next_room)
        return
    if finished_ids:
        await _broadcast(
            bot,
            finished_ids,
            "Игра завершена! Для просмотра историй напишите /result",
        )



@router.message(F.text, ~F.text.startswith("/"))
async def on_answer(message: Message, bot: Bot, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    text = (message.text or "").strip()
    if not text:
        return
    async with room_lock:
        rooms = st.load_rooms()
        room = st.find_player_room(user.id, rooms)
        if not room or room.get("status") != config.STATUS_PLAYING:
            return
        await state.clear()
        uid_key = str(user.id)
        already = room.setdefault("answers_this_round", {})
        if uid_key in already:
            await message.answer("Вы уже ответили, ожидайте остальных игроков...")
            return
        if len(text) > config.MAX_ANSWER_LENGTH:
            await message.answer(
                f"Ответ слишком длинный. Сократите до {config.MAX_ANSWER_LENGTH} символов и отправьте заново."
            )
            return
        ack = _ack_for_answer(text)
        already[uid_key] = {
            "text": text,
            "message_id": message.message_id,
            "round_index": int(room.get("current_question_index", 0)),
        }
        st.save_rooms(rooms)
        n_players = len(room.get("players", []))
        n_answers = len(already)
        round_complete = n_answers >= n_players
        room_id = room["id"]
    await message.answer(ack)
    if round_complete:
        await _advance_round(bot, room_id)


@router.edited_message(F.text, ~F.text.startswith("/"))
async def on_answer_edited(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    text = (message.text or "").strip()
    ack: str | None = None
    async with room_lock:
        rooms = st.load_rooms()
        room = st.find_player_room(user.id, rooms)
        if not room or room.get("status") != config.STATUS_PLAYING:
            return
        uid_key = str(user.id)
        already = room.setdefault("answers_this_round", {})
        entry = already.get(uid_key)
        if not isinstance(entry, dict):
            return
        if int(entry.get("message_id") or 0) != message.message_id:
            return
        if int(entry.get("round_index") or -1) != int(room.get("current_question_index", 0)):
            return
        if len(text) > config.MAX_ANSWER_LENGTH:
            ack = (
                f"Отредактированный ответ слишком длинный "
                f"(максимум {config.MAX_ANSWER_LENGTH} символов), "
                "сохранён прежний вариант ответа."
            )
        else:
            entry["text"] = text
            st.save_rooms(rooms)
            if _has_chugun(text):
                ack = _next_chugun_phrase()
            else:
                ack = "Ответ обновлён, ожидайте ответа других игроков..."
    if ack:
        await message.answer(ack)


async def _advance_round(bot: Bot, room_id: str) -> None:
    next_q: str | None = None
    ids: list[int] = []
    finish: dict[str, Any] | None = None
    async with room_lock:
        rooms = st.load_rooms()
        room = rooms.get(room_id)
        if not room or room.get("status") != config.STATUS_PLAYING:
            return
        players = room.get("players", [])
        answers_map = room.get("answers_this_round") or {}
        try:
            pairs = [
                [int(p["user_id"]), _round_answer_text(answers_map[str(p["user_id"])])]
                for p in players
            ]
        except KeyError:
            return
        stories = room.setdefault("stories", [[] for _ in players])
        while len(stories) < len(players):
            stories.append([])
        assigned = _assign_round(pairs, stories)
        for i, pair in enumerate(assigned):
            stories[i].append(pair)
        idx = int(room.get("current_question_index", 0))
        questions = room.get("questions") or st.snapshot_questions()
        if idx + 1 >= len(questions):
            finish = _close_room(rooms, room)
        else:
            room["current_question_index"] = idx + 1
            room["answers_this_round"] = {}
            st.save_rooms(rooms)
            next_q = questions[idx + 1]["text"]
            ids = _player_ids(room)
    if finish:
        await _reveal_current(bot, finish)
        return
    if next_q:
        await _broadcast(bot, ids, next_q)


def _close_room(rooms: dict[str, Any], room: dict[str, Any]) -> dict[str, Any]:
    players = list(room.get("players", []))
    stories = list(room.get("stories", []))
    story_order = list(range(len(players)))
    random.shuffle(story_order)
    assignment: list[tuple[dict, list]] = []
    for i, player in enumerate(players):
        assignment.append((player, stories[story_order[i]]))
    random.shuffle(assignment)
    if len(assignment) > 1 and int(assignment[0][0]["user_id"]) == config.OWNER_ID:
        swap_with = random.randint(1, len(assignment) - 1)
        assignment[0], assignment[swap_with] = assignment[swap_with], assignment[0]
    date = st.today_str()
    history_text = _build_history_text(room, date, assignment)
    raw = _build_history_raw(room, date, assignment)
    st.append_history(str(room["id"]), date, players, history_text, raw=raw)
    room["status"] = config.STATUS_REVEALING
    room["answers_this_round"] = {}
    room["reveal_order"] = [int(player["user_id"]) for player, _ in assignment]
    room["reveal_stories"] = [_story_text(answers) for _, answers in assignment]
    room["reveal_index"] = 0
    st.save_rooms(rooms)
    return room


def _current_reader_room(rooms: dict[str, Any], user_id: int) -> dict[str, Any] | None:
    for room in rooms.values():
        if room.get("status") != config.STATUS_REVEALING:
            continue
        order = room.get("reveal_order") or []
        idx = int(room.get("reveal_index", 0))
        if 0 <= idx < len(order) and int(order[idx]) == user_id:
            return room
    return None


async def _reveal_current(bot: Bot, room: dict[str, Any]) -> None:
    idx = int(room.get("reveal_index", 0))
    order = [int(x) for x in (room.get("reveal_order") or [])]
    stories = room.get("reveal_stories") or []
    if idx < 0 or idx >= len(order) or idx >= len(stories):
        return
    reader_id = order[idx]
    story = stories[idx]
    players = room.get("players", [])
    reader = next((p for p in players if int(p["user_id"]) == reader_id), None)
    reader_label = st.format_user_label(
        reader_id,
        reader.get("username") if reader else None,
    )
    others = [int(p["user_id"]) for p in players if int(p["user_id"]) != reader_id]
    await _broadcast(bot, others, f"{reader_label} читает историю, слушаем!")
    await _safe_send(
        bot,
        reader_id,
        "Тебе выпала честь, прочесть эту легендарную историю! После окончания прочтения нажми на /finish",
    )
    await _safe_send(bot, reader_id, story)



async def main() -> None:
    if not config.BOT_TOKEN:
        log.error("Задайте BOT_TOKEN в окружении или в файле .env")
        sys.exit(1)
    if not config.OWNER_ID:
        log.error("Задайте OWNER_ID в окружении или в файле .env")
        sys.exit(1)
    st.ensure_files()
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()
    dp.message.middleware(AccessMiddleware())
    dp.edited_message.middleware(AccessMiddleware())
    dp.include_router(router)
    log.info("Бот «Чепуха» запущен, owner_id=%s", config.OWNER_ID)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
