import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import discord
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from bot import DeadlockBot, register_commands
from test_bot import StateFixture
from web import WebPanel


class WebTests(StateFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.env = patch.dict('os.environ', {
            'WEB_BASE_URL': 'http://localhost:8080',
            'DISCORD_CLIENT_ID': '123', 'DISCORD_CLIENT_SECRET': 'test-only-secret',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.bot = DeadlockBot(self.store)
        register_commands(self.bot)
        self.bot.is_ready = lambda: True
        self.bot._connection.user = SimpleNamespace(id=999)
        self.member = SimpleNamespace(id=7, name='Player', display_name='Player', bot=False,
                                      mention='<@7>', roles=[], guild_permissions=discord.Permissions.all())
        self.role = MagicMock(spec=discord.Role)
        self.role.id, self.role.name, self.role.managed = 42, 'Players', False
        self.role.permissions = discord.Permissions.none()
        self.role.is_default.return_value = False
        self.role.__lt__.return_value = True
        self.role.mention = '<@&42>'
        self.role.members = [self.member]
        self.member.roles = [self.role]
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id, self.channel.name = 10, 'general'
        self.channel.permissions_for.return_value = discord.Permissions.all()
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=99, jump_url='https://discord.com/channels/1/10/99'))
        self.guild = SimpleNamespace(id=1, name='Test server', icon=None, chunked=True,
            roles=[self.role], members=[self.member], text_channels=[self.channel], channels=[self.channel],
            fetch_member=AsyncMock(return_value=self.member),
            get_member=lambda uid: self.member if uid == 7 else None,
            get_channel=lambda cid: self.channel if cid == 10 else None,
            get_role=lambda rid: self.role if rid == 42 else None,
            me=SimpleNamespace(top_role=SimpleNamespace(id=100), guild_permissions=discord.Permissions.all()))
        self.channel.guild = self.guild
        self.member.guild = self.guild
        self.bot._connection._guilds = {1: self.guild}
        self.panel = WebPanel(self.bot)
        self.sid = self.panel.create_session({'id': '7', 'name': 'Player', 'avatar_url': None})
        self.session = self.panel.sessions[self.sid]
        self.client = TestClient(TestServer(self.panel.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.headers = {'Cookie': f'tn_session={self.sid}', 'X-CSRF-Token': self.session['csrf'],
                        'Origin': 'http://localhost:8080'}
        async with self.store.edit(1) as state:
            state['role_id'], state['channel_id'] = 42, 10

    async def post(self, body, headers=None, guild_id=1):
        return await self.client.post(f'/api/guilds/{guild_id}/commands', json=body,
                                      headers=self.headers if headers is None else headers)

    async def test_public_page_assets_and_security_headers(self):
        response = await self.client.get('/')
        self.assertEqual(response.status, 200)
        html = await response.text()
        self.assertIn('/app.js', html)
        self.assertIn('/i18n.js', html)
        self.assertIn('lang="ru"', html)
        self.assertIn('/style.css', html)
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        for asset in ('app.js', 'style.css', 'i18n.js'):
            response = await self.client.get('/' + asset)
            self.assertEqual(response.status, 200)
            self.assertTrue(await response.text())

    async def test_authentication_csrf_and_expired_session(self):
        response = await self.client.get('/api/me')
        self.assertEqual(response.status, 401)
        body = {'command': 'clear', 'channel_id': '10', 'arguments': {}}
        response = await self.post(body, {'Cookie': f'tn_session={self.sid}', 'Origin': 'http://localhost:8080'})
        self.assertEqual(response.status, 403)
        response = await self.post(body, {**self.headers, 'Origin': 'https://attacker.invalid'})
        self.assertEqual(response.status, 403)
        self.session['expires'] = time.time() - 1
        response = await self.client.get('/api/me', headers=self.headers)
        self.assertEqual(response.status, 401)

    async def test_oauth_state_is_browser_bound_one_use_and_logout_invalidates_session(self):
        response = await self.client.get('/auth/login', allow_redirects=False)
        self.assertEqual(response.status, 302)
        state = parse_qs(urlsplit(response.headers['Location']).query)['state'][0]
        self.client.session.cookie_jar.clear()
        response = await self.client.get('/auth/callback', params={'state': state, 'code': 'fake'},
                                         headers={'Cookie': 'tn_oauth=wrong'}, allow_redirects=False)
        self.assertEqual(response.status, 403)
        self.assertIn(state, self.panel.states)

        def context(data):
            result = MagicMock()
            result.__aenter__ = AsyncMock(return_value=SimpleNamespace(status=200, json=AsyncMock(return_value=data)))
            result.__aexit__ = AsyncMock(return_value=False)
            return result

        self.panel.http = SimpleNamespace(
            post=MagicMock(return_value=context({'access_token': 'must-stay-on-server'})),
            get=MagicMock(return_value=context({'id': '7', 'username': 'Player', 'avatar': None})),
            close=AsyncMock())
        response = await self.client.get('/auth/callback', params={'state': state, 'code': 'fake'},
                                         headers={'Cookie': f'tn_oauth={state}; tn_session={self.sid}'},
                                         allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertNotIn(state, self.panel.states)
        self.assertNotIn(self.sid, self.panel.sessions)
        cookie = response.cookies['tn_session']
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Lax')
        sid = cookie.value
        session = self.panel.sessions[sid]
        self.assertNotIn('must-stay-on-server', str(session))
        response = await self.client.get('/auth/callback', params={'state': state, 'code': 'fake'},
                                         headers={'Cookie': f'tn_oauth={state}'}, allow_redirects=False)
        self.assertEqual(response.status, 403)
        response = await self.client.post('/auth/logout', headers={'Cookie': f'tn_session={sid}',
            'Origin': 'http://localhost:8080', 'X-CSRF-Token': session['csrf']})
        self.assertEqual(response.status, 200)
        self.assertNotIn(sid, self.panel.sessions)
        await self.panel.close()

    async def test_implicit_target_channels_cannot_be_accessed_through_visible_context(self):
        private = MagicMock(spec=discord.TextChannel)
        private.id, private.guild = 11, self.guild
        private.permissions_for.return_value = discord.Permissions.none()
        self.guild.get_channel = lambda cid: self.channel if cid == 10 else private if cid == 11 else None
        async with self.store.edit(1) as state:
            state['channel_id'] = 11
            state['mplus_settings'] = {'channel_id': 11}
            state['mplus_groups']['3'] = {'status': 'open', 'channel_id': 11}
            state['reaction_roles']['99'] = {'channel_id': 11, 'roles': {'🎮': 42}}
        bodies = [
            {'command': 'tag', 'arguments': {}},
            {'command': 'mplus show', 'arguments': {'group': '3'}},
            {'command': 'mplus create', 'arguments': {'dungeon': 'Dungeon', 'level': 10,
                'start': '2026-12-01T20:00+02:00', 'mode': 'push'},
             'player': {'class_name': 'Warrior', 'spec': 'Protection'}},
            {'command': 'reactionrole remove', 'arguments': {'message_id': '99', 'emoji': '🎮'}},
        ]
        for body in bodies:
            with self.subTest(command=body['command']):
                response = await self.post({**body, 'channel_id': '10'})
                self.assertEqual(response.status, 403)

    async def test_bot_shutdown_closes_panel_and_clears_sessions(self):
        self.bot.web = self.panel
        self.panel.http = SimpleNamespace(close=AsyncMock())
        http = self.panel.http
        self.panel.states['pending'] = {'expires': time.time() + 600}
        with patch('discord.Client.close', new_callable=AsyncMock):
            await self.bot.close()
        http.close.assert_awaited_once()
        self.assertEqual(self.panel.sessions, {})
        self.assertEqual(self.panel.states, {})

    async def test_catalog_covers_all_registered_slash_commands(self):
        response = await self.client.get('/api/guilds/1', headers=self.headers)
        self.assertEqual(response.status, 200)
        body = await response.json()
        expected = {cmd.qualified_name for cmd in self.bot.tree.walk_commands()
                    if isinstance(cmd, discord.app_commands.Command)}
        self.assertTrue(expected <= {cmd['name'] for cmd in body['commands']})
        self.assertEqual(body['guild']['id'], '1')
        self.assertIn('classes', body)
        self.assertNotIn('test-only-secret', str(body))
        self.assertNotIn(self.sid, str(body))

    async def test_state_hides_private_channels_and_preserves_large_role_ids(self):
        role_id = 1561398187577581589
        async with self.store.edit(1) as state:
            state['reaction_roles']['99'] = {'channel_id': 10, 'roles': {'🎮': role_id}}
            state['reaction_roles']['100'] = {'channel_id': 11, 'roles': {'🎮': 42}}
            state['mplus_groups']['private'] = {'channel_id': 11, 'status': 'open'}
            state['dashboard'] = {'channel_id': 11, 'name': 'Private Guild'}
        response = await self.client.get('/api/guilds/1', headers=self.headers)
        self.assertEqual(response.status, 200)
        state = (await response.json())['state']
        self.assertEqual(state['reaction_roles']['99']['roles']['🎮'], str(role_id))
        self.assertNotIn('100', state['reaction_roles'])
        self.assertNotIn('private', state['mplus_groups'])
        self.assertIsNone(state.get('dashboard'))
        self.member.guild_permissions = discord.Permissions(view_channel=True)
        response = await self.client.get('/api/guilds/1', headers=self.headers)
        self.assertEqual(response.status, 403)
        self.assertNotIn('reaction_roles', str(await response.json()))

    async def test_only_administrators_see_guilds_and_live_revocation_blocks_commands(self):
        payload = {'command': 'settings', 'channel_id': '10', 'arguments': {'cooldown': 120}}
        response = await self.client.get('/api/me', headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual([guild['id'] for guild in (await response.json())['guilds']], ['1'])
        response = await self.post(payload)
        self.assertEqual(response.status, 200)
        fetches_before = self.guild.fetch_member.await_count
        self.member.guild_permissions = discord.Permissions(view_channel=True, manage_guild=True,
                                                             manage_roles=True, manage_messages=True)
        response = await self.client.get('/api/me', headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['guilds'], [])
        response = await self.client.get('/api/guilds/1', headers=self.headers)
        self.assertEqual(response.status, 403)
        response = await self.post({**payload, 'arguments': {'cooldown': 300}})
        self.assertEqual(response.status, 403)
        self.assertEqual((await self.store.read(1))['cooldown'], 120)
        self.assertEqual(self.guild.fetch_member.await_count - fetches_before, 3)
        self.assertIn(self.sid, self.panel.sessions, 'The existing session must not bypass freshly revoked rights')

    async def test_administrator_permission_is_scoped_to_each_guild(self):
        other_member = SimpleNamespace(id=7, bot=False, guild_permissions=discord.Permissions(manage_guild=True))
        other = SimpleNamespace(id=2, name='Other server', icon=None,
                                fetch_member=AsyncMock(return_value=other_member))
        self.bot._connection._guilds[2] = other
        response = await self.client.get('/api/me', headers=self.headers)
        self.assertEqual([guild['id'] for guild in (await response.json())['guilds']], ['1'])
        response = await self.client.get('/api/guilds/2', headers=self.headers)
        self.assertEqual(response.status, 403)
        response = await self.post({'command': 'clear', 'arguments': {}}, guild_id=2)
        self.assertEqual(response.status, 403)
        self.assertIsNone((await self.store.read(2))['role_id'])
        other.fetch_member.assert_awaited()

    async def test_invite_url_is_public_fixed_and_contains_no_secrets(self):
        response = await self.client.get('/invite', params={
            'client_id': '999', 'permissions': '8', 'redirect_uri': 'https://attacker.invalid',
            'next': 'https://attacker.invalid'}, allow_redirects=False)
        self.assertEqual(response.status, 302)
        target = response.headers['Location']
        url = urlsplit(target)
        self.assertEqual((url.scheme, url.netloc, url.path), ('https', 'discord.com', '/oauth2/authorize'))
        query = parse_qs(url.query)
        self.assertEqual(query['client_id'], ['123'])
        self.assertEqual(set(query['scope'][0].split()), {'bot', 'applications.commands'})
        self.assertEqual(query['integration_type'], ['0'])
        requested = discord.Permissions(int(query['permissions'][0]))
        expected = discord.Permissions(view_channel=True, send_messages=True, read_message_history=True,
            embed_links=True, add_reactions=True, manage_roles=True)
        self.assertEqual(requested.value, expected.value)
        self.assertFalse(requested.administrator)
        self.assertNotIn('redirect_uri', query)
        self.assertNotIn('next', query)
        for secret in ('test-only-secret', self.sid, self.session['csrf'], 'attacker.invalid'):
            self.assertNotIn(secret, target)
        response = await self.client.get('/invite', allow_redirects=False)
        self.assertEqual(response.headers['Location'], target)

    async def test_live_permissions_prevent_admin_command_and_foreign_guild(self):
        self.member.guild_permissions = discord.Permissions.none()
        response = await self.post({'command': 'clear', 'channel_id': '10', 'arguments': {}})
        self.assertEqual(response.status, 403)
        self.guild.fetch_member.assert_awaited()
        response = await self.post({'command': 'stats', 'channel_id': '10', 'arguments': {}}, guild_id=2)
        self.assertIn(response.status, (403, 404))
        self.assertIsNone((await self.store.read(2))['role_id'])

    async def test_argument_validation_and_channel_access(self):
        invalid = [
            {'command': 'settings', 'arguments': {'cooldown': -1}},
            {'command': 'settings', 'arguments': {'cooldown': True}},
            {'command': 'dashboard setup', 'arguments': {'region': 'invalid', 'realm': 'realm',
                                                        'guild_name': 'Guild', 'channel': '10'}},
            {'command': 'setup', 'arguments': {'role': '999', 'channel': '10'}},
            {'command': 'setup', 'arguments': {'role': '42', 'channel': '999'}},
        ]
        for body in invalid:
            with self.subTest(body=body):
                response = await self.post({**body, 'channel_id': '10'})
                self.assertEqual(response.status, 400)
        self.channel.permissions_for.return_value = discord.Permissions.none()
        response = await self.post({'command': 'stats', 'channel_id': '10', 'arguments': {}})
        self.assertEqual(response.status, 403)

    async def test_reaction_role_safety_and_missing_target_member(self):
        body = {'command': 'reactionrole add', 'channel_id': '10', 'arguments': {
            'channel': '10', 'message_id': '99', 'emoji': '🎮', 'role': '42'}}
        for managed, permissions, below in ((True, discord.Permissions.none(), True),
                                             (False, discord.Permissions(administrator=True), True),
                                             (False, discord.Permissions.none(), False)):
            self.role.managed, self.role.permissions = managed, permissions
            self.role.__lt__.return_value = below
            response = await self.post(body)
            self.assertEqual(response.status, 400)
        self.channel.fetch_message.assert_not_called()
        self.guild.fetch_member.side_effect = [self.member,
            discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'not a member')]
        response = await self.post({'command': 'mplus kick', 'channel_id': '10',
                                    'arguments': {'group': '3', 'member': '8'}})
        self.assertEqual(response.status, 404)

    async def test_cleanup_requires_administrator_and_deletes_only_bot_messages(self):
        self.channel.permissions_for.return_value = discord.Permissions(view_channel=True, manage_messages=True)
        bot_message = SimpleNamespace(author=SimpleNamespace(id=999), delete=AsyncMock())
        user_message = SimpleNamespace(author=SimpleNamespace(id=7), delete=AsyncMock())

        async def history(**kwargs):
            yield bot_message
            yield user_message

        self.channel.history = history
        response = await self.post({'command': 'cleanup', 'channel_id': '10', 'arguments': {}})
        self.assertEqual(response.status, 200)
        bot_message.delete.assert_awaited_once()
        user_message.delete.assert_not_awaited()
        self.member.guild_permissions = discord.Permissions(view_channel=True, manage_messages=True)
        response = await self.post({'command': 'cleanup', 'channel_id': '10', 'arguments': {}})
        self.assertEqual(response.status, 403)

    async def test_commands_reuse_callbacks_and_isolate_state(self):
        response = await self.post({'command': 'settings', 'channel_id': '10',
                                    'arguments': {'cooldown': 120, 'autotag': 'disable'}})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())['messages'])
        self.assertEqual((await self.store.read(1))['cooldown'], 120)
        self.assertEqual((await self.store.read(2))['cooldown'], 60)
        response = await self.post({'command': 'reactionrole panel', 'channel_id': '10',
                                    'arguments': {'channel': '10', 'text': r'Deadlock\nRetail'}})
        self.assertEqual(response.status, 200)
        self.channel.send.assert_awaited_once_with('Deadlock\nRetail')

    async def test_mplus_create_and_join_accept_class_and_specialization(self):
        self.bot.mplus.create = AsyncMock(return_value='3')
        self.bot.mplus.join = AsyncMock()
        response = await self.post({'command': 'mplus create', 'channel_id': '10',
            'arguments': {'dungeon': 'Dungeon', 'level': 10, 'start': '2026-12-01T20:00+02:00', 'mode': 'push'},
            'player': {'class_name': 'Warrior', 'spec': 'Protection'}})
        self.assertEqual(response.status, 200)
        self.bot.mplus.create.assert_awaited_once()
        async with self.store.edit(1) as state:
            state['mplus_groups']['3'] = {'status': 'open', 'channel_id': 10}
        response = await self.post({'command': 'mplus join', 'channel_id': '10', 'arguments': {'group': '3'},
                                    'player': {'class_name': 'Mage', 'spec': 'Frost'}})
        self.assertEqual(response.status, 200)
        self.bot.mplus.join.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
