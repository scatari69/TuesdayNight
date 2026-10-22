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
import bnet
import mplus
import wow
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
        self.wow = wow.RaiderIO()
        self.dashboard_tasks = {}
        self.dashboard_locks = {}
        self.bnet = bnet.BattleNet(self)
        self.mplus = mplus.MPlus(self)

    async def setup_hook(self):
        await self.bnet.start()
        self.add_view(GatheringView())
        self.add_view(mplus.GroupView())
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.scheduler.start()
        self.dashboard_scheduler.start()
        self.mplus_scheduler.start()

    async def close(self):
        self.scheduler.cancel()
        self.dashboard_scheduler.cancel()
        self.mplus_scheduler.cancel()
        for task in self.dashboard_tasks.values():
            task.cancel()
        await asyncio.gather(*self.dashboard_tasks.values(), return_exceptions=True)
        await self.wow.close()
        await self.bnet.close()
        if deadlock._http_session is not None:
            await deadlock._http_session.close()
        await super().close()

    async def roster(self, guild, state):
        if not guild.chunked:
            await guild.chunk(cache=True)
        role = guild.get_role(state["role_id"]) if state["role_id"] else None
        if role is None:
            raise ValueError("Set up the player role with /setup first.")
        return eligible_members(role)

    async def gathering(self, guild, state):
        members = await self.roster(guild, state)
        lines = ["🚨 **Time to play! Who is joining?**"]
        for vote, label in (("+", "✅ Joining"), ("-", "❌ Not joining")):
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
                return f"You are not on the roster! Attempts: {count}."
            remaining = state["cooldown"] - (time.time() - state["last_tag"])
            if remaining > 0:
                return f"Please wait another {int(remaining) + 1} seconds."
            pending = [m for m in members if state["votes"].get(str(m.id)) != "+"]
            if not pending:
                return "All players have already joined, or the role has no players."
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
            return "Gathering posted."

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
            log.warning("Role %s can no longer be assigned in guild %s", role_id, guild.id)
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
            log.exception("Failed to update reaction role")

    async def on_raw_reaction_add(self, payload):
        await self.reaction(payload, True)

    async def on_raw_reaction_remove(self, payload):
        await self.reaction(payload, False)

    async def on_raw_message_delete(self, payload):
        if payload.guild_id:
            async with self.store.edit(payload.guild_id) as data:
                data["reaction_roles"].pop(str(payload.message_id), None)
                data["messages"] = [mid for mid in data["messages"] if mid != payload.message_id]
                for group in data['mplus_groups'].values():
                    if group['message_id'] == payload.message_id:
                        group['dirty'] = True

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
                log.exception("Scheduler error for %s", guild.id)

    @scheduler.before_loop
    async def before_scheduler(self):
        await self.wait_until_ready()

    def queue_dashboard(self, guild):
        task = self.dashboard_tasks.get(guild.id)
        if task is not None and not task.done():
            return 'An update is already running.'
        self.dashboard_tasks[guild.id] = asyncio.create_task(self.refresh_dashboard(guild))
        return 'Dashboard update queued. Large guilds may take several minutes.'

    async def publish_dashboard(self, guild, config):
        channel = guild.get_channel(config['channel_id'])
        if channel is None:
            raise ValueError('Dashboard channel is unavailable. Run /dashboard setup again.')
        embeds = wow.dashboard_embeds(config, config.get('snapshot'), config.get('error'))
        message = None
        if config.get('message_id'):
            try:
                message = await channel.fetch_message(config['message_id'])
            except discord.NotFound:
                pass
        if message is None:
            message = await channel.send(embeds=embeds, allowed_mentions=NO_MENTIONS)
            async with self.store.edit(guild.id) as state:
                state['dashboard']['message_id'] = message.id
            config['message_id'] = message.id
            try:
                await message.pin(reason='WoW guild dashboard')
            except discord.HTTPException:
                log.warning('Could not pin dashboard in guild %s; pin it manually.', guild.id)
        else:
            await message.edit(embeds=embeds, allowed_mentions=NO_MENTIONS)

    async def refresh_dashboard(self, guild):
        lock = self.dashboard_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            config = (await self.store.read(guild.id))['dashboard']
            if config is None:
                return
            try:
                async with self.store.edit(guild.id) as state:
                    state['dashboard']['last_attempt'] = time.time()
                try:
                    snapshot = await self.wow.snapshot(config)
                    config['snapshot'], config['error'] = snapshot, None
                except (ValueError, KeyError, TypeError, AttributeError):
                    log.exception('WoW dashboard data update failed for %s', guild.id)
                    config['error'] = 'Could not update Raider.IO data. Check the guild settings and try again later.'
                async with self.store.edit(guild.id) as state:
                    state['dashboard']['snapshot'] = config.get('snapshot')
                    state['dashboard']['error'] = config['error']
                await self.publish_dashboard(guild, config)
            except (discord.HTTPException, ValueError, OSError):
                log.exception('WoW dashboard publication failed for %s', guild.id)
                try:
                    async with self.store.edit(guild.id) as state:
                        state['dashboard']['error'] = 'Dashboard publication failed. Check channel access, Embed Links, and message permissions.'
                except OSError:
                    log.exception('Could not save dashboard error for %s', guild.id)

    @tasks.loop(minutes=1)
    async def dashboard_scheduler(self):
        for guild in self.guilds:
            config = (await self.store.read(guild.id))['dashboard']
            if config and time.time() - config.get('last_attempt', 0) >= wow.REFRESH_SECONDS:
                self.queue_dashboard(guild)

    @dashboard_scheduler.before_loop
    async def before_dashboard_scheduler(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def mplus_scheduler(self):
        for guild in self.guilds:
            try:
                await self.mplus.maintenance(guild)
            except (discord.HTTPException, ValueError, OSError):
                log.exception('Mythic+ maintenance failed for %s', guild.id)

    @mplus_scheduler.before_loop
    async def before_mplus_scheduler(self):
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
                await interaction.followup.send("This gathering is closed.", ephemeral=True)
                return
            members = await bot.roster(interaction.guild, state)
            if interaction.user.id not in {m.id for m in members}:
                await interaction.followup.send("You need the player role to vote.", ephemeral=True)
                return
            async with bot.store.edit(interaction.guild_id) as data:
                changed = record_vote(data, interaction.user.id, value)
            state = await bot.store.read(interaction.guild_id)
            await interaction.message.edit(content=await bot.gathering(interaction.guild, state),
                                           view=self, allowed_mentions=NO_MENTIONS)
            await interaction.followup.send("Vote recorded!" if changed else "Already recorded.", ephemeral=True)

    @discord.ui.button(label="Joining", emoji="➕", style=discord.ButtonStyle.success,
                       custom_id="deadlock:plus")
    async def plus(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.vote(interaction, "+")

    @discord.ui.button(label="Not joining", emoji="➖", style=discord.ButtonStyle.secondary,
                       custom_id="deadlock:minus")
    async def minus(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.vote(interaction, "-")

    async def on_error(self, interaction, error, item):
        log.error("Button error", exc_info=(type(error), error, error.__traceback__))
        if interaction.response.is_done():
            await interaction.followup.send("Could not process your vote. Please try again later.", ephemeral=True)
        else:
            await interaction.response.send_message("Could not process your vote.", ephemeral=True)


def register_commands(bot):
    mplus.register_commands(bot)
    character = app_commands.Group(name='character', description='Link and manage your verified WoW characters', guild_only=True)

    @character.command(name='link', description='Verify ownership of your WoW characters with Battle.net')
    @app_commands.choices(region=[app_commands.Choice(name=r.upper(), value=r) for r in bnet.REGIONS])
    async def character_link(interaction: discord.Interaction, region: str):
        url = bot.bnet.link(interaction.guild_id, interaction.user.id, region)
        view = discord.ui.View(timeout=600)
        view.add_item(discord.ui.Button(label='Sign in with Battle.net', url=url))
        await interaction.response.send_message(
            'Sign in with Battle.net, then return here and use /character add. '
            'This personal link expires in 10 minutes. Do not share it.', view=view, ephemeral=True)

    @character.command(name='add', description='Choose a character verified by your Battle.net sign-in')
    async def character_add(interaction: discord.Interaction, character: str):
        await interaction.response.defer(ephemeral=True)
        selected = await bot.bnet.add(interaction.guild_id, interaction.user.id, character)
        await interaction.followup.send(f"Linked {wow.clean(selected['name'])} / {wow.clean(selected['realm'])}. "
                                      'Your first character is your main. Change it with /character main.', ephemeral=True)

    @character_add.autocomplete('character')
    async def verified_choices(interaction: discord.Interaction, current: str):
        candidates = bot.bnet.candidates(interaction.guild_id, interaction.user.id)
        return character_choices(candidates, current)

    @character.command(name='list', description='Show your linked WoW characters and main')
    async def character_list(interaction: discord.Interaction):
        state = await bot.store.read(interaction.guild_id)
        roster = state['wow_characters'].get(str(interaction.user.id), {'characters': {}, 'main': None})
        lines = [f"{'★ ' if key == roster['main'] else ''}{wow.clean(c['name'])} / {wow.clean(c['realm'])} ({c['region'].upper()})"
                 for key, c in roster['characters'].items()]
        await interaction.response.send_message(('\n'.join(lines) or 'No linked characters. Use /character link.')[:1900], ephemeral=True)

    @character.command(name='main', description='Choose your main from your linked characters')
    async def character_main(interaction: discord.Interaction, character: str):
        async with bot.store.edit(interaction.guild_id) as state:
            roster = state['wow_characters'].get(str(interaction.user.id))
            if roster is None or character not in roster['characters']:
                raise ValueError('Choose one of your linked characters from autocomplete.')
            roster['main'] = character
        await interaction.response.send_message('Main character updated.', ephemeral=True)

    @character.command(name='remove', description='Unlink one of your WoW characters')
    async def character_remove(interaction: discord.Interaction, character: str):
        async with bot.store.edit(interaction.guild_id) as state:
            roster = state['wow_characters'].get(str(interaction.user.id))
            if roster is None or character not in roster['characters']:
                raise ValueError('Choose one of your linked characters from autocomplete.')
            del roster['characters'][character]
            mplus.unlink_entries(state, interaction.user.id, character)
            if roster['main'] == character:
                roster['main'] = next(iter(roster['characters']), None)
            if not roster['characters']:
                del state['wow_characters'][str(interaction.user.id)]
        bot.bnet.invalidate(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message('Character unlinked. Any remaining character is now your main if needed.', ephemeral=True)

    @character_main.autocomplete('character')
    @character_remove.autocomplete('character')
    async def linked_choices(interaction: discord.Interaction, current: str):
        state = await bot.store.read(interaction.guild_id)
        roster = state['wow_characters'].get(str(interaction.user.id), {'characters': {}})
        return character_choices(roster['characters'], current)

    @character.command(name='unlink', description='Remove all your WoW links on this Discord server')
    async def character_unlink(interaction: discord.Interaction):
        bot.bnet.invalidate(interaction.guild_id, interaction.user.id)
        async with bot.store.edit(interaction.guild_id) as state:
            state['wow_characters'].pop(str(interaction.user.id), None)
            mplus.unlink_entries(state, interaction.user.id)
        await interaction.response.send_message('All your WoW character links on this server were removed.', ephemeral=True)

    bot.tree.add_command(character)

    dashboard = app_commands.Group(name='dashboard', description='WoW guild dashboard',
                                  guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    @dashboard.command(name='setup', description='Configure the WoW guild and dashboard channel')
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(region=[app_commands.Choice(name=r.upper(), value=r) for r in ('eu', 'us', 'kr', 'tw')])
    async def dashboard_setup(interaction: discord.Interaction, region: str, realm: str,
                              guild_name: str, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        if region not in ('eu', 'us', 'kr', 'tw') or not realm.strip() or not guild_name.strip():
            raise ValueError('Choose a supported region and enter the realm and guild name.')
        if len(realm) > 100 or len(guild_name) > 100:
            raise ValueError('Realm and guild names must be at most 100 characters.')
        permissions = channel.permissions_for(interaction.guild.me)
        if not (permissions.view_channel and permissions.send_messages and
                permissions.read_message_history and permissions.embed_links):
            raise ValueError('The bot needs View Channel, Send Messages, Read Message History, and Embed Links.')
        lock = bot.dashboard_locks.setdefault(interaction.guild_id, asyncio.Lock())
        if lock.locked():
            raise ValueError('A dashboard update is running. Wait for it to finish before changing settings.')
        async with lock:
            config = {'region': region, 'realm': realm.strip(), 'name': guild_name.strip(),
                      'channel_id': channel.id, 'message_id': None, 'snapshot': None,
                      'last_attempt': 0, 'error': None}
            profile = await bot.wow.guild(config)
            if not isinstance(profile.get('members'), list):
                raise ValueError('Raider.IO did not return the guild roster. Check the guild/realm names.')
            previous = (await bot.store.read(interaction.guild_id))['dashboard']
            if previous and previous['channel_id'] == channel.id:
                config['message_id'] = previous.get('message_id')
            async with bot.store.edit(interaction.guild_id) as state:
                state['dashboard'] = config
            await bot.publish_dashboard(interaction.guild, config)
        await interaction.followup.send(bot.queue_dashboard(interaction.guild), ephemeral=True)

    @dashboard.command(name='refresh', description='Queue a dashboard update using cached API data')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def dashboard_refresh(interaction: discord.Interaction):
        config = (await bot.store.read(interaction.guild_id))['dashboard']
        if config is None:
            raise ValueError('Configure the dashboard with /dashboard setup first.')
        if time.time() - config.get('last_attempt', 0) < 60:
            raise ValueError('Please wait at least 60 seconds between dashboard updates.')
        await interaction.response.send_message(bot.queue_dashboard(interaction.guild), ephemeral=True)

    @dashboard.command(name='status', description='Show dashboard settings and the last update error')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def dashboard_status(interaction: discord.Interaction):
        config = (await bot.store.read(interaction.guild_id))['dashboard']
        if config is None:
            raise ValueError('Configure the dashboard with /dashboard setup first.')
        task = bot.dashboard_tasks.get(interaction.guild_id)
        text = (f"Guild: {wow.clean(config['name'])} / {wow.clean(config['realm'])} ({config['region'].upper()})\n"
                f"Updating: {'Yes' if task and not task.done() else 'No'}\n"
                f"Last error: {config.get('error') or 'None'}")
        await interaction.response.send_message(text, ephemeral=True)

    bot.tree.add_command(dashboard)

    @bot.tree.command(description="Set the player role and gathering channel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(interaction: discord.Interaction, role: discord.Role, channel: discord.TextChannel):
        if role.is_default() or role.managed:
            raise ValueError("Choose a regular player role, such as @deadlock.")
        permissions = channel.permissions_for(interaction.guild.me)
        if not (permissions.view_channel and permissions.send_messages and permissions.read_message_history):
            raise ValueError("The bot needs View Channel, Send Messages, and Read Message History permissions.")
        async with bot.actions:
            async with bot.store.edit(interaction.guild_id) as state:
                if state["role_id"] != role.id:
                    state["votes"] = {}
                    state["messages"] = []
                state["role_id"], state["channel_id"] = role.id, channel.id
        await interaction.response.send_message(f"Role: {role.mention}. Channel: {channel.mention}.", ephemeral=True)

    @bot.tree.command(description="Invite players who have not joined yet")
    @app_commands.guild_only()
    async def tag(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        state = await bot.store.read(interaction.guild_id)
        channel = interaction.guild.get_channel(state["channel_id"])
        if channel is None:
            raise ValueError("Set up the channel with /setup first.")
        text = await bot.start_gathering(interaction.guild, channel, interaction.user)
        await interaction.followup.send(text, ephemeral=True)

    @bot.tree.command(description="List current members of the player role")
    @app_commands.guild_only()
    async def team(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        members = await bot.roster(interaction.guild, await bot.store.read(interaction.guild_id))
        names = "\n".join(discord.utils.escape_markdown(m.display_name) for m in members)
        await interaction.followup.send((f"Players: {len(members)}\n" + names)[:1900], ephemeral=True)

    @bot.tree.command(description="Gathering settings: HH:MM times; disable turns off automatic gatherings")
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
            text = f"Cooldown: {state['cooldown']} seconds. Reset: {state['reset_time']}. Auto-gather: {state['autotag'] or 'disabled'}."
        await interaction.response.send_message(text, ephemeral=True)

    @bot.tree.command(description="Close the gathering and clear votes")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def clear(interaction: discord.Interaction):
        async with bot.actions:
            async with bot.store.edit(interaction.guild_id) as state:
                state["votes"], state["messages"], state["last_tag"] = {}, [], 0
        await interaction.response.send_message("Gathering closed. You can start a new one.", ephemeral=True)

    @bot.tree.command(description="Gathering statistics and top players")
    @app_commands.guild_only()
    async def stats(interaction: discord.Interaction):
        state = await bot.store.read(interaction.guild_id)
        lines = [f"Gatherings: {state['tags']}"]
        for label, field in (("Gathering starters", "user_tags"), ("Off-roster attempts", "eggs"), ("Declines", "declines")):
            top = sorted(state[field].items(), key=lambda pair: pair[1], reverse=True)[:3]
            lines.append(f"\n**{label}:**")
            lines.extend(f"<@{uid}> — {count}" for uid, count in top)
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(description="Link a Steam/tracklock profile or numeric ID; remove unlinks it")
    @app_commands.guild_only()
    async def link(interaction: discord.Interaction, profile: str):
        await interaction.response.defer(ephemeral=True)
        key = str(interaction.user.id)
        if profile == "remove":
            async with bot.store.edit(interaction.guild_id) as state:
                state["links"].pop(key, None)
            await interaction.followup.send("Profile unlinked.", ephemeral=True)
            return
        account, error = await deadlock.resolve_account_id(profile)
        if error or account is None or not 0 < account < 2**32:
            raise ValueError("Could not identify the profile. Steam /id/name links require STEAM_API_KEY.")
        async with bot.store.edit(interaction.guild_id) as state:
            state["links"][key] = account
        await interaction.followup.send("Profile linked. View your stats with /me.", ephemeral=True)

    @bot.tree.command(description="Your profile and Deadlock statistics")
    @app_commands.guild_only()
    async def me(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        state = await bot.store.read(interaction.guild_id)
        key = str(interaction.user.id)
        members = await bot.roster(interaction.guild, state)
        lines = ["On the roster" if interaction.user.id in {m.id for m in members} else "Not on the roster",
                 f"Gatherings started: {state['user_tags'].get(key, 0)}. Off-roster attempts: {state['eggs'].get(key, 0)}.",
                 f"Declines: {state['declines'].get(key, 0)}."]
        if key in state["links"]:
            try:
                lines.append(await deadlock.render_deadlock_stats(state["links"][key]))
            except (KeyError, TypeError, ValueError):
                log.exception("Unexpected Deadlock API response")
                lines.append("Deadlock statistics are temporarily unavailable.")
        else:
            lines.append("Link your Steam profile with /link.")
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

    @bot.tree.command(description="Delete this bot's messages from the last 24 hours in this channel")
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
        await interaction.followup.send(f"Messages deleted: {count}.", ephemeral=True)

    reactionrole = app_commands.Group(name="reactionrole", description="Reaction roles",
                                     guild_only=True, default_permissions=discord.Permissions(manage_roles=True))

    @reactionrole.command(name="add", description="Link an emoji to a role on this bot's message")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reaction_add(interaction: discord.Interaction, channel: discord.TextChannel,
                           message_id: str, emoji: str, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        if not role_is_safe(role, interaction.guild.me):
            raise ValueError("Choose a role without permissions, below the bot, other than @everyone or an integration-managed role.")
        if not interaction.user.guild_permissions.administrator and role >= interaction.user.top_role:
            raise ValueError("You cannot assign a role at or above your highest role.")
        if not interaction.guild.me.guild_permissions.manage_roles:
            raise ValueError("The bot needs the Manage Roles permission.")
        if not message_id.isdigit():
            raise ValueError("Enter a numeric message ID.")
        parsed = discord.PartialEmoji.from_str(emoji)
        message = await channel.fetch_message(int(message_id))
        if message.author.id != bot.user.id:
            raise ValueError("Only this bot's messages can have role mappings. Create one with /reactionrole panel.")
        # Проверяем доступность эмодзи до сохранения конфигурации.
        await message.add_reaction(parsed)
        async with bot.store.edit(interaction.guild_id) as state:
            mapping = state["reaction_roles"].setdefault(message_id, {"channel_id": channel.id, "roles": {}})
            key = emoji_key(parsed)
            if key in mapping["roles"] and mapping["roles"][key] != role.id:
                raise ValueError("This emoji already has another role. Remove the mapping first.")
            if role.id in mapping["roles"].values() and mapping["roles"].get(key) != role.id:
                raise ValueError("This role is already linked to another emoji on this message.")
            mapping["roles"][key] = role.id
        await interaction.followup.send(f"{parsed} → {role.mention}. React to receive the role.", ephemeral=True)

    @reactionrole.command(name="panel", description="Create a reaction role panel")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reaction_panel(interaction: discord.Interaction, channel: discord.TextChannel, text: str):
        await interaction.response.defer(ephemeral=True)
        if not 1 <= len(text) <= 2000:
            raise ValueError("Panel text must contain between 1 and 2000 characters.")
        message = await channel.send(text)
        await interaction.followup.send(f"Panel: {message.jump_url}\nID: {message.id}. Add mappings with /reactionrole add.", ephemeral=True)

    @reactionrole.command(name="remove", description="Remove an emoji mapping (existing roles are kept)")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reaction_remove(interaction: discord.Interaction, message_id: str, emoji: str):
        async with bot.store.edit(interaction.guild_id) as state:
            mapping = state["reaction_roles"].get(message_id)
            if mapping:
                mapping["roles"].pop(emoji_key(discord.PartialEmoji.from_str(emoji)), None)
                if not mapping["roles"]:
                    state["reaction_roles"].pop(message_id)
        await interaction.response.send_message("Mapping removed. Previously assigned roles are kept.", ephemeral=True)

    @reactionrole.command(name="list", description="List configured role mappings")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reaction_list(interaction: discord.Interaction):
        state = await bot.store.read(interaction.guild_id)
        lines = []
        for mid, mapping in state["reaction_roles"].items():
            url = f"https://discord.com/channels/{interaction.guild_id}/{mapping['channel_id']}/{mid}"
            for emoji, role_id in mapping["roles"].items():
                lines.append(f"{url} | {emoji} → <@&{role_id}>")
        await interaction.response.send_message(("\n".join(lines) or "No role mappings.")[:1900], ephemeral=True)

    bot.tree.add_command(reactionrole)

    @bot.tree.error
    async def on_error(interaction, error):
        original = getattr(error, "original", error)
        if isinstance(original, (ValueError, app_commands.CheckFailure)):
            text = str(original)
        elif isinstance(original, discord.Forbidden):
            text = "The bot lacks channel permissions or its role is below the role being assigned."
        elif isinstance(original, discord.NotFound):
            text = "The message, member, or channel no longer exists."
        else:
            log.error("Command error", exc_info=(type(original), original, original.__traceback__))
            text = "Could not run the command. See the bot logs for details."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


def character_choices(characters, current):
    choices = []
    for key, character in characters.items():
        label = f"{character['name']} / {character['realm']} ({character['region'].upper()})"
        if current.casefold() in label.casefold():
            choices.append(app_commands.Choice(name=label[:100], value=key))
            if len(choices) == 25:
                break
    return choices


def main():
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in the environment or in .env for Docker Compose.")
    bot = DeadlockBot(Store(Path(os.getenv("DATA_DIR", "data")) / "state.json"))
    register_commands(bot)
    bot.run(token)


if __name__ == "__main__":
    main()
