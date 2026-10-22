import asyncio
import logging
import time
from datetime import datetime, timezone

import discord
from discord import app_commands

import wow

log = logging.getLogger(__name__)
CAPACITY = {'tank': 1, 'healer': 1, 'dps': 3}
CLASSES = {
    'Warrior': ('tank', 'dps'), 'Paladin': ('tank', 'healer', 'dps'), 'Hunter': ('dps',),
    'Rogue': ('dps',), 'Priest': ('healer', 'dps'), 'Death Knight': ('tank', 'dps'),
    'Shaman': ('healer', 'dps'), 'Mage': ('dps',), 'Warlock': ('dps',),
    'Monk': ('tank', 'healer', 'dps'), 'Druid': ('tank', 'healer', 'dps'),
    'Demon Hunter': ('tank', 'dps'), 'Evoker': ('healer', 'dps'),
}
CLASS_IDS = {1: 'Warrior', 2: 'Paladin', 3: 'Hunter', 4: 'Rogue', 5: 'Priest',
             6: 'Death Knight', 7: 'Shaman', 8: 'Mage', 9: 'Warlock', 10: 'Monk',
             11: 'Druid', 12: 'Demon Hunter', 13: 'Evoker'}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def current_season(data, region):
    now = time.time()
    seasons = [s for s in data.get('seasons', []) if s.get('is_main_season') and
               (timestamp(s.get('starts', {}).get(region)) or float('inf')) <= now <
               (timestamp(s.get('ends', {}).get(region)) or 0)]
    if len(seasons) != 1 or not isinstance(seasons[0].get('dungeons'), list):
        raise ValueError('The current Mythic+ season is unavailable. Check /mplus setup.')
    return seasons[0]


def rule_for(settings, group):
    rules = [r for r in settings['rules'].values() if r['mode'] == group['mode'] and r['from_level'] <= group['level']]
    if not rules:
        if group['mode'] == 'push':
            raise ValueError('An officer must configure a Push rule for this key level with /mplus rule.')
        return {'min_score': 0, 'min_role_score': 0, 'min_ilvl': 0, 'max_step': None, 'require_dungeon': False}
    return max(rules, key=lambda r: r['from_level'])


def linked(state, user_id, key):
    character = state['wow_characters'].get(str(user_id), {}).get('characters', {}).get(key)
    if character is None:
        raise ValueError('Select your linked character. Use /character link and /character add first.')
    return character


def check_profile(profile, character, group, rule, role):
    if role not in CAPACITY:
        raise ValueError('Choose tank, healer, or dps.')
    if character['region'] != group['region']:
        raise ValueError('Your character must be in the same region as the group.')
    class_name = profile.get('class') or CLASS_IDS.get(character.get('class_id'))
    if role not in CLASSES.get(class_name, ()):
        raise ValueError('This class cannot fill that role, or its class could not be verified.')
    reasons = []
    now = time.time()
    if not profile:
        return ['API unavailable: leader review required.']
    if (str(profile.get('name', '')).casefold() != character['name'].casefold() or
            str(profile.get('realm', '')).lower().replace(' ', '-') != character['realm'].lower().replace(' ', '-')):
        raise ValueError('The profile identity changed. Verify your character again with /character link.')
    seasons = profile.get('mythic_plus_scores_by_season') or []
    season = next((s for s in seasons if s.get('season') == group['season']), None)
    if season is None:
        reasons.append('Current-season score is unavailable.')
    for field, minimum in (('all', rule['min_score']), (role, rule['min_role_score'])):
        score = season.get('scores', {}).get(field) if season else None
        if not wow.number(score) or score < minimum:
            reasons.append(f'{field.title()} score requires {minimum:g}; verified: {score if wow.number(score) else "unknown"}.')
    gear = profile.get('gear') or {}
    ilvl = gear.get('item_level_equipped')
    if not wow.number(ilvl) or ilvl < rule['min_ilvl']:
        reasons.append(f'Equipped ilvl requires {rule["min_ilvl"]:g}; verified: {ilvl if wow.number(ilvl) else "unknown"}.')
    for label, date in (('Profile', profile.get('last_crawled_at')), ('Gear', gear.get('updated_at'))):
        ts = timestamp(date)
        if ts is None or not -300 <= now - ts <= 86400:
            reasons.append(f'{label} source is older than 24 hours or its date is unknown.')
    if rule['max_step'] is not None:
        needed = max(2, group['level'] - rule['max_step'])
        runs = []
        for field in ('mythic_plus_best_runs', 'mythic_plus_recent_runs', 'mythic_plus_highest_level_runs'):
            runs.extend(profile.get(field) or [])
        timed = [r for r in runs if r.get('role') == role and wow.number(r.get('mythic_level')) and
                 wow.number(r.get('clear_time_ms')) and wow.number(r.get('par_time_ms')) and
                 r['par_time_ms'] > 0 and r['clear_time_ms'] <= r['par_time_ms']]
        if not season or not any(r['mythic_level'] >= needed for r in timed):
            reasons.append(f'Insufficient verified data: timed +{needed} on {role} required.')
        if rule['require_dungeon'] and (not season or not any(
                r.get('map_challenge_mode_id') == group['dungeon_id'] and r['mythic_level'] >= needed for r in timed)):
            reasons.append(f'Insufficient verified data: timed +{needed} in this dungeon on {role} required.')
    return reasons


def seat_available(group, role):
    return sum(e['role'] == role and e['status'] in ('joined', 'offered') for e in group['players'].values()) < CAPACITY[role]


def promote(group):
    offered = []
    if group['status'] != 'open':
        return offered
    for uid, entry in group['players'].items():
        if entry['status'] == 'waiting' and seat_available(group, entry['role']):
            entry['status'], entry['offer_expires'] = 'offered', time.time() + 300
            entry['offer_notified'] = False
            offered.append(int(uid))
    return offered


def open_group(state, group_id):
    group = state['mplus_groups'].get(str(group_id))
    if group is None or group['status'] != 'open':
        raise ValueError('This group is closed or does not exist on this server.')
    return group


def require_leader(group, user):
    if group['leader_id'] != user.id and not user.guild_permissions.manage_guild:
        raise ValueError('Only the group leader or a member with Manage Server can do that.')


def unlink_entries(state, uid, key=None):
    for group in state['mplus_groups'].values():
        entry = group['players'].get(str(uid))
        if group['status'] == 'open' and entry and (key is None or entry['key'] == key):
            del group['players'][str(uid)]
            if group['leader_id'] == uid:
                group['status'] = 'cancelled'
            group['revision'] += 1
            group['dirty'] = True


class MPlus:
    def __init__(self, bot):
        self.bot = bot
        self.publish_locks = {}

    async def pool(self, settings, region):
        if settings is None:
            raise ValueError('An officer must configure /mplus setup first.')
        data = await self.bot.wow.get('mythic-plus/static-data', {'expansion_id': settings['expansion_id']}, ttl=300)
        return current_season(data, region)

    async def member(self, guild, uid, state):
        member = await guild.fetch_member(uid)
        if member.bot:
            raise ValueError('Bots cannot join Mythic+ groups.')
        role_id = state['role_id']
        if role_id and not member.guild_permissions.manage_guild and not any(r.id == role_id for r in member.roles):
            raise ValueError('You need the configured player role to join Mythic+ groups.')
        return member

    async def profile(self, character, fresh=False):
        try:
            return await self.bot.wow.get('characters/profile', {
                'region': character['region'], 'realm': character['realm'], 'name': character['name'],
                'fields': 'gear,mythic_plus_scores_by_season:current,mythic_plus_best_runs:all,mythic_plus_recent_runs,mythic_plus_highest_level_runs',
            }, ttl=0 if fresh else 300)
        except wow.APIError:
            return {}

    async def evidence(self, state, group, uid, key, role, fresh=False):
        char = linked(state, uid, key)
        if not wow.number(char.get('verified_at')) or char['verified_at'] > time.time():
            raise ValueError('Invalid character verification date.')
        settings = state['mplus_settings']
        season = await self.pool(settings, group['region'])
        if season['slug'] != group['season']:
            raise ValueError('The season changed. Create a new Mythic+ group.')
        profile = await self.profile(char, fresh)
        rule = rule_for(settings, group)
        reasons = check_profile(profile, char, group, rule, role)
        return {'key': key, 'character': char, 'role': role, 'reasons': reasons, 'approved': False,
                'checked_at': time.time(), 'rule': rule, 'status': 'pending' if reasons else 'joined'}

    async def create(self, guild, uid, key, role, dungeon, level, mode, start):
        if type(level) is not int or not 2 <= level <= 99 or mode not in ('learning', 'push') or role not in CAPACITY:
            raise ValueError('Choose a valid key level, mode, and role.')
        start_at = timestamp(start)
        if start_at is None or not time.time() <= start_at <= time.time() + 30 * 86400:
            raise ValueError('Use an ISO time with timezone, within the next 30 days (e.g. 2026-10-25T20:00+02:00).')
        state = await self.bot.store.read(guild.id)
        await self.member(guild, uid, state)
        char = linked(state, uid, key)
        settings = state['mplus_settings']
        season = await self.pool(settings, char['region'])
        selected = next((d for d in season['dungeons'] if str(d['challenge_mode_id']) == dungeon), None)
        if selected is None:
            raise ValueError('Choose a dungeon from the current season using autocomplete.')
        group = {'leader_id': uid, 'region': char['region'], 'season': season['slug'], 'dungeon': selected['name'],
                 'dungeon_id': selected['challenge_mode_id'], 'level': level, 'mode': mode, 'start': start_at,
                 'status': 'open', 'players': {}, 'channel_id': settings['channel_id'], 'message_id': None,
                 'revision': 0, 'created': time.time(), 'dirty': True}
        entry = await self.evidence(state, group, uid, key, role)
        if entry['reasons']:
            raise ValueError('The leader must meet the group requirements: ' + ' '.join(entry['reasons']))
        group['players'][str(uid)] = entry
        async with self.bot.store.edit(guild.id) as data:
            self.check_settings(data, state)
            if linked(data, uid, key) != char:
                raise ValueError('Character links changed. Try again.')
            if sum(g['status'] == 'open' and g['leader_id'] == uid for g in data['mplus_groups'].values()) >= 3:
                raise ValueError('Close one of your three open groups before creating another.')
            group_id = str(data['mplus_next'])
            data['mplus_next'] += 1
            data['mplus_groups'][group_id] = group
            for old in list(data['mplus_groups']):
                previous = data['mplus_groups'][old]
                if previous['status'] != 'open' and not previous.get('dirty') and time.time() - previous['created'] > 7 * 86400:
                    del data['mplus_groups'][old]
        await self.publish(guild, group_id)
        return group_id

    def check_settings(self, latest, before):
        if latest['mplus_settings'] != before['mplus_settings'] or latest['role_id'] != before['role_id']:
            raise ValueError('Guild requirements changed during the check. Please try again.')

    async def join(self, guild, uid, group_id, key, role):
        state = await self.bot.store.read(guild.id)
        group = open_group(state, group_id)
        await self.member(guild, uid, state)
        entry = await self.evidence(state, group, uid, key, role)
        async with self.bot.store.edit(guild.id) as data:
            self.check_settings(data, state)
            group = open_group(data, group_id)
            if linked(data, uid, key) != entry['character']:
                raise ValueError('Character links changed. Try again.')
            existing = group['players'].get(str(uid))
            if existing:
                if existing['status'] != 'offered' or existing['key'] != key or existing['role'] != role:
                    raise ValueError('You already have a seat, request, or queue entry. Leave it before changing character or role.')
                if existing['offer_expires'] <= time.time():
                    raise ValueError('Your seat offer expired. Leave and join the queue again.')
                entry['approved'] = existing['approved'] and existing['rule'] == entry['rule']
                del group['players'][str(uid)]
            if entry['approved']:
                entry['status'] = 'joined'
            if entry['status'] == 'joined' and not seat_available(group, role):
                entry['status'] = 'waiting'
            if len(group['players']) >= 100:
                raise ValueError('This group already has 100 participants and requests.')
            group['players'][str(uid)] = entry
            group['revision'] += 1
            group['dirty'] = True
            result = entry['status']
        await self.publish(guild, str(group_id))
        return {'pending': 'Request sent to the leader: ' + ' '.join(entry['reasons']),
                'waiting': 'Your role is full. You are in its queue.', 'joined': 'You joined the group.'}[result]

    async def change(self, guild, user, group_id, action, target=None):
        state = await self.bot.store.read(guild.id)
        user = await guild.fetch_member(user.id)
        group = open_group(state, group_id)
        if action == 'approve':
            require_leader(group, user)
            entry = group['players'].get(str(target))
            if not entry or entry['status'] != 'pending':
                raise ValueError('That member has no pending request.')
            await self.member(guild, target, state)
            checked = await self.evidence(state, group, target, entry['key'], entry['role'])
        async with self.bot.store.edit(guild.id) as data:
            group = open_group(data, group_id)
            if action != 'leave':
                require_leader(group, user)
            if action == 'cancel' or action == 'leave' and user.id == group['leader_id']:
                group['status'] = 'cancelled'
            elif action in ('leave', 'kick'):
                uid = str(user.id if action == 'leave' else target)
                if uid not in group['players']:
                    raise ValueError('That member has no entry in this group.')
                if int(uid) == group['leader_id']:
                    raise ValueError('Transfer leadership or cancel the group instead.')
                del group['players'][uid]
                promote(group)
            elif action == 'approve':
                self.check_settings(data, state)
                latest = group['players'].get(str(target))
                if latest != entry or linked(data, target, entry['key']) != checked['character']:
                    raise ValueError('The request changed. Please try again.')
                checked['approved'] = True
                checked['status'] = 'joined' if seat_available(group, checked['role']) else 'waiting'
                group['players'][str(target)] = checked
            elif action == 'transfer':
                if str(target) not in group['players'] or group['players'][str(target)]['status'] != 'joined':
                    raise ValueError('Leadership can only be transferred to a joined member.')
                group['leader_id'] = target
            else:
                raise ValueError('Unknown group action.')
            group['revision'] += 1
            group['dirty'] = True
        await self.publish(guild, str(group_id))

    async def start(self, guild, user, group_id):
        state = await self.bot.store.read(guild.id)
        group = open_group(state, group_id)
        user = await self.member(guild, user.id, state)
        require_leader(group, user)
        season = await self.pool(state['mplus_settings'], group['region'])
        if season['slug'] != group['season']:
            raise ValueError('The season changed. Create a new group.')
        joined = {uid: e for uid, e in group['players'].items() if e['status'] == 'joined'}
        if len(joined) != 5 or any(sum(e['role'] == role for e in joined.values()) != count for role, count in CAPACITY.items()):
            raise ValueError('Starting requires one tank, one healer, and three DPS with confirmed seats.')
        checked = {}
        for uid, entry in joined.items():
            try:
                await self.member(guild, int(uid), state)
                checked[uid] = await self.evidence(state, group, int(uid), entry['key'], entry['role'], fresh=True)
            except (ValueError, discord.NotFound):
                checked[uid] = None
        failures = []
        async with self.bot.store.edit(guild.id) as data:
            self.check_settings(data, state)
            latest = open_group(data, group_id)
            if latest['revision'] != group['revision']:
                raise ValueError('The group changed during the check. Please try starting again.')
            for uid, previous in joined.items():
                fresh = checked[uid]
                try:
                    still_linked = linked(data, int(uid), previous['key']) == previous['character']
                except ValueError:
                    still_linked = False
                if not still_linked or fresh is None:
                    del latest['players'][uid]
                    failures.append(uid)
                elif fresh['reasons'] and not (previous['approved'] and previous['rule'] == fresh['rule']):
                    fresh['status'] = 'pending'
                    latest['players'][uid] = fresh
                    failures.append(uid)
            latest['revision'] += 1
            latest['dirty'] = True
            if not failures:
                latest['status'] = 'started'
            promote(latest)
        await self.publish(guild, str(group_id))
        if failures:
            raise ValueError('Some participants need review or lost access. The group remains open; check the card.')

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
                kwargs = {'embed': group_embed(group_id, group, rule_for(state['mplus_settings'], group)),
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
                offers = [int(uid) for uid, e in group['players'].items() if e['status'] == 'offered' and
                          not e.get('offer_notified') and e['offer_expires'] > time.time()]
                if offers:
                    await channel.send(' '.join(f'<@{uid}>' for uid in offers) +
                        f' — a seat is available in group #{group_id}. Confirm within 5 minutes using Confirm seat.',
                        allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=uid) for uid in offers], roles=False, everyone=False))
                    async with self.bot.store.edit(guild.id) as data:
                        for uid in offers:
                            latest = data['mplus_groups'][str(group_id)]['players'].get(str(uid))
                            if latest and latest.get('offer_expires') == group['players'][str(uid)].get('offer_expires'):
                                latest['offer_notified'] = True
            except (discord.HTTPException, ValueError):
                log.exception('Could not publish Mythic+ group %s in guild %s', group_id, guild.id)
                async with self.bot.store.edit(guild.id) as data:
                    data['mplus_groups'][str(group_id)]['publish_error'] = 'Could not update the group card. Check channel permissions.'
                    data['mplus_groups'][str(group_id)]['dirty'] = True

    async def maintenance(self, guild):
        changed = []
        state = await self.bot.store.read(guild.id)
        if not state['mplus_groups']:
            return
        async with self.bot.store.edit(guild.id) as data:
            for gid, group in data['mplus_groups'].items():
                offers = []
                if group['status'] == 'open':
                    for uid in list(group['players']):
                        entry = group['players'][uid]
                        if entry['status'] == 'offered' and entry['offer_expires'] <= time.time():
                            del group['players'][uid]
                            group['revision'] += 1
                            group['dirty'] = True
                    if time.time() > group['start'] + 3600:
                        group['status'] = 'expired'
                        group['revision'] += 1
                        group['dirty'] = True
                    offers = promote(group)
                    if offers:
                        group['revision'] += 1
                        group['dirty'] = True
                if group.get('dirty'):
                    changed.append(gid)
        for gid in changed:
            await self.publish(guild, gid)


def group_embed(group_id, group, rule):
    embed = discord.Embed(title=f"M+ #{group_id} • +{group['level']} {group['dungeon']}"[:256],
        description=f"{group['mode'].title()} • {group['status'].title()} • {group['region'].upper()}\n"
                    f"Start: <t:{int(group['start'])}:F>\nSeason: {wow.clean(group['season'])}", color=0x5865F2)
    embed.add_field(name='Requirements', value=f"Score ≥ {rule['min_score']:g} • Role score ≥ {rule['min_role_score']:g} • ilvl ≥ {rule['min_ilvl']:g}\n"
                    f"Timed key step: {rule['max_step'] if rule['max_step'] is not None else 'not required'} • Dungeon experience: {rule['require_dungeon']}", inline=False)
    for role, size in CAPACITY.items():
        players = [(uid, e) for uid, e in group['players'].items() if e['role'] == role and e['status'] in ('joined', 'offered')]
        lines = []
        for uid, entry in players:
            line = f"{wow.clean(entry['character']['name'])} / {wow.clean(entry['character']['realm'])}"
            if int(uid) == group['leader_id']:
                line += ' ★ leader'
            if entry['approved']:
                line += ' • leader approved'
            if entry['status'] == 'offered':
                line += f" • confirm <t:{int(entry['offer_expires'])}:R>"
            lines.append(line)
        embed.add_field(name=f'{role.title()} ({len(players)}/{size})', value=('\n'.join(lines) or 'Open')[:1024], inline=False)
    for status, label in (('pending', 'Leader review'), ('waiting', 'Role queues')):
        rows = [(uid, e) for uid, e in group['players'].items() if e['status'] == status]
        text = '\n'.join(f"{uid}: {wow.clean(e['character']['name'])} ({e['role']})" for uid, e in rows[:10])
        if rows:
            embed.add_field(name=f'{label} ({len(rows)})', value=text[:1024], inline=False)
    if group.get('publish_error'):
        embed.add_field(name='Publication error', value=group['publish_error'], inline=False)
    embed.set_footer(text='Raider.IO • Manual approvals override score/experience/freshness only • /mplus show for requests')
    embed.url = 'https://raider.io'
    return embed


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
        state = await interaction.client.store.read(interaction.guild_id)
        roster = state['wow_characters'].get(str(interaction.user.id), {})
        if not roster.get('characters'):
            raise ValueError('Link a character with /character link and /character add first.')
        await interaction.response.send_message('Choose a character and role, then submit. For more characters use /mplus join.',
            view=JoinView(gid, interaction.user.id, roster), ephemeral=True)

    @discord.ui.button(label='Confirm seat', custom_id='mplus:confirm')
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        gid = await self.group_id(interaction)
        state = await interaction.client.store.read(interaction.guild_id)
        entry = open_group(state, gid)['players'].get(str(interaction.user.id))
        if not entry or entry['status'] != 'offered':
            raise ValueError('You do not have a seat offer in this group.')
        text = await interaction.client.mplus.join(interaction.guild, interaction.user.id, gid, entry['key'], entry['role'])
        await interaction.followup.send(text, ephemeral=True)

    async def action(self, interaction, action):
        await interaction.response.defer(ephemeral=True)
        gid = await self.group_id(interaction)
        service = interaction.client.mplus
        if action == 'start':
            await service.start(interaction.guild, interaction.user, gid)
        else:
            await service.change(interaction.guild, interaction.user, gid, action)
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
        text = str(error) if isinstance(error, ValueError) else 'Could not update the group. Try again later.'
        if not isinstance(error, ValueError):
            log.error('Mythic+ button error', exc_info=(type(error), error, error.__traceback__))
        sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await sender(text[:1900], ephemeral=True)


class JoinView(GroupView):
    def __init__(self, group_id, uid, roster):
        discord.ui.View.__init__(self, timeout=120)
        self.clear_items()
        self.group_id_value, self.uid = group_id, uid
        characters = roster['characters']
        keys = sorted(characters, key=lambda k: k != roster['main'])[:25]
        self.character = discord.ui.Select(placeholder='Character', options=[discord.SelectOption(
            label=f"{characters[k]['name']} / {characters[k]['realm']}"[:100], value=k) for k in keys])
        self.role = discord.ui.Select(placeholder='Role', options=[discord.SelectOption(label=r.title(), value=r) for r in CAPACITY])
        self.character.callback = self.selected
        self.role.callback = self.selected
        self.add_item(self.character)
        self.add_item(self.role)
        submit = discord.ui.Button(label='Submit', style=discord.ButtonStyle.success)
        submit.callback = self.submit
        self.add_item(submit)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message('This selection belongs to another member.', ephemeral=True)
            return False
        return True

    async def selected(self, interaction):
        await interaction.response.defer()

    async def submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        if not self.character.values or not self.role.values:
            raise ValueError('Select both a character and a role.')
        text = await interaction.client.mplus.join(interaction.guild, interaction.user.id,
            self.group_id_value, self.character.values[0], self.role.values[0])
        await interaction.followup.send(text, ephemeral=True)
        self.stop()


def register_commands(bot):
    commands = app_commands.Group(name='mplus', description='Mythic+ groups', guild_only=True)
    roles = [app_commands.Choice(name=r.title(), value=r) for r in CAPACITY]
    modes = [app_commands.Choice(name=m.title(), value=m) for m in ('learning', 'push')]

    @commands.command(name='setup', description='Set the Mythic+ channel and expansion ID')
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(interaction: discord.Interaction, channel: discord.TextChannel,
                    expansion_id: app_commands.Range[int, 6, 99]):
        await interaction.response.defer(ephemeral=True)
        permissions = channel.permissions_for(interaction.guild.me)
        if not (permissions.view_channel and permissions.send_messages and permissions.read_message_history and permissions.embed_links):
            raise ValueError('The bot needs View Channel, Send Messages, Read Message History, and Embed Links.')
        data = await bot.wow.get('mythic-plus/static-data', {'expansion_id': expansion_id}, ttl=300)
        if not isinstance(data.get('seasons'), list) or not data['seasons']:
            raise ValueError('No Mythic+ seasons found for that expansion.')
        async with bot.store.edit(interaction.guild_id) as state:
            previous = state['mplus_settings'] or {'rules': {}}
            state['mplus_settings'] = {'channel_id': channel.id, 'expansion_id': expansion_id, 'rules': previous['rules']}
        await interaction.followup.send('Mythic+ configured. Configure Push level bands with /mplus rule before creating Push groups.', ephemeral=True)

    @commands.command(name='rule', description='Set minimum requirements for a mode and key-level band')
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(mode=modes)
    async def rule(interaction: discord.Interaction, mode: str, from_level: app_commands.Range[int, 2, 99],
                   min_score: app_commands.Range[int, 0, 10000], min_role_score: app_commands.Range[int, 0, 10000],
                   min_ilvl: app_commands.Range[int, 0, 10000], max_step: app_commands.Range[int, 0, 99] = 2,
                   require_dungeon: bool = False, require_experience: bool = True):
        if mode not in ('learning', 'push'):
            raise ValueError('Choose Learning or Push.')
        if require_dungeon and not require_experience:
            raise ValueError('Dungeon experience requires require_experience:true.')
        async with bot.store.edit(interaction.guild_id) as state:
            settings = state['mplus_settings']
            if settings is None:
                raise ValueError('Configure /mplus setup first.')
            settings['rules'][f'{mode}:{from_level}'] = {
                'mode': mode, 'from_level': from_level, 'min_score': min_score,
                'min_role_score': min_role_score, 'min_ilvl': min_ilvl,
                'max_step': max_step if require_experience else None, 'require_dungeon': require_dungeon}
            for group in state['mplus_groups'].values():
                if group['status'] == 'open' and group['mode'] == mode and group['level'] >= from_level:
                    group['dirty'] = True
                    group['revision'] += 1
        await interaction.response.send_message('Requirements saved. They apply to new joins and start checks in existing open groups.', ephemeral=True)

    @commands.command(name='rules', description='Show configured key-level requirements')
    async def rules(interaction: discord.Interaction):
        state = await bot.store.read(interaction.guild_id)
        settings = state['mplus_settings']
        if settings is None:
            raise ValueError('Configure /mplus setup first.')
        lines = ['The highest matching level band applies. Learning defaults to no score/experience minimums; Push requires a configured rule.']
        for r in sorted(settings['rules'].values(), key=lambda r: (r['mode'], r['from_level'])):
            lines.append(f"{r['mode']} +{r['from_level']}+: score {r['min_score']}, role {r['min_role_score']}, "
                         f"ilvl {r['min_ilvl']}, step {r['max_step']}, dungeon {r['require_dungeon']}")
        await interaction.response.send_message('\n'.join(lines)[:1900], ephemeral=True)

    @commands.command(name='create', description='Create a Mythic+ group and take your seat')
    @app_commands.choices(role=roles, mode=modes)
    async def create(interaction: discord.Interaction, dungeon: str, level: app_commands.Range[int, 2, 99],
                     character: str, role: str, start: str, mode: str = 'push'):
        await interaction.response.defer(ephemeral=True)
        gid = await bot.mplus.create(interaction.guild, interaction.user.id, character, role, dungeon, level, mode, start)
        await interaction.followup.send(f'Group #{gid} created. Use /mplus show if the card is unavailable.', ephemeral=True)

    @commands.command(name='join', description='Join with your linked character, queue, or request leader approval')
    @app_commands.choices(role=roles)
    async def join(interaction: discord.Interaction, group: str, character: str, role: str):
        await interaction.response.defer(ephemeral=True)
        text = await bot.mplus.join(interaction.guild, interaction.user.id, group, character, role)
        await interaction.followup.send(text, ephemeral=True)

    @create.autocomplete('character')
    @join.autocomplete('character')
    async def character_choices(interaction: discord.Interaction, current: str):
        state = await bot.store.read(interaction.guild_id)
        chars = state['wow_characters'].get(str(interaction.user.id), {}).get('characters', {})
        return [app_commands.Choice(name=f"{c['name']} / {c['realm']} ({c['region']})"[:100], value=k)
                for k, c in chars.items() if current.casefold() in f"{c['name']} {c['realm']}".casefold()][:25]

    @create.autocomplete('dungeon')
    async def dungeon_choices(interaction: discord.Interaction, current: str):
        state = await bot.store.read(interaction.guild_id)
        chars = state['wow_characters'].get(str(interaction.user.id), {})
        char = chars.get('characters', {}).get(getattr(interaction.namespace, 'character', None) or chars.get('main'))
        region = char['region'] if char else (state['dashboard'] or {}).get('region', 'eu')
        try:
            season = await asyncio.wait_for(bot.mplus.pool(state['mplus_settings'], region), timeout=2)
            return [app_commands.Choice(name=d['name'][:100], value=str(d['challenge_mode_id']))
                    for d in season['dungeons'] if current.casefold() in d['name'].casefold()][:25]
        except (ValueError, asyncio.TimeoutError):
            return []

    @commands.command(name='show', description='Show a group, its requests, and any publication error')
    async def show(interaction: discord.Interaction, group: str):
        state = await bot.store.read(interaction.guild_id)
        selected = state['mplus_groups'].get(group)
        if selected is None:
            raise ValueError('That group does not exist on this server.')
        embed = group_embed(group, selected, rule_for(state['mplus_settings'], selected))
        text = ''
        if interaction.user.id == selected['leader_id'] or interaction.user.guild_permissions.manage_guild:
            text = '\n'.join(f"{uid}: {' '.join(e['reasons'])}" for uid, e in selected['players'].items() if e['status'] == 'pending')
        await interaction.response.send_message(content=text[:1900] or None, embed=embed, ephemeral=True)

    @commands.command(name='approve', description='Approve a specific request as an explicit exception')
    async def approve(interaction: discord.Interaction, group: str, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        await bot.mplus.change(interaction.guild, interaction.user, group, 'approve', member.id)
        await interaction.followup.send('Request explicitly approved. The exception is marked on the group card.', ephemeral=True)

    @commands.command(name='kick', description='Remove a participant, queue entry, or pending request')
    async def kick(interaction: discord.Interaction, group: str, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        await bot.mplus.change(interaction.guild, interaction.user, group, 'kick', member.id)
        await interaction.followup.send('Entry removed.', ephemeral=True)

    @commands.command(name='transfer', description='Transfer leadership to a confirmed participant')
    async def transfer(interaction: discord.Interaction, group: str, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        await bot.mplus.change(interaction.guild, interaction.user, group, 'transfer', member.id)
        await interaction.followup.send('Leadership transferred.', ephemeral=True)

    @commands.command(name='leave', description='Leave a group; leaving as leader cancels it')
    async def leave(interaction: discord.Interaction, group: str):
        await interaction.response.defer(ephemeral=True)
        await bot.mplus.change(interaction.guild, interaction.user, group, 'leave')
        await interaction.followup.send('You left the group.', ephemeral=True)

    @commands.command(name='start', description='Recheck the confirmed five-player group and close signups')
    async def start_group(interaction: discord.Interaction, group: str):
        await interaction.response.defer(ephemeral=True)
        await bot.mplus.start(interaction.guild, interaction.user, group)
        await interaction.followup.send('Group started. Signups are closed.', ephemeral=True)

    @commands.command(name='cancel', description='Cancel the group and close signups')
    async def cancel(interaction: discord.Interaction, group: str):
        await interaction.response.defer(ephemeral=True)
        await bot.mplus.change(interaction.guild, interaction.user, group, 'cancel')
        await interaction.followup.send('Group cancelled.', ephemeral=True)

    @join.autocomplete('group')
    @show.autocomplete('group')
    @approve.autocomplete('group')
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
