import asyncio
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit

import aiohttp
import discord
from aiohttp import web

REGIONS = ('eu', 'us', 'kr', 'tw')
SESSION_TTL = 600


class BattleNet:
    def __init__(self, bot):
        self.bot = bot
        self.client_id = os.getenv('BNET_CLIENT_ID', '').strip()
        self.client_secret = os.getenv('BNET_CLIENT_SECRET', '').strip()
        self.redirect_uri = os.getenv('BNET_REDIRECT_URI', '').strip()
        self.session = None
        self.runner = None
        self.pending = {}
        self.verified = {}
        self.attempts = {}

    @property
    def enabled(self):
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    async def start(self):
        if not any((self.client_id, self.client_secret, self.redirect_uri)):
            return
        url = urlsplit(self.redirect_uri)
        if (not self.enabled or url.scheme != 'https' or not url.hostname or
                url.username or url.password or url.query or url.fragment or url.path != '/oauth/battlenet/callback'):
            raise ValueError('Set BNET_CLIENT_ID, BNET_CLIENT_SECRET, and an HTTPS BNET_REDIRECT_URI ending in /oauth/battlenet/callback.')
        app = web.Application(client_max_size=16384)
        app.router.add_get('/oauth/battlenet/callback', self.callback, allow_head=False)
        # Callback URL содержит одноразовый code: access log выключен.
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=15)
        try:
            await self.runner.setup()
            await web.TCPSite(self.runner, os.getenv('BNET_BIND_HOST', '127.0.0.1'), 8080).start()
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
        if self.session is not None:
            await self.session.close()
            self.session = None
        self.pending.clear()
        self.verified.clear()
        self.attempts.clear()

    def prune(self):
        now = time.time()
        self.pending = {s: p for s, p in self.pending.items() if p['expires'] > now}
        self.verified = {k: v for k, v in self.verified.items() if v['expires'] > now}
        self.attempts = {k: v for k, v in self.attempts.items() if v['expires'] > now}

    def invalidate(self, guild_id, user_id):
        owner = (guild_id, user_id)
        self.pending = {s: p for s, p in self.pending.items() if p['owner'] != owner}
        self.verified.pop(owner, None)
        self.attempts.pop(owner, None)

    def link(self, guild_id, user_id, region):
        if not self.enabled or self.runner is None:
            raise ValueError('Battle.net linking is not configured. Ask an administrator to configure OAuth.')
        if region not in REGIONS:
            raise ValueError('Choose EU, US, KR, or TW.')
        self.prune()
        owner = (guild_id, user_id)
        if len(self.attempts.keys() | self.verified.keys()) >= 1000 and owner not in self.attempts and owner not in self.verified:
            raise ValueError('Too many linking requests. Please try again later.')
        self.invalidate(guild_id, user_id)
        state = secrets.token_urlsafe(32)
        expires = time.time() + SESSION_TTL
        self.attempts[owner] = {'state': state, 'expires': expires}
        self.pending[state] = {'owner': owner, 'region': region, 'expires': expires}
        return 'https://oauth.battle.net/authorize?' + urlencode({
            'client_id': self.client_id, 'redirect_uri': self.redirect_uri,
            'response_type': 'code', 'scope': 'wow.profile', 'state': state, 'region': region,
        })

    async def fetch_characters(self, code, region):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
        try:
            async with self.session.post('https://oauth.battle.net/token',
                    auth=aiohttp.BasicAuth(self.client_id, self.client_secret),
                    data={'grant_type': 'authorization_code', 'code': code, 'redirect_uri': self.redirect_uri},
                    allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError('Battle.net authorization failed. Run /character link again.')
                token = await response.json()
            access_token = token.get('access_token')
            scope = token.get('scope')
            if (not isinstance(access_token, str) or not access_token or
                    scope is not None and (not isinstance(scope, str) or 'wow.profile' not in scope.split())):
                raise ValueError('Battle.net did not grant wow.profile access. Run /character link again.')
            # OAuth допускает отсутствие scope в ответе; доступ подтверждает сам account API.
            async with self.session.get(f'https://{region}.api.blizzard.com/profile/user/wow',
                    headers={'Authorization': 'Bearer ' + access_token},
                    params={'namespace': 'profile-' + region, 'locale': 'en_US'},
                    allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError('Could not read your WoW characters. Check your region and try linking again.')
                data = await response.json()
            return parse_characters(data, region)
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, AttributeError) as error:
            raise ValueError('Could not verify your WoW characters. Please try linking again later.') from error
        # Токены используются только для этого запроса, не хранятся в state или кэше.

    async def callback(self, request):
        self.prune()
        pending = self.pending.pop(request.query.get('state', ''), None)
        if pending is None:
            return reply('Invalid or expired link. Return to Discord and run /character link again.', 400)
        owner = pending['owner']
        attempt = self.attempts.get(owner)
        if attempt is None:
            return reply('This link has expired. Run /character link again.', 400)
        if request.query.get('error') or not request.query.get('code'):
            self.attempts.pop(owner, None)
            return reply('Authorization was cancelled. Return to Discord and run /character link again.', 400)
        try:
            guild = self.bot.get_guild(owner[0])
            if guild is None:
                return reply('Discord server is unavailable. Please try again later.', 403)
            await guild.fetch_member(owner[1])
            characters = await self.fetch_characters(request.query['code'], pending['region'])
            if self.attempts.get(owner) != attempt or pending['expires'] <= time.time():
                return reply('This link has expired or was replaced. Run /character link again.', 400)
            if not characters:
                return reply('No Retail characters found in this region. Run /character link with the correct region.', 400)
            self.verified[owner] = {'characters': characters, 'expires': time.time() + SESSION_TTL}
            return reply('Ownership verified. Return to Discord and use /character add to choose a character within 10 minutes. Do not share your sign-in link.')
        except ValueError:
            return reply('Battle.net verification failed. Check your region and wow.profile consent, then run /character link again.', 502)
        except discord.HTTPException:
            return reply('Could not verify Discord server membership. Please try again later.', 403)

    def candidates(self, guild_id, user_id):
        self.prune()
        return self.verified.get((guild_id, user_id), {}).get('characters', {})

    async def add(self, guild_id, user_id, key):
        character = self.candidates(guild_id, user_id).get(key)
        if character is None:
            raise ValueError('Choose a verified character from autocomplete. Run /character link if your verification expired.')
        async with self.bot.store.edit(guild_id) as state:
            if self.candidates(guild_id, user_id).get(key) is not character:
                raise ValueError('Verification expired or was replaced. Run /character link again.')
            for owner, roster in state['wow_characters'].items():
                if owner != str(user_id) and key in roster['characters']:
                    raise ValueError('This character is already linked to another Discord member on this server.')
            roster = state['wow_characters'].setdefault(str(user_id), {'characters': {}, 'main': None})
            roster['characters'][key] = {**character, 'verified_at': time.time()}
            if roster['main'] is None:
                roster['main'] = key
        return character


def parse_characters(data, region):
    result = {}
    if not isinstance(data, dict) or not isinstance(data.get('wow_accounts'), list):
        raise ValueError('Battle.net returned an invalid account profile.')
    for account in data['wow_accounts']:
        if not isinstance(account, dict) or not isinstance(account.get('characters'), list):
            raise ValueError('Battle.net returned an invalid character list.')
        for character in account['characters']:
            if not isinstance(character, dict) or not isinstance(character.get('realm'), dict):
                raise ValueError('Battle.net returned an invalid character.')
            realm = character['realm']
            cid, rid = character.get('id'), realm.get('id')
            name, slug = character.get('name'), realm.get('slug')
            if (type(cid) is not int or cid <= 0 or type(rid) is not int or rid <= 0 or
                    not isinstance(name, str) or not 1 <= len(name) <= 50 or
                    not isinstance(slug, str) or not 1 <= len(slug) <= 100):
                raise ValueError('Battle.net returned an invalid character identity.')
            key = f'{region}:{rid}:{cid}'
            result[key] = {'region': region, 'realm_id': rid, 'character_id': cid, 'name': name, 'realm': slug}
            class_id = (character.get('playable_class') or {}).get('id')
            if type(class_id) is int and class_id > 0:
                result[key]['class_id'] = class_id
    return result


def reply(text, status=200):
    return web.Response(text=text, status=status, headers={
        'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
        'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'", 'X-Content-Type-Options': 'nosniff',
    })
