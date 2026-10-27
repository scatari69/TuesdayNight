import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bot import DeadlockBot, register_commands
from state import Store
from test_bot import StateFixture
from wow import APIError, RaiderIO, build_snapshot, dashboard_embeds


def config():
    return {'region': 'eu', 'realm': 'example-realm', 'name': 'Example Guild',
            'channel_id': 10, 'message_id': 99, 'last_attempt': 0, 'snapshot': None, 'error': None}


def guild_data():
    return {'members': [{'character': {'name': n, 'realm': 'example-realm'}} for n in ('Alpha', 'Beta')],
            'last_crawled_at': '2026-10-20T12:00:00Z',
            'raid_progression': {'example-raid': {'total_bosses': 8, 'mythic_bosses_killed': 3}},
            'raid_rankings': {'example-raid': {'mythic': {'realm': 4}, 'normal': {'realm': 0}}}}


def profile(name='Alpha', ilvl=200, score=1000, season='example-season'):
    return {'name': name, 'realm': 'example-realm', 'class': 'Druid',
            'last_crawled_at': '2026-10-20T12:00:00Z',
            'gear': {'item_level_equipped': ilvl, 'updated_at': '2026-10-19T12:00:00Z'},
            'mythic_plus_scores_by_season': [{'season': season, 'scores': {'all': score}}]}


class RenderingTests(unittest.TestCase):
    def test_top_order_progress_and_partial_data(self):
        snap = build_snapshot(config(), guild_data(), [profile(), profile('Beta', 220, 900)], 3)
        self.assertEqual([p['name'] for p in snap['ilvl']], ['Beta', 'Alpha'])
        self.assertEqual([p['name'] for p in snap['scores']], ['Alpha', 'Beta'])
        embeds = dashboard_embeds(config(), snap)
        text = str([e.to_dict() for e in embeds])
        for expected in ('Partial data', '2/3', '#4', '3/8', 'Unranked', 'Unavailable', '2026-10-19', 'raider.io'):
            self.assertIn(expected, text)

    def test_mixed_seasons_missing_values_and_invalid_numbers(self):
        snap = build_snapshot(config(), guild_data(),
                              [profile(ilvl=float('nan')), profile('Beta', score=-1, season='new-season')], 2)
        self.assertIsNone(snap['season'])
        self.assertEqual(snap['score_count'], 0)
        self.assertEqual(snap['ilvl_count'], 1)
        no_scores = profile()
        no_scores.pop('mythic_plus_scores_by_season')
        snap = build_snapshot(config(), guild_data(), [no_scores], 1)
        self.assertEqual(snap['score_count'], 0)
        self.assertEqual(build_snapshot(config(), guild_data(), [profile(score=0)], 1)['score_count'], 1)

    def test_embed_limits_escaping_and_stale_message(self):
        chars = [profile('[' + '@everyone' * 10 + str(i), 200 + i, 1000 + i) for i in range(20)]
        snap = build_snapshot(config(), guild_data(), chars, 20)
        self.assertEqual(len(snap['ilvl']), 10)
        embeds = dashboard_embeds(config(), snap, 'API unavailable')
        self.assertLessEqual(sum(len(e) for e in embeds), 6000)
        self.assertTrue(all(len(e.description or '') <= 4096 for e in embeds))
        self.assertIn('Stale', str(embeds[0].to_dict()))
        self.assertNotIn('@everyone', str([e.to_dict() for e in embeds]))
        self.assertEqual(len(dashboard_embeds(config(), None, 'API unavailable')), 1)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, status=200, payload=None, headers=None):
        client = RaiderIO()
        response = SimpleNamespace(status=status, headers=headers or {}, json=AsyncMock(return_value=payload))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        client.session = SimpleNamespace(closed=False, get=MagicMock(return_value=context), close=AsyncMock())
        return client, response

    async def test_cache_timeout_and_close(self):
        client, response = self.client(payload={'name': 'Example'})
        self.assertEqual(await client.get('guilds/profile', {'name': 'Example'}), {'name': 'Example'})
        await client.get('guilds/profile', {'name': 'Example'})
        self.assertEqual(client.session.get.call_count, 1)
        await client.close()
        client.session.close.assert_awaited_once()
        client.cache.clear()
        client.next_request = 0
        client.session.get.side_effect = asyncio.TimeoutError
        with self.assertRaises(APIError):
            await client.get('guilds/profile', {})
        self.assertGreater(client.retry_at, 0)

    async def test_rate_limit_stops_requests_until_retry_after(self):
        client, response = self.client(status=429, headers={'Retry-After': '120'})
        with patch('wow.time.time', return_value=1000):
            with self.assertRaises(APIError):
                await client.get('guilds/profile', {})
            self.assertEqual(client.retry_at, 1120)
            with self.assertRaises(APIError):
                await client.get('characters/profile', {})
        self.assertEqual(client.session.get.call_count, 1)

    async def test_bad_http_and_json(self):
        for status, data in ((404, {}), (200, []), (503, {})):
            client, _ = self.client(status=status, payload=data)
            with self.subTest(status=status, data=data), self.assertRaises(APIError):
                await client.get('guilds/profile', {})
        client, response = self.client()
        response.json.side_effect = ValueError('bad JSON')
        with self.assertRaisesRegex(APIError, 'invalid JSON'):
            await client.get('guilds/profile', {})

    async def test_expired_cache_fetches_again(self):
        client, _ = self.client(payload={'name': 'Example'})
        with patch('wow.time.time', return_value=1000):
            await client.get('guilds/profile', {}, ttl=30)
        with patch('wow.time.time', return_value=1031):
            await client.get('guilds/profile', {}, ttl=30)
        self.assertEqual(client.session.get.call_count, 2)

    async def test_partial_profiles_deduplication_and_global_failure(self):
        client = RaiderIO()
        guild = guild_data()
        guild['members'].append(copy.deepcopy(guild['members'][0]))
        client.guild = AsyncMock(return_value=guild)
        client.get = AsyncMock(side_effect=[profile(), APIError('not found')])
        snap = await client.snapshot(config())
        self.assertEqual(snap['loaded'], 1)
        self.assertEqual(client.get.await_count, 2)
        client.retry_at = float('inf')
        client.get = AsyncMock(side_effect=APIError('limited'))
        with self.assertRaises(APIError):
            await client.snapshot(config())


class DashboardTests(StateFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot = DeadlockBot(self.store)
        register_commands(self.bot)
        self.message = SimpleNamespace(id=99, edit=AsyncMock(), pin=AsyncMock())
        self.channel = SimpleNamespace(id=10, fetch_message=AsyncMock(return_value=self.message),
                                       send=AsyncMock(return_value=self.message))
        self.guild = SimpleNamespace(id=1, get_channel=lambda cid: self.channel)
        async with self.store.edit(1) as state:
            state['dashboard'] = config()
        self.snapshot = build_snapshot(config(), guild_data(), [profile()], 2)
        self.bot.wow.snapshot = AsyncMock(return_value=self.snapshot)

    async def test_refresh_edits_existing_and_survives_restart(self):
        await self.bot.refresh_dashboard(self.guild)
        self.message.edit.assert_awaited_once()
        self.channel.send.assert_not_awaited()
        restored = (await Store(self.path).read(1))['dashboard']
        self.assertEqual(restored['snapshot']['loaded'], 1)
        self.assertIsNone((await self.store.read(2))['dashboard'])
        self.assertIsNone(restored['error'])
        self.assertFalse(self.message.edit.call_args.kwargs['allowed_mentions'].everyone)

    async def test_deleted_message_is_recreated_and_id_saved(self):
        self.channel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='gone'), 'gone')
        self.message.id = 100
        await self.bot.refresh_dashboard(self.guild)
        self.channel.send.assert_awaited_once()
        self.message.pin.assert_awaited_once()
        self.assertEqual((await self.store.read(1))['dashboard']['message_id'], 100)
        self.channel.fetch_message.side_effect = None
        await self.bot.refresh_dashboard(self.guild)
        self.assertEqual(self.channel.send.await_count, 1)

    async def test_failure_keeps_last_snapshot_and_reports_permissions(self):
        async with self.store.edit(1) as state:
            state['dashboard']['snapshot'] = self.snapshot
        self.bot.wow.snapshot.side_effect = APIError('offline')
        with self.assertLogs('bot', level='ERROR'):
            await self.bot.refresh_dashboard(self.guild)
        stored = (await self.store.read(1))['dashboard']
        self.assertEqual(stored['snapshot'], self.snapshot)
        self.assertIn('Could not update', stored['error'])
        self.message.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='denied'), 'denied')
        with self.assertLogs('bot', level='ERROR'):
            await self.bot.refresh_dashboard(self.guild)
        self.assertIn('publication failed', (await self.store.read(1))['dashboard']['error'])

    async def test_queue_deduplicates_and_commands_require_manage_guild(self):
        self.bot.refresh_dashboard = AsyncMock()
        self.assertIn('queued', self.bot.queue_dashboard(self.guild))
        self.assertIn('already running', self.bot.queue_dashboard(self.guild))
        await self.bot.dashboard_tasks[1]
        self.bot.refresh_dashboard.assert_awaited_once()
        group = self.bot.tree.get_command('dashboard')
        self.assertTrue(group.guild_only)
        self.assertEqual({c.name for c in group.commands}, {'setup', 'refresh', 'status'})
        interaction = SimpleNamespace(permissions=discord.Permissions.none())
        for command in group.commands:
            with self.assertRaises(discord.app_commands.MissingPermissions):
                await command.checks[0](interaction)

    async def test_setup_validates_and_preserves_other_features(self):
        self.channel.permissions_for = lambda member: discord.Permissions(view_channel=True, send_messages=True,
            read_message_history=True, embed_links=True)
        interaction = SimpleNamespace(guild_id=1, guild=self.guild,
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        self.guild.me = object()
        self.bot.wow.guild = AsyncMock(return_value=guild_data())
        self.bot.queue_dashboard = MagicMock(return_value='queued')
        command = self.bot.tree.get_command('dashboard').get_command('setup')
        await command.callback(interaction, 'eu', 'example-realm', 'Example Guild', self.channel)
        self.assertEqual((await self.store.read(1))['dashboard']['message_id'], 99)
        self.assertEqual((await self.store.read(1))['votes'], {})
        self.channel.send.assert_not_awaited()
        self.channel.permissions_for = lambda member: discord.Permissions.none()
        with self.assertRaisesRegex(ValueError, 'Embed Links'):
            await command.callback(interaction, 'eu', 'realm', 'guild', self.channel)

    async def test_old_state_defaults_and_scheduler_due_check(self):
        self.path.write_text('{"3": {"links": {"7": 123}, "tags": 4}}')
        restored = Store(self.path)
        await restored.migrate()
        old = await restored.read(3)
        self.assertIsNone(old['dashboard'])
        self.assertNotIn('wow_characters', old)
        self.assertIsNone(old['mplus_settings'])
        self.assertEqual(old['mplus_groups'], {})
        self.assertEqual(old['mplus_next'], 1)
        self.assertNotIn('links', old)
        self.bot._connection._guilds = {1: self.guild}
        self.bot.queue_dashboard = MagicMock()
        await self.bot.dashboard_scheduler.coro(self.bot)
        self.bot.queue_dashboard.assert_called_once_with(self.guild)
        async with self.store.edit(1) as state:
            state['dashboard']['last_attempt'] = float('inf')
        await self.bot.dashboard_scheduler.coro(self.bot)
        self.assertEqual(self.bot.queue_dashboard.call_count, 1)

    async def test_shutdown_cancels_scan_and_closes_http(self):
        task = asyncio.create_task(asyncio.sleep(100))
        self.bot.dashboard_tasks[1] = task
        self.bot.wow.close = AsyncMock()
        with patch('discord.Client.close', new_callable=AsyncMock):
            await self.bot.close()
        self.assertTrue(task.cancelled())
        self.bot.wow.close.assert_awaited_once()

    async def test_refresh_cooldown_and_missing_setup(self):
        interaction = SimpleNamespace(guild_id=1, guild=self.guild,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()))
        command = self.bot.tree.get_command('dashboard').get_command('refresh')
        with patch('bot.time.time', return_value=100):
            async with self.store.edit(1) as state:
                state['dashboard']['last_attempt'] = 90
            with self.assertRaisesRegex(ValueError, '60 seconds'):
                await command.callback(interaction)
        async with self.store.edit(1) as state:
            state['dashboard'] = None
        with self.assertRaisesRegex(ValueError, 'setup first'):
            await command.callback(interaction)


if __name__ == '__main__':
    unittest.main()
