import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

USERS_FILE = BASE_DIR / "users.txt"
ROOMS_FILE = BASE_DIR / "rooms.json"
QUESTIONS_FILE = BASE_DIR / "questions_config.json"
HISTORY_DIR = BASE_DIR / "history"
HISTORY_INDEX_FILE = BASE_DIR / "history_index.json"

TIMEZONE = os.environ.get("CHEPUHA_TZ", "Europe/Moscow")
MIN_PLAYERS = 2
MAX_PLAYERS = int(os.environ.get("MAX_PLAYERS", "20"))
MAX_ANSWER_LENGTH = int(os.environ.get("MAX_ANSWER_LENGTH", "500"))

DEFAULT_QUESTIONS = {
    "1": "Кто?",
    "2": "Когда?",
    "3": "С кем?",
    "4": "Где?",
    "5": "Почему?",
    "6": "Что они там делали?",
    "7": "Кто к ним пришёл?",
    "8": "Зачем?",
    "9": "Чем всё закончилось?",
}

DEFAULT_ORDER = [1, 2, 3, 4, 5, 6, 7, 8, 9]

STATUS_LOBBY = "LOBBY"
STATUS_PLAYING = "PLAYING"
STATUS_REVEALING = "REVEALING"
STATUS_FINISHED = "FINISHED"
STATUS_CANCELLED = "CANCELLED"

UNKNOWN_USERNAME = "неизвестен"
