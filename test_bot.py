import asyncio
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from bot import DeadlockBot, GatheringView, register_commands
from state import Store, emoji_key, parse_time, record_vote, role_is_safe


class StateFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.store = Store(self.path)


class StateTests(StateFixture):
    async def test_persistence_isolation_and_snapshot(self):
        async with self.store.edit(1) as state:
            state["role_id"] = 42
            record_vote(state, 7, "+")
        snapshot = await self.store.read(1)
        snapshot["votes"].clear()
        restored = Store(self.path)
        self.assertEqual((await restored.read(1))["votes"], {"7": "+"})
        self.assertIsNone((await restored.read(2))["role_id"])

    async def test_concurrent_votes_and_duplicate_decline(self):
        async def vote(uid):
            async with self.store.edit(1) as state:
                record_vote(state, uid, "-")
                self.assertFalse(record_vote(state, uid, "-"))
        await asyncio.gather(*(vote(uid) for uid in range(20)))
        state = await self.store.read(1)
        self.assertEqual(len(state["votes"]), 20)
        self.assertEqual(sum(state["declines"].values()), 20)

    async def test_failed_transaction_does_not_change_memory_or_disk(self):
        async with self.store.edit(1) as state:
            state["tags"] = 4
        with self.assertRaises(ValueError):
            async with self.store.edit(1) as state:
                state["tags"] = 99
                raise ValueError("cancel")
        self.assertEqual((await self.store.read(1))["tags"], 4)
        self.assertEqual(json.loads(self.path.read_text())["1"]["tags"], 4)

    async def test_failed_disk_write_rolls_back_memory(self):
        async with self.store.edit(1) as state:
            state["tags"] = 4
        def fail():
            raise OSError("disk full")
        self.store._write = fail
        with self.assertRaises(OSError):
            async with self.store.edit(1) as state:
                state["tags"] = 99
        self.assertEqual((await self.store.read(1))["tags"], 4)
        self.assertEqual(json.loads(self.path.read_text())["1"]["tags"], 4)

    async def test_cancellation_finishes_write_before_releasing_lock(self):
        started, release = threading.Event(), threading.Event()
        original = self.store._write
        def blocked_write():
            started.set()
            release.wait(timeout=2)
            original()
        self.store._write = blocked_write
        async def change():
            async with self.store.edit(1) as state:
                state["tags"] = 8
        task = asyncio.create_task(change())
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(self.store.lock.locked())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((await self.store.read(1))["tags"], 8)
        self.assertEqual(json.loads(self.path.read_text())["1"]["tags"], 8)

    async def test_corrupted_data_is_not_overwritten(self):
        self.path.write_text("broken")
        with self.assertRaises(json.JSONDecodeError):
            Store(self.path)
        self.assertEqual(self.path.read_text(), "broken")

    def test_times_and_emoji(self):
        self.assertEqual(parse_time("9:5"), "09:05")
        for invalid in ("24:00", "12:60", "abc", "12", "-1:00"):
            with self.assertRaisesRegex(ValueError, "Enter a time in HH:MM format"):
                parse_time(invalid)
        self.assertEqual(emoji_key(discord.PartialEmoji.from_str("<:old:123456789012345678>")),
                         emoji_key(discord.PartialEmoji.from_str("<:new:123456789012345678>")))


class Role:
    id = 42
    managed = False
    permissions = discord.Permissions.none()

    def is_default(self):
        return False

    def __lt__(self, other):
        return self.id < other.id


class BotTests(StateFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot = DeadlockBot(self.store)
        register_commands(self.bot)
        self.role = Role()
        self.member = SimpleNamespace(id=7, bot=False, mention="<@7>", display_name="Player",
                                      add_roles=AsyncMock(), remove_roles=AsyncMock())
        self.role.members = [self.member]
        self.guild = SimpleNamespace(id=1, chunked=True, get_role=lambda rid: self.role if rid == 42 else None,
                                     fetch_member=AsyncMock(return_value=self.member),
                                     me=SimpleNamespace(top_role=SimpleNamespace(id=100),
                                                        guild_permissions=discord.Permissions(manage_roles=True)))
        async with self.store.edit(1) as state:
            state["role_id"] = 42
            state["reaction_roles"]["99"] = {"channel_id": 10, "roles": {"🎮": 42}}

    async def test_command_registration_and_persistent_buttons(self):
        names = {cmd.name for cmd in self.bot.tree.get_commands()}
        self.assertTrue({"setup", "tag", "team", "reactionrole", "mplus", "settings"} <= names)
        self.assertFalse({"link", "character"} & names)
        view = GatheringView()
        self.assertTrue(view.is_persistent())
        self.assertEqual([(button.custom_id, button.label) for button in view.children],
                         [("deadlock:plus", "Joining"), ("deadlock:minus", "Not joining")])

    async def test_roster_tracks_role_changes_without_database(self):
        state = await self.store.read(1)
        self.assertEqual(await self.bot.roster(self.guild, state), [self.member])
        self.role.members = []
        self.assertEqual(await self.bot.roster(self.guild, state), [])

    async def test_reaction_panel_line_breaks_and_length(self):
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()))
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(jump_url='panel', id=99)))
        command = self.bot.tree.get_command('reactionrole').get_command('panel')
        await command.callback(interaction, channel, r'Deadlock\nRetail\nClassic')
        channel.send.assert_awaited_once_with('Deadlock\nRetail\nClassic')
        channel.send.reset_mock()
        with self.assertRaises(ValueError):
            await command.callback(interaction, channel, 'x' * 2001)
        channel.send.assert_not_awaited()

    async def test_reaction_on_uncached_message_add_remove_and_unknown(self):
        self.bot._connection.user = SimpleNamespace(id=999)
        self.bot.get_guild = lambda gid: self.guild
        payload = SimpleNamespace(guild_id=1, channel_id=10, message_id=99, user_id=7,
                                  emoji=discord.PartialEmoji(name="🎮"))
        await self.bot.on_raw_reaction_add(payload)
        self.member.add_roles.assert_awaited_once_with(self.role, reason="Reaction role")
        await self.bot.on_raw_reaction_remove(payload)
        self.member.remove_roles.assert_awaited_once_with(self.role, reason="Reaction role")
        payload.message_id = 100
        await self.bot.on_raw_reaction_add(payload)
        self.assertEqual(self.member.add_roles.await_count, 1)

    async def test_unsafe_role_is_rejected_at_event_time(self):
        self.role.permissions = discord.Permissions(administrator=True)
        self.assertFalse(role_is_safe(self.role, self.guild.me))
        self.bot._connection.user = SimpleNamespace(id=999)
        self.bot.get_guild = lambda gid: self.guild
        await self.bot.on_raw_reaction_add(SimpleNamespace(guild_id=1, channel_id=10, message_id=99,
                                          user_id=7, emoji=discord.PartialEmoji(name="🎮")))
        self.member.add_roles.assert_not_awaited()

    async def test_tag_excludes_agreed_players_and_enforces_cooldown(self):
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
        self.assertEqual(await self.bot.start_gathering(self.guild, channel, self.member), "Gathering posted.")
        self.assertEqual(channel.send.await_count, 2)
        await self.bot.start_gathering(self.guild, channel, self.member)
        self.assertEqual(channel.send.await_count, 2)
        async with self.store.edit(1) as state:
            state["last_tag"] = 0
            record_vote(state, 7, "+")
        await self.bot.start_gathering(self.guild, channel, self.member)
        self.assertEqual(channel.send.await_count, 2)

    async def test_old_gathering_cannot_vote(self):
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()), client=self.bot,
                                      guild_id=1, message=SimpleNamespace(id=123),
                                      followup=SimpleNamespace(send=AsyncMock()))
        await GatheringView().vote(interaction, "+")
        self.assertEqual((await self.store.read(1))["votes"], {})
        self.assertIn("closed", interaction.followup.send.call_args.args[0])

    async def test_scheduler_resets_before_autotag_and_runs_once(self):
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
        self.guild.get_channel = lambda cid: channel
        self.bot._connection._guilds = {1: self.guild}
        async with self.store.edit(1) as state:
            state["autotag"] = state["reset_time"] = datetime.now().strftime("%H:%M")
            state["channel_id"] = 10
            state["votes"]["7"] = "+"
            state["messages"] = [999]
        await self.bot.scheduler.coro(self.bot)
        await self.bot.scheduler.coro(self.bot)
        state = await self.store.read(1)
        self.assertEqual(state["votes"], {})
        self.assertEqual(state["messages"], [123])
        self.assertEqual(state["tags"], 1)
        self.assertEqual(channel.send.await_count, 2)


if __name__ == "__main__":
    unittest.main()
