import asyncio
import os
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bnet import BattleNet, parse_characters
from bot import DeadlockBot, character_choices, register_commands
from state import Store
from test_bot import StateFixture

ENV = {'BNET_CLIENT_ID': 'test-client', 'BNET_CLIENT_SECRET': 'test-secret',
       'BNET_REDIRECT_URI': 'https://bot.example.com/oauth/battlenet/callback'}


def account():
    return {'wow_accounts': [{'characters': [
        {'id': 101, 'name': 'Alpha', 'realm': {'id': 1, 'slug': 'example-realm'}},
        {'id': 102, 'name': 'Beta', 'realm': {'id': 1, 'slug': 'example-realm'}},
    ]}]}


class IdentityTests(unittest.TestCase):
    def test_account_parsing_ids_regions_and_invalid_data(self):
        characters = parse_characters(account(), 'eu')
        self.assertEqual(set(characters), {'eu:1:101', 'eu:1:102'})
        self.assertEqual(characters['eu:1:101']['realm'], 'example-realm')
        self.assertIn('us:1:101', parse_characters(account(), 'us'))
        self.assertEqual(parse_characters({'wow_accounts': []}, 'eu'), {})
        for invalid in ({}, [], {'wow_accounts': [{}]}, {'wow_accounts': [{'characters': [None]}]}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_characters(invalid, 'eu')
        for value in (True, -1, '101'):
            data = account()
            data['wow_accounts'][0]['characters'][0]['id'] = value
            with self.assertRaises(ValueError):
                parse_characters(data, 'eu')

    def test_autocomplete_search_and_discord_limit(self):
        characters = {str(i): {'name': 'Hero' + str(i), 'realm': 'example', 'region': 'eu'} for i in range(60)}
        self.assertEqual(len(character_choices(characters, 'hero')), 25)
        choices = character_choices(characters, 'Hero59')
        self.assertEqual(choices[0].value, '59')
        self.assertEqual(character_choices(characters, 'unknown'), [])


class OAuthTests(StateFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.guild = SimpleNamespace(fetch_member=AsyncMock(return_value=object()))
        self.bot = SimpleNamespace(store=self.store, get_guild=lambda gid: self.guild if gid in (1, 2) else None)
        with patch.dict(os.environ, ENV):
            self.oauth = BattleNet(self.bot)
        self.oauth.runner = SimpleNamespace(cleanup=AsyncMock())
        self.oauth.fetch_characters = AsyncMock(return_value=parse_characters(account(), 'eu'))

    def state(self, guild=1, user=7):
        url = self.oauth.link(guild, user, 'eu')
        return parse_qs(urlsplit(url).query)['state'][0]

    async def callback(self, state, **params):
        return await self.oauth.callback(SimpleNamespace(query={'state': state, 'code': 'test-code', **params}))

    async def verify(self, guild=1, user=7):
        response = await self.callback(self.state(guild, user))
        self.assertEqual(response.status, 200)
        return response

    async def test_authorization_url_is_personal_random_and_replaced(self):
        url = self.oauth.link(1, 7, 'eu')
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query['scope'], ['wow.profile'])
        self.assertEqual(query['redirect_uri'], [ENV['BNET_REDIRECT_URI']])
        self.assertNotIn('test-secret', url)
        old = query['state'][0]
        new = self.state()
        self.assertNotEqual(old, new)
        self.assertGreaterEqual(len(new), 40)
        self.assertNotIn(old, self.oauth.pending)
        self.assertEqual((await self.callback(old)).status, 400)

    async def test_callback_is_one_time_and_returns_private_no_cache_response(self):
        state = self.state()
        response = await self.callback(state)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertNotIn('Alpha', response.text)
        self.assertNotIn('test-code', response.text)
        self.assertEqual((await self.callback(state)).status, 400)
        self.oauth.fetch_characters.assert_awaited_once_with('test-code', 'eu')
        self.assertEqual(len(self.oauth.candidates(1, 7)), 2)
        self.assertEqual(self.oauth.candidates(1, 8), {})
        self.assertEqual(self.oauth.candidates(2, 7), {})

    async def test_expiry_cancel_and_membership_failure(self):
        with patch('bnet.time.time', return_value=1000):
            state = self.state()
        with patch('bnet.time.time', return_value=1601):
            self.assertEqual((await self.callback(state)).status, 400)
        self.assertEqual((await self.callback(self.state(), error='access_denied')).status, 400)
        self.oauth.fetch_characters.assert_not_awaited()
        self.guild.fetch_member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='gone'), 'gone')
        self.assertEqual((await self.callback(self.state())).status, 403)
        self.oauth.fetch_characters.assert_not_awaited()

    async def test_inflight_verification_survives_prune_but_not_unlink_or_new_link(self):
        async def during_fetch(code, region):
            self.oauth.prune()
            return parse_characters(account(), region)
        self.oauth.fetch_characters.side_effect = during_fetch
        await self.verify()
        async def unlink_during_fetch(code, region):
            self.oauth.invalidate(1, 7)
            return parse_characters(account(), region)
        self.oauth.fetch_characters.side_effect = unlink_during_fetch
        self.assertEqual((await self.callback(self.state())).status, 400)
        self.assertEqual(self.oauth.candidates(1, 7), {})
        async def replace_during_fetch(code, region):
            self.state()
            return parse_characters(account(), region)
        self.oauth.fetch_characters.side_effect = replace_during_fetch
        self.assertEqual((await self.callback(self.state())).status, 400)
        self.assertEqual(self.oauth.candidates(1, 7), {})

    async def test_api_error_and_empty_profile_do_not_authorize(self):
        self.oauth.fetch_characters.side_effect = ValueError('provider failure containing test-secret')
        response = await self.callback(self.state())
        self.assertEqual(response.status, 502)
        self.assertNotIn('test-secret', response.text)
        self.assertEqual(self.oauth.candidates(1, 7), {})
        self.oauth.fetch_characters.side_effect = None
        self.oauth.fetch_characters.return_value = {}
        self.assertEqual((await self.callback(self.state())).status, 400)

    async def test_add_persistence_main_uniqueness_and_isolation(self):
        await self.verify()
        await self.oauth.add(1, 7, 'eu:1:101')
        await self.oauth.add(1, 7, 'eu:1:102')
        restored = await Store(self.path).read(1)
        self.assertEqual(restored['wow_characters']['7']['main'], 'eu:1:101')
        self.assertEqual(len(restored['wow_characters']['7']['characters']), 2)
        self.assertEqual(restored['links'], {})
        self.assertNotIn('test-secret', self.path.read_text())
        self.assertNotIn('access_token', self.path.read_text())
        with self.assertRaises(ValueError):
            await self.oauth.add(1, 7, 'eu:1:999')
        await self.verify(user=8)
        with self.assertRaisesRegex(ValueError, 'already linked'):
            await self.oauth.add(1, 8, 'eu:1:101')
        await self.verify(guild=2, user=8)
        await self.oauth.add(2, 8, 'eu:1:101')
        self.assertEqual((await self.store.read(1))['wow_characters']['7']['main'], 'eu:1:101')

    async def test_concurrent_claims_have_one_owner(self):
        await self.verify(user=7)
        await self.verify(user=8)
        results = await asyncio.gather(self.oauth.add(1, 7, 'eu:1:101'), self.oauth.add(1, 8, 'eu:1:101'), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        state = await self.store.read(1)
        self.assertEqual(len(state['wow_characters']), 1)

    async def test_verification_expiry_prevents_add(self):
        with patch('bnet.time.time', return_value=1000):
            await self.verify()
        with patch('bnet.time.time', return_value=1601):
            with self.assertRaisesRegex(ValueError, 'expired'):
                await self.oauth.add(1, 7, 'eu:1:101')

    async def test_start_disabled_and_invalid_settings_and_shutdown(self):
        with patch.dict(os.environ, {'BNET_CLIENT_ID': '', 'BNET_CLIENT_SECRET': '', 'BNET_REDIRECT_URI': ''}):
            disabled = BattleNet(self.bot)
        await disabled.start()
        with self.assertRaisesRegex(ValueError, 'not configured'):
            disabled.link(1, 7, 'eu')
        for invalid in ('http://bot.example.com/oauth/battlenet/callback', 'https://bot.example.com/wrong',
                        'https://user@bot.example.com/oauth/battlenet/callback',
                        ENV['BNET_REDIRECT_URI'] + '?code=invalid'):
            self.oauth.redirect_uri = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'HTTPS'):
                await self.oauth.start()
        self.oauth.session = SimpleNamespace(close=AsyncMock())
        close_session = self.oauth.session.close
        await self.oauth.close()
        close_session.assert_awaited_once()
        self.assertEqual(self.oauth.pending, {})
        self.assertIsNone(self.oauth.runner)

    async def test_listener_lifecycle_without_opening_a_socket(self):
        self.oauth.runner = None
        runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
        site = SimpleNamespace(start=AsyncMock())
        with patch('bnet.web.AppRunner', return_value=runner) as make_runner, patch('bnet.web.TCPSite', return_value=site):
            await self.oauth.start()
        self.assertIsNone(make_runner.call_args.kwargs['access_log'])
        runner.setup.assert_awaited_once()
        site.start.assert_awaited_once()
        routes = list(make_runner.call_args.args[0].router.routes())
        self.assertEqual([route.method for route in routes], ['GET'])
        await self.oauth.close()
        runner.cleanup.assert_awaited_once()


class ExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_exchange_headers_scope_and_error_handling(self):
        with patch.dict(os.environ, ENV):
            oauth = BattleNet(None)
        token_response = SimpleNamespace(status=200, json=AsyncMock(return_value={
            'access_token': 'temporary-token', 'scope': 'wow.profile'}))
        profile_response = SimpleNamespace(status=200, json=AsyncMock(return_value=account()))
        def context(response):
            value = MagicMock()
            value.__aenter__ = AsyncMock(return_value=response)
            value.__aexit__ = AsyncMock(return_value=False)
            return value
        oauth.session = SimpleNamespace(closed=False, post=MagicMock(return_value=context(token_response)),
            get=MagicMock(return_value=context(profile_response)))
        self.assertEqual(len(await oauth.fetch_characters('private-code', 'eu')), 2)
        kwargs = oauth.session.post.call_args.kwargs
        self.assertEqual(kwargs['data']['grant_type'], 'authorization_code')
        self.assertEqual(kwargs['data']['redirect_uri'], ENV['BNET_REDIRECT_URI'])
        self.assertEqual(kwargs['auth'].password, 'test-secret')
        kwargs = oauth.session.get.call_args.kwargs
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer temporary-token')
        self.assertEqual(kwargs['params']['namespace'], 'profile-eu')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertFalse(hasattr(oauth, 'access_token'))
        token_response.json.return_value.pop('scope')
        self.assertEqual(len(await oauth.fetch_characters('private-code', 'eu')), 2)
        token_response.json.return_value['scope'] = 'openid'
        with self.assertRaisesRegex(ValueError, 'wow.profile'):
            await oauth.fetch_characters('private-code', 'eu')
        token_response.status = 401
        with self.assertRaisesRegex(ValueError, 'authorization failed'):
            await oauth.fetch_characters('private-code', 'eu')
        oauth.session.post.side_effect = asyncio.TimeoutError
        with self.assertRaisesRegex(ValueError, 'try linking again later'):
            await oauth.fetch_characters('private-code', 'eu')


class CharacterCommandTests(StateFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        with patch.dict(os.environ, ENV):
            self.bot = DeadlockBot(self.store)
        register_commands(self.bot)
        self.group = self.bot.tree.get_command('character')
        self.bot.bnet.runner = SimpleNamespace(cleanup=AsyncMock())
        self.bot.bnet.verified[(1, 7)] = {'characters': parse_characters(account(), 'eu'), 'expires': float('inf')}
        self.interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=7),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))

    async def call(self, name, *args):
        await self.group.get_command(name).callback(self.interaction, *args)

    async def test_add_main_remove_unlink_and_private_responses(self):
        self.assertTrue(self.group.guild_only)
        self.assertEqual({c.name for c in self.group.commands}, {'link', 'add', 'list', 'main', 'remove', 'unlink'})
        await self.call('add', 'eu:1:101')
        await self.call('add', 'eu:1:102')
        await self.call('main', 'eu:1:102')
        await self.call('list')
        self.assertIn('★ Beta', self.interaction.response.send_message.call_args.args[0])
        await self.call('remove', 'eu:1:102')
        roster = (await self.store.read(1))['wow_characters']['7']
        self.assertEqual(roster['main'], 'eu:1:101')
        self.assertEqual(self.bot.bnet.candidates(1, 7), {})
        with self.assertRaises(ValueError):
            await self.call('main', 'eu:1:999')
        await self.call('unlink')
        self.assertEqual((await self.store.read(1))['wow_characters'], {})
        for call in self.interaction.response.send_message.call_args_list:
            self.assertTrue(call.kwargs['ephemeral'])
        self.assertTrue(self.interaction.followup.send.call_args.kwargs['ephemeral'])

    async def test_link_button_and_autocomplete_are_private(self):
        await self.call('link', 'eu')
        call = self.interaction.response.send_message.call_args
        self.assertTrue(call.kwargs['ephemeral'])
        self.assertIn('oauth.battle.net/authorize?', call.kwargs['view'].children[0].url)
        self.assertEqual(self.bot.bnet.candidates(1, 7), {})
        self.bot.bnet.verified[(1, 7)] = {'characters': parse_characters(account(), 'eu'), 'expires': float('inf')}
        autocomplete = self.group.get_command('add').get_parameter('character').autocomplete
        self.assertTrue(autocomplete)
