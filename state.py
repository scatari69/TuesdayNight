import asyncio
import copy
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path


def new_guild():
    return {
        "role_id": None, "channel_id": None, "cooldown": 60,
        "reset_time": "03:00", "autotag": None,
        "last_reset": None, "last_autotag": None, "last_tag": 0,
        "votes": {}, "messages": [], "tags": 0,
        "user_tags": {}, "declines": {}, "eggs": {}, "links": {},
        "reaction_roles": {},
        "dashboard": None,
        "wow_characters": {},
        "mplus_settings": None, "mplus_groups": {}, "mplus_next": 1,
    }


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = asyncio.Lock()
        # Поврежденный файл не заменяем пустым состоянием.
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}
        if not isinstance(self.data, dict):
            raise ValueError("State must be a JSON object")

    def _guild(self, guild_id):
        guild = self.data.setdefault(str(guild_id), new_guild())
        for key, value in new_guild().items():
            guild.setdefault(key, value)
        return guild

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as file:
            json.dump(self.data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, self.path)

    @asynccontextmanager
    async def edit(self, guild_id):
        async with self.lock:
            before = copy.deepcopy(self.data)
            try:
                yield self._guild(guild_id)
            except BaseException:
                self.data = before
                raise
            write = asyncio.create_task(asyncio.to_thread(self._write))
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                # Не выпускаем лок, пока поток не закончил атомарную запись.
                try:
                    await write
                except BaseException:
                    self.data = before
                    raise
                raise
            except BaseException:
                self.data = before
                raise

    async def read(self, guild_id):
        async with self.lock:
            return copy.deepcopy(self._guild(guild_id))


def parse_time(text):
    parts = text.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError("Enter a time in HH:MM format")
    hour, minute = map(int, parts)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Enter a time in HH:MM format")
    return f"{hour:02}:{minute:02}"


def emoji_key(emoji):
    # Имя custom emoji может измениться; его ID остается прежним.
    return str(emoji.id) if emoji.id else str(emoji)


def role_is_safe(role, bot_member):
    return bool(role and bot_member and not role.is_default() and not role.managed
                and not role.permissions.value and role < bot_member.top_role)


def eligible_members(role):
    return [member for member in role.members if not member.bot]


def record_vote(state, user_id, vote):
    key = str(user_id)
    if state["votes"].get(key) == vote:
        return False
    state["votes"][key] = vote
    if vote == "-":
        state["declines"][key] = state["declines"].get(key, 0) + 1
    return True
