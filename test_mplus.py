import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

import discord

from bot import DeadlockBot, register_commands
from mplus import (CAPACITY, GroupView, JoinView, check_profile, current_season, group_embed,
                   rule_for, unlink_entries)
from state import Store
from test_bot import StateFixture
import wow


def char(uid):
    return {'name': f'Hero{uid}', 'realm': 'example', 'region': 'eu', 'realm_id': 1,
            'character_id': uid, 'class_id': 11, 'verified_at': time.time() - 60}


def rule():
    return {'mode': 'push', 'from_level': 2, 'min_score': 1000, 'min_role_score': 900,
            'min_ilvl': 200, 'max_step': 2, 'require_dungeon': True}


def group():
    return {'region': 'eu', 'season': 'test-season', 'level': 10, 'mode': 'push', 'dungeon_id': 123}


def profile(uid, score=2500):
    date = datetime.now(timezone.utc).isoformat()
    return {'name': f'Hero{uid}', 'realm': 'example', 'class': 'Druid', 'last_crawled_at': date,
            'gear': {'updated_at': date, 'item_level_equipped': 250},
            'mythic_plus_scores_by_season': [{'season': 'test-season',
                'scores': {key: score for key in ('all', 'tank', 'healer', 'dps')}}],
            'mythic_plus_best_runs': [{'role': role, 'mythic_level': 12, 'clear_time_ms': 1000,
                'par_time_ms': 2000, 'map_challenge_mode_id': 123} for role in CAPACITY]}


def pool():
    return {'seasons': [{'slug': 'test-season', 'is_main_season': True,
                        'starts': {'eu': '2020-01-01T00:00:00Z'}, 'ends': {'eu': '2099-01-01T00:00:00Z'},
                        'dungeons': [{'name': 'Example Dungeon', 'challenge_mode_id': 123}]}]}


class AdmissionTests(unittest.TestCase):
    def test_low_rating_rejected_and_high_rating_can_join_lower_key(self):
        self.assertEqual(check_profile(profile(1), char(1), group(), rule(), 'tank'), [])
        reasons = check_profile(profile(1, 100), char(1), group(), rule(), 'tank')
        self.assertTrue(any('score requires' in r for r in reasons))
        low = {**group(), 'level': 2}
        self.assertEqual(check_profile(profile(1, 5000), char(1), low, rule(), 'tank'), [])

    def test_role_experience_dungeon_timer_missing_metrics_and_freshness(self):
        data = profile(1)
        data['mythic_plus_best_runs'] = [r for r in data['mythic_plus_best_runs'] if r['role'] == 'dps']
        self.assertTrue(any('tank required' in r for r in check_profile(data, char(1), group(), rule(), 'tank')))
        data = profile(1)
        data['mythic_plus_best_runs'][0]['clear_time_ms'] = 3000
        self.assertTrue(check_profile(data, char(1), group(), rule(), 'tank'))
        data = profile(1)
        data['mythic_plus_best_runs'][0]['map_challenge_mode_id'] = 999
        self.assertTrue(any('this dungeon' in r for r in check_profile(data, char(1), group(), rule(), 'tank')))
        data = profile(1)
        data['gear']['updated_at'] = '2000-01-01T00:00:00Z'
        data['gear']['item_level_equipped'] = None
        self.assertGreaterEqual(len(check_profile(data, char(1), group(), rule(), 'tank')), 2)
        data['mythic_plus_scores_by_season'] = []
        self.assertTrue(any('season' in r for r in check_profile(data, char(1), group(), rule(), 'tank')))

    def test_role_class_and_region_cannot_be_overridden(self):
        data = {**profile(1), 'class': 'Mage'}
        with self.assertRaisesRegex(ValueError, 'class'):
            check_profile(data, char(1), group(), rule(), 'tank')
        with self.assertRaisesRegex(ValueError, 'region'):
            check_profile(profile(1), {**char(1), 'region': 'us'}, group(), rule(), 'dps')
        with self.assertRaisesRegex(ValueError, 'identity'):
            check_profile(profile(2), char(1), group(), rule(), 'dps')
        self.assertIn('API unavailable', check_profile({}, char(1), group(), rule(), 'tank')[0])
        with self.assertRaises(ValueError):
            check_profile({}, {**char(1), 'class_id': None}, group(), rule(), 'tank')

    def test_rule_bands_and_season_selection(self):
        settings = {'rules': {'push:2': rule(), 'push:12': {**rule(), 'from_level': 12, 'min_score': 3000}}}
        self.assertEqual(rule_for(settings, group())['min_score'], 1000)
        self.assertEqual(rule_for(settings, {**group(), 'level': 15})['min_score'], 3000)
        self.assertEqual(rule_for({'rules': {}}, {**group(), 'mode': 'learning'})['min_score'], 0)
        with self.assertRaises(ValueError):
            rule_for({'rules': {}}, group())
        self.assertEqual(current_season(pool(), 'eu')['slug'], 'test-season')
        with self.assertRaises(ValueError):
            current_season(pool(), 'us')


class GroupTests(StateFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot = DeadlockBot(self.store)
        register_commands(self.bot)
        self.service = self.bot.mplus
        self.messages = {}
        async def send(**kwargs):
            mid = 100 + len(self.messages)
            msg = SimpleNamespace(id=mid, edit=AsyncMock())
            self.messages[mid] = msg
            return msg
        async def fetch(mid):
            if mid not in self.messages:
                raise discord.NotFound(SimpleNamespace(status=404, reason='gone'), 'gone')
            return self.messages[mid]
        self.channel = SimpleNamespace(id=10, send=AsyncMock(side_effect=send), fetch_message=AsyncMock(side_effect=fetch))
        self.users = {uid: SimpleNamespace(id=uid, bot=False, roles=[], guild_permissions=discord.Permissions.none()) for uid in range(1, 12)}
        self.guild = SimpleNamespace(id=1, fetch_member=AsyncMock(side_effect=lambda uid: self.users[uid]), get_channel=lambda cid: self.channel)
        async def api(endpoint, params, ttl=300):
            if endpoint == 'mythic-plus/static-data':
                return pool()
            return profile(int(params['name'].removeprefix('Hero')))
        self.bot.wow.get = AsyncMock(side_effect=api)
        async with self.store.edit(1) as data:
            data['mplus_settings'] = {'expansion_id': 11, 'channel_id': 10, 'rules': {'push:2': rule()}}
            data['wow_characters'] = {str(uid): {'main': f'eu:1:{uid}', 'characters': {f'eu:1:{uid}': char(uid)}} for uid in self.users}
        self.start_time = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.gid = await self.service.create(self.guild, 1, 'eu:1:1', 'tank', '123', 10, 'push', self.start_time)

    async def read(self):
        return (await self.store.read(1))['mplus_groups'][self.gid]

    async def join(self, uid, role='dps'):
        return await self.service.join(self.guild, uid, self.gid, f'eu:1:{uid}', role)

    async def fill(self):
        await self.join(2, 'healer')
        for uid in (3, 4, 5):
            await self.join(uid)

    async def test_multiple_groups_persistence_and_closed_buttons(self):
        second = await self.service.create(self.guild, 2, 'eu:1:2', 'healer', '123', 5, 'learning', self.start_time)
        self.assertNotEqual(second, self.gid)
        self.assertEqual(len((await Store(self.path).read(1))['mplus_groups']), 2)
        self.assertEqual((await self.store.read(2))['mplus_groups'], {})
        self.assertTrue(GroupView().is_persistent())
        self.assertEqual(len(GroupView().children), 5)
        message = SimpleNamespace(id=(await self.read())['message_id'])
        interaction = SimpleNamespace(client=self.bot, guild_id=1, channel_id=10, message=message)
        self.assertEqual(await GroupView().group_id(interaction), self.gid)
        await self.service.change(self.guild, self.users[1], self.gid, 'cancel')
        with self.assertRaises(ValueError):
            await GroupView().group_id(interaction)
        with self.assertRaises(ValueError):
            await self.join(3)

    async def test_concurrent_joins_cannot_add_a_sixth_or_duplicate_alt(self):
        await self.join(2, 'healer')
        await asyncio.gather(*(self.join(uid) for uid in (3, 4, 5, 6, 7)))
        players = (await self.read())['players']
        self.assertEqual(sum(e['status'] == 'joined' for e in players.values()), 5)
        self.assertEqual(sum(e['status'] == 'waiting' for e in players.values()), 2)
        with self.assertRaisesRegex(ValueError, 'already'):
            await self.join(3, 'tank')

    async def test_queue_offers_confirmation_and_expiration(self):
        await self.fill()
        await self.join(6)
        await self.join(7)
        self.channel.send.side_effect = None
        self.channel.send.return_value = SimpleNamespace(id=500, edit=AsyncMock())
        await self.service.change(self.guild, self.users[3], self.gid, 'leave')
        self.assertEqual((await self.read())['players']['6']['status'], 'offered')
        self.assertIn('joined', await self.join(6))
        await self.service.change(self.guild, self.users[6], self.gid, 'leave')
        async with self.store.edit(1) as state:
            state['mplus_groups'][self.gid]['players']['7']['offer_expires'] = time.time() - 1
        await self.service.maintenance(self.guild)
        self.assertNotIn('7', (await self.read())['players'])

    async def test_failed_offer_notification_retries_without_losing_reservation(self):
        await self.fill()
        await self.join(6)
        self.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='denied'), 'denied')
        with self.assertLogs('mplus', level='ERROR'):
            await self.service.change(self.guild, self.users[3], self.gid, 'leave')
        self.assertEqual((await self.read())['players']['6']['status'], 'offered')
        self.assertFalse((await self.read())['players']['6']['offer_notified'])
        self.channel.send.side_effect = None
        await self.service.maintenance(self.guild)
        self.assertTrue((await self.read())['players']['6']['offer_notified'])
        mentions = self.channel.send.call_args.kwargs['allowed_mentions']
        self.assertFalse(mentions.roles)
        self.assertFalse(mentions.everyone)
        self.assertEqual([user.id for user in mentions.users], [6])

    async def test_pending_approval_hierarchy_and_impossible_role(self):
        data = profile(3, score=100)
        self.service.profile = AsyncMock(return_value=data)
        text = await self.join(3)
        self.assertIn('Request sent', text)
        with self.assertRaisesRegex(ValueError, 'leader'):
            await self.service.change(self.guild, self.users[2], self.gid, 'approve', 3)
        await self.service.change(self.guild, self.users[1], self.gid, 'approve', 3)
        self.assertTrue((await self.read())['players']['3']['approved'])
        data['class'] = 'Mage'
        self.service.profile.return_value = data
        with self.assertRaisesRegex(ValueError, 'class'):
            await self.join(4, 'tank')

    async def test_start_rechecks_roles_links_and_closes_only_full_group(self):
        with self.assertRaisesRegex(ValueError, 'one tank'):
            await self.service.start(self.guild, self.users[1], self.gid)
        await self.fill()
        await self.service.start(self.guild, self.users[1], self.gid)
        self.assertEqual((await self.read())['status'], 'started')
        self.assertTrue(any(c.kwargs.get('ttl') == 0 for c in self.bot.wow.get.await_args_list))
        with self.assertRaises(ValueError):
            await self.join(6)

    async def test_start_failure_keeps_open_and_drops_unlinked_member(self):
        await self.fill()
        async with self.store.edit(1) as state:
            del state['wow_characters']['3']
        with self.assertRaisesRegex(ValueError, 'remains open'):
            await self.service.start(self.guild, self.users[1], self.gid)
        self.assertEqual((await self.read())['status'], 'open')
        self.assertNotIn('3', (await self.read())['players'])

    async def test_api_outage_requires_manual_review_and_approval_survives_start(self):
        await self.join(2, 'healer')
        await self.join(3)
        await self.join(4)
        original = self.service.profile
        async def offline(character, fresh=False):
            return {} if character['character_id'] == 5 else await original(character, fresh)
        self.service.profile = offline
        self.assertIn('API unavailable', await self.join(5))
        await self.service.change(self.guild, self.users[1], self.gid, 'approve', 5)
        await self.service.start(self.guild, self.users[1], self.gid)
        self.assertEqual((await self.read())['status'], 'started')

    async def test_rule_changes_invalidate_previous_manual_exceptions(self):
        await self.fill()
        async with self.store.edit(1) as state:
            group = state['mplus_groups'][self.gid]
            group['players']['3']['approved'] = True
            state['mplus_settings']['rules']['push:2']['min_score'] = 5000
        with self.assertRaisesRegex(ValueError, 'remains open'):
            await self.service.start(self.guild, self.users[1], self.gid)
        self.assertEqual((await self.read())['players']['3']['status'], 'pending')

    async def test_role_removed_before_start_and_group_changes_during_checks(self):
        await self.fill()
        async with self.store.edit(1) as state:
            state['role_id'] = 42
        for uid, user in self.users.items():
            user.roles = [SimpleNamespace(id=42)] if uid != 5 else []
        with self.assertRaisesRegex(ValueError, 'remains open'):
            await self.service.start(self.guild, self.users[1], self.gid)
        self.assertNotIn('5', (await self.read())['players'])
        self.users[5].roles = [SimpleNamespace(id=42)]
        await self.join(5)
        original = self.service.profile
        async def changed(character, fresh=False):
            async with self.store.edit(1) as state:
                state['mplus_groups'][self.gid]['revision'] += 1
            return await original(character, fresh)
        self.service.profile = changed
        with self.assertRaisesRegex(ValueError, 'group changed'):
            await self.service.start(self.guild, self.users[1], self.gid)
        self.assertEqual((await self.read())['status'], 'open')

    async def test_expired_groups_and_deleted_message_event(self):
        mid = (await self.read())['message_id']
        await self.bot.on_raw_message_delete(SimpleNamespace(guild_id=1, message_id=mid))
        self.assertTrue((await self.read())['dirty'])
        async with self.store.edit(1) as state:
            state['mplus_groups'][self.gid]['start'] = time.time() - 3601
        await self.service.maintenance(self.guild)
        self.assertEqual((await self.read())['status'], 'expired')

    async def test_policy_and_membership_changes_cannot_bypass_start(self):
        await self.fill()
        async with self.store.edit(1) as state:
            state['mplus_settings']['rules']['push:2']['min_score'] = 4000
        with self.assertRaisesRegex(ValueError, 'remains open'):
            await self.service.start(self.guild, self.users[1], self.gid)
        self.assertTrue(all(e['status'] == 'pending' for e in (await self.read())['players'].values()))

    async def test_season_change_prevents_join_and_start(self):
        await self.fill()
        self.service.pool = AsyncMock(return_value={**pool()['seasons'][0], 'slug': 'next-season'})
        with self.assertRaisesRegex(ValueError, 'season changed'):
            await self.join(6)
        with self.assertRaisesRegex(ValueError, 'season changed'):
            await self.service.start(self.guild, self.users[1], self.gid)
        self.assertEqual(len((await self.read())['players']), 5)

    async def test_transfer_leave_and_unlink_cleanup(self):
        await self.join(2, 'healer')
        await self.service.change(self.guild, self.users[1], self.gid, 'transfer', 2)
        await self.service.change(self.guild, self.users[1], self.gid, 'leave')
        self.assertEqual((await self.read())['leader_id'], 2)
        async with self.store.edit(1) as state:
            unlink_entries(state, 2, 'eu:1:2')
        self.assertEqual((await self.read())['status'], 'cancelled')

    async def test_deleted_card_and_publish_failure_are_retried(self):
        mid = (await self.read())['message_id']
        del self.messages[mid]
        await self.service.publish(self.guild, self.gid)
        self.assertTrue(self.channel.send.await_count >= 2)
        msg = self.messages[(await self.read())['message_id']]
        msg.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='denied'), 'denied')
        async with self.store.edit(1) as state:
            state['mplus_groups'][self.gid]['dirty'] = True
        with self.assertLogs('mplus', level='ERROR'):
            await self.service.publish(self.guild, self.gid)
        self.assertIn('permissions', (await self.read())['publish_error'])
        msg.edit.side_effect = None
        await self.service.maintenance(self.guild)
        self.assertFalse((await self.read())['dirty'])
        self.assertNotIn('publish_error', await self.read())

    async def test_requirements_changed_during_http_check(self):
        original = self.service.profile
        async def changed(char, fresh=False):
            async with self.store.edit(1) as state:
                state['mplus_settings']['rules']['push:2']['min_score'] = 4000
            return await original(char, fresh)
        self.service.profile = changed
        with self.assertRaisesRegex(ValueError, 'requirements changed'):
            await self.join(3)
        self.assertNotIn('3', (await self.read())['players'])

    async def test_ui_authorization_limits_and_command_permissions(self):
        group = await self.read()
        embed = group_embed(self.gid, group, rule())
        self.assertLessEqual(len(embed), 6000)
        roster = (await self.store.read(1))['wow_characters']['1']
        view = JoinView(self.gid, 1, roster)
        foreign = SimpleNamespace(user=self.users[2], response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(foreign))
        commands = self.bot.tree.get_command('mplus')
        self.assertTrue(commands.guild_only)
        for name in ('setup', 'rule'):
            with self.assertRaises(discord.app_commands.MissingPermissions):
                commands.get_command(name).checks[0](SimpleNamespace(permissions=discord.Permissions.none()))


if __name__ == '__main__':
    unittest.main()
