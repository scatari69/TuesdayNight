import asyncio
import os
import re
import time
from collections import Counter

import aiohttp
import discord

STEAM_API_KEY = os.getenv("STEAM_API_KEY", "").strip()
STEAMID64_BASE = 76561197960265728
DEADLOCK_API = "https://api.deadlock-api.com"
API_TIMEOUT = 12
RECENT_MATCHES = 30
MH_CACHE_TTL = 900

_http_session = None
_asset_cache = {}   # "ranks"/"heroes" -> {id: name}
_mh_cache = {}      # account_id -> (expiry_ts, matches)


async def _get_session():
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(headers={"User-Agent": "napul-bot"})
    return _http_session


async def api_json(url: str, params: dict = None):
    """GET JSON; None при любой ошибке или не-200 — вызывающий решает, что сказать."""
    try:
        session = await _get_session()
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as r:
            if r.status != 200:
                return None
            return await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"Request failed {url}: {e}")
        return None


async def _load_asset_names(kind: str) -> dict:
    """kind: 'ranks' (tier->name) или 'heroes' (id->name). Кэшируется на весь процесс."""
    if kind not in _asset_cache:
        data = await api_json(f"{DEADLOCK_API}/v1/assets/{kind}")
        if not data:
            return {}
        key = "tier" if kind == "ranks" else "id"
        _asset_cache[kind] = {item[key]: item["name"] for item in data}
    return _asset_cache.get(kind, {})


async def resolve_account_id(text: str):
    """Ссылка Steam / tracklock / голый id -> (account_id, None) либо (None, код_ошибки)."""
    text = text.strip()

    m = re.search(r"tracklock\.gg/players/(\d+)", text)
    if m:
        return int(m.group(1)), None

    m = re.search(r"steamcommunity\.com/profiles/(\d{17})", text)
    if m:
        return int(m.group(1)) - STEAMID64_BASE, None

    m = re.search(r"steamcommunity\.com/id/([^/?\s]+)", text)
    if m:
        return await _resolve_vanity(m.group(1))

    if text.isdigit():
        n = int(text)
        # 17-значный SteamID64 приводим к account_id, короткое число — уже account_id
        return (n - STEAMID64_BASE if n > STEAMID64_BASE else n), None

    return None, "unknown"


async def _resolve_vanity(name: str):
    if not STEAM_API_KEY:
        return None, "no_key"
    data = await api_json(
        "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/",
        {"key": STEAM_API_KEY, "vanityurl": name})
    resp = (data or {}).get("response", {})
    if resp.get("success") == 1 and resp.get("steamid"):
        return int(resp["steamid"]) - STEAMID64_BASE, None
    return None, "vanity_not_found"


async def fetch_player_summary(account_id: int):
    """Дешевые вызовы: текущий ранг (mmr) и Steam-профиль. Любой может быть None."""
    mmr, steam = await asyncio.gather(
        api_json(f"{DEADLOCK_API}/v1/players/mmr", {"account_ids": str(account_id)}),
        api_json(f"{DEADLOCK_API}/v1/players/steam", {"account_ids": str(account_id)}),
    )
    return (mmr[0] if mmr else None), (steam[0] if steam else None)


_rank_cache = {}          # account_id -> (expiry_ts, rank_str или None)
RANK_CACHE_TTL = 3600     # ранг меняется медленно, держим час


async def get_rank_string(account_id: int):
    """Короткая строка ранга вида «Eternus 6» для списка сбора. Кэшируется."""
    now = time.time()
    cached = _rank_cache.get(account_id)
    if cached and cached[0] > now:
        return cached[1]

    mmr = await api_json(f"{DEADLOCK_API}/v1/players/mmr", {"account_ids": str(account_id)})
    rank_str = None
    if mmr and mmr[0].get("division") is not None:
        ranks = await _load_asset_names("ranks")
        div = mmr[0]["division"]
        name = ranks.get(div, f"Rank {div}")
        sub = mmr[0].get("division_tier", "")
        rank_str = f"{name} {sub}".strip()

    _rank_cache[account_id] = (now + RANK_CACHE_TTL, rank_str)
    return rank_str


async def fetch_recent_stats(account_id: int):
    """Винрейт/KDA/топ-герой по последним матчам. match-history тяжелый — кэшируем."""
    now = time.time()
    cached = _mh_cache.get(account_id)
    if cached and cached[0] > now:
        matches = cached[1]
    else:
        matches = await api_json(f"{DEADLOCK_API}/v1/players/{account_id}/match-history")
        if matches is None:
            return None
        _mh_cache[account_id] = (now + MH_CACHE_TTL, matches)

    last = matches[:RECENT_MATCHES]
    if not last:
        return None

    n = len(last)
    wins = sum(1 for m in last if m["match_result"] == m["player_team"])
    top_hero_id = Counter(m["hero_id"] for m in last).most_common(1)[0][0]
    return {
        "n": n,
        "winrate": wins / n * 100,
        "kills": sum(m["player_kills"] for m in last) / n,
        "deaths": sum(m["player_deaths"] for m in last) / n,
        "assists": sum(m["player_assists"] for m in last) / n,
        "top_hero_id": top_hero_id,
    }


async def render_deadlock_stats(account_id: int) -> str:
    """Блок статистики Deadlock для /me. Пустая строка, если совсем нет данных."""
    (mmr, steam), recent = await asyncio.gather(
        fetch_player_summary(account_id),
        fetch_recent_stats(account_id),
    )

    if not mmr and not steam and not recent:
        return ("\n\n🎮 **Deadlock:** no data available: the profile is private "
                "or Steam statistics are disabled.")

    lines = ["\n\n🎮 **Deadlock**"]

    if steam and steam.get("personaname"):
        lines.append(f"👤 {discord.utils.escape_markdown(steam['personaname'])}")

    if mmr and mmr.get("division") is not None:
        ranks = await _load_asset_names("ranks")
        rank_name = ranks.get(mmr["division"], f"Rank {mmr['division']}")
        subrank = mmr.get("division_tier", "")
        score = mmr.get("player_score")
        line = f"🏅 {discord.utils.escape_markdown(rank_name)} {subrank}".rstrip()
        if score is not None:
            line += f" ({round(score)})"
        lines.append(line)

    if recent:
        heroes = await _load_asset_names("heroes")
        hero = heroes.get(recent["top_hero_id"], f"#{recent['top_hero_id']}")
        lines.append(
            f"📈 Last {recent['n']} matches: win rate {recent['winrate']:.0f}%, "
            f"KDA {recent['kills']:.1f}/{recent['deaths']:.1f}/{recent['assists']:.1f}")
        lines.append(f"🦸 Most played hero: {discord.utils.escape_markdown(hero)}")

    if steam and steam.get("matches_played_last_30d") is not None:
        lines.append(f"🗓 Matches in the last 30 days: {steam['matches_played_last_30d']}")

    return "\n".join(lines)
