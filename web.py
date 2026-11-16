import asyncio
import inspect
import logging
import os
from pathlib import Path
import secrets
import time
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web
import discord
from discord import app_commands

import mplus

log = logging.getLogger(__name__)
COOKIE = 'tn_session'
SESSION_SECONDS = 8 * 3600
STATIC = Path(__file__).with_name('web_static')


def snowflake(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or not 0 < int(value) < 2 ** 64:
        raise ValueError('Некорректный Discord ID.')
    return int(value)


class WebInteraction:
    def __init__(self, bot, guild, member, channel, player=None):
        self.client, self.guild, self.guild_id, self.user = bot, guild, guild.id, member
        self.channel, self.channel_id = channel, channel.id if channel else None
        self.permissions = channel.permissions_for(member) if channel else member.guild_permissions
        self.response = self
        self.followup = SimpleNamespace(send=self.send_message)
        self.messages, self.player, self.done = [], player, False

    def is_done(self):
        return self.done

    async def defer(self, **kwargs):
        self.done = True

    async def send_message(self, content=None, *, embed=None, embeds=None, **kwargs):
        self.done = True
        self.messages.append({'content': content or '',
                              'embeds': [e.to_dict() for e in (embeds or ([embed] if embed else []))]})

    async def send_modal(self, modal):
        if not isinstance(modal, mplus.PlayerModal) or not isinstance(self.player, dict):
            raise ValueError('Выберите класс и специализацию.')
        if set(self.player) != {'class_name', 'spec'}:
            raise ValueError('Выберите класс и специализацию.')
        class_name, spec = self.player['class_name'], self.player['spec']
        if not isinstance(class_name, str) or not isinstance(spec, str):
            raise ValueError('Выберите класс и специализацию.')
        mplus.player(class_name, spec)
        if modal.creation is not None:
            gid = await self.client.mplus.create(self.guild, self.user.id, class_name, spec, **modal.creation)
            await self.send_message(f'Сбор #{gid} создан.')
        else:
            await self.client.mplus.join(self.guild, self.user.id, modal.group_id, class_name, spec)
            await self.send_message('Вы записаны в сбор.')


def allowed(command, member, permissions=None):
    effective = permissions if permissions is not None else member.guild_permissions
    if effective.administrator:
        return True
    node = command
    while node is not None:
        permissions = node.default_permissions
        if permissions is not None and effective.value & permissions.value != permissions.value:
            return False
        node = node.parent
    return True


class WebPanel:
    def __init__(self, bot):
        self.bot = bot
        self.base_url = os.getenv('WEB_BASE_URL', '').rstrip('/')
        self.client_id = os.getenv('DISCORD_CLIENT_ID', '')
        self.client_secret = os.getenv('DISCORD_CLIENT_SECRET', '')
        self.sessions, self.states = {}, {}
        self.http, self.runner = None, None
        self.app = web.Application(middlewares=[self.errors, self.security], client_max_size=32 * 1024)
        self.app.on_response_prepare.append(self.response_headers)
        self.app.add_routes([
            web.get('/', self.index), web.get('/app.js', self.asset), web.get('/style.css', self.asset),
            web.get('/i18n.js', self.asset),
            web.get('/invite', self.invite),
            web.get('/auth/login', self.login), web.get('/auth/callback', self.callback),
            web.post('/auth/logout', self.logout), web.get('/api/me', self.me),
            web.get('/api/guilds/{guild_id}', self.guild_info),
            web.post('/api/guilds/{guild_id}/commands', self.command),
        ])

    @web.middleware
    async def errors(self, request, handler):
        try:
            return await handler(request)
        except web.HTTPException as error:
            if error.status in (301, 302, 303, 307, 308):
                raise
            return web.json_response({'error': error.reason}, status=error.status)
        except (ValueError, app_commands.CheckFailure) as error:
            return web.json_response({'error': str(error)}, status=400)
        except discord.Forbidden:
            return web.json_response({'error': 'У бота недостаточно прав Discord.'}, status=403)
        except discord.NotFound:
            return web.json_response({'error': 'Объект Discord больше не существует.'}, status=404)
        except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError):
            log.exception('Web Discord request failed')
            return web.json_response({'error': 'Discord временно недоступен. Повторите позже.'}, status=502)
        except Exception:
            log.exception('Web panel request failed')
            return web.json_response({'error': 'Не удалось выполнить действие.'}, status=500)

    @web.middleware
    async def security(self, request, handler):
        if request.method not in ('GET', 'HEAD'):
            session = self.session(request)
            if request.headers.get('Origin') != self.base_url:
                raise web.HTTPForbidden(reason='Недопустимый источник запроса.')
            token = request.headers.get('X-CSRF-Token', '')
            if not token or not secrets.compare_digest(token, session['csrf']):
                raise web.HTTPForbidden(reason='Некорректный CSRF-токен.')
        return await handler(request)

    async def response_headers(self, request, response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' https://cdn.discordapp.com; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"

    def prune(self):
        now = time.time()
        for records in (self.sessions, self.states):
            for key in list(records):
                if records[key]['expires'] <= now:
                    del records[key]

    def create_session(self, user):
        self.prune()
        if len(self.sessions) >= 1000:
            raise web.HTTPServiceUnavailable(reason='Слишком много активных сессий.')
        sid = secrets.token_urlsafe(32)
        self.sessions[sid] = {'user': user, 'csrf': secrets.token_urlsafe(32),
                              'expires': time.time() + SESSION_SECONDS}
        return sid

    def session(self, request):
        self.prune()
        session = self.sessions.get(request.cookies.get(COOKIE, ''))
        if session is None:
            raise web.HTTPUnauthorized(reason='Войдите через Discord.')
        return session

    async def index(self, request):
        return web.FileResponse(STATIC / 'index.html')

    async def asset(self, request):
        return web.FileResponse(STATIC / request.path.lstrip('/'))

    async def start(self):
        if not self.base_url:
            return
        url = urlsplit(self.base_url)
        if (url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1'))
                or not url.hostname or url.path not in ('', '/') or url.query or url.fragment or url.username):
            raise ValueError('WEB_BASE_URL должен быть HTTPS-адресом без пути (HTTP разрешён только для localhost).')
        if not self.client_id or not self.client_secret:
            raise ValueError('Для веб-панели нужны DISCORD_CLIENT_ID и DISCORD_CLIENT_SECRET.')
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self.runner = web.AppRunner(self.app, access_log=None)
        try:
            await self.runner.setup()
            await web.TCPSite(self.runner, os.getenv('WEB_HOST', '0.0.0.0'), int(os.getenv('WEB_PORT', '8080'))).start()
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
        if self.http is not None:
            await self.http.close()
            self.http = None
        self.sessions.clear()
        self.states.clear()

    async def login(self, request):
        if not self.base_url or not self.client_id or not self.client_secret:
            raise web.HTTPServiceUnavailable(reason='Веб-панель не настроена.')
        self.prune()
        if len(self.states) >= 1000:
            raise web.HTTPTooManyRequests(reason='Повторите вход позже.')
        state = secrets.token_urlsafe(32)
        self.states[state] = {'expires': time.time() + 600}
        response = web.Response(status=302, headers={'Location': 'https://discord.com/oauth2/authorize?' + urlencode({
            'client_id': self.client_id, 'response_type': 'code', 'scope': 'identify',
            'redirect_uri': self.base_url + '/auth/callback', 'state': state,
        })})
        response.set_cookie('tn_oauth', state, max_age=600, httponly=True, secure=self.base_url.startswith('https://'),
                            samesite='Lax', path='/auth')
        return response

    async def invite(self, request):
        if not self.client_id:
            raise web.HTTPServiceUnavailable(reason='Веб-панель не настроена.')
        client_id = str(snowflake(self.client_id))
        permissions = discord.Permissions(view_channel=True, send_messages=True, read_message_history=True,
                                           embed_links=True, add_reactions=True, manage_roles=True)
        url = 'https://discord.com/oauth2/authorize?' + urlencode({
            'client_id': client_id, 'scope': 'bot applications.commands',
            'permissions': permissions.value, 'integration_type': 0,
        })
        return web.Response(status=302, headers={'Location': url})

    async def callback(self, request):
        state = request.query.get('state', '')
        cookie = request.cookies.get('tn_oauth', '')
        self.prune()
        if not state or not cookie or not secrets.compare_digest(state, cookie) or state not in self.states:
            raise web.HTTPForbidden(reason='Ссылка входа устарела или недействительна.')
        del self.states[state]
        code = request.query.get('code', '')
        if not code or self.http is None:
            raise web.HTTPBadRequest(reason='Не удалось завершить вход Discord.')
        async with self.http.post('https://discord.com/api/v10/oauth2/token', data={
            'client_id': self.client_id, 'client_secret': self.client_secret, 'grant_type': 'authorization_code',
            'code': code, 'redirect_uri': self.base_url + '/auth/callback',
        }) as response:
            if response.status != 200:
                raise web.HTTPBadGateway(reason='Discord отклонил вход.')
            token = await response.json()
        async with self.http.get('https://discord.com/api/v10/users/@me',
                                 headers={'Authorization': 'Bearer ' + token['access_token']}) as response:
            if response.status != 200:
                raise web.HTTPBadGateway(reason='Не удалось получить профиль Discord.')
            user = await response.json()
        uid = str(snowflake(user['id']))
        avatar = user.get('avatar')
        sid = self.create_session({'id': uid, 'name': user.get('global_name') or user['username'],
                                   'avatar_url': f'https://cdn.discordapp.com/avatars/{uid}/{avatar}.png' if avatar else None})
        old = request.cookies.get(COOKIE)
        self.sessions.pop(old, None)
        result = web.Response(status=302, headers={'Location': '/'})
        result.set_cookie(COOKIE, sid, max_age=SESSION_SECONDS, httponly=True,
                          secure=self.base_url.startswith('https://'), samesite='Lax', path='/')
        result.del_cookie('tn_oauth', path='/auth')
        return result

    async def logout(self, request):
        self.sessions.pop(request.cookies.get(COOKIE, ''), None)
        response = web.json_response({'ok': True})
        response.del_cookie(COOKIE, path='/')
        return response

    async def member(self, guild, uid):
        try:
            member = await guild.fetch_member(snowflake(uid))
        except discord.NotFound:
            raise web.HTTPForbidden(reason='Вы не состоите в этом сервере.') from None
        if member.bot:
            raise web.HTTPForbidden(reason='Боты не могут пользоваться панелью.')
        if not member.guild_permissions.administrator:
            raise web.HTTPForbidden(reason='Панель доступна только администраторам сервера Discord.')
        return member

    async def context(self, request):
        session = self.session(request)
        if not self.bot.is_ready():
            raise web.HTTPServiceUnavailable(reason='Бот подключается к Discord. Повторите позже.')
        guild = self.bot.get_guild(snowflake(request.match_info['guild_id']))
        if guild is None:
            raise web.HTTPNotFound(reason='Сервер недоступен.')
        return guild, await self.member(guild, session['user']['id'])

    async def me(self, request):
        session = self.session(request)
        if not self.bot.is_ready():
            raise web.HTTPServiceUnavailable(reason='Бот подключается к Discord. Повторите позже.')
        guilds = []
        for guild in self.bot.guilds:
            try:
                await self.member(guild, session['user']['id'])
            except (web.HTTPForbidden, discord.Forbidden):
                continue
            guilds.append({'id': str(guild.id), 'name': guild.name,
                           'icon_url': str(guild.icon.url) if guild.icon else None})
        return web.json_response({'user': session['user'], 'csrf': session['csrf'], 'guilds': guilds})

    def commands(self):
        return {cmd.qualified_name: cmd for cmd in self.bot.tree.walk_commands() if isinstance(cmd, app_commands.Command)}

    def catalog(self, member=None, guild=None):
        result = []
        for name, command in self.commands().items():
            if member is not None and not allowed(command, member):
                if name != 'cleanup' or guild is None or not any(
                        channel.permissions_for(member).view_channel and
                        channel.permissions_for(member).manage_messages for channel in guild.text_channels):
                    continue
            result.append({'name': name, 'description': command.description, 'parameters': [
                {'name': p.name, 'type': p.type.name, 'required': p.required,
                 'min_value': p.min_value, 'max_value': p.max_value,
                 'choices': [{'name': c.name, 'value': c.value} for c in p.choices]}
                for p in command.parameters]})
        return result

    def channel(self, guild, member, value):
        channel = guild.get_channel(snowflake(value))
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            raise ValueError('Выберите текстовый канал этого сервера.')
        if not channel.permissions_for(member).view_channel:
            raise web.HTTPForbidden(reason='У вас нет доступа к этому каналу.')
        return channel

    async def guild_info(self, request):
        guild, member = await self.context(request)
        state = await self.bot.store.read(guild.id)
        channels = [c for c in guild.text_channels if c.permissions_for(member).view_channel]
        visible = {c.id for c in channels}
        safe_state = {'tags': state['tags']}
        if member.guild_permissions.manage_guild:
            safe_state.update(role_id=str(state['role_id']) if state['role_id'] else None,
                              channel_id=str(state['channel_id']) if state['channel_id'] in visible else None,
                              cooldown=state['cooldown'], reset_time=state['reset_time'], autotag=state['autotag'])
            safe_state['dashboard'] = (state['dashboard'] if state['dashboard']
                                       and state['dashboard']['channel_id'] in visible else None)
            safe_state['mplus_settings'] = (state['mplus_settings'] if state['mplus_settings']
                                            and state['mplus_settings']['channel_id'] in visible else None)
        safe_state['mplus_groups'] = {gid: group for gid, group in state['mplus_groups'].items()
                                     if group['channel_id'] in visible}
        if member.guild_permissions.manage_roles:
            safe_state['reaction_roles'] = {
                mid: {**value, 'roles': {emoji: str(role) for emoji, role in value['roles'].items()}}
                for mid, value in state['reaction_roles'].items() if value['channel_id'] in visible}
        if not guild.chunked:
            await asyncio.wait_for(guild.chunk(cache=True), timeout=30)
        # Discord ID передаём строками: JavaScript не сохраняет точность snowflake.
        def ids(value, key=''):
            if isinstance(value, dict):
                return {k: ids(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [ids(v, key) for v in value]
            return str(value) if isinstance(value, int) and key.endswith('_id') else value
        return web.json_response({'guild': {'id': str(guild.id), 'name': guild.name},
            'commands': self.catalog(member, guild), 'channels': [{'id': str(c.id), 'name': c.name} for c in channels],
            'roles': [{'id': str(r.id), 'name': r.name} for r in guild.roles],
            'members': [{'id': str(m.id), 'name': m.display_name} for m in guild.members if not m.bot],
            'classes': mplus.CLASSES, 'state': ids(safe_state)})

    async def arguments(self, command, arguments, guild, member):
        if not isinstance(arguments, dict) or set(arguments) - {p.name for p in command.parameters}:
            raise ValueError('Неизвестные параметры команды.')
        result = {}
        for parameter in command.parameters:
            if parameter.name not in arguments or arguments[parameter.name] is None:
                if parameter.required:
                    raise ValueError(f'Заполните параметр {parameter.name}.')
                continue
            value = arguments[parameter.name]
            kind = parameter.type
            if kind == discord.AppCommandOptionType.string:
                if not isinstance(value, str) or len(value) > 6000:
                    raise ValueError(f'Некорректный текст: {parameter.name}.')
                bounded = len(value)
            elif kind == discord.AppCommandOptionType.integer:
                if type(value) is not int:
                    raise ValueError(f'Нужно целое число: {parameter.name}.')
                bounded = value
            elif kind == discord.AppCommandOptionType.channel:
                value = self.channel(guild, member, value)
                bounded = None
            elif kind == discord.AppCommandOptionType.role:
                value = guild.get_role(snowflake(value))
                if value is None:
                    raise ValueError('Роль не найдена на этом сервере.')
                bounded = None
            elif kind == discord.AppCommandOptionType.user:
                value = await guild.fetch_member(snowflake(value))
                bounded = None
            else:
                raise ValueError('Тип параметра пока не поддерживается.')
            if bounded is not None and ((parameter.min_value is not None and bounded < parameter.min_value)
                                        or (parameter.max_value is not None and bounded > parameter.max_value)):
                raise ValueError(f'Значение вне допустимого диапазона: {parameter.name}.')
            if parameter.choices and value not in [c.value for c in parameter.choices]:
                raise ValueError(f'Выберите значение из списка: {parameter.name}.')
            result[parameter.name] = value
        return result

    async def execute(self, guild, member, payload):
        if not member.guild_permissions.administrator:
            raise web.HTTPForbidden(reason='Панель доступна только администраторам сервера Discord.')
        if not isinstance(payload, dict) or set(payload) - {'command', 'channel_id', 'arguments', 'player'}:
            raise ValueError('Некорректный запрос команды.')
        name = payload.get('command')
        if not isinstance(name, str) or name not in self.commands():
            raise ValueError('Команда не найдена.')
        command = self.commands()[name]
        channel = self.channel(guild, member, payload['channel_id']) if payload.get('channel_id') else None
        if name == 'cleanup' and channel is None:
            raise ValueError('Выберите канал для удаления сообщений.')
        if not allowed(command, member, channel.permissions_for(member) if name == 'cleanup' else None):
            raise web.HTTPForbidden(reason='Недостаточно прав для этой команды.')
        interaction = WebInteraction(self.bot, guild, member, channel, payload.get('player'))
        for check in command.checks:
            passed = check(interaction)
            if inspect.isawaitable(passed):
                passed = await passed
            if not passed:
                raise web.HTTPForbidden(reason='Недостаточно прав для этой команды.')
        arguments = await self.arguments(command, payload.get('arguments', {}), guild, member)
        state = await self.bot.store.read(guild.id)
        targets = []
        if name == 'tag' and state['channel_id']:
            targets.append(state['channel_id'])
        elif name == 'mplus create' and state['mplus_settings']:
            targets.append(state['mplus_settings']['channel_id'])
        elif name.startswith('mplus ') and arguments.get('group'):
            group = state['mplus_groups'].get(arguments['group'])
            if group:
                targets.append(group['channel_id'])
        elif name.startswith('dashboard ') and name != 'dashboard setup' and state['dashboard']:
            targets.append(state['dashboard']['channel_id'])
        elif name == 'reactionrole remove':
            mapping = state['reaction_roles'].get(arguments.get('message_id'))
            if mapping:
                targets.append(mapping['channel_id'])
        elif name == 'reactionrole list':
            targets.extend(mapping['channel_id'] for mapping in state['reaction_roles'].values())
        for target in targets:
            self.channel(guild, member, str(target))
        await command.callback(interaction, **arguments)
        return {'messages': interaction.messages}

    async def command(self, request):
        guild, member = await self.context(request)
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(reason='Некорректный JSON.') from None
        async with asyncio.timeout(90):
            result = await self.execute(guild, member, payload)
        return web.json_response(result)
