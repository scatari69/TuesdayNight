# TuesdayNight

Standalone Discord bot for gatherings and reaction roles, currently with Deadlock
profile support. All bot messages, buttons, and command descriptions are in English.
The roster comes from current members of the configured Discord role; bots are
excluded and player lists are not stored. Settings and statistics are isolated by
Discord server. Each server has one current gathering and one gathering channel.

The WoW guild roadmap is in [WOW_ROADMAP.md](WOW_ROADMAP.md). The WoW dashboard is
available, as are Battle.net character linking and Mythic+ groups.
Public API verification and remaining integration checks are documented in
[WOW_API_CHECK.md](WOW_API_CHECK.md) (Russian).

## Installation

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable **Server Members Intent** on the **Bot** page. Message Content Intent
   is not required: the bot uses slash commands.
3. Invite the bot with `bot` and `applications.commands` scopes. Grant View Channels,
   Send Messages, Read Message History, Add Reactions, and Manage Roles. Place the
   bot's role above roles it will assign. Administrator is not required.
4. Create a regular player role, such as `deadlock`, without server permissions.
   Channel permission overrides can grant access. Reaction roles must have no
   server permissions to prevent granting moderator privileges.
5. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`. Set `DISCORD_GUILD_ID` for
   immediate command registration on a test server; otherwise commands register
   globally and may take time to appear.
6. Start the bot:

```bash
cp .env.example .env
# Fill in .env before starting.
docker compose up -d --build
```

For a local run from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Only Docker Compose loads .env automatically.
export DISCORD_TOKEN='your-token'
python bot.py
```

`DATA_DIR` selects the state directory (default: `data`; container: `/app/data`).
The `./data` volume survives rebuilds. JSON writes are atomic and locked; corrupt
state stops startup instead of being replaced. Run one instance per data directory.
Telegram data is not migrated: Discord uses different user and server IDs.

## Initial setup

A member with Manage Server runs:

```text
/setup role:@deadlock channel:#gatherings
/settings cooldown:60 reset_time:03:00 autotag:20:00
```

`/settings` without arguments shows current settings. `autotag:disable` disables
scheduled gatherings. Times use the process `TZ`; the container defaults to
Europe/Kyiv. The scheduler runs while the bot is online and does not replay missed
events. The daily reset and `/clear` close old gathering buttons.

## Reaction roles

A member with Manage Roles:

1. Runs `/reactionrole panel channel:#roles text:React with 🎮 to get the Deadlock role`.
2. Copies the message ID from the response.
3. Runs `/reactionrole add channel:#roles message_id:... emoji:🎮 role:@deadlock`.

Adding 🎮 assigns the role; removing it removes the role. Multiple emoji/role pairs
can share a message. Unicode and custom emoji (`<:name:id>` / `<a:name:id>`) are
supported. Roles must be below the bot's highest role and the configuring member's
highest role (except for administrators).

- `/reactionrole list` lists server mappings.
- `/reactionrole remove message_id:... emoji:🎮` removes a mapping.
- Mappings survive restarts and do not depend on the message cache.
- Only this bot's messages can have mappings.
- Removing a mapping or message preserves previously assigned roles.
- Removing a reaction removes the role even if it was assigned manually. Avoid
  using the same role in multiple panels: removing any mapped reaction removes it.
- Reactions added before configuration or while the bot was offline are not
  reconciled automatically; users must remove and add the reaction again.
  Clearing all reactions also does not revoke assigned roles.

## Commands

| Command | Action |
| --- | --- |
| `/tag` | A player-role member invites players who have not joined; cooldown applies |
| `/team` | Current player-role members |
| `/me` | Personal statistics and Deadlock profile |
| `/link profile:...` | Link Steam/tracklock/numeric ID; `remove` unlinks |
| `/stats` | Gathering count and top three starters, off-roster attempts, and declines |
| `/clear` | Close the gathering and reset cooldown; Manage Server |
| `/setup` | Set role and channel; Manage Server |
| `/settings` | Configure cooldown and schedule; Manage Server |
| `/cleanup` | Delete this bot's messages from the last 24 hours in this channel; Manage Messages |
| `/reactionrole ...` | Create panels, configure mappings, and list them; Manage Roles |
| `/dashboard setup` | Configure the Retail WoW guild and dashboard channel; Manage Server |
| `/dashboard refresh` | Queue an update using cached API data; Manage Server |
| `/dashboard status` | Show settings, update activity, and the last error; Manage Server |
| `/character link region:eu` | Get a private Battle.net sign-in link to verify character ownership |
| `/character add character:...` | Select a verified character using autocomplete |
| `/character list` | List your linked characters; ★ marks your main |
| `/character main character:...` | Choose your main from linked characters |
| `/character remove character:...` | Unlink one character |
| `/character unlink` | Remove all your WoW links on this Discord server |
| `/mplus setup`, `/mplus rule` | Configure the group channel, expansion, and requirements; Manage Server |
| `/mplus rules` | Show the configured key-level bands |
| `/mplus create` | Create a group and join as leader with a linked character |
| `/mplus join`, `/mplus leave` | Join, request approval, queue, or leave |
| `/mplus show` | Show the card; leaders also see reasons for pending requests |
| `/mplus approve`, `/mplus kick`, `/mplus transfer` | Approve exceptions, remove entries, or transfer leadership; leader or Manage Server |
| `/mplus start`, `/mplus cancel` | Recheck and start, or cancel; leader or Manage Server |

## Mythic+ groups

Configure the channel and the expansion's Raider.IO ID with Manage Server:

```text
/mplus setup channel:#mythic-plus expansion_id:11
```

Expansion `11` is the API ID used for Midnight in the API verification; select
the expansion you actually play. The bot discovers the current main season and
dungeon pool from Raider.IO using the character's region. Seasons and dungeons
are not hardcoded. If dungeon autocomplete times out, wait a few seconds and try
again to allow the shared API client to populate its cache.

Configure **Push** rules before creating Push groups. Example of command syntax,
not recommended seasonal thresholds:

```text
/mplus rule mode:push from_level:2 min_score:0 min_role_score:0 min_ilvl:0 max_step:2 require_dungeon:false
/mplus rule mode:push from_level:10 min_score:2000 min_role_score:1800 min_ilvl:250 max_step:2 require_dungeon:true
```

Choose real thresholds with your officers. The highest matching `from_level`
band applies up to the next band. Setting the same mode and level replaces that
band. `/mplus rules` lists them. Rules apply to joins and start checks in existing
groups too; a changed rule invalidates previous manual exceptions for it.
An officer may disable the experience check with `require_experience:false`;
`require_dungeon:true` requires the experience check to be enabled.

**Learning** defaults to no rating, ilvl, or experience minimums, unless officers
configure Learning rules. Ownership, a valid class/role, region, available current
season data, and source freshness are still checked. Use Learning explicitly for
training; it does not inherit Push requirements.

Link your characters first, then create a group:

```text
/mplus create dungeon:... level:10 character:... role:tank start:2026-10-25T20:00+02:00 mode:push
```

Select the actual current dungeon and linked character from autocomplete and use
your actual future date/time. The start time requires a timezone offset, within
the next 30 days, and is rendered in each Discord user's timezone. The group posts
in the configured channel and gets an ID. Multiple groups can run independently;
each leader can have up to three open groups. The creator must pass admission and
occupies one seat. Existing `/tag`, `/clear`, and nightly vote resets do not affect
Mythic+ groups. If a Discord player role was configured with `/setup`, participants
need it (members with Manage Server are exempt); otherwise linked server members
can participate.

Use **Join** on the card to select a linked character and role, or:

```text
/mplus join group:... character:... role:dps
```

The card has one tank seat, one healer seat, and three DPS seats. One Discord user
has only one entry per group, including requests and queue entries. Leave before
changing character or role. The Join picker shows at most 25 linked characters;
use slash autocomplete to search the rest. Other characters' ratings and the main
character's rating do not transfer to the selected character.

Admission checks the selected character's overall and role-specific seasonal
ratings, equipped ilvl, and class capabilities. When required, timed experience
must be at least `max(2, group level - max_step)` on the selected role; dungeon
experience must also match the selected dungeon at that level. Best, recent and
highest-level API runs provide evidence, but are not a complete history. There is
no upper rating or experience limit: experienced characters can join lower keys.
Character profiles are cached for at most five minutes; start checks fetch them
again. API rate limiting and timeouts use the shared Raider.IO client.

A candidate below requirements, with missing verified runs, or with profile/gear
source dates older than 24 hours or unknown enters **Leader review**, not a seat.
Use `/mplus show` to inspect reasons and `/mplus approve group:... member:...` to
accept a specific exception. Exceptions are visible on the card. They can override
rating, experience, and data freshness only: they cannot override missing links,
an impossible class/role, a changed season/region, duplicate entries, or seat limits.
During an API outage a request can be reviewed only if the class was already
verified by Battle.net; otherwise retry once the profile is available. A fresh
HTTP response does not force Raider.IO to refresh its own source snapshots.

Qualified candidates whose role is full enter that role's FIFO queue. When a seat
opens, the first queued candidate gets a five-minute reservation and an explicit
individual mention. Use **Confirm seat** to recheck eligibility and accept it.
Unconfirmed offers are removed on the next minute tick, then the next candidate
is offered the seat. Ordinary cards and replies do not ping users or roles.
Queues and offers survive restarts; expired offers are processed after startup.

The leader can **Start** only with a confirmed 1/1/3 group. The bot rechecks links,
server membership, the configured player role, current requirements and profiles.
Invalid entries are removed or returned to review, and the group stays open.
Manual exceptions remain valid only for the rule they approved. Concurrent changes
abort the start check so it can be retried. **Start** records the Discord group's
state; it neither verifies a keystone in anyone's inventory nor starts WoW itself.

**Leave** frees your seat; leaving as leader cancels the group. Transfer leadership
to another confirmed participant first if needed. **Cancel** closes all signups.
Removing a character link removes its entries in open groups; unlinking a leader's
participating character cancels that group. Open groups expire one hour after
their scheduled start if they have not started. Closed records older than seven
days are pruned when new groups are created; they are not an attendance archive.

Cards use persistent buttons. Deleted cards are recreated on the next minute
tick; failed edits are retried. If posting fails, signup state remains saved and
`/mplus show` displays the publication error. After changing channel permissions,
allow the next tick to retry. No further API keys or dependencies are needed
beyond the existing dashboard and Battle.net setup.

## Battle.net character linking

The dashboard does not require OAuth, but character ownership verification does.
Create an application in the [Battle.net developer portal](https://develop.battle.net/access/clients).
Register an exact HTTPS redirect URI, for example:

```text
https://bot.example.com/oauth/battlenet/callback
```

Set these variables in `.env` for Docker Compose, or export them for a local run:

```text
BNET_CLIENT_ID=your-client-id
BNET_CLIENT_SECRET=your-client-secret
BNET_REDIRECT_URI=https://bot.example.com/oauth/battlenet/callback
```

Leave all three blank to disable linking. Partial or invalid configuration stops
startup with an error. The callback path must be `/oauth/battlenet/callback`,
with no query string or fragment, and match the registered URI exactly.

The bot serves the callback on port 8080. Compose publishes it only at
`127.0.0.1:8080`; local Python also binds to loopback by default. Put an HTTPS
reverse proxy in front of it. For example, with Caddy running on the same host:

```caddyfile
bot.example.com {
    handle /oauth/battlenet/callback {
        reverse_proxy 127.0.0.1:8080
    }
    respond "Not found" 404
}
```

Use your domain, point DNS to the host, and allow the proxy to obtain a TLS
certificate. Do not enable access logging of callback query strings at the proxy;
they contain a one-time authorization code. The bot disables callback access logs
and sends `no-store` / `no-referrer` responses. If the proxy is in another
container, route it to the bot's port 8080 on their shared private network instead
of the host loopback address. `BNET_BIND_HOST` defaults to `127.0.0.1` for a local
run and is set to `0.0.0.0` inside the Compose container.

Restart/rebuild after configuring the application and proxy. Then:

1. Run `/character link region:eu` (or US/KR/TW) and open the private sign-in button.
   Select the region where your Retail characters live. Never share the link.
2. Sign in on Battle.net and grant `wow.profile`. The callback checks that you are
   still a member of the Discord server and reads the account's characters.
3. Return to Discord and run `/character add`. Choose a character from autocomplete;
   typing an arbitrary name or someone else's ID does not prove ownership.
   Type part of a name or realm to search beyond the first 25 suggestions.
4. Repeat `add` for alts. The first character becomes your main; change it with
   `/character main`. Use `/character list` to see your links.

The sign-in link is single-use and expires after 10 minutes. After verification,
you have 10 minutes to add characters; re-run `link` if it expires. Starting another
sign-in invalidates the previous pending sign-in and selection list. Restarting the
bot invalidates unfinished sign-ins, but saved links survive. Every reply is private.

Saved identity is region + realm ID + character ID, with name, realm slug and
verification time for later profile lookups. The bot stores only characters you
select, not your full account roster, BattleTag, password, or OAuth tokens. Tokens
are used only to retrieve characters and are discarded afterwards. Different
servers have isolated links; on one server, a character can belong to only one
Discord user. Existing Steam links are separate. Renames/transfers require a new
verification to update the saved identity; ownership is verified at linking time,
not continuously monitored.

Removing your main promotes the first remaining linked character. Removing a
character also invalidates unfinished verification for you on that server.
`/character unlink` clears all your saved WoW links and unfinished verification
there; it does not remove links on other servers or your Steam profile. If an
existing link belongs to the wrong Discord account, that account must unlink it
before another member can claim it. Manual officer overrides are not implemented.

## WoW guild dashboard

After restarting the bot to sync commands, a member with Manage Server runs:

```text
/dashboard setup region:eu realm:tarren-mill guild_name:Your Guild channel:#guild-dashboard
```

Use your actual region, realm and guild name. Supported regions: EU, US, KR, TW.
The bot needs View Channel, Send Messages, Read Message History, and Embed Links
in the dashboard channel. It tries to pin the message; if pinning is not permitted,
pin it manually. Setup validates the guild on Raider.IO before saving it.

One message shows current-tier raid progression and realm ranks by difficulty,
the top ten guild characters by equipped item level, and the top ten by total
Mythic+ score for the current season. Alts count as separate characters. The WoW
roster comes from Raider.IO, independently of the Discord gathering role.
No API key or Battle.net login is needed for this stage.

Updates run in the background every 30 minutes after the previous scan starts.
Raider.IO requests are serialized across servers, at most one per second;
large guilds can take many minutes. Ordinary gatherings remain available.
Guild responses are cached for one hour and character responses for 30 minutes
in memory. `/dashboard refresh` respects those caches and a 60-second cooldown;
it cannot force Raider.IO to refresh its own data. Duplicate scans are suppressed.
HTTP 429 respects `Retry-After`; temporary outages pause requests. Automatic
attempts resume on the next scheduled update, subject to that pause.

Missing profiles or metrics are reported as partial coverage. The top lists are
computed from available profiles, so a partial scan is not a definitive guild top.
An API outage preserves the last snapshot with a stale/error notice. Source dates
are shown separately from the scan time; equipment may be older than the profile.
Mixed-season results suppress the M+ top until a consistent scan is available.
The snapshot and message ID survive restarts; the full roster is not stored on disk.
A deleted message is recreated at the next update. Publication failures are
available in `/dashboard status` and the bot log. Setup in the same channel reuses
the existing message; moving channels leaves the old message as a historical copy.

The data provider is [Raider.IO](https://raider.io/api). Its profiles may be delayed
or incomplete. Names and links use English bot labels; player names retain their
original spelling. Lists may be shortened to fit Discord's embed size limits.

## Gathering behavior

The **Joining** / **Not joining** buttons work after restarts. Only current
player-role members can vote. Repeated votes do not increase decline counts.
`/tag` continues the current gathering; its messages share votes, and voting
updates the clicked message. The latest 100 gathering messages accept votes.
Changing the role with `/setup` closes old gatherings. Off-roster `/tag` attempts
increase a personal counter without pinging players. Replies suppress mentions;
gathering invitations explicitly mention only players who have not joined.

`/cleanup` also deletes reaction role panels in the selected channel. Keep panels
in a separate channel that you do not clean with this command.

Deadlock statistics show the last 30 matches, rank, and most played hero.
`STEAM_API_KEY` is required only for Steam vanity links (`steamcommunity.com/id/...`).
Match history is cached for 15 minutes and ranks for one hour. API downtime does
not prevent gatherings. Ranks appear in `/me`; gatherings show display names.
Large lists are limited to the Discord message size.

## English rollout

Restart the bot to sync English command descriptions. Command names, persistent
button IDs, and the state format are unchanged. Existing messages keep their
previous text until updated; recreate reaction role panels with English text if
needed. User-supplied names and panel text are displayed as supplied.

## Checks

From the repository root with dependencies installed:

```bash
python -m unittest discover -s . -v
```

Tests do not connect to Discord or use live state. They cover persistence, server
isolation, concurrent votes, live role membership, cooldowns, closed gatherings,
reaction roles, and slash command registration.
Dashboard tests also cover API timeouts and throttling, cache expiry, partial
coverage, season changes, embed limits, message recreation, and shutdown.
OAuth tests mock Battle.net and callback requests: expiry/replay protection,
membership checks, concurrent claims, server isolation, main selection, unlinking,
token exchange, and error handling. A real Battle.net login and HTTPS callback
still require an integration check with your registered application.
Mythic+ tests cover role and score admission, experience, freshness, concurrent
joins, queue offers, explicit exceptions, changed rules/seasons, start rechecks,
unlink cleanup, persistent cards and publication failures. They do not call live
Discord or Raider.IO.

Library documentation: [intents](https://discordpy.readthedocs.io/en/stable/intents.html),
[raw reactions](https://discordpy.readthedocs.io/en/stable/api.html#discord.on_raw_reaction_add),
[persistent views](https://github.com/Rapptz/discord.py/blob/master/examples/views/persistent.py).
