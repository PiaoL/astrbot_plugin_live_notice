from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

from .card_renderer import FONT_CACHE_PATH, render_notice_card


logger = logging.getLogger("astrbot")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
BILIBILI_API = "https://api.live.bilibili.com/room/v1/Room/get_info"
BILIBILI_ROOM_BY_UID_API = "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld"
BILIBILI_MASTER_INFO_API = "https://api.live.bilibili.com/live_user/v1/Master/info"
HUYA_API = "https://mp.huya.com/cache.php"
DOUYU_API = "https://www.douyu.com/betard/{room_id}"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_STREAMS_API = "https://api.twitch.tv/helix/streams"
TWITCH_USERS_API = "https://api.twitch.tv/helix/users"
KICK_TOKEN_URL = "https://id.kick.com/oauth/token"
KICK_CHANNELS_API = "https://api.kick.com/public/v1/channels"
KICK_LIVESTREAMS_API = "https://api.kick.com/public/v1/livestreams"
YOUTUBE_SEARCH_API = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_CHANNELS_API = "https://www.googleapis.com/youtube/v3/channels"

PLATFORM_ALIASES = {
    "bilibili": "bilibili", "bili": "bilibili", "b站": "bilibili", "哔哩哔哩": "bilibili",
    "huya": "huya", "虎牙": "huya",
    "douyu": "douyu", "斗鱼": "douyu",
    "twitch": "twitch",
    "kick": "kick",
    "youtube": "youtube", "yt": "youtube", "油管": "youtube",
}
PLATFORM_LABELS = {
    "bilibili": "哔哩哔哩", "huya": "虎牙", "douyu": "斗鱼",
    "twitch": "Twitch", "kick": "Kick", "youtube": "YouTube",
}
MAX_SUBSCRIPTIONS_PER_SESSION = 50
YOUTUBE_CACHE_SECONDS = 300
TWITCH_CACHE_SECONDS = 30
FONT_URL = "https://github.com/lxgw/LxgwWenKai-Screen/releases/download/v1.530/LXGWWenKaiScreen.ttf"
FONT_SIZE = 26_095_590
FONT_SHA256 = "abd13364163e3cd84372b333f2fdcee9efb848634668f1c0ce5788dcd059ee61"
FONT_RETRY_SECONDS = 900


@register(
    "astrbot_plugin_live_notice",
    "PiaoL",
    "统一订阅多个直播平台的开播提醒。",
    "1.3.2",
)
class LiveNoticePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._font_task: asyncio.Task | None = None
        self._font_retry_at = 0.0
        self._lock = asyncio.Lock()
        self._data_path = Path("data/plugin_data/astrbot_plugin_live_notice/subscriptions.json")
        self._subscriptions: dict[str, dict[str, dict[str, Any]]] = {}
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._avatar_cache: dict[str, tuple[float, str, str]] = {}
        self._twitch_token: str = ""
        self._twitch_token_expiry = 0.0
        self._kick_token: str = ""
        self._kick_token_expiry = 0.0
        self._load_subscriptions()

    async def initialize(self) -> None:
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.huya.com/"},
        )
        interval = self._check_interval()
        self._task = asyncio.create_task(self._monitor_loop(interval))
        self._ensure_default_font(self._config_value("font_path"))

    async def terminate(self) -> None:
        tasks = (self._task, self._font_task)
        for task in tasks:
            if task:
                task.cancel()
        for task in tasks:
            if not task:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.session and not self.session.closed:
            await self.session.close()

    def _ensure_default_font(self, configured: str) -> None:
        if self._font_task and not self._font_task.done():
            return
        if time.monotonic() < self._font_retry_at:
            return
        with suppress(OSError):
            if configured and Path(configured).is_file():
                return
        with suppress(OSError):
            if FONT_CACHE_PATH.stat().st_size == FONT_SIZE:
                return
        if not self.session or self.session.closed:
            return
        self._font_task = asyncio.create_task(self._download_default_font(self.session))

    async def _download_default_font(self, session: aiohttp.ClientSession) -> None:
        partial_path: Path | None = None
        try:
            FONT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            timeout = aiohttp.ClientTimeout(total=90, connect=10, sock_read=20)
            digest = hashlib.sha256()
            size = 0
            async with session.get(FONT_URL, timeout=timeout) as response:
                response.raise_for_status()
                with tempfile.NamedTemporaryFile(
                    dir=FONT_CACHE_PATH.parent, suffix=".tmp", delete=False
                ) as partial:
                    partial_path = Path(partial.name)
                    async for chunk in response.content.iter_chunked(128 * 1024):
                        size += len(chunk)
                        if size > FONT_SIZE:
                            raise ValueError("字体文件超出预期大小")
                        digest.update(chunk)
                        partial.write(chunk)
            if size != FONT_SIZE or digest.hexdigest() != FONT_SHA256:
                raise ValueError("字体文件校验失败")
            partial_path.replace(FONT_CACHE_PATH)
            logger.info("开播提醒已缓存霞鹜文楷屏幕版。")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
            self._font_retry_at = time.monotonic() + FONT_RETRY_SECONDS
            logger.warning("开播提醒下载默认图卡字体失败，将尝试使用系统字体：%s", exc)
        finally:
            if partial_path is not None:
                with suppress(OSError):
                    partial_path.unlink(missing_ok=True)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开播订阅")
    async def add_subscription(
        self, event: AstrMessageEvent, platform: str = "", identifier: str = "", identifier_kind: str = ""
    ):
        normalized = self._normalize_platform(platform)
        target = self._normalize_identifier(normalized, identifier) if normalized else None
        kind: str | None = None
        if normalized == "bilibili":
            normalized_kind = self._normalize_bilibili_identifier_kind(identifier_kind)
            if normalized_kind is False:
                yield event.plain_result("第三个参数请填写“房间号”或“UID”。")
                return
            kind = normalized_kind
        if not normalized or not target:
            yield event.plain_result(
                "用法：/开播订阅 <平台> <房间号/用户ID> [房间号|UID]\n"
                "支持：b站、虎牙、斗鱼、Twitch、Kick、YouTube。"
            )
            return
        if normalized in {"twitch", "kick", "youtube"} and not self._credentials_ready(normalized):
            yield event.plain_result(self._credentials_help(normalized))
            return

        if normalized == "bilibili":
            try:
                if kind:
                    info = await self._fetch_live(normalized, target, kind)
                    if not info:
                        kind_label = "房间号" if kind == "room" else "UID"
                        yield event.plain_result(f"按{kind_label}未查询到 B 站主播，未添加订阅。请检查数字后重试。")
                        return
                else:
                    info, kind, ambiguous = await self._resolve_bilibili_identifier(target)
                    if ambiguous:
                        yield event.plain_result(self._bilibili_ambiguity_message(target, ambiguous))
                        return
                    if not info or not kind:
                        yield event.plain_result("暂时无法确认这个 B 站数字对应的房间号或 UID，未添加订阅。请稍后重试，或明确加上“房间号”/“UID”。")
                        return
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError) as exc:
                logger.info("检查 B 站房间号/UID 冲突失败：%s (%s)", target, type(exc).__name__)
                yield event.plain_result("暂时无法同时核对 B 站房间号和 UID，未添加订阅以免选错。请稍后重试，或明确加上“房间号”/“UID”。")
                return
        else:
            info = None

        session_id = event.unified_msg_origin
        key = self._subscription_key(normalized, target, kind)
        error = ""
        async with self._lock:
            rooms = self._subscriptions.setdefault(session_id, {})
            if key in rooms:
                existing_name = str(rooms[key].get("name") or "主播信息暂不可用")
                error = (
                    f"已订阅{PLATFORM_LABELS[normalized]} {existing_name}"
                    f"（{self._identifier_summary(normalized, target, rooms[key])}）"
                    "已在当前会话订阅列表中。"
                )
            elif len(rooms) >= MAX_SUBSCRIPTIONS_PER_SESSION:
                error = f"当前会话最多订阅 {MAX_SUBSCRIPTIONS_PER_SESSION} 个主播。"
            else:
                rooms[key] = {"platform": normalized, "identifier": target, "live": False}
                if kind:
                    rooms[key]["identifier_kind"] = kind
                if info:
                    rooms[key].update(self._stored_info(info))
                    rooms[key]["live"] = bool(info.get("live"))
                    if info.get("live"):
                        rooms[key]["started_at"] = int(time.time())
                await self._save()
        if error:
            yield event.plain_result(error)
            return

        try:
            if info is None:
                info = await self._fetch_live(normalized, target, kind)
            if info:
                async with self._lock:
                    current = self._subscriptions.get(session_id, {}).get(key)
                    if current is not None:
                        current.update(self._stored_info(info))
                        current["live"] = bool(info.get("live"))
                        if info.get("live"):
                            current["started_at"] = int(time.time())
                        await self._save()
                status = "当前正在直播；下次开播时提醒。" if info["live"] else "当前未开播，开播时提醒。"
            else:
                status = "已添加，首次查询未返回资料；后台会继续检查。"
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            logger.info("首次查询直播间失败：%s %s (%s)", normalized, target, type(exc).__name__)
            status = "已添加，但首次查询失败；请检查平台凭据和网络，后台会继续检查。"
        async with self._lock:
            current = self._subscriptions.get(session_id, {}).get(key, {})
            nickname = str(current.get("name") or "主播信息暂不可用")
            id_summary = self._identifier_summary(normalized, target, current)
        yield event.plain_result(
            f"已订阅{PLATFORM_LABELS[normalized]} {nickname}（{id_summary}）。{status}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开播取消")
    async def remove_subscription(
        self, event: AstrMessageEvent, platform: str = "", identifier: str = "", identifier_kind: str = ""
    ):
        normalized = self._normalize_platform(platform)
        target = self._normalize_identifier(normalized, identifier) if normalized else None
        kind: str | None = None
        if normalized == "bilibili":
            normalized_kind = self._normalize_bilibili_identifier_kind(identifier_kind)
            if normalized_kind is False:
                yield event.plain_result("第三个参数请填写“房间号”或“UID”。")
                return
            kind = normalized_kind
        if not normalized or not target:
            yield event.plain_result("用法：/开播取消 <平台> <房间号/用户ID> [房间号|UID]")
            return
        rooms = self._subscriptions.get(event.unified_msg_origin, {})
        if normalized == "bilibili" and not kind:
            matches = [
                (key, item) for key, item in rooms.items()
                if item.get("platform") == normalized and item.get("identifier") == target
            ]
            if len(matches) > 1:
                by_kind = {str(item.get("identifier_kind")): item for _, item in matches}
                room_item = by_kind.get("room", matches[0][1])
                uid_item = by_kind.get("uid", matches[1][1])
                yield event.plain_result(
                    self._bilibili_ambiguity_message(target, (room_item, uid_item), "开播取消")
                )
                return
            if matches:
                key = matches[0][0]
            else:
                key = self._subscription_key(normalized, target)
        else:
            key = self._subscription_key(normalized, target, kind)
        removed = False
        removed_item: dict[str, Any] = {}
        async with self._lock:
            rooms = self._subscriptions.get(event.unified_msg_origin, {})
            if key in rooms:
                removed_item = dict(rooms[key])
                del rooms[key]
                removed = True
                if not rooms:
                    self._subscriptions.pop(event.unified_msg_origin, None)
                await self._save()
        if removed:
            id_summary = self._identifier_summary(normalized, target, removed_item)
            yield event.plain_result(f"已取消订阅{PLATFORM_LABELS[normalized]}（{id_summary}）。")
        else:
            id_summary = self._identifier_summary(normalized, target)
            yield event.plain_result(f"当前会话没有订阅{PLATFORM_LABELS[normalized]}（{id_summary}）。")

    @filter.command("开播列表")
    async def list_subscriptions(self, event: AstrMessageEvent, platform: str = ""):
        normalized = self._normalize_platform(platform) if platform else None
        rooms = self._subscriptions.get(event.unified_msg_origin, {})
        selected = [
            item for item in rooms.values()
            if normalized is None or item.get("platform") == normalized
        ]
        if not selected:
            yield event.plain_result("当前会话没有匹配的开播订阅。")
            return
        lines = ["当前会话的开播订阅："]
        for item in selected:
            platform_key = str(item.get("platform", ""))
            label = PLATFORM_LABELS.get(platform_key, platform_key)
            status = "直播中" if item.get("live") else "未开播/待检测"
            name = f" {item.get('name')}" if item.get("name") else ""
            id_summary = self._identifier_summary(platform_key, str(item.get("identifier", "")), item)
            lines.append(f"- {label} {name}（{id_summary}；{status}）")
        yield event.plain_result("\n".join(lines))

    @filter.command("开播查询")
    async def check_live(
        self, event: AstrMessageEvent, platform: str = "", identifier: str = "", identifier_kind: str = ""
    ):
        normalized = self._normalize_platform(platform)
        target = self._normalize_identifier(normalized, identifier) if normalized else None
        kind: str | None = None
        if normalized == "bilibili":
            normalized_kind = self._normalize_bilibili_identifier_kind(identifier_kind)
            if normalized_kind is False:
                yield event.plain_result("第三个参数请填写“房间号”或“UID”。")
                return
            kind = normalized_kind
        if not normalized or not target:
            yield event.plain_result("用法：/开播查询 <平台> <房间号/用户ID> [房间号|UID]")
            return
        if normalized in {"twitch", "kick", "youtube"} and not self._credentials_ready(normalized):
            yield event.plain_result(self._credentials_help(normalized))
            return
        try:
            if normalized == "bilibili" and not kind:
                info, kind, ambiguous = await self._resolve_bilibili_identifier(target)
                if ambiguous:
                    yield event.plain_result(self._bilibili_ambiguity_message(target, ambiguous, "开播查询"))
                    return
            else:
                info = await self._fetch_live(normalized, target, kind)
            if not info:
                yield event.plain_result("没能查询到该主播资料，请检查标识、凭据或平台 API 配额。")
                return
            status = "正在直播" if info["live"] else "当前未开播"
            text = (
                f"{PLATFORM_LABELS[normalized]}｜{info['name']}（{self._identifier_summary(normalized, target, info)}）：{status}\n"
                f"{info['title']}\n{info['url']}"
            )
            if info.get("cover"):
                yield event.chain_result([Plain(text), Image.fromURL(info["cover"])])
            else:
                yield event.plain_result(text)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            yield event.plain_result("暂时无法连接该平台，稍后再试。")
        except (ValueError, TypeError, KeyError):
            yield event.plain_result("平台返回的数据暂时无法解析，请查看日志后重试。")

    async def _monitor_loop(self, interval: int) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                await self._check_subscriptions()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("多平台开播提醒后台检查异常。")

    async def _check_subscriptions(self) -> None:
        async with self._lock:
            snapshot = [
                (session_id, key, dict(item))
                for session_id, rooms in self._subscriptions.items()
                for key, item in rooms.items()
            ]
        semaphore = asyncio.Semaphore(5)

        async def check_one(session_id: str, key: str, old: dict[str, Any]) -> None:
            platform = str(old.get("platform", ""))
            identifier = str(old.get("identifier", ""))
            if not platform or not identifier or (platform in {"twitch", "kick", "youtube"} and not self._credentials_ready(platform)):
                return
            try:
                async with semaphore:
                    info = await self._fetch_live(platform, identifier, old.get("identifier_kind"))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError) as exc:
                logger.debug("直播状态查询失败：%s %s (%s)", platform, identifier, type(exc).__name__)
                return
            if not info:
                return
            event_type: str | None = None
            notice_info = dict(info)
            async with self._lock:
                current = self._subscriptions.get(session_id, {}).get(key)
                if current is None:
                    return
                was_live = bool(current.get("live"))
                is_live = bool(info.get("live"))
                if is_live and not was_live:
                    event_type = "live"
                    current["started_at"] = int(time.time())
                elif was_live and not is_live:
                    event_type = "end"
                    notice_info = {**info, **current, "live": False}
                    notice_info["duration"] = max(0, int(time.time()) - int(current.get("started_at") or time.time()))
                if is_live or not was_live:
                    current.update(self._stored_info(info))
                current["live"] = is_live
                await self._save()
            if event_type:
                await self._send_notice(session_id, platform, identifier, notice_info, event_type)

        await asyncio.gather(*(check_one(session_id, key, item) for session_id, key, item in snapshot))

    async def _send_notice(
        self,
        session_id: str,
        platform: str,
        identifier: str,
        info: dict[str, Any],
        event_type: str,
    ) -> None:
        is_live = event_type == "live"
        name = str(info.get("name") or "主播").strip()
        generic_names = {
            f"B站房间 {identifier}", f"虎牙房间 {identifier}", f"斗鱼房间 {identifier}",
        }
        if name in generic_names:
            name = "主播"
        id_summary = self._identifier_summary(platform, identifier, info)
        subject = f"{name}（{PLATFORM_LABELS.get(platform, platform)} {id_summary}）"
        if is_live:
            text = (
                f"🔴 {subject} 开播啦！\n"
                f"📺 直播标题: {info['title']}\n"
                f"🔗 直播间: {info['url']}"
            )
        else:
            text = f"⚫ {subject} 已结束直播"
        card_path = await self._make_card(event_type, platform, identifier, info)
        chain_parts = [Plain(text)]
        if card_path:
            chain_parts.append(Image.fromFileSystem(str(card_path)))
        elif info.get("cover"):
            chain_parts.append(Image.fromURL(info["cover"]))
        chain = MessageChain(chain=chain_parts)
        try:
            await self.context.send_message(session_id, chain)
        except Exception:
            logger.exception("发送直播状态提醒失败：type=%s platform=%s identifier=%s", event_type, platform, identifier)

    async def _make_card(
        self, event_type: str, platform: str, identifier: str, info: dict[str, Any]
    ) -> Path | None:
        font_path = self._config_value("font_path")
        card_dir = Path("data/plugin_data/astrbot_plugin_live_notice/cards")
        try:
            avatar_bytes = await self._download_image(info.get("avatar", ""), platform)
            cover_bytes = await self._download_image(info.get("cover", ""), platform)
            if not cover_bytes:
                return None
            self._ensure_default_font(font_path)
            card_dir.mkdir(parents=True, exist_ok=True)
            output_path = card_dir / f"{event_type}-{platform}-{identifier}-{time.time_ns()}.jpg"
            await asyncio.to_thread(
                render_notice_card,
                event_type,
                info,
                avatar_bytes,
                cover_bytes,
                str(output_path),
                font_path or None,
            )
            return output_path
        except FileNotFoundError:
            logger.warning("未找到支持中文的字体；请在插件配置 font_path 中填写字体文件路径。")
        except Exception:
            logger.exception("生成直播通知卡片失败，将回退到原封面图发送。")
        return None

    async def _download_image(self, url: str, platform: str = "") -> bytes | None:
        if not url or not url.startswith(("http://", "https://")) or not self.session:
            return None
        try:
            referers = {
                "bilibili": "https://live.bilibili.com/",
                "huya": "https://www.huya.com/",
                "douyu": "https://www.douyu.com/",
                "twitch": "https://www.twitch.tv/",
                "kick": "https://kick.com/",
                "youtube": "https://www.youtube.com/",
            }
            headers = {"Referer": referers[platform]} if platform in referers else None
            if platform == "bilibili":
                headers["Origin"] = "https://live.bilibili.com"
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=10), headers=headers
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").lower()
                if not content_type.startswith("image/"):
                    return None
                max_bytes = 8 * 1024 * 1024
                if response.content_length is not None and response.content_length > max_bytes:
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks) if total else None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def _fetch_live(
        self, platform: str, identifier: str, identifier_kind: str | None = None
    ) -> dict[str, Any] | None:
        if not self.session or self.session.closed:
            return None
        cache_key = f"{platform}:{identifier}:{identifier_kind or 'auto'}"
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and now - cached[0] < self._cache_ttl(platform):
            return cached[1]
        if platform == "bilibili":
            info = await self._get_bilibili(identifier, identifier_kind)
        elif platform == "huya":
            info = await self._get_huya(identifier)
        elif platform == "douyu":
            info = await self._get_douyu(identifier)
        elif platform == "twitch":
            info = await self._get_twitch(identifier)
        elif platform == "kick":
            info = await self._get_kick(identifier)
        elif platform == "youtube":
            info = await self._get_youtube(identifier)
        else:
            return None
        self._cache[cache_key] = (time.monotonic(), info)
        return info

    async def _resolve_bilibili_identifier(
        self, identifier: str
    ) -> tuple[dict[str, Any] | None, str | None, tuple[dict[str, Any], dict[str, Any]] | None]:
        room_info = await self._fetch_live("bilibili", identifier, "room")
        uid_info = await self._fetch_live("bilibili", identifier, "uid")
        if room_info and uid_info:
            same_anchor = bool(room_info.get("uid") and room_info.get("uid") == uid_info.get("uid"))
            same_room = bool(room_info.get("room_id") and room_info.get("room_id") == uid_info.get("room_id"))
            if not (same_anchor or same_room):
                return None, None, (room_info, uid_info)
            return room_info, "room", None
        if room_info:
            return room_info, "room", None
        if uid_info:
            return uid_info, "uid", None
        return None, None, None

    def _bilibili_ambiguity_message(
        self,
        identifier: str,
        candidates: tuple[dict[str, Any], dict[str, Any]],
        command: str = "开播订阅",
    ) -> str:
        room_info, uid_info = candidates
        return (
            f"数字 {identifier} 同时对应两个不同的 B 站主播，本次未执行。\n"
            f"按房间号：{room_info.get('name', '主播')}（房间号：{room_info.get('room_id')}，UID：{room_info.get('uid')}）\n"
            f"命令：/{command} bilibili {identifier} 房间号\n"
            f"按 UID：{uid_info.get('name', '主播')}（房间号：{uid_info.get('room_id')}，UID：{uid_info.get('uid')}）\n"
            f"命令：/{command} bilibili {identifier} UID"
        )

    @staticmethod
    def _normalize_bilibili_identifier_kind(value: str) -> str | None | bool:
        normalized = (value or "").strip().lower()
        if not normalized:
            return None
        if normalized in {"room", "roomid", "room_id", "房间", "房间号"}:
            return "room"
        if normalized in {"uid", "主播uid", "用户uid"}:
            return "uid"
        return False

    @staticmethod
    def _subscription_key(platform: str, identifier: str, identifier_kind: str | None = None) -> str:
        if platform == "bilibili" and identifier_kind in {"room", "uid"}:
            return f"bilibili:{identifier_kind}:{identifier}"
        return f"{platform}:{identifier}"

    async def _get_bilibili(self, identifier: str, identifier_kind: str | None = None) -> dict[str, Any] | None:
        headers = {"Referer": "https://live.bilibili.com/", "Origin": "https://live.bilibili.com"}
        uid_room: dict[str, Any] = {}
        data: dict[str, Any] | None = None
        if identifier_kind != "uid":
            payload = await self._get_json(
                BILIBILI_API, {"room_id": identifier}, headers=headers
            )
            candidate_data = payload.get("data")
            if isinstance(candidate_data, dict) and "live_status" in candidate_data:
                data = candidate_data
        if data is None and identifier_kind != "room":
            # Bilibili accepts both room IDs and anchor UIDs; explicitly selected
            # kinds avoid silently choosing the wrong streamer when they collide.
            uid_payload = await self._get_json(
                BILIBILI_ROOM_BY_UID_API, {"mid": identifier}, headers=headers
            )
            candidate = uid_payload.get("data")
            if not isinstance(candidate, dict) or str(candidate.get("roomStatus")) != "1":
                return None
            uid_room = candidate
            resolved_room_id = str(candidate.get("roomid") or candidate.get("room_id") or "")
            if not resolved_room_id:
                return None
            payload = await self._get_json(
                BILIBILI_API, {"room_id": resolved_room_id}, headers=headers
            )
            detail_data = payload.get("data")
            if isinstance(detail_data, dict) and "live_status" in detail_data:
                data = detail_data
            else:
                data = {
                    "uid": identifier,
                    "room_id": resolved_room_id,
                    "live_status": candidate.get("liveStatus", 0),
                    "title": candidate.get("title"),
                    "user_cover": candidate.get("cover"),
                }
        if data is None:
            return None
        live = str(data.get("live_status", uid_room.get("liveStatus", 0))) == "1"
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        avatar = self._avatar_url(owner, data.get("avatar"), data.get("face"))
        name = data.get("uname") or ""
        uid = data.get("uid") or (identifier if uid_room else None)
        if (not avatar or not name) and uid:
            try:
                profile_name, profile_avatar = await self._get_bilibili_profile(str(uid))
                name = name or profile_name
                avatar = avatar or profile_avatar
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError):
                logger.debug("B站主播头像补查失败：uid=%s", uid)
        return self._info(
            live, name or f"B站房间 {identifier}", data.get("title") or "哔哩哔哩直播",
            f"https://live.bilibili.com/{data.get('room_id') or uid_room.get('roomid') or identifier}",
            data.get("user_cover") or data.get("keyframe") or uid_room.get("cover"),
            avatar=avatar,
            room_id=data.get("room_id") or uid_room.get("roomid") or identifier,
            uid=uid,
            category=" / ".join(
                str(value) for value in (data.get("parent_area_name"), data.get("area_name")) if value
            ),
            description=data.get("description"),
        )

    async def _get_huya(self, room_id: str) -> dict[str, Any] | None:
        payload = await self._get_json(
            HUYA_API, {"m": "Live", "do": "profileRoom", "roomid": room_id}
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        live_data = data.get("liveData") if isinstance(data.get("liveData"), dict) else {}
        profile = data.get("profileInfo") if isinstance(data.get("profileInfo"), dict) else {}
        status = data.get("realLiveStatus", data.get("liveStatus", live_data.get("liveStatus")))
        if status is None:
            return None
        live = str(status).lower() in {"1", "true", "on", "live", "living"}
        return self._info(
            live,
            profile.get("nick") or profile.get("nickName") or data.get("nick") or f"虎牙房间 {room_id}",
            live_data.get("introduction") or live_data.get("title") or data.get("introduction") or "虎牙直播",
            f"https://www.huya.com/{room_id}",
            live_data.get("screenshot") or live_data.get("cover") or data.get("screenshot"),
            avatar=self._avatar_url(profile, live_data, data),
            category=(
                live_data.get("gameFullName") or live_data.get("gameHostName")
                or live_data.get("gameName") or data.get("gameName") or data.get("game_name")
            ),
            description=live_data.get("contentIntro") or data.get("contentIntro"),
        )

    async def _get_douyu(self, room_id: str) -> dict[str, Any] | None:
        payload = await self._get_json(DOUYU_API.format(room_id=room_id))
        room = payload.get("room") or payload.get("data")
        if not isinstance(room, dict):
            return None
        status = room.get("show_status", room.get("room_status", room.get("showStatus")))
        if status is None:
            return None
        live = str(status).lower() in {"1", "true", "live", "living"}
        return self._info(
            live, room.get("nickname") or room.get("owner_name") or f"斗鱼房间 {room_id}",
            room.get("room_name") or room.get("roomName") or "斗鱼直播",
            f"https://www.douyu.com/{room_id}", room.get("room_src") or room.get("coverSrc"),
            avatar=self._avatar_url(
                room.get("avatar"), room.get("avatar_mid"), room.get("avatarUrl"),
                room.get("owner_avatar"), room.get("face"),
            ),
            category=room.get("cate_name") or room.get("game_name") or room.get("cate2_name"),
            description=(room.get("room_intro") or room.get("room_desc") or room.get("room_introduction") or ""),
        )

    async def _get_twitch_token(self) -> str:
        now = time.monotonic()
        if self._twitch_token and now < self._twitch_token_expiry:
            return self._twitch_token
        client_id = self._config_value("twitch_client_id")
        client_secret = self._config_value("twitch_client_secret")
        payload = await self._post_json(
            TWITCH_TOKEN_URL,
            data={"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"},
        )
        self._twitch_token = str(payload["access_token"])
        self._twitch_token_expiry = now + int(payload.get("expires_in", 3600)) - 60
        return self._twitch_token

    async def _get_twitch(self, login: str) -> dict[str, Any] | None:
        token = await self._get_twitch_token()
        client_id = self._config_value("twitch_client_id")
        headers = {"Authorization": f"Bearer {token}", "Client-Id": client_id}
        user_payload = await self._get_json(TWITCH_USERS_API, {"login": login}, headers=headers)
        user_rows = user_payload.get("data")
        user = user_rows[0] if isinstance(user_rows, list) and user_rows else {}
        payload = await self._get_json(
            TWITCH_STREAMS_API, {"user_login": login},
            headers=headers,
        )
        rows = payload.get("data")
        if not isinstance(rows, list):
            return None
        if not rows:
            return self._info(
                False, user.get("display_name") or login, user.get("description") or "Twitch",
                f"https://www.twitch.tv/{login}", avatar=self._avatar_url(user), category="Twitch",
                description=user.get("description"),
            )
        data = rows[0]
        cover = str(data.get("thumbnail_url", "")).replace("{width}", "640").replace("{height}", "360")
        return self._info(
            True, data.get("user_name") or user.get("display_name") or login,
            data.get("title") or "Twitch直播", f"https://www.twitch.tv/{login}", cover,
            avatar=self._avatar_url(user), category=data.get("game_name") or "Twitch",
            description=user.get("description"),
        )

    async def _get_kick_token(self) -> str:
        now = time.monotonic()
        if self._kick_token and now < self._kick_token_expiry:
            return self._kick_token
        payload = await self._post_json(
            KICK_TOKEN_URL,
            data={
                "client_id": self._config_value("kick_client_id"),
                "client_secret": self._config_value("kick_client_secret"),
                "grant_type": "client_credentials",
            },
        )
        self._kick_token = str(payload["access_token"])
        self._kick_token_expiry = now + int(payload.get("expires_in", 3600)) - 60
        return self._kick_token

    async def _get_kick(self, slug: str) -> dict[str, Any] | None:
        token = await self._get_kick_token()
        payload = await self._get_json(
            KICK_CHANNELS_API, {"slug": slug},
            headers={"Authorization": f"Bearer {token}", "Client-Id": self._config_value("kick_client_id")},
        )
        rows = payload.get("data")
        if not isinstance(rows, list):
            return None
        if not rows:
            return None
        data = rows[0]
        stream = data.get("stream") if isinstance(data.get("stream"), dict) else {}
        live = bool(stream.get("is_live", data.get("is_live", False)))
        broadcaster = data.get("broadcaster_user")
        if isinstance(broadcaster, dict):
            broadcaster_name = broadcaster.get("username") or broadcaster.get("name")
        else:
            broadcaster_name = broadcaster
        info = self._info(
            live, broadcaster_name or data.get("slug") or slug,
            stream.get("title") or data.get("stream_title") or "Kick直播",
            f"https://kick.com/{slug}", stream.get("thumbnail") or data.get("banner_picture"),
            avatar=self._avatar_url(broadcaster, data.get("profile_picture"), data.get("profile_pic")),
            category=(
                (stream.get("category") or data.get("category") or {}).get("name", "Kick")
                if isinstance(stream.get("category") or data.get("category"), dict)
                else (stream.get("category") or data.get("category") or "Kick")
            ),
            description=data.get("channel_description") or data.get("description") or "",
        )

        # Kick's channels endpoint may omit the broadcaster avatar; live objects
        # expose profile_picture, so use it as a best-effort fallback while live.
        if live and not info.get("avatar") and data.get("broadcaster_user_id"):
            try:
                live_payload = await self._get_json(
                    KICK_LIVESTREAMS_API,
                    {"broadcaster_user_id": data["broadcaster_user_id"]},
                    headers={"Authorization": f"Bearer {token}", "Client-Id": self._config_value("kick_client_id")},
                )
                live_rows = live_payload.get("data")
                if isinstance(live_rows, list) and live_rows:
                    info["avatar"] = self._avatar_url(live_rows[0])
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError):
                logger.debug("Kick 直播头像补查失败：%s", slug)
        return info

    async def _get_youtube(self, channel_id: str) -> dict[str, Any] | None:
        payload = await self._get_json(
            YOUTUBE_SEARCH_API,
            {
                "part": "snippet", "channelId": channel_id, "eventType": "live",
                "type": "video", "maxResults": 1, "key": self._config_value("youtube_api_key"),
            },
        )
        rows = payload.get("items")
        if not isinstance(rows, list):
            return None
        if not rows:
            return self._info(False, channel_id, "YouTube", f"https://www.youtube.com/channel/{channel_id}")
        item = rows[0]
        snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
        video_id = item.get("id", {}).get("videoId") if isinstance(item.get("id"), dict) else ""
        thumbnails = snippet.get("thumbnails", {})
        thumb = thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}
        channel_payload = await self._get_json(
            YOUTUBE_CHANNELS_API,
            {"part": "snippet", "id": channel_id, "key": self._config_value("youtube_api_key")},
        )
        channel_rows = channel_payload.get("items")
        channel_snippet = channel_rows[0].get("snippet", {}) if isinstance(channel_rows, list) and channel_rows else {}
        channel_thumbnails = channel_snippet.get("thumbnails", {})
        avatar = self._avatar_url(channel_thumbnails.get("high"), channel_thumbnails.get("default"))
        return self._info(
            True, snippet.get("channelTitle") or channel_id, snippet.get("title") or "YouTube直播",
            f"https://www.youtube.com/watch?v={video_id}" if video_id else f"https://www.youtube.com/channel/{channel_id}/live",
            thumb.get("url"), avatar=avatar, category="YouTube",
            description=channel_snippet.get("description"),
        )

    async def _get_json(
        self, url: str, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.session:
            raise RuntimeError("HTTP session is not initialized")
        async with self.session.get(url, params=params, headers=headers) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        return payload

    async def _post_json(self, url: str, data: dict[str, Any]) -> dict[str, Any]:
        if not self.session:
            raise RuntimeError("HTTP session is not initialized")
        async with self.session.post(url, data=data) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        return payload

    @staticmethod
    def _info(
        live: bool, name: Any, title: Any, url: str, cover: Any = "",
        avatar: Any = "", category: Any = "", duration: int = 0, description: Any = "",
        room_id: Any = "", uid: Any = "",
    ) -> dict[str, Any]:
        return {
            "live": bool(live), "name": str(name or "主播"), "title": str(title or "直播中"),
            "url": url, "cover": str(cover or ""), "avatar": str(avatar or ""),
            "category": str(category or ""), "duration": int(duration or 0),
            "description": str(description or "").strip(), "room_id": str(room_id or ""),
            "uid": str(uid or ""),
        }

    async def _get_bilibili_profile(self, uid: str) -> tuple[str, str]:
        now = time.monotonic()
        cached = self._avatar_cache.get(uid)
        if cached and now - cached[0] < 12 * 60 * 60:
            return cached[1], cached[2]
        payload = await self._get_json(
            BILIBILI_MASTER_INFO_API,
            {"uid": uid},
            headers={"Referer": "https://live.bilibili.com/", "Origin": "https://live.bilibili.com"},
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        name = str(info.get("uname") or "")
        avatar = self._avatar_url(info)
        self._avatar_cache[uid] = (now, name, avatar)
        return name, avatar

    @staticmethod
    def _avatar_url(*values: Any) -> str:
        """Extract profile image URLs from platform-specific or nested fields."""
        keys = (
            "face", "avatar180", "avatar", "avatarUrl", "avatar_url", "avatar_mid",
            "profile_image_url", "profile_picture", "profile_pic", "head_url",
            "big", "mid", "small", "url",
        )
        for value in values:
            if isinstance(value, str):
                url = value.strip()
                if url.startswith("//"):
                    return "https:" + url
                if url.startswith(("http://", "https://")):
                    return url
            elif isinstance(value, dict):
                for key in keys:
                    if key in value:
                        found = LiveNoticePlugin._avatar_url(value[key])
                        if found:
                            return found
            elif isinstance(value, (list, tuple)):
                found = LiveNoticePlugin._avatar_url(*value)
                if found:
                    return found
        return ""

    @staticmethod
    def _identifier_summary(
        platform: str, identifier: str, info: dict[str, Any] | None = None
    ) -> str:
        info = info or {}
        if platform == "bilibili":
            room_id = str(info.get("room_id") or "")
            uid = str(info.get("uid") or "")
            if room_id and not uid and identifier and identifier != room_id:
                uid = identifier
            elif uid and not room_id and identifier and identifier != uid:
                room_id = identifier
            if room_id and uid:
                return f"房间号：{room_id}，UID：{uid}"
            if room_id:
                return f"房间号：{room_id}，UID：未获取"
            if uid:
                return f"房间号：未获取，UID：{uid}"
            return f"房间号/UID：{identifier}"
        if platform in {"huya", "douyu"}:
            return f"房间号：{identifier}"
        if platform in {"twitch", "kick"}:
            return f"频道名：{identifier}"
        if platform == "youtube":
            return f"频道 ID：{identifier}"
        return f"ID：{identifier}"

    @staticmethod
    def _stored_info(info: dict[str, Any]) -> dict[str, Any]:
        return {key: info.get(key) for key in ("name", "title", "url", "cover", "avatar", "category", "description", "room_id", "uid") if info.get(key) is not None}

    @staticmethod
    def _normalize_platform(value: str) -> str | None:
        return PLATFORM_ALIASES.get((value or "").strip().lower())

    @staticmethod
    def _normalize_identifier(platform: str | None, value: str) -> str | None:
        value = (value or "").strip()
        if not value:
            return None
        if value.startswith("http://") or value.startswith("https://"):
            parsed = urlparse(value)
            host = (parsed.hostname or "").lower()
            path = parsed.path.strip("/").split("/")
            if platform == "bilibili" and host.endswith("bilibili.com") and path:
                return path[-1] if path[-1].isdigit() else None
            if platform == "huya" and host.endswith("huya.com") and path and path[0].isdigit():
                return path[0]
            if platform == "douyu" and host.endswith("douyu.com") and path and path[0].isdigit():
                return path[0]
            if platform in {"twitch", "kick"} and path:
                return path[0]
            if platform == "youtube" and "youtube.com" in host:
                if "channel" in path:
                    pos = path.index("channel")
                    return path[pos + 1] if len(path) > pos + 1 else None
                if "live" in path:
                    return path[path.index("live") + 1] if len(path) > path.index("live") + 1 else None
            return None
        return value

    def _config_value(self, key: str) -> str:
        try:
            return str(self.config.get(key, "") or "").strip()
        except Exception:
            return ""

    def _credentials_ready(self, platform: str) -> bool:
        required = {
            "twitch": ("twitch_client_id", "twitch_client_secret"),
            "kick": ("kick_client_id", "kick_client_secret"),
            "youtube": ("youtube_api_key",),
        }.get(platform, ())
        return all(self._config_value(key) for key in required)

    @staticmethod
    def _credentials_help(platform: str) -> str:
        return {
            "twitch": "先在插件配置中填写 Twitch Client ID 和 Client Secret，再重试。",
            "kick": "先在插件配置中填写 Kick Client ID 和 Client Secret，再重试。",
            "youtube": "先在插件配置中填写 YouTube Data API Key，再重试。",
        }[platform]

    def _check_interval(self) -> int:
        try:
            value = int(self.config.get("check_interval", 60))
        except (TypeError, ValueError):
            value = 60
        return max(30, min(value, 1800))

    @staticmethod
    def _cache_ttl(platform: str) -> int:
        return YOUTUBE_CACHE_SECONDS if platform == "youtube" else TWITCH_CACHE_SECONDS

    def _load_subscriptions(self) -> None:
        try:
            source_path = self._data_path
            if not source_path.exists():
                old_path = Path(
                    "data/plugin_data/astrbot_plugin_huya_live_notice/subscriptions.json"
                )
                if old_path.exists():
                    source_path = old_path
            saved = json.loads(source_path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                return
            for session_id, entries in saved.items():
                if not isinstance(entries, dict):
                    continue
                self._subscriptions[str(session_id)] = {}
                for key, value in entries.items():
                    if not isinstance(value, dict):
                        continue
                    # Migrate the earlier Huya-only package's session -> room -> state layout.
                    if ":" not in str(key) and str(key).isdigit():
                        platform, identifier, new_key = "huya", str(key), f"huya:{key}"
                        item = {"platform": platform, "identifier": identifier, **value}
                    else:
                        key_text = str(key)
                        key_parts = key_text.split(":")
                        platform = key_parts[0]
                        if platform == "bilibili" and len(key_parts) == 3 and key_parts[1] in {"room", "uid"}:
                            identifier_kind, identifier = key_parts[1], key_parts[2]
                            sep = ":"
                        else:
                            identifier_kind = None
                            platform, sep, identifier = key_text.partition(":")
                        if not sep or platform not in PLATFORM_LABELS:
                            continue
                        item = {"platform": platform, "identifier": identifier, **value}
                        if platform == "bilibili":
                            kind = item.get("identifier_kind") or identifier_kind
                            if kind not in {"room", "uid"}:
                                if str(item.get("room_id") or "") == identifier:
                                    kind = "room"
                                elif str(item.get("uid") or "") == identifier:
                                    kind = "uid"
                            if kind in {"room", "uid"}:
                                item["identifier_kind"] = kind
                            new_key = self._subscription_key(platform, identifier, kind)
                        else:
                            new_key = str(key)
                    self._subscriptions[str(session_id)][new_key] = item
        except (OSError, ValueError, TypeError):
            logger.info("多平台开播提醒：未找到可读取的订阅数据，将从空列表开始。")

    async def _save(self) -> None:
        self._data_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._data_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(self._subscriptions, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self._data_path)
