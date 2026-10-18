import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

import deadlock
from state import Store, eligible_members, emoji_key, parse_time, record_vote, role_is_safe

log = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()


class DeadlockBot(discord.Client):
    def __init__(self, store):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents, allowed_mentions=NO_MENTIONS)
        self.store = store
        self.tree = app_commands.CommandTree(self)
        self.actions = asyncio.Lock()

    async def setup_hook(self):
        self.add_view(GatheringView())
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.scheduler.start()

    async def close(self):
        self.scheduler.cancel()
        if deadlock._http_session is not None:
            await deadlock._http_session.close()
        await super().close()

    async def roster(self, guild, state):
        if not guild.chunked:
            await guild.chunk(cache=True)
        role = guild.get_role(state["role_id"]) if state["role_id"] else None
        if role is None:
            raise ValueError("Сначала настрой роль командой /setup.")
        return eligible_members(role)

    async def gathering(self, guild, state):
        members = await self.roster(guild, state)
        lines = ["🚨 **Играем! Кто идет?**"]
        for vote, label in (("+", "✅ Играют"), ("-", "❌ Не играют")):
            names = [discord.utils.escape_markdown(m.display_name)
                     for m in members if state["votes"].get(str(m.id)) == vote]
            lines.append(f"\n**{label}:** " + (", ".join(names) if names else "—"))
        # Большой состав не должен превысить лимит Discord на текст сообщения.
        return "\n".join(lines)[:1900]

    async def start_gathering(self, guild, channel, caller=None):
        async with self.actions:
            state = await self.store.read(guild.id)
            members = await self.roster(guild, state)
            if caller is not None and caller.id not in {m.id for m in members}:
                async with self.store.edit(guild.id) as data:
                    key = str(caller.id)
                    data["eggs"][key] = data["eggs"].get(key, 0) + 1
                    count = data["eggs"][key]
                return f"Напулял! Уже {count} раз."
            remaining = state["cooldown"] - (time.time() - state["last_tag"])
            if remaining > 0:
                return f"Подожди еще {int(remaining) + 1} сек."
            pending = [m for m in members if state["votes"].get(str(m.id)) != "+"]
            if not pending:
                return "Все игроки уже согласились или роль пока пуста."
            # Пингуем только еще не согласившихся, не всю роль и не @everyone.
            for offset in range(0, len(pending), 50):
                batch = pending[offset:offset + 50]
                await channel.send(" ".join(m.mention for m in batch),
                                   allowed_mentions=discord.AllowedMentions(
                                       users=batch, roles=False, everyone=False, replied_user=False))
            message = await channel.send(await self.gathering(guild, state), view=GatheringView())
            async with self.store.edit(guild.id) as data:
                data["last_tag"] = time.time()
                data["tags"] += 1
                data["messages"].append(message.id)
                data["messages"] = data["messages"][-100:]
                if caller:
                    key = str(caller.id)
                    data["user_tags"][key] = data["user_tags"].get(key, 0) + 1
            return "Сбор опубликован."

    async def reaction(self, payload, add):
        async with self.actions:
            await self._reaction(payload, add)

    async def _reaction(self, payload, add):
        if not payload.guild_id or payload.user_id == self.user.id:
            return
        state = await self.store.read(payload.guild_id)
        mapping = state["reaction_roles"].get(str(payload.message_id), {})
        if mapping.get("channel_id") != payload.channel_id:
            return
        role_id = mapping.get("roles", {}).get(emoji_key(payload.emoji))
        if role_id is None:
            return
        guild = self.get_guild(payload.guild_id)
        if guild is None:
            return
        role = guild.get_role(role_id)
        if not role_is_safe(role, guild.me) or not guild.me.guild_permissions.manage_roles:
            log.warning("Роль %s больше нельзя выдавать на сервере %s", role_id, guild.id)
            return
        try:
            member = await guild.fetch_member(payload.user_id)
            if member.bot:
                return
            if add:
                await member.add_roles(role, reason="Reaction role")
            else:
                await member.remove_roles(role, reason="Reaction role")
        except discord.HTTPException:
            log.exception("Не удалось изменить reaction role")

    async def on_raw_reaction_add(self, payload):
        await self.reaction(payload, True)

    async def on_raw_reaction_remove(self, payload):
        await self.reaction(payload, False)

    async def on_raw_message_delete(self, payload):
        if payload.guild_id:
            async with self.store.edit(payload.guild_id) as data:
                data["reaction_roles"].pop(str(payload.message_id), None)
                data["messages"] = [mid for mid in data["messages"] if mid != payload.message_id]

    @tasks.loop(seconds=20)
    async def scheduler(self):
        now = datetime.now()
        today, hhmm = now.date().isoformat(), now.strftime("%H:%M")
        for guild in self.guilds:
            try:
                state = await self.store.read(guild.id)
                if hhmm == state["reset_time"] and state["last_reset"] != today:
                    async with self.actions:
                        async with self.store.edit(guild.id) as data:
                            data["votes"] = {}
                            data["messages"] = []
                            data["last_tag"] = 0
                            data["last_reset"] = today
                if hhmm == state["autotag"] and state["last_autotag"] != today:
                    channel = guild.get_channel(state["channel_id"])
                    if channel:
                        await self.start_gathering(guild, channel)
                        async with self.store.edit(guild.id) as data:
                            data["last_autotag"] = today
            except (discord.HTTPException, ValueError, OSError):
                log.exception("Ошибка планировщика для %s", guild.id)

    @scheduler.before_loop
    async def before_scheduler(self):
        await self.wait_until_ready()


class GatheringView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def vote(self, interaction, value):
        await interaction.response.defer(ephemeral=True)
        bot = interaction.client
        async with bot.actions:
            state = await bot.store.read(interaction.guild_id)
            if interaction.message.id not in state["messages"]:
                await interaction.followup.send("Этот сбор уже закрыт.", ephemeral=True)
                return
            members = await bot.roster(interaction.guild, state)
            if interaction.user.id not in {m.id for m in members}:
                await interaction.followup.send("Для голосования нужна роль игроков.", ephemeral=True)
                return
            async with bot.store.edit(interaction.guild_id) as data:
                changed = record_vote(data, interaction.user.id, value)
            state = await bot.store.read(interaction.guild_id)
            await interaction.message.edit(content=await bot.gathering(interaction.guild, state),
                                           view=self, allowed_mentions=NO_MENTIONS)
            await interaction.followup.send("Принято!" if changed else "Уже учтено.", ephemeral=True)

    @discord.ui.button(label="Играю", emoji="➕", style=discord.ButtonStyle.success,
                       custom_id="deadlock:plus")
    async def plus(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.vote(interaction, "+")

    @discord.ui.button(label="Не играю", emoji="➖", style=discord.ButtonStyle.secondary,
                       custom_id="deadlock:minus")
    async def minus(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.vote(interaction, "-")

    async def on_error(self, interaction, error, item):
        log.error("Ошибка кнопки", exc_info=(type(error), error, error.__traceback__))
        if interaction.response.is_done():
            await interaction.followup.send("Не удалось обработать голос. Попробуй позже.", ephemeral=True)
        else:
            await interaction.response.send_message("Не удалось обработать голос.", ephemeral=True)


def register_commands(bot):
    @bot.tree.command(description="Выбрать роль игроков и канал сборов")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(interaction: discord.Interaction, role: discord.Role, channel: discord.TextChannel):
        if role.is_default() or role.managed:
            raise ValueError("Выбери обычную роль игроков, например @deadlock.")
        permissions = channel.permissions_for(interaction.guild.me)
        if not (permissions.view_channel and permissions.send_messages and permissions.read_message_history):
            raise ValueError("Боту нужны просмотр канала, отправка сообщений и чтение истории.")
        async with bot.actions:
            async with bot.store.edit(interaction.guild_id) as state:
                if state["role_id"] != role.id:
                    state["votes"] = {}
                    state["messages"] = []
                state["role_id"], state["channel_id"] = role.id, channel.id
        await interaction.response.send_message(f"Роль: {role.mention}. Канал: {channel.mention}.", ephemeral=True)

    @bot.tree.command(description="Позвать игроков, которые еще не согласились")
    @app_commands.guild_only()
    async def tag(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        state = await bot.store.read(interaction.guild_id)
        channel = interaction.guild.get_channel(state["channel_id"])
        if channel is None:
            raise ValueError("Сначала настрой канал через /setup.")
        text = await bot.start_gathering(interaction.guild, channel, interaction.user)
        await interaction.followup.send(text, ephemeral=True)

    @bot.tree.command(description="Текущие игроки с ролью Deadlock")
    @app_commands.guild_only()
    async def team(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        members = await bot.roster(interaction.guild, await bot.store.read(interaction.guild_id))
        names = "\n".join(discord.utils.escape_markdown(m.display_name) for m in members)
        await interaction.followup.send((f"Игроков: {len(members)}\n" + names)[:1900], ephemeral=True)

    @bot.tree.command(description="Настройки сборов: время ЧЧ:ММ, disable выключает автосбор")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def settings(interaction: discord.Interaction, cooldown: app_commands.Range[int, 0, 86400] = None,
                       reset_time: str = None, autotag: str = None):
        reset = parse_time(reset_time) if reset_time is not None else None
        auto = None if autotag == "disable" else parse_time(autotag) if autotag is not None else None
        async with bot.store.edit(interaction.guild_id) as state:
            if cooldown is not None:
                state["cooldown"] = cooldown
            if reset is not None:
                state["reset_time"] = reset
            if autotag is not None:
                state["autotag"] = auto
            text = f"Кулдаун: {state['cooldown']} сек. Сброс: {state['reset_time']}. Автосбор: {state['autotag'] or 'выкл.'}."
        await interaction.response.send_message(text, ephemeral=True)

    @bot.tree.command(description="Закрыть сбор и очистить голоса")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def clear(interaction: discord.Interaction):
        async with bot.actions:
            async with bot.store.edit(interaction.guild_id) as state:
                state["votes"], state["messages"], state["last_tag"] = {}, [], 0
        await interaction.response.send_message("Сбор закрыт. Можно начинать новый.", ephemeral=True)

    @bot.tree.command(description="Статистика сборов и топ игроков")
    @app_commands.guild_only()
    async def stats(interaction: discord.Interaction):
        state = await bot.store.read(interaction.guild_id)
        lines = [f"Сборов: {state['tags']}"]
        for label, field in (("Тегавшие", "user_tags"), ("Напулявшие", "eggs"), ("Отказы", "declines")):
            top = sorted(state[field].items(), key=lambda pair: pair[1], reverse=True)[:3]
            lines.append(f"\n**{label}:**")
            lines.extend(f"<@{uid}> — {count}" for uid, count in top)
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(description="Привязать Steam/tracklock/числовой ID; remove — отвязать")
    @app_commands.guild_only()
    async def link(interaction: discord.Interaction, profile: str):
        await interaction.response.defer(ephemeral=True)
        key = str(interaction.user.id)
        if profile == "remove":
            async with bot.store.edit(interaction.guild_id) as state:
                state["links"].pop(key, None)
            await interaction.followup.send("Профиль отвязан.", ephemeral=True)
            return
        account, error = await deadlock.resolve_account_id(profile)
        if error or account is None or not 0 < account < 2**32:
            raise ValueError("Не удалось распознать профиль. Для Steam /id/имя нужен STEAM_API_KEY.")
        async with bot.store.edit(interaction.guild_id) as state:
            state["links"][key] = account
        await interaction.followup.send("Профиль привязан. Статистика доступна в /me.", ephemeral=True)

    @bot.tree.command(description="Личная карточка и статистика Deadlock")
    @app_commands.guild_only()
    async def me(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        state = await bot.store.read(interaction.guild_id)
        key = str(interaction.user.id)
        members = await bot.roster(interaction.guild, state)
        lines = ["В составе" if interaction.user.id in {m.id for m in members} else "Не в составе",
                 f"Тегал: {state['user_tags'].get(key, 0)}. Напулял: {state['eggs'].get(key, 0)}.",
                 f"Отказов: {state['declines'].get(key, 0)}."]
        if key in state["links"]:
            try:
                lines.append(await deadlock.render_deadlock_stats(state["links"][key]))
            except (KeyError, TypeError, ValueError):
                log.exception("Неожиданный ответ Deadlock API")
                lines.append("Статистика Deadlock временно недоступна.")
        else:
            lines.append("Привяжи Steam через /link.")
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

    @bot.tree.command(description="Удалить сообщения этого бота в текущем канале за сутки")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def cleanup(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cutoff = discord.utils.utcnow() - timedelta(days=1)
        count = 0
        async for message in interaction.channel.history(limit=None, after=cutoff):
            if message.author.id == bot.user.id:
                await message.delete()
                count += 1
        await interaction.followup.send(f"Удалено сообщений: {count}.", ephemeral=True)

    reactionrole = app_commands.Group(name="reactionrole", description="Роли по реакциям",
                                     guild_only=True, default_permissions=discord.Permissions(manage_roles=True))

    @reactionrole.command(name="add", description="Привязать эмодзи к роли на сообщении этого бота")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reaction_add(interaction: discord.Interaction, channel: discord.TextChannel,
                           message_id: str, emoji: str, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        if not role_is_safe(role, interaction.guild.me):
            raise ValueError("Нужна роль без разрешений, ниже роли бота, не @everyone и не управляемая интеграцией.")
        if not interaction.user.guild_permissions.administrator and role >= interaction.user.top_role:
            raise ValueError("Нельзя выдавать роль на уровне твоей старшей роли или выше.")
        if not interaction.guild.me.guild_permissions.manage_roles:
            raise ValueError("У бота нет права Manage Roles.")
        if not message_id.isdigit():
            raise ValueError("Укажи числовой ID сообщения.")
        parsed = discord.PartialEmoji.from_str(emoji)
        message = await channel.fetch_message(int(message_id))
        if message.author.id != bot.user.id:
            raise ValueError("Привязки разрешены только к сообщениям этого бота. Создай /reactionrole panel.")
        # Проверяем доступность эмодзи до сохранения конфигурации.
        await message.add_reaction(parsed)
        async with bot.store.edit(interaction.guild_id) as state:
            mapping = state["reaction_roles"].setdefault(message_id, {"channel_id": channel.id, "roles": {}})
            key = emoji_key(parsed)
            if key in mapping["roles"] and mapping["roles"][key] != role.id:
                raise ValueError("У эмодзи уже есть другая роль. Сначала удали привязку.")
            if role.id in mapping["roles"].values() and mapping["roles"].get(key) != role.id:
                raise ValueError("Эта роль уже привязана к другому эмодзи этого сообщения.")
            mapping["roles"][key] = role.id
        await interaction.followup.send(f"{parsed} → {role.mention}. Поставь реакцию, чтобы получить роль.", ephemeral=True)

    @reactionrole.command(name="panel", description="Создать сообщение для ролей по реакциям")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reaction_panel(interaction: discord.Interaction, channel: discord.TextChannel, text: str):
        await interaction.response.defer(ephemeral=True)
        if not 1 <= len(text) <= 2000:
            raise ValueError("Текст панели должен содержать от 1 до 2000 символов.")
        message = await channel.send(text)
        await interaction.followup.send(f"Панель: {message.jump_url}\nID: {message.id}. Добавь привязки через /reactionrole add.", ephemeral=True)

    @reactionrole.command(name="remove", description="Удалить привязку эмодзи (выданные роли сохранятся)")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reaction_remove(interaction: discord.Interaction, message_id: str, emoji: str):
        async with bot.store.edit(interaction.guild_id) as state:
            mapping = state["reaction_roles"].get(message_id)
            if mapping:
                mapping["roles"].pop(emoji_key(discord.PartialEmoji.from_str(emoji)), None)
                if not mapping["roles"]:
                    state["reaction_roles"].pop(message_id)
        await interaction.response.send_message("Привязка удалена. Уже выданные роли сохранены.", ephemeral=True)

    @reactionrole.command(name="list", description="Показать настроенные привязки")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reaction_list(interaction: discord.Interaction):
        state = await bot.store.read(interaction.guild_id)
        lines = []
        for mid, mapping in state["reaction_roles"].items():
            url = f"https://discord.com/channels/{interaction.guild_id}/{mapping['channel_id']}/{mid}"
            for emoji, role_id in mapping["roles"].items():
                lines.append(f"{url} | {emoji} → <@&{role_id}>")
        await interaction.response.send_message(("\n".join(lines) or "Привязок нет.")[:1900], ephemeral=True)

    bot.tree.add_command(reactionrole)

    @bot.tree.error
    async def on_error(interaction, error):
        original = getattr(error, "original", error)
        if isinstance(original, (ValueError, app_commands.CheckFailure)):
            text = str(original)
        elif isinstance(original, discord.Forbidden):
            text = "Боту не хватает прав в канале или его роль ниже выдаваемой."
        elif isinstance(original, discord.NotFound):
            text = "Сообщение, участник или канал больше не существует."
        else:
            log.error("Ошибка команды", exc_info=(type(original), original, original.__traceback__))
            text = "Не удалось выполнить команду. Подробности в журнале бота."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


def main():
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Задай DISCORD_TOKEN в окружении или .env для Docker Compose.")
    bot = DeadlockBot(Store(Path(os.getenv("DATA_DIR", "data")) / "state.json"))
    register_commands(bot)
    bot.run(token)


if __name__ == "__main__":
    main()
