"""群聊静音插件：自动发现群聊，并让机器人在选定群聊里完全不出声。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import json_response, request

PLUGIN_NAME = "astrbot_plugin_group_mute"
_STATE_FILE = "state.json"
_SAVE_INTERVAL_SECONDS = 20.0

_ACTIVE: "GroupMutePlugin | None" = None


def _group_id_of(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_group_id() or "").strip().lstrip("#")
    except Exception:
        return ""


def _group_name_of(event: AstrMessageEvent) -> str:
    try:
        group = getattr(event.message_obj, "group", None)
        name = getattr(group, "group_name", "") if group is not None else ""
        return str(name or "").strip()
    except Exception:
        return ""


def _split_ids(value: Any) -> list[str]:
    """Accept comma/newline separated group ids from the plugin config."""

    raw = str(value or "").replace("，", ",").replace("\r", "")
    items: list[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        item = chunk.strip().lstrip("#")
        if item:
            items.append(item)
    return items


class _MutedGroupFilter(CustomFilter):
    """Activate the guard only for muted groups, so other chats stay untouched."""

    def filter(self, event: AstrMessageEvent, _config: Any) -> bool:
        plugin = _ACTIVE
        if plugin is None:
            return False
        return plugin.is_muted_group(event)


class GroupMutePlugin(Star):
    """Silence the bot in selected group chats."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._data_dir: Path | None = None
        self._lock = asyncio.Lock()
        self._muted: set[str] = set()
        self._groups: dict[str, dict[str, Any]] = {}
        self._last_save = 0.0
        self._initialized = False

    async def initialize(self) -> None:
        global _ACTIVE
        if self._initialized:
            return
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = data_dir
        await self._load_state()
        self._muted.update(_split_ids(self.config.get("muted_groups", "")))
        self._register_routes()
        _ACTIVE = self
        self._initialized = True
        logger.info(
            "group-mute | 已就绪 | 已知群 %d 个 | 静音群 %d 个",
            len(self._groups),
            len(self._muted),
        )

    async def terminate(self) -> None:
        global _ACTIVE
        self._unregister_routes()
        await self._save_state()
        if _ACTIVE is self:
            _ACTIVE = None
        self._initialized = False

    # ------------------------------------------------------------------ 状态

    def _state_path(self) -> Path | None:
        return None if self._data_dir is None else self._data_dir / _STATE_FILE

    async def _load_state(self) -> None:
        path = self._state_path()
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("group-mute | 状态文件解析失败，已忽略", exc_info=True)
            return
        muted = payload.get("muted")
        if isinstance(muted, list):
            self._muted = {str(item).strip() for item in muted if str(item).strip()}
        groups = payload.get("groups")
        if isinstance(groups, dict):
            self._groups = {
                str(key): dict(value)
                for key, value in groups.items()
                if isinstance(value, dict)
            }

    async def _save_state(self) -> None:
        path = self._state_path()
        if path is None:
            return
        async with self._lock:
            payload = {
                "muted": sorted(self._muted),
                "groups": self._groups,
            }
            try:
                temp = path.with_suffix(".tmp")
                temp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                temp.replace(path)
                self._last_save = time.time()
            except Exception:
                logger.warning("group-mute | 状态保存失败", exc_info=True)

    def is_muted_group(self, event: AstrMessageEvent) -> bool:
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False
        group_id = _group_id_of(event)
        if not group_id or group_id not in self._muted:
            return False
        if self.config.get("reply_when_mentioned", False) and event.is_at_or_wake_command:
            return False
        return True

    # ------------------------------------------------------------------ 钩子

    @filter.custom_filter(_MutedGroupFilter)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def mute_guard(self, event: AstrMessageEvent) -> None:
        """在静音群里直接终止事件，连 LLM 都不会被调用。"""

        text = (event.message_str or "").strip()
        if text.startswith(("群静音", "静音列表", "群聊列表")):
            # 允许在静音群里用指令解除静音
            return
        logger.debug("group-mute | 拦截静音群消息 | group=%s", _group_id_of(event))
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9990)
    async def record_group(self, event: AstrMessageEvent) -> None:
        """被动收集群号与群名，供页面列出可选的群聊。"""

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return
        if not self.config.get("record_groups", True):
            return
        group_id = _group_id_of(event)
        if not group_id:
            return
        name = _group_name_of(event)
        now = time.time()
        info = self._groups.get(group_id) or {}
        if name and info.get("name") != name:
            info["name"] = name
        info["last_seen"] = now
        info["messages"] = int(info.get("messages") or 0) + 1
        self._groups[group_id] = info
        if now - self._last_save > _SAVE_INTERVAL_SECONDS:
            await self._save_state()

    # ------------------------------------------------------------------ 指令

    @filter.command("群静音")
    async def cmd_toggle_mute(self, event: AstrMessageEvent):
        """切换当前群的静音状态。"""
        group_id = _group_id_of(event)
        if not group_id:
            yield event.plain_result("这个指令只能在群聊里用。")
            return
        if group_id in self._muted:
            self._muted.discard(group_id)
            await self._save_state()
            yield event.plain_result(f"已取消静音，本群（{group_id}）会恢复正常回复。")
            return
        self._muted.add(group_id)
        await self._save_state()
        yield event.plain_result(f"已静音，本群（{group_id}）不会再听到我说话。")

    @filter.command("静音列表")
    async def cmd_list_muted(self, event: AstrMessageEvent):
        """列出所有被静音的群。"""
        if not self._muted:
            yield event.plain_result("目前没有静音的群。")
            return
        lines = ["当前静音的群："]
        for group_id in sorted(self._muted):
            name = (self._groups.get(group_id) or {}).get("name") or "未知群名"
            lines.append(f"- {name}（{group_id}）")
        yield event.plain_result("\n".join(lines))

    @filter.command("群聊列表")
    async def cmd_list_groups(self, event: AstrMessageEvent):
        """列出机器人已经见过的群聊。"""
        if not self._groups:
            yield event.plain_result("还没记录到群聊，等我收到群消息后再试。")
            return
        rows = sorted(
            self._groups.items(),
            key=lambda item: float(item[1].get("last_seen") or 0),
            reverse=True,
        )
        lines = ["我见过的群聊："]
        for group_id, info in rows[:30]:
            name = info.get("name") or "未知群名"
            flag = "🔇" if group_id in self._muted else "🔊"
            lines.append(f"{flag} {name}（{group_id}）")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ Web API

    def _register_routes(self) -> None:
        prefix = f"/{PLUGIN_NAME}"
        self.context.register_web_api(
            f"{prefix}/groups",
            self.api_groups,
            ["GET"],
            "群聊静音：列出已知群聊",
        )
        self.context.register_web_api(
            f"{prefix}/mute",
            self.api_set_mute,
            ["POST"],
            "群聊静音：设置静音状态",
        )

    def _unregister_routes(self) -> None:
        routes = self.context.registered_web_apis
        routes[:] = [
            route for route in routes if not str(route[0]).startswith(f"/{PLUGIN_NAME}/")
        ]

    async def api_groups(self):
        items: list[dict[str, Any]] = []
        for group_id, info in self._groups.items():
            items.append(
                {
                    "group_id": group_id,
                    "name": info.get("name") or "",
                    "muted": group_id in self._muted,
                    "messages": int(info.get("messages") or 0),
                    "last_seen": int(info.get("last_seen") or 0),
                }
            )
        for group_id in self._muted:
            if group_id not in self._groups:
                items.append(
                    {
                        "group_id": group_id,
                        "name": "",
                        "muted": True,
                        "messages": 0,
                        "last_seen": 0,
                    }
                )
        items.sort(key=lambda row: (not row["muted"], -row["last_seen"]))
        return json_response(
            {"groups": items, "total": len(items), "muted": len(self._muted)}
        )

    async def api_set_mute(self):
        payload = await request.json(default={}) or {}
        if not isinstance(payload, dict):
            return json_response({"status": "error", "message": "请求体格式错误"}, status_code=400)
        group_id = str(payload.get("group_id") or "").strip().lstrip("#")
        muted = bool(payload.get("muted"))
        if not group_id:
            return json_response({"status": "error", "message": "缺少群号"}, status_code=400)
        if muted:
            self._muted.add(group_id)
        else:
            self._muted.discard(group_id)
        await self._save_state()
        logger.info("group-mute | 页面设置 | group=%s muted=%s", group_id, muted)
        return json_response({"group_id": group_id, "muted": muted})
