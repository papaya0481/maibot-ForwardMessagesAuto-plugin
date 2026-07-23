"""麦麦自动跨群转发插件。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Iterable

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

PLUGIN_VERSION = "0.1.0"
CONFIG_VERSION = PLUGIN_VERSION
FORWARD_TOOL_NAME = "request_cross_group_forward"
VIEW_FORWARD_TOOL_NAME = "view_forward_message"

_STAGE_RANK = {
    "pending": 0,
    "sent": 1,
    "context_appended": 2,
    "planner_queued": 3,
}


@dataclass(slots=True)
class ViewCacheEntry:
    """一次 view_forward_message 的展开结果。"""

    content: str
    cached_at: float


@dataclass(slots=True)
class ForwardJob:
    """后台转发任务快照。"""

    job_id: str
    state_key: str
    source_stream_id: str
    source_group_id: str
    source_message_id: str
    target_group_ids: list[str]
    forward_segment: dict[str, Any]
    forward_messages: list[dict[str, Any]]
    expanded_content: str
    sharing_reason: str


def normalize_group_ids(values: Iterable[Any]) -> list[str]:
    """清洗群号并保持首次出现顺序。"""

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        group_id = str(value or "").strip()
        if not group_id or group_id in seen:
            continue
        seen.add(group_id)
        result.append(group_id)
    return result


def extract_forward_payload(message: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """从 SDK 消息字典提取 Host forward 消息段和发送节点。"""

    raw_message = message.get("raw_message")
    if not isinstance(raw_message, list):
        return None

    for segment in raw_message:
        if not isinstance(segment, dict) or str(segment.get("type") or "").strip().lower() != "forward":
            continue
        raw_nodes = segment.get("data")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            continue

        normalized_nodes: list[dict[str, Any]] = []
        forward_messages: list[dict[str, Any]] = []
        for index, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, dict):
                continue
            raw_content = raw_node.get("content")
            if not isinstance(raw_content, list):
                continue
            content = [dict(item) for item in raw_content if isinstance(item, dict)]
            if not content:
                continue

            node = {
                "user_id": str(raw_node.get("user_id") or ""),
                "user_nickname": str(raw_node.get("user_nickname") or "未知用户"),
                "user_cardname": str(raw_node.get("user_cardname") or ""),
                "message_id": str(raw_node.get("message_id") or f"forward_node_{index}"),
                "content": content,
            }
            normalized_nodes.append(node)
            forward_messages.append(
                {
                    "user_id": node["user_id"],
                    "nickname": node["user_nickname"],
                    "user_cardname": node["user_cardname"],
                    "message_id": node["message_id"],
                    "segments": content,
                }
            )

        if normalized_nodes:
            return {"type": "forward", "data": normalized_nodes}, forward_messages
    return None


def extract_view_forward_results(messages: Any) -> list[tuple[str, str]]:
    """从 Planner Hook 消息中配对 view_forward_message 调用及结果。"""

    if not isinstance(messages, list):
        return []

    call_to_message_id: dict[str, str] = {}
    results: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        if role == "assistant":
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if isinstance(function, dict):
                    tool_name = str(function.get("name") or "").strip()
                    arguments = function.get("arguments")
                else:
                    tool_name = str(tool_call.get("name") or "").strip()
                    arguments = tool_call.get("arguments")
                if tool_name != VIEW_FORWARD_TOOL_NAME or not isinstance(arguments, dict):
                    continue
                call_id = str(tool_call.get("id") or tool_call.get("call_id") or "").strip()
                message_id = str(arguments.get("msg_id") or "").strip()
                if call_id and message_id:
                    call_to_message_id[call_id] = message_id
            continue

        if role != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "").strip()
        message_id = call_to_message_id.get(call_id, "")
        content = message.get("content")
        if not message_id or not isinstance(content, str) or not content.strip():
            continue
        results.append((message_id, content.strip()))
    return results


def get_tool_definition_name(definition: Any) -> str:
    """读取 OpenAI 兼容或扁平工具定义中的工具名。"""

    if not isinstance(definition, dict):
        return ""
    function = definition.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "").strip()
    return str(definition.get("name") or "").strip()


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用自动跨群转发")
    version: str = Field(default=PLUGIN_VERSION, description="插件版本")
    config_version: str = Field(default=CONFIG_VERSION, description="配置版本")


class RoutingConfig(PluginConfigBase):
    """QQ 群白名单配置。"""

    __ui_label__ = "群聊路由"
    __ui_icon__ = "route"
    __ui_order__ = 1

    source_groups: list[str] = Field(
        default_factory=list,
        description="允许触发自动转发的 QQ 群号列表",
    )
    target_groups: list[str] = Field(
        default_factory=list,
        description="允许接收自动转发的 QQ 群号列表，列表顺序即处理顺序",
    )


class BehaviorConfig(PluginConfigBase):
    """转发行为配置。"""

    __ui_label__ = "转发行为"
    __ui_icon__ = "message-square-share"
    __ui_order__ = 2

    view_cache_ttl_seconds: int = Field(
        default=1800,
        description="view_forward_message 完整内容的缓存秒数",
    )
    dedupe_ttl_seconds: int = Field(
        default=604800,
        description="已处理转发任务的去重记录保留秒数",
    )
    trigger_target_planner: bool = Field(
        default=True,
        description="发送成功后是否触发目标群 Planner 自主决定是否评论",
    )


class ForwardMessagesAutoConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)


class ForwardMessagesAutoPlugin(MaiBotPlugin):
    """自动跨群转发插件。"""

    config_model = ForwardMessagesAutoConfig

    def __init__(self) -> None:
        super().__init__()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._active_job_keys: set[str] = set()
        self._view_cache: dict[tuple[str, str], ViewCacheEntry] = {}
        self._streams_by_group: dict[str, str] = {}
        self._source_stream_ids: set[str] = set()
        self._jobs_state: dict[str, dict[str, Any]] = {}
        self._state_path: Path | None = None
        self._state_lock = asyncio.Lock()
        self._is_unloading = False

    def get_components(self) -> list[dict[str, Any]]:
        """将转发工具明确限制为群聊组件。"""

        components = super().get_components()
        for component in components:
            if component.get("name") != FORWARD_TOOL_NAME:
                continue
            component["chat_scope"] = "group"
            metadata = component.get("metadata")
            if isinstance(metadata, dict):
                metadata.pop("chat_scope", None)
        return components

    async def on_load(self) -> None:
        """初始化插件运行时状态。"""

        self._is_unloading = False
        self._state_path = self.ctx.paths.data_dir / "forward_state.json"
        await asyncio.to_thread(self._load_state_sync)
        await self._prune_and_save_state()
        await self._refresh_stream_index()
        self._warn_for_version_mismatch()
        self.ctx.logger.info(
            "麦麦自动跨群转发插件 v%s 已加载，enabled=%s source=%d target=%d",
            PLUGIN_VERSION,
            self.config.plugin.enabled,
            len(self._source_groups()),
            len(self._target_groups()),
        )

    async def on_unload(self) -> None:
        """取消仍在运行的后台任务。"""

        self._is_unloading = True
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        await self._save_state()
        self.ctx.logger.info("麦麦自动跨群转发插件已卸载")

    async def on_config_update(
        self,
        scope: str,
        config_data: dict[str, Any],
        version: str,
    ) -> None:
        """响应插件配置热更新。"""

        del config_data
        self._cleanup_view_cache()
        await self._prune_and_save_state()
        await self._refresh_stream_index()
        self._warn_for_version_mismatch()
        self.ctx.logger.info("自动跨群转发配置已更新: scope=%s version=%s", scope, version)

    def _warn_for_version_mismatch(self) -> None:
        """记录用户配置中不一致的只读版本字段。"""

        if self.config.plugin.version != PLUGIN_VERSION:
            self.ctx.logger.warning(
                "配置中的 plugin.version=%s 与插件版本 %s 不一致",
                self.config.plugin.version,
                PLUGIN_VERSION,
            )
        if self.config.plugin.config_version != CONFIG_VERSION:
            self.ctx.logger.warning(
                "配置版本 %s 与插件要求的 %s 不一致",
                self.config.plugin.config_version,
                CONFIG_VERSION,
            )

    def _source_groups(self) -> list[str]:
        """返回清洗后的 source 群号。"""

        return normalize_group_ids(self.config.routing.source_groups)

    def _target_groups(self) -> list[str]:
        """返回清洗后的 target 群号。"""

        return normalize_group_ids(self.config.routing.target_groups)

    def _view_cache_ttl(self) -> int:
        """返回最小为一分钟的查看缓存 TTL。"""

        return max(60, int(self.config.behavior.view_cache_ttl_seconds))

    def _dedupe_ttl(self) -> int:
        """返回最小为一分钟的去重状态 TTL。"""

        return max(60, int(self.config.behavior.dedupe_ttl_seconds))

    async def _refresh_stream_index(self) -> None:
        """刷新 QQ 群号到聊天流的映射。"""

        self._streams_by_group.clear()
        self._source_stream_ids.clear()
        if not self.config.plugin.enabled:
            return

        try:
            result = await self.ctx.chat.get_group_streams(platform="qq")
        except Exception as exc:
            self.ctx.logger.warning("读取 QQ 群聊流失败: %s", exc)
            return
        if not isinstance(result, dict) or not result.get("success", False):
            self.ctx.logger.warning(
                "读取 QQ 群聊流被拒绝: %s",
                result.get("error", "未知错误") if isinstance(result, dict) else "返回格式错误",
            )
            return

        streams = result.get("streams")
        if not isinstance(streams, list):
            return
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            if str(stream.get("platform") or "").strip().lower() != "qq":
                continue
            group_id = str(stream.get("group_id") or "").strip()
            stream_id = str(stream.get("stream_id") or stream.get("session_id") or "").strip()
            if group_id and stream_id and group_id not in self._streams_by_group:
                self._streams_by_group[group_id] = stream_id

        self._source_stream_ids = {
            stream_id for group_id in self._source_groups() if (stream_id := self._streams_by_group.get(group_id))
        }

    @HookHandler(
        "maisaka.planner.before_request",
        name="capture_view_forward_result",
        description="缓存源群已经展开的合并转发内容，并仅在 source 白名单会话暴露转发工具。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def capture_view_forward_result(self, **kwargs: Any) -> dict[str, Any]:
        """缓存 view_forward_message 结果并按 source 会话过滤工具。"""

        session_id = str(kwargs.get("session_id") or "").strip()
        is_source_session = self.config.plugin.enabled and bool(session_id) and session_id in self._source_stream_ids

        if is_source_session:
            now = time.time()
            for message_id, content in extract_view_forward_results(kwargs.get("messages")):
                self._view_cache[(session_id, message_id)] = ViewCacheEntry(
                    content=content,
                    cached_at=now,
                )
            self._cleanup_view_cache(now)

        tool_definitions = kwargs.get("tool_definitions")
        if isinstance(tool_definitions, list) and not is_source_session:
            kwargs["tool_definitions"] = [
                definition
                for definition in tool_definitions
                if get_tool_definition_name(definition) != FORWARD_TOOL_NAME
            ]
        return {"action": "continue", "modified_kwargs": kwargs}

    @Tool(
        FORWARD_TOOL_NAME,
        brief_description="把已经完整查看且值得分享的合并转发消息交给插件，按目标群白名单顺序转发。",
        detailed_description=(
            "仅在你已经成功调用 view_forward_message 查看 msg_id 的全部内容，并自主判断值得分享时调用。"
            "目标群由插件白名单决定，禁止自行指定目标群。content_summary 用于完整内容缓存失效时降级。"
        ),
        parameters=[
            ToolParameterInfo(
                name="msg_id",
                param_type=ToolParamType.STRING,
                description="刚刚通过 view_forward_message 完整查看过的源合并转发消息 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="sharing_reason",
                param_type=ToolParamType.STRING,
                description="认为这则内容值得分享的简短原因",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="content_summary",
                param_type=ToolParamType.STRING,
                description="对完整转发内容的忠实摘要，仅在插件缓存失效时使用",
                required=False,
                default="",
            ),
        ],
        visibility="visible",
        chat_scope="group",
    )
    async def request_cross_group_forward(
        self,
        msg_id: str,
        sharing_reason: str = "",
        content_summary: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """校验源消息并创建后台顺序转发任务。"""

        if not self.config.plugin.enabled:
            return self._tool_failure("自动跨群转发插件当前未启用。")

        platform = str(kwargs.get("platform") or "").strip().lower()
        source_group_id = str(kwargs.get("group_id") or "").strip()
        source_stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        source_message_id = str(msg_id or "").strip()
        if platform != "qq":
            return self._tool_failure("初版仅支持 SnowLuma Adapter 下的 QQ 群聊。")
        if not source_stream_id or not source_group_id:
            return self._tool_failure("缺少当前 QQ 群聊上下文，无法创建转发任务。")
        if source_group_id not in set(self._source_groups()):
            return self._tool_failure("当前群不在 source 白名单中，不能发起跨群转发。")
        if not source_message_id:
            return self._tool_failure("必须提供刚刚查看过的合并转发消息 msg_id。")

        target_group_ids = [group_id for group_id in self._target_groups() if group_id != source_group_id]
        if not target_group_ids:
            return self._tool_failure("没有可用的 target 白名单群，请先完成插件配置。")

        try:
            result = await self.ctx.message.get_by_id(
                source_message_id,
                stream_id=source_stream_id,
                include_binary_data=True,
            )
        except Exception as exc:
            self.ctx.logger.warning("读取源消息失败: msg_id=%s error=%s", source_message_id, exc)
            return self._tool_failure("读取源消息失败，暂时无法创建转发任务。")
        if not isinstance(result, dict) or not result.get("success", False):
            return self._tool_failure(
                f"读取源消息失败：{result.get('error', '未知错误') if isinstance(result, dict) else '返回格式错误'}"
            )

        message = result.get("message")
        if not isinstance(message, dict):
            return self._tool_failure("没有找到指定的源消息。")
        if str(message.get("session_id") or "").strip() != source_stream_id:
            return self._tool_failure("指定消息不属于当前 source 聊天流。")
        if str(message.get("group_id") or "").strip() != source_group_id:
            return self._tool_failure("指定消息不属于当前 source 群。")
        if str(message.get("platform") or "").strip().lower() != "qq":
            return self._tool_failure("指定消息不是 QQ 群聊消息。")

        forward_payload = extract_forward_payload(message)
        if forward_payload is None:
            return self._tool_failure("指定消息不是可发送的合并转发消息。")
        forward_segment, forward_messages = forward_payload

        expanded_content = self._get_cached_view_content(source_stream_id, source_message_id)
        if not expanded_content:
            expanded_content = str(content_summary or "").strip()
            if expanded_content:
                self.ctx.logger.info("完整内容缓存未命中，使用 Planner 摘要: msg_id=%s", source_message_id)
        if not expanded_content:
            expanded_content = str(message.get("processed_plain_text") or "").strip() or "[合并转发消息]"
            self.ctx.logger.warning("完整内容缓存及 Planner 摘要均缺失，使用消息预览: msg_id=%s", source_message_id)

        state_key = self._build_state_key(source_stream_id, source_message_id, target_group_ids)
        job_id = state_key.rsplit(":", 1)[-1]
        if state_key in self._active_job_keys:
            return {
                "success": True,
                "content": f"该合并转发已经在处理中，任务 ID：{job_id}。",
                "job_id": job_id,
                "accepted": False,
            }
        if self._job_is_complete(state_key, target_group_ids):
            return {
                "success": True,
                "content": f"该合并转发已经完成当前白名单投递，任务 ID：{job_id}。",
                "job_id": job_id,
                "accepted": False,
            }

        job = ForwardJob(
            job_id=job_id,
            state_key=state_key,
            source_stream_id=source_stream_id,
            source_group_id=source_group_id,
            source_message_id=source_message_id,
            target_group_ids=target_group_ids,
            forward_segment=forward_segment,
            forward_messages=forward_messages,
            expanded_content=expanded_content,
            sharing_reason=str(sharing_reason or "").strip(),
        )
        self._active_job_keys.add(state_key)
        task = asyncio.create_task(self._run_forward_job(job), name=f"cross-group-forward:{job_id}")
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)
        return {
            "success": True,
            "content": (
                f"已接受跨群转发请求，将按白名单顺序处理 {len(target_group_ids)} 个目标群。任务 ID：{job_id}。"
            ),
            "job_id": job_id,
            "accepted": True,
            "target_count": len(target_group_ids),
        }

    @staticmethod
    def _tool_failure(content: str) -> dict[str, Any]:
        """构造可供 Planner 阅读的工具失败结果。"""

        return {"success": False, "content": content}

    def _cleanup_view_cache(self, now: float | None = None) -> None:
        """删除过期的完整内容缓存。"""

        current_time = now if now is not None else time.time()
        cutoff = current_time - self._view_cache_ttl()
        self._view_cache = {key: entry for key, entry in self._view_cache.items() if entry.cached_at >= cutoff}

    def _get_cached_view_content(self, stream_id: str, message_id: str) -> str:
        """读取仍在有效期内的完整内容缓存。"""

        self._cleanup_view_cache()
        entry = self._view_cache.get((stream_id, message_id))
        return entry.content if entry is not None else ""

    @staticmethod
    def _build_state_key(stream_id: str, message_id: str, target_group_ids: list[str]) -> str:
        """生成包含目标集合版本的稳定任务键。"""

        route_text = ",".join(sorted(set(target_group_ids)))
        digest = hashlib.sha256(f"{stream_id}\n{message_id}\n{route_text}".encode()).hexdigest()[:16]
        return f"{stream_id}:{message_id}:{digest}"

    def _job_is_complete(self, state_key: str, target_group_ids: list[str]) -> bool:
        """判断当前路由的所有目标是否已经完成所需阶段。"""

        job_state = self._jobs_state.get(state_key)
        if not isinstance(job_state, dict):
            return False
        target_states = job_state.get("targets")
        if not isinstance(target_states, dict):
            return False
        required_stage = "planner_queued" if self.config.behavior.trigger_target_planner else "context_appended"
        required_rank = _STAGE_RANK[required_stage]
        return all(
            _STAGE_RANK.get(str(target_states.get(group_id) or "pending"), 0) >= required_rank
            for group_id in target_group_ids
        )

    def _on_background_task_done(self, task: asyncio.Task[Any]) -> None:
        """回收后台任务并记录未处理异常。"""

        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return
        if exception is not None:
            self.ctx.logger.error("跨群转发后台任务异常结束: %s", exception, exc_info=exception)

    async def _run_forward_job(self, job: ForwardJob) -> None:
        """按 target 白名单顺序执行一次转发任务。"""

        try:
            await self._ensure_job_state(job)
            for target_group_id in job.target_group_ids:
                if self._is_unloading:
                    return
                try:
                    await self._process_target(job, target_group_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.ctx.logger.error(
                        "处理目标群失败: job=%s target=%s error=%s",
                        job.job_id,
                        target_group_id,
                        exc,
                        exc_info=True,
                    )
        finally:
            self._active_job_keys.discard(job.state_key)

    async def _process_target(self, job: ForwardJob, target_group_id: str) -> None:
        """发送到单个目标群，并依次追加上下文和触发 Planner。"""

        target_stream_id = await self._resolve_target_stream(target_group_id)
        if not target_stream_id:
            self.ctx.logger.warning("无法解析 target 群聊流: job=%s target=%s", job.job_id, target_group_id)
            return

        stage = self._get_target_stage(job.state_key, target_group_id)
        if _STAGE_RANK.get(stage, 0) < _STAGE_RANK["sent"]:
            send_result = await self.ctx.send.forward(
                job.forward_messages,
                target_stream_id,
                processed_plain_text="[跨群分享的合并转发消息]",
                storage_message=True,
                sync_to_maisaka_history=False,
                maisaka_source_kind=f"cross_group_forward:{job.job_id}",
            )
            if not self._capability_succeeded(send_result):
                self.ctx.logger.warning(
                    "合并转发发送失败: job=%s target=%s error=%s",
                    job.job_id,
                    target_group_id,
                    self._capability_error(send_result),
                )
                return
            await self._set_target_stage(job, target_group_id, "sent")
            stage = "sent"

        if _STAGE_RANK.get(stage, 0) < _STAGE_RANK["context_appended"]:
            visible_text = f"[麦麦从其他群聊分享了一则合并转发消息；以下内容已在源群完整展开]\n{job.expanded_content}"
            context_result = await self.ctx.maisaka.context.append(
                target_stream_id,
                segments=[job.forward_segment],
                visible_text=visible_text,
                source_kind=f"cross_group_forward:{job.job_id}",
                message_id=f"cross-forward:{job.job_id}:{target_group_id}",
            )
            if not self._capability_succeeded(context_result):
                self.ctx.logger.warning(
                    "目标群上下文写入失败: job=%s target=%s error=%s",
                    job.job_id,
                    target_group_id,
                    self._capability_error(context_result),
                )
                return
            await self._set_target_stage(job, target_group_id, "context_appended")
            stage = "context_appended"

        if not self.config.behavior.trigger_target_planner:
            return
        if _STAGE_RANK.get(stage, 0) >= _STAGE_RANK["planner_queued"]:
            return

        intent = (
            "你刚刚把一则来自其他群聊的合并转发分享到了本群。"
            "消息完整内容已经写入当前上下文，无需再次调用 view_forward_message。"
            "请结合本群近期聊天、群友关系、记忆和你的表达习惯，自主决定是否发表一句自然的整体看法；"
            "如果没有合适或有价值的话可说，就保持沉默。不要机械复述消息，也不要暴露插件内部流程。"
        )
        proactive_result = await self.ctx.maisaka.proactive.trigger(
            target_stream_id,
            intent=intent,
            reason=job.sharing_reason or "源群 Planner 判断这则内容值得分享",
            priority="normal",
            metadata={
                "job_id": job.job_id,
                "source_message_id": job.source_message_id,
            },
        )
        if not self._capability_succeeded(proactive_result):
            self.ctx.logger.warning(
                "目标群 Planner 触发失败: job=%s target=%s error=%s",
                job.job_id,
                target_group_id,
                self._capability_error(proactive_result),
            )
            return
        await self._set_target_stage(job, target_group_id, "planner_queued")
        self.ctx.logger.info("目标群处理完成: job=%s target=%s", job.job_id, target_group_id)

    async def _resolve_target_stream(self, group_id: str) -> str:
        """按 QQ 群号获取或创建聊天流。"""

        if stream_id := self._streams_by_group.get(group_id):
            return stream_id

        try:
            result = await self.ctx.chat.get_stream_by_group_id(group_id, platform="qq")
        except Exception as exc:
            self.ctx.logger.debug("按群号查询聊天流失败: group=%s error=%s", group_id, exc)
            result = None
        stream_id = self._extract_stream_id(result)
        if stream_id:
            self._streams_by_group[group_id] = stream_id
            return stream_id

        try:
            result = await self.ctx.chat.open_session(
                platform="qq",
                chat_type="group",
                group_id=group_id,
            )
        except Exception as exc:
            self.ctx.logger.warning("创建目标群聊天流失败: group=%s error=%s", group_id, exc)
            return ""
        stream_id = self._extract_stream_id(result)
        if stream_id:
            self._streams_by_group[group_id] = stream_id
        return stream_id

    @staticmethod
    def _extract_stream_id(result: Any) -> str:
        """从聊天能力结果中读取 stream_id。"""

        if not isinstance(result, dict) or not result.get("success", False):
            return ""
        stream = result.get("stream")
        if isinstance(stream, dict):
            return str(stream.get("stream_id") or stream.get("session_id") or "").strip()
        return str(result.get("stream_id") or result.get("session_id") or "").strip()

    @staticmethod
    def _capability_succeeded(result: Any) -> bool:
        """统一判断 SDK capability 调用是否成功。"""

        if isinstance(result, bool):
            return result
        return isinstance(result, dict) and bool(result.get("success", False))

    @staticmethod
    def _capability_error(result: Any) -> str:
        """提取 SDK capability 的可读错误。"""

        if isinstance(result, dict):
            return str(result.get("error") or result.get("message") or "未知错误")
        return "返回格式错误"

    def _get_target_stage(self, state_key: str, target_group_id: str) -> str:
        """读取单个目标群的持久化阶段。"""

        job_state = self._jobs_state.get(state_key)
        if not isinstance(job_state, dict):
            return "pending"
        target_states = job_state.get("targets")
        if not isinstance(target_states, dict):
            return "pending"
        stage = str(target_states.get(target_group_id) or "pending")
        return stage if stage in _STAGE_RANK else "pending"

    async def _ensure_job_state(self, job: ForwardJob) -> None:
        """确保任务状态存在并写入磁盘。"""

        async with self._state_lock:
            self._jobs_state.setdefault(
                job.state_key,
                {
                    "job_id": job.job_id,
                    "source_stream_id": job.source_stream_id,
                    "source_group_id": job.source_group_id,
                    "source_message_id": job.source_message_id,
                    "target_group_ids": list(job.target_group_ids),
                    "created_at": time.time(),
                    "updated_at": time.time(),
                    "targets": {},
                },
            )
            await asyncio.to_thread(self._save_state_sync)

    async def _set_target_stage(
        self,
        job: ForwardJob,
        target_group_id: str,
        stage: str,
    ) -> None:
        """提升单个目标群的阶段并持久化。"""

        async with self._state_lock:
            job_state = self._jobs_state.setdefault(
                job.state_key,
                {
                    "job_id": job.job_id,
                    "source_stream_id": job.source_stream_id,
                    "source_group_id": job.source_group_id,
                    "source_message_id": job.source_message_id,
                    "target_group_ids": list(job.target_group_ids),
                    "created_at": time.time(),
                    "targets": {},
                },
            )
            target_states = job_state.setdefault("targets", {})
            current_stage = str(target_states.get(target_group_id) or "pending")
            if _STAGE_RANK.get(stage, 0) > _STAGE_RANK.get(current_stage, 0):
                target_states[target_group_id] = stage
                job_state["updated_at"] = time.time()
                await asyncio.to_thread(self._save_state_sync)

    async def _prune_and_save_state(self) -> None:
        """清理超出去重 TTL 的状态记录并保存。"""

        cutoff = time.time() - self._dedupe_ttl()
        async with self._state_lock:
            self._jobs_state = {
                key: value
                for key, value in self._jobs_state.items()
                if isinstance(value, dict) and float(value.get("updated_at") or value.get("created_at") or 0) >= cutoff
            }
            await asyncio.to_thread(self._save_state_sync)

    async def _save_state(self) -> None:
        """持久化当前任务状态。"""

        if self._state_path is None:
            return
        async with self._state_lock:
            await asyncio.to_thread(self._save_state_sync)

    def _load_state_sync(self) -> None:
        """从插件数据目录读取任务状态。"""

        if self._state_path is None or not self._state_path.is_file():
            self._jobs_state = {}
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.ctx.logger.warning("读取转发状态失败，将使用空状态: %s", exc)
            self._jobs_state = {}
            return
        raw_jobs = payload.get("jobs") if isinstance(payload, dict) else None
        self._jobs_state = (
            {str(key): value for key, value in raw_jobs.items() if isinstance(value, dict)}
            if isinstance(raw_jobs, dict)
            else {}
        )

    def _save_state_sync(self) -> None:
        """以原子替换方式写入任务状态。"""

        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._state_path.with_suffix(".tmp")
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "jobs": self._jobs_state,
        }
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(self._state_path)


def create_plugin() -> ForwardMessagesAutoPlugin:
    """创建插件实例。"""

    return ForwardMessagesAutoPlugin()
