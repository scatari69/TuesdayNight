import asyncio
import math
import time
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp
import discord

REFRESH_SECONDS = 1800
# shortcut: Midnight; обновить ID при выходе следующего дополнения.
EXPANSION_ID = 11


class APIError(ValueError):
    pass


class RaiderIO:
    def __init__(self):
        self.session = None
        self.lock = asyncio.Lock()
        self.next_request = 0
        self.retry_at = 0
        self.cache = {}

    async def close(self):
        if self.session is not None:
            await self.session.close()

    async def get(self, endpoint, params, ttl=REFRESH_SECONDS):
        key = (endpoint, tuple(sorted(params.items())))
        async with self.lock:
            now = time.time()
            cached = self.cache.get(key)
            if cached and now - cached[0] < ttl:
                return cached[1]
            if now < self.retry_at:
                raise APIError('Raider.IO is temporarily unavailable; automatic retry is scheduled.')
            await asyncio.sleep(max(0, self.next_request - now))
            self.next_request = time.time() + 1
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
            try:
                async with self.session.get('https://raider.io/api/v1/' + endpoint, params=params) as response:
                    if response.status == 429:
                        try:
                            delay = float(response.headers.get('Retry-After', '60'))
                            if not math.isfinite(delay):
                                delay = 60
                        except ValueError:
                            delay = 60
                        self.retry_at = time.time() + max(1, delay)
                        raise APIError('Raider.IO rate limit reached; automatic retry is scheduled.')
                    if response.status >= 500:
                        self.retry_at = time.time() + 60
                    if response.status != 200:
                        raise APIError(f'Raider.IO request failed (HTTP {response.status}). Check the guild/realm names.')
                    try:
                        data = await response.json()
                    except ValueError as error:
                        raise APIError('Raider.IO returned invalid JSON.') from error
                    if not isinstance(data, dict):
                        raise APIError('Raider.IO returned an invalid response.')
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                self.retry_at = time.time() + 60
                raise APIError('Could not reach Raider.IO; automatic retry is scheduled.') from error
            # Кэш временный; состав гильдии не записываем в state.
            self.cache = {k: v for k, v in self.cache.items() if time.time() - v[0] < 3600}
            self.cache[key] = (time.time(), data)
            return data

    async def guild(self, config):
        metadata = await self.get('raiding/static-data', {'expansion_id': EXPANSION_ID}, ttl=3600)
        try:
            now = datetime.now(timezone.utc)
            raids = {raid['slug']: raid['name'] for raid in metadata['raids']
                     if datetime.fromisoformat(raid['starts'][config['region']].replace('Z', '+00:00')) <= now
                     < datetime.fromisoformat(raid['ends'][config['region']].replace('Z', '+00:00'))}
        except (KeyError, TypeError, ValueError) as error:
            raise APIError('Raider.IO returned invalid raid dates.') from error
        if not raids:
            raise APIError('Raider.IO did not return any active raids.')
        seasons = await self.get('mythic-plus/static-data', {'expansion_id': EXPANSION_ID}, ttl=3600)
        try:
            active = [s for s in seasons['seasons'] if s['is_main_season']
                      and datetime.fromisoformat(s['starts'][config['region']].replace('Z', '+00:00')) <= now
                      < datetime.fromisoformat(s['ends'][config['region']].replace('Z', '+00:00'))]
            gear_cutoff = max(datetime.fromisoformat(s['starts'][config['region']].replace('Z', '+00:00'))
                              for s in active).timestamp()
        except (KeyError, TypeError, ValueError) as error:
            raise APIError('Raider.IO did not return valid current season dates.') from error
        guild = await self.get('guilds/profile', {
            'region': config['region'], 'realm': config['realm'], 'name': config['name'],
            'fields': 'members,raid_progression:current-expansion,raid_rankings:current-expansion',
        }, ttl=3600)
        if not isinstance(guild.get('raid_progression'), dict):
            raise APIError('Raider.IO did not return raid data.')
        return {**guild, 'raid_names': raids, 'gear_cutoff': gear_cutoff,
                'raid_progression': {slug: value for slug, value in guild.get('raid_progression', {}).items()
                                     if slug in raids}}

    async def snapshot(self, config):
        guild = await self.guild(config)
        members = guild.get('members')
        if not isinstance(members, list):
            raise APIError('Raider.IO did not return the guild roster.')
        profiles = []
        seen = set()
        for member in members:
            try:
                char = member['character']
                identity = (char['realm'].casefold(), char['name'].casefold())
                if identity in seen:
                    continue
                seen.add(identity)
                profile = await self.get('characters/profile', {
                    'region': config['region'], 'realm': char['realm'], 'name': char['name'],
                    'fields': 'gear,mythic_plus_scores_by_season:current',
                })
                profiles.append(profile)
            except (APIError, KeyError, TypeError):
                if self.retry_at > time.time():
                    raise APIError('Raider.IO paused requests; keeping the previous dashboard.')
        return build_snapshot(config, guild, profiles, len(members))


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def profile_url(region, realm, name, kind='characters'):
    return f'https://raider.io/{kind}/{region}/{quote(realm.lower().replace(" ", "-"), safe="")}/{quote(name, safe="")}'


def build_snapshot(config, guild, profiles, total):
    rows = []
    seasons = set()
    for p in profiles:
        try:
            row = {'name': str(p['name'])[:40], 'realm': str(p['realm'])[:60],
                   'class': str(p.get('class', 'Unknown'))[:30],
                   'crawled': p.get('last_crawled_at'),
                   'gear_updated': (p.get('gear') or {}).get('updated_at'),
                   'ilvl': (p.get('gear') or {}).get('item_level_equipped'), 'score': None}
            # Дата обхода профиля не подтверждает обновление экипировки после сквиша.
            try:
                gear_date = datetime.fromisoformat(row['gear_updated'].replace('Z', '+00:00'))
                if not gear_date.tzinfo or not guild['gear_cutoff'] <= gear_date.timestamp() <= time.time():
                    row['ilvl'] = None
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                row['ilvl'] = None
            scores = p.get('mythic_plus_scores_by_season') or []
            if scores and isinstance(scores[0].get('season'), str):
                row['season'] = scores[0]['season']
                row['score'] = scores[0].get('scores', {}).get('all')
                seasons.add(row['season'])
            rows.append(row)
        except (KeyError, TypeError, AttributeError):
            continue
    # При смене сезона смешанный кэш не выдаём за актуальный рейтинг.
    season = next(iter(seasons)) if len(seasons) == 1 else None
    ilvl = sorted((r for r in rows if number(r['ilvl'])), key=lambda r: (-r['ilvl'], r['name']))
    scores = sorted((r for r in rows if season and number(r['score'])), key=lambda r: (-r['score'], r['name']))
    progress = guild.get('raid_progression')
    rankings = guild.get('raid_rankings')
    if not isinstance(progress, dict) or not isinstance(rankings, dict):
        raise APIError('Raider.IO did not return raid data.')
    if (any(not isinstance(value, dict) for value in progress.values()) or
            any(not isinstance(value, dict) or any(not isinstance(rank, dict) for rank in value.values())
                for value in rankings.values())):
        raise APIError('Raider.IO returned invalid raid data.')
    return {'updated': time.time(), 'total': total, 'loaded': len(rows),
            'ilvl_count': len(ilvl), 'score_count': len(scores), 'season': season,
            'ilvl': ilvl[:10], 'scores': scores[:10],
            'progress': progress, 'rankings': rankings,
            'raid_names': guild.get('raid_names', {}),
            'guild_crawled': guild.get('last_crawled_at')}


def clean(text):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(text)))


def dashboard_embeds(config, snapshot, error=None):
    url = profile_url(config['region'], config['realm'], config['name'], 'guilds')
    main = discord.Embed(title=clean(config['name'])[:256], url=url, color=0x5865F2,
                         description=f"{clean(config['region'].upper())} / {clean(config['realm'])}\n[Raider.IO]({url})")
    if error:
        main.add_field(name='Stale / update failed', value=clean(error)[:1024], inline=False)
    if not snapshot:
        main.add_field(name='Data status', value='Waiting for the first successful update.', inline=False)
        return [main]
    main.timestamp = datetime.fromtimestamp(snapshot['updated'], timezone.utc)
    main.set_footer(text='Last scan • Source snapshots may be older • Guild: ' +
                         str(snapshot.get('guild_crawled') or 'Unknown')[:80])
    main.add_field(name='Data status', value=(
        f"Profiles: {snapshot['loaded']}/{snapshot['total']} • "
        f"ilvl: {snapshot['ilvl_count']} • M+: {snapshot['score_count']} • "
        f"{'Partial data' if min(snapshot['loaded'], snapshot['ilvl_count'], snapshot['score_count']) < snapshot['total'] else 'Complete scan'}"), inline=False)
    progress = snapshot['progress']
    lines = []
    for raid, p in sorted(progress.items()):
        counts = []
        for difficulty, suffix in (('heroic', 'H'), ('mythic', 'M')):
            killed, total = p.get(difficulty + '_bosses_killed'), p.get('total_bosses')
            counts.append(f'{killed}/{total}{suffix}' if number(killed) and number(total) else f'?{suffix}')
        name = snapshot.get('raid_names', {}).get(raid, raid.replace('-', ' ').title())
        lines.append(f"{clean(name)[:150]}: {' '.join(counts)}")
    main.add_field(name='Raid progression', value='\n'.join(lines)[:1024] or 'Unavailable', inline=False)
    season = snapshot['season'] or 'Season unavailable'
    if season.startswith('season-mn-') and season.removeprefix('season-mn-').isdigit():
        season = 'Midnight Season ' + season.removeprefix('season-mn-')
    for title, key, metric in [('Top item level', 'ilvl', 'ilvl'),
                                ('Top Mythic+ • ' + season, 'scores', 'score')]:
        lines = []
        for i, row in enumerate(snapshot[key], 1):
            link = profile_url(config['region'], row['realm'], row['name'])
            lines.append(f"{i}. [{clean(row['name'])}]({link}) **{row[metric]:g}**")
        dates = [r.get('gear_updated' if key == 'ilvl' else 'crawled') for r in snapshot[key]]
        known = [d for d in dates if isinstance(d, str)]
        source = ('Source: ' + min(known)[:10] if known else 'Source: Unknown')
        if len(known) < len(dates):
            source += ' • Some dates unknown'
        value = '\n'.join(lines) or 'No verified data.'
        if len(value) + len(source) + 1 > 1024:
            value = value[:1024 - len(source) - 40].rsplit('\n', 1)[0] + '\n… See Raider.IO for more.'
        main.add_field(name=clean(title)[:256], value=value + '\n' + source, inline=True)
    return [main]
