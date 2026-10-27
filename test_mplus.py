import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from bot import DeadlockBot, register_commands
from mplus import CLASSES, GroupView, PlayerModal, group_embed, player
from state import Store


class MPlusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'state.json'
        self.store = Store(self.path)
        self.bot = DeadlockBot(self.store)
        register_commands(self.bot)
        self.service = self.bot.mplus
        self.message = SimpleNamespace(id=99, edit=AsyncMock())
        self.channel = SimpleNamespace(send=AsyncMock(return_value=self.message),
                                       fetch_message=AsyncMock(return_value=self.message))
        self.users = {uid: SimpleNamespace(id=uid, bot=False, roles=[],
                      guild_permissions=discord.Permissions.none()) for uid in range(1, 12)}
        self.guild = SimpleNamespace(id=1, get_channel=lambda cid: self.channel,
                                     fetch_member=AsyncMock(side_effect=lambda uid: self.users[uid]))
        self.start = datetime.fromtimestamp(time.time() + 600, timezone.utc).isoformat()
        async with self.store.edit(1) as state:
            state['mplus_settings'] = {'channel_id': 10}
        self.gid = await self.create()

    async def create(self, mode='push', **kwargs):
        values = dict(class_name='Warrior', spec='Protection', dungeon='Test dungeon',
                      level=10, mode=mode, start=self.start)
        values.update(kwargs)
        return await self.service.create(self.guild, 1, **values)

    async def group(self):
        return (await self.store.read(1))['mplus_groups'][self.gid]

    def interaction(self, uid=2):
        return SimpleNamespace(user=self.users[uid], guild=self.guild, guild_id=1, client=self.bot,
            channel_id=10, message=self.message,
            response=SimpleNamespace(defer=AsyncMock(), send_modal=AsyncMock(),
                                     send_message=AsyncMock(), is_done=lambda: True),
            followup=SimpleNamespace(send=AsyncMock()))

    async def test_creation_modes_no_api_persistence_and_isolation(self):
        self.bot.wow.get = AsyncMock(side_effect=AssertionError('M+ must not call API'))
        for mode in ('relaxed', 'carry'):
            gid = await self.create(mode)
            self.assertEqual((await self.store.read(1))['mplus_groups'][gid]['mode'], mode)
        self.bot.wow.get.assert_not_awaited()
        restored = await Store(self.path).read(1)
        self.assertEqual(restored['mplus_groups'][self.gid]['players']['1'],
                         {'class': 'Warrior', 'spec': 'Protection', 'role': 'tank'})
        self.assertEqual((await self.store.read(2))['mplus_groups'], {})
        self.assertNotIn('links', restored)
        self.assertNotIn('wow_characters', restored)
        self.assertEqual(self.channel.send.call_args.kwargs['allowed_mentions'].to_dict()['parse'], [])

    async def test_validation_and_capacity_under_concurrent_joins(self):
        with self.assertRaises(ValueError):
            player('Mage', 'Protection')
        with self.assertRaises(ValueError):
            await self.create(mode='learning')
        for value in ('2026-10-25T20:00', 'bad', datetime.fromtimestamp(time.time() - 10, timezone.utc).isoformat()):
            with self.assertRaises(ValueError):
                await self.create(start=value)
        with self.assertRaises(ValueError):
            await self.create(dungeon=' ')
        results = await asyncio.gather(*(self.service.join(self.guild, uid, self.gid, 'Mage', 'Frost')
                                         for uid in range(2, 9)), return_exceptions=True)
        self.assertEqual(sum(result is None for result in results), 3)
        self.assertTrue(all(result is None or isinstance(result, ValueError) for result in results))
        self.assertEqual(len((await self.group())['players']), 4)
        with self.assertRaisesRegex(ValueError, 'already joined'):
            await self.service.join(self.guild, 1, self.gid, 'Warrior', 'Fury')
        with self.assertRaisesRegex(ValueError, 'taken'):
            await self.service.join(self.guild, 9, self.gid, 'Death Knight', 'Blood')

    async def test_actions_permissions_full_group_and_leader_leave(self):
        with self.assertRaisesRegex(ValueError, 'leader'):
            await self.service.change(self.guild, self.users[2], self.gid, 'cancel')
        with self.assertRaisesRegex(ValueError, '1 tank'):
            await self.service.change(self.guild, self.users[1], self.gid, 'start')
        await self.service.join(self.guild, 2, self.gid, 'Priest', 'Holy')
        for uid in (3, 4, 5):
            await self.service.join(self.guild, uid, self.gid, 'Mage', 'Fire')
        await self.service.change(self.guild, self.users[1], self.gid, 'kick', 5)
        await self.service.join(self.guild, 6, self.gid, 'Evoker', 'Augmentation')
        await self.service.change(self.guild, self.users[1], self.gid, 'transfer', 2)
        await self.service.change(self.guild, self.users[2], self.gid, 'start')
        self.assertEqual((await self.group())['status'], 'started')
        with self.assertRaisesRegex(ValueError, 'closed'):
            await self.service.join(self.guild, 7, self.gid, 'Mage', 'Fire')
        gid = await self.create()
        await self.service.change(self.guild, self.users[1], gid, 'leave')
        self.assertEqual((await self.store.read(1))['mplus_groups'][gid]['status'], 'cancelled')

    async def test_membership_role_and_server_isolation(self):
        self.users[2].bot = True
        with self.assertRaisesRegex(ValueError, 'Bots'):
            await self.service.join(self.guild, 2, self.gid, 'Mage', 'Fire')
        async with self.store.edit(1) as state:
            state['role_id'] = 42
        with self.assertRaisesRegex(ValueError, 'player role'):
            await self.service.join(self.guild, 3, self.gid, 'Mage', 'Fire')
        self.users[3].roles = [SimpleNamespace(id=42)]
        await self.service.join(self.guild, 3, self.gid, 'Mage', 'Fire')
        other = SimpleNamespace(id=2, fetch_member=self.guild.fetch_member)
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            await self.service.join(other, 4, self.gid, 'Mage', 'Fire')

    async def test_publication_retry_deleted_card_and_expiry(self):
        self.message.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'blocked')
        with self.assertLogs('mplus', level='ERROR'):
            await self.service.join(self.guild, 2, self.gid, 'Mage', 'Fire')
        self.assertTrue((await self.group())['dirty'])
        self.assertIn('2', (await self.group())['players'])
        self.message.edit.side_effect = None
        await self.service.maintenance(self.guild)
        self.assertFalse((await self.group())['dirty'])
        self.channel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
        self.channel.send.reset_mock()
        async with self.store.edit(1) as state:
            state['mplus_groups'][self.gid]['dirty'] = True
        await self.service.maintenance(self.guild)
        self.channel.send.assert_awaited_once()
        async with self.store.edit(1) as state:
            state['mplus_groups'][self.gid]['start'] = time.time() - 3601
        await self.service.maintenance(self.guild)
        self.assertEqual((await self.group())['status'], 'expired')
        self.assertIsNone(self.channel.send.call_args.kwargs['view'])

    async def test_modal_contains_all_specs_and_submits_creation_and_join(self):
        creation = dict(dungeon='Manual dungeon', level=5, mode='carry', start=self.start)
        modal = PlayerModal(2, creation=creation)
        self.assertEqual(modal.title, 'Choose class and specialization')
        serialized = modal.to_components()
        self.assertEqual(len(serialized), 2)
        options = [o for select in modal.selects for o in select.options]
        self.assertEqual(len(options), sum(len(specs) for specs in CLASSES.values()))
        self.assertTrue(all(len(select.options) <= 25 for select in modal.selects))
        self.assertEqual(len({o.value for o in options}), len(options))
        interaction = self.interaction()
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            await modal.on_submit(interaction)
        modal.selects[0]._values = ['Priest:Holy']
        modal.selects[1]._values = ['Mage:Frost']
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            await modal.on_submit(interaction)
        modal.selects[1]._values = []
        await modal.on_submit(interaction)
        created = (await self.store.read(1))['mplus_groups']['2']
        self.assertEqual(created['mode'], 'carry')
        self.assertEqual(created['players']['2']['role'], 'healer')
        join = PlayerModal(2, group_id=self.gid)
        join.selects[1]._values = ['Mage:Frost']
        await join.on_submit(interaction)
        self.assertEqual((await self.group())['players']['2']['spec'], 'Frost')
        with self.assertRaisesRegex(ValueError, 'another member'):
            await join.on_submit(self.interaction(3))

    async def test_persistent_buttons_and_registered_commands_open_modal(self):
        view = GroupView()
        self.assertTrue(view.is_persistent())
        self.assertEqual([item.label for item in view.children], ['Join', 'Leave', 'Start', 'Cancel'])
        self.assertEqual({item.custom_id for item in view.children},
                         {'mplus:join', 'mplus:leave', 'mplus:start', 'mplus:cancel'})
        interaction = self.interaction()
        await view.children[0].callback(interaction)
        self.assertIsInstance(interaction.response.send_modal.call_args.args[0], PlayerModal)
        commands = self.bot.tree.get_command('mplus')
        self.assertEqual({cmd.name for cmd in commands.commands},
                         {'setup', 'create', 'join', 'show', 'kick', 'transfer', 'leave', 'start', 'cancel'})
        await commands.get_command('create').callback(interaction, 'Dungeon', 5, self.start, 'relaxed')
        modal = interaction.response.send_modal.call_args.args[0]
        self.assertEqual(modal.creation['mode'], 'relaxed')
        embed = group_embed(self.gid, await self.group()).to_dict()
        self.assertIn('Starts:', embed['description'])
        self.assertEqual(embed['fields'][1]['value'], 'Available')
        self.assertNotIn('Requirements', str(embed))

    async def test_migration_removes_accounts_closes_old_groups_and_is_idempotent(self):
        async with self.store.edit(1) as state:
            state['links'] = {'1': 123}
            state['wow_characters'] = {'1': {'characters': {'old': {'name': 'Old'}}}}
            state['mplus_settings'].update(expansion_id=11, rules={'push:2': {}})
            state['mplus_groups']['old'] = {
                'status': 'open', 'mode': 'learning', 'players': {'1': {'key': 'old'}},
                'revision': 0, 'start': time.time(), 'created': time.time(), 'channel_id': 10,
                'message_id': 77, 'dungeon': 'Old dungeon', 'level': 2}
            state['tags'] = 42
        await self.store.migrate()
        state = await self.store.read(1)
        self.assertNotIn('links', state)
        self.assertNotIn('wow_characters', state)
        self.assertEqual(state['mplus_settings'], {'channel_id': 10})
        self.assertEqual(state['mplus_groups']['old']['status'], 'cancelled')
        self.assertEqual(state['mplus_groups']['old']['players'], {})
        self.assertEqual(state['mplus_groups'][self.gid]['status'], 'open')
        self.assertEqual(state['tags'], 42)
        await self.store.migrate()
        self.assertEqual(await self.store.read(1), state)
        self.assertEqual(json.loads(self.path.read_text())['1'], state)
        await self.service.maintenance(self.guild)


if __name__ == '__main__':
    unittest.main()
