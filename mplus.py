import asyncio
import logging
import time
from datetime import datetime

import discord
from discord import app_commands

log = logging.getLogger(__name__)
CAPACITY = {'tank': 1, 'healer': 1, 'dps': 3}
MODES = ('push', 'relaxed', 'carry')
# Роль определяется специализацией, а не вводится отдельно.
CLASSES = {
    'Warrior': {'Arms': 'dps', 'Fury': 'dps', 'Protection': 'tank'},
    'Paladin': {'Holy': 'healer', 'Protection': 'tank', 'Retribution': 'dps'},
    'Hunter': {'Beast Mastery': 'dps', 'Marksmanship': 'dps', 'Survival': 'dps'},
    'Rogue': {'Assassination': 'dps', 'Outlaw': 'dps', 'Subtlety': 'dps'},
    'Priest': {'Discipline': 'healer', 'Holy': 'healer', 'Shadow': 'dps'},
    'Death Knight': {'Blood': 'tank', 'Frost': 'dps', 'Unholy': 'dps'},
    'Shaman': {'Elemental': 'dps', 'Enhancement': 'dps', 'Restoration': 'healer'},
    'Mage': {'Arcane': 'dps', 'Fire': 'dps', 'Frost': 'dps'},
    'Warlock': {'Affliction': 'dps', 'Demonology': 'dps', 'Destruction': 'dps'},
    'Monk': {'Brewmaster': 'tank', 'Mistweaver': 'healer', 'Windwalker': 'dps'},
    'Druid': {'Balance': 'dps', 'Feral': 'dps', 'Guardian': 'tank', 'Restoration': 'healer'},
    'Demon Hunter': {'Havoc': 'dps', 'Vengeance': 'tank', 'Devourer': 'dps'},
    'Evoker': {'Devastation': 'dps', 'Preservation': 'healer', 'Augmentation': 'dps'},
}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def player(class_name, spec):
    role = CLASSES.get(class_name, {}).get(spec)
    if role is None:
        raise ValueError('Choose a class and its specialization from the list.')
    return {'class': class_name, 'spec': spec, 'role': role}


def open_group(state, group_id):
    group = state['mplus_groups'].get(str(group_id))
    if group is None or group['status'] != 'open':
        raise ValueError('This group is closed or does not exist on this server.')
    return group


def require_leader(group, user):
    if group['leader_id'] != user.id and not user.guild_permissions.manage_guild:
        raise ValueError('Only the leader or a member with Manage Server permission can do this.')


class MPlus:
    def __init__(self, bot):
        self.bot = bot
        self.publish_locks = {}

    async def member(self, guild, uid, state):
        member = await guild.fetch_member(uid)
        if member.bot:
            raise ValueError('Bots cannot join groups.')
        role_id = state['role_id']
        if role_id and not member.guild_permissions.manage_guild and not any(r.id == role_id for r in member.roles):
            raise ValueError('You need the configured player role to join.')
        return member

    async def create(self, guild, uid, class_name, spec, dungeon, level, mode, start):
        entry = player(class_name, spec)
        dungeon = dungeon.strip()
        if not dungeon or len(dungeon) > 100 or type(level) is not int or not 2 <= level <= 99 or mode not in MODES:
            raise ValueError('Enter a dungeon (up to 100 characters), level 2–99, and mode: push, relaxed, or carry.')
        start_at = timestamp(start)
        if start_at is None or not time.time() <= start_at <= time.time() + 30 * 86400:
            raise ValueError('Enter a future time with a timezone within the next 30 days, e.g. 2026-10-25T20:00+02:00.')
        state = await self.bot.store.read(guild.id)
        await self.member(guild, uid, state)
        async with self.bot.store.edit(guild.id) as data:
            settings = data['mplus_settings']
            if settings is None:
                raise ValueError('Set up the channel with /mplus setup first.')
            if data['role_id'] != state['role_id']:
                raise ValueError('The player role has changed. Please try again.')
            if sum(g['status'] == 'open' and g['leader_id'] == uid for g in data['mplus_groups'].values()) >= 3:
                raise ValueError('Close one of your three open groups first.')
            group_id = str(data['mplus_next'])
            data['mplus_next'] += 1
            data['mplus_groups'][group_id] = {
                'version': 2, 'leader_id': uid, 'dungeon': dungeon, 'level': level, 'mode': mode,
                'start': start_at, 'status': 'open', 'players': {str(uid): entry},
                'channel_id': settings['channel_id'], 'message_id': None,
                'revision': 0, 'created': time.time(), 'dirty': True,
            }
            for gid in list(data['mplus_groups']):
                old = data['mplus_groups'][gid]
                if old['status'] != 'open' and not old.get('dirty') and time.time() - old['created'] > 7 * 86400:
                    del data['mplus_groups'][gid]
        await self.publish(guild, group_id)
        return group_id

    async def join(self, guild, uid, group_id, class_name, spec):
        entry = player(class_name, spec)
        state = await self.bot.store.read(guild.id)
        await self.member(guild, uid, state)
        async with self.bot.store.edit(guild.id) as data:
            if data['role_id'] != state['role_id']:
                raise ValueError('The player role has changed. Please try again.')
            group = open_group(data, group_id)
            if str(uid) in group['players']:
                raise ValueError('You have already joined. Leave the group before changing specialization.')
            if sum(e['role'] == entry['role'] for e in group['players'].values()) >= CAPACITY[entry['role']]:
                raise ValueError('All slots for this role are taken.')
            group['players'][str(uid)] = entry
            group['revision'] += 1
            group['dirty'] = True
        await self.publish(guild, group_id)

    async def change(self, guild, user, group_id, action, target=None):
        async with self.bot.store.edit(guild.id) as data:
            group = open_group(data, group_id)
            if action != 'leave':
                require_leader(group, user)
            if action in ('leave', 'kick'):
                uid = user.id if action == 'leave' else target
                if str(uid) not in group['players']:
                    raise ValueError('This member has not joined the group.')
                del group['players'][str(uid)]
                if uid == group['leader_id']:
                    group['status'] = 'cancelled'
            elif action == 'transfer':
                if str(target) not in group['players']:
                    raise ValueError('You can only transfer leadership to a group member.')
                group['leader_id'] = target
            elif action == 'cancel':
                group['status'] = 'cancelled'
            elif action == 'start':
                if any(sum(e['role'] == role for e in group['players'].values()) != size
                       for role, size in CAPACITY.items()):
                    raise ValueError('Starting requires 1 tank, 1 healer, and 3 DPS.')
                group['status'] = 'started'
            else:
                raise ValueError('Unknown action.')
            group['revision'] += 1
            group['dirty'] = True
        await self.publish(guild, group_id)

    async def publish(self, guild, group_id):
        lock = self.publish_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            state = await self.bot.store.read(guild.id)
            group = state['mplus_groups'].get(str(group_id))
            if not group:
                return
            try:
                channel = guild.get_channel(group['channel_id'])
                if channel is None:
                    raise ValueError('The group channel is unavailable.')
                message = None
                if group['message_id']:
                    try:
                        message = await channel.fetch_message(group['message_id'])
                    except discord.NotFound:
                        pass
                kwargs = {'embed': group_embed(group_id, group),
                          'view': GroupView() if group['status'] == 'open' else None,
                          'allowed_mentions': discord.AllowedMentions.none()}
                if message:
                    await message.edit(**kwargs)
                else:
                    message = await channel.send(**kwargs)
                async with self.bot.store.edit(guild.id) as data:
                    latest = data['mplus_groups'][str(group_id)]
                    latest['message_id'] = message.id
                    if latest['revision'] == group['revision']:
                        latest['dirty'] = False
                        latest.pop('publish_error', None)
            except (discord.HTTPException, ValueError):
                log.exception('Could not publish Mythic+ group %s in guild %s', group_id, guild.id)
                async with self.bot.store.edit(guild.id) as data:
                    data['mplus_groups'][str(group_id)]['publish_error'] = 'Could not update the card. Check channel permissions.'
                    data['mplus_groups'][str(group_id)]['dirty'] = True

    async def maintenance(self, guild):
        state = await self.bot.store.read(guild.id)
        if not state['mplus_groups']:
            return
        changed = []
        async with self.bot.store.edit(guild.id) as data:
            for gid, group in data['mplus_groups'].items():
                if group['status'] == 'open' and time.time() > group['start'] + 3600:
                    group['status'] = 'expired'
                    group['revision'] += 1
                    group['dirty'] = True
                if group.get('dirty'):
                    changed.append(gid)
        for gid in changed:
            await self.publish(guild, gid)


def group_embed(group_id, group):
    embed = discord.Embed(title=f"M+ #{group_id} • +{group['level']} {discord.utils.escape_markdown(group['dungeon'])}"[:256],
        description=f"{group['mode'].title()} • {group['status']}\nStarts: <t:{int(group['start'])}:F>", color=0x5865F2)
    for role, size in CAPACITY.items():
        rows = [(uid, e) for uid, e in group['players'].items() if e['role'] == role]
        lines = [f"<@{uid}> — {e['class']} / {e['spec']}" + (' ★' if int(uid) == group['leader_id'] else '')
                 for uid, e in rows]
        embed.add_field(name=f'{role.title()} ({len(rows)}/{size})', value='\n'.join(lines) or 'Available', inline=False)
    if group.get('publish_error'):
        embed.add_field(name='Publication error', value=group['publish_error'], inline=False)
    return embed


async def interaction_error(interaction, error):
    text = str(error) if isinstance(error, ValueError) else 'Could not update the group. Please try again later.'
    if not isinstance(error, ValueError):
        log.error('Mythic+ interaction error', exc_info=(type(error), error, error.__traceback__))
    sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    await sender(text[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class PlayerModal(discord.ui.Modal):
    def __init__(self, uid, group_id=None, creation=None):
        super().__init__(title='Choose class and specialization', timeout=300)
        self.uid, self.group_id, self.creation = uid, group_id, creation
        options = [discord.SelectOption(label=f'{cls} / {spec}', value=f'{cls}:{spec}')
                   for cls, specs in CLASSES.items() for spec in specs]
        self.selects = []
        # В одном списке Discord допускает не более 25 вариантов.
        for offset, label in ((0, 'Classes: Warrior — Death Knight'), (18, 'Classes: Shaman — Evoker')):
            select = discord.ui.Select(options=options[offset:18] if offset == 0 else options[offset:],
                                       min_values=0, max_values=1, required=False, placeholder='Choose from one of the two lists')
            self.selects.append(select)
            self.add_item(discord.ui.Label(text=label, component=select))

    async def on_submit(self, interaction):
        if interaction.user.id != self.uid:
            raise ValueError('This form belongs to another member.')
        choices = [value for select in self.selects for value in select.values]
        if len(choices) != 1:
            raise ValueError('Choose exactly one specialization from one of the two lists.')
        class_name, spec = choices[0].split(':', 1)
        await interaction.response.defer(ephemeral=True)
        service = interaction.client.mplus
        if self.creation is not None:
            gid = await service.create(interaction.guild, self.uid, class_name, spec, **self.creation)
            text = f'Group #{gid} created.'
        else:
            await service.join(interaction.guild, self.uid, self.group_id, class_name, spec)
            text = 'You have joined the group.'
        await interaction.followup.send(text, ephemeral=True)

    async def on_error(self, interaction, error):
        await interaction_error(interaction, error)


class GroupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def group_id(self, interaction):
        state = await interaction.client.store.read(interaction.guild_id)
        for gid, group in state['mplus_groups'].items():
            if group['message_id'] == interaction.message.id and group['channel_id'] == interaction.channel_id:
                open_group(state, gid)
                return gid
        raise ValueError('This group card is no longer active.')

    @discord.ui.button(label='Join', style=discord.ButtonStyle.success, custom_id='mplus:join')
    async def join(self, interaction, button):
        gid = await self.group_id(interaction)
        await interaction.response.send_modal(PlayerModal(interaction.user.id, group_id=gid))

    async def action(self, interaction, action):
        await interaction.response.defer(ephemeral=True)
        gid = await self.group_id(interaction)
        await interaction.client.mplus.change(interaction.guild, interaction.user, gid, action)
        await interaction.followup.send('Group updated.', ephemeral=True)

    @discord.ui.button(label='Leave', custom_id='mplus:leave')
    async def leave(self, interaction, button):
        await self.action(interaction, 'leave')

    @discord.ui.button(label='Start', custom_id='mplus:start', style=discord.ButtonStyle.primary)
    async def start(self, interaction, button):
        await self.action(interaction, 'start')

    @discord.ui.button(label='Cancel', custom_id='mplus:cancel', style=discord.ButtonStyle.danger)
    async def cancel(self, interaction, button):
        await self.action(interaction, 'cancel')

    async def on_error(self, interaction, error, item):
        await interaction_error(interaction, error)


def register_commands(bot):
    commands = app_commands.Group(name='mplus', description='Mythic+ groups', guild_only=True)

    @commands.command(name='setup', description='Configure the Mythic+ group channel')
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
        permissions = channel.permissions_for(interaction.guild.me)
        if not (permissions.view_channel and permissions.send_messages and permissions.read_message_history and permissions.embed_links):
            raise ValueError('The bot needs View Channel, Send Messages, Read Message History, and Embed Links.')
        async with bot.store.edit(interaction.guild_id) as state:
            state['mplus_settings'] = {'channel_id': channel.id}
        await interaction.response.send_message('Group channel configured.', ephemeral=True)

    @commands.command(name='create', description='Create a group and choose your specialization')
    @app_commands.choices(mode=[app_commands.Choice(name=m, value=m) for m in MODES])
    async def create(interaction: discord.Interaction, dungeon: app_commands.Range[str, 1, 100],
                     level: app_commands.Range[int, 2, 99], start: str, mode: str):
        await interaction.response.send_modal(PlayerModal(interaction.user.id,
            creation={'dungeon': dungeon, 'level': level, 'start': start, 'mode': mode}))

    @commands.command(name='join', description='Join a group: choose your class and specialization')
    async def join(interaction: discord.Interaction, group: str):
        state = await bot.store.read(interaction.guild_id)
        open_group(state, group)
        await interaction.response.send_modal(PlayerModal(interaction.user.id, group_id=group))

    @commands.command(name='show', description='Show a group')
    async def show(interaction: discord.Interaction, group: str):
        state = await bot.store.read(interaction.guild_id)
        selected = state['mplus_groups'].get(group)
        if selected is None:
            raise ValueError('This group does not exist on this server.')
        await interaction.response.send_message(embed=group_embed(group, selected), ephemeral=True,
                                               allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name='kick', description='Remove a member from a group')
    async def kick(interaction: discord.Interaction, group: str, member: discord.Member):
        await action(interaction, group, 'kick', member.id)

    @commands.command(name='transfer', description='Transfer leadership to a group member')
    async def transfer(interaction: discord.Interaction, group: str, member: discord.Member):
        await action(interaction, group, 'transfer', member.id)

    @commands.command(name='leave', description='Leave a group; the leader leaving cancels it')
    async def leave(interaction: discord.Interaction, group: str):
        await action(interaction, group, 'leave')

    @commands.command(name='start', description='Start a full 1/1/3 group and close sign-ups')
    async def start_group(interaction: discord.Interaction, group: str):
        await action(interaction, group, 'start')

    @commands.command(name='cancel', description='Cancel a group')
    async def cancel(interaction: discord.Interaction, group: str):
        await action(interaction, group, 'cancel')

    async def action(interaction, group, name, target=None):
        await interaction.response.defer(ephemeral=True)
        await bot.mplus.change(interaction.guild, interaction.user, group, name, target)
        await interaction.followup.send('Group updated.', ephemeral=True)

    @join.autocomplete('group')
    @show.autocomplete('group')
    @kick.autocomplete('group')
    @transfer.autocomplete('group')
    @leave.autocomplete('group')
    @start_group.autocomplete('group')
    @cancel.autocomplete('group')
    async def group_choices(interaction: discord.Interaction, current: str):
        state = await bot.store.read(interaction.guild_id)
        return [app_commands.Choice(name=f"#{gid} +{g['level']} {g['dungeon']} ({g['mode']})"[:100], value=gid)
                for gid, g in state['mplus_groups'].items() if g['status'] == 'open' and
                current.casefold() in f"{gid} {g['dungeon']}".casefold()][:25]

    bot.tree.add_command(commands)
