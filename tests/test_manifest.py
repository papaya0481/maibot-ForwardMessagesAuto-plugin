"""插件 Manifest 的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from forward_messages_auto.config import CONFIG_VERSION, PLUGIN_VERSION
from plugin import ForwardMessagesAutoPlugin


def test_manifest_has_required_capabilities_without_adapter_dependency() -> None:
    """确保 Manifest 只声明实现所需能力且不绑定适配器插件。

    预期 ``dependencies`` 为空、保留主动任务能力但不再申请
    ``maisaka.context.append``，并申请可信 source 会话反查所需的群聊流列表。
    该测试防止适配器尚未激活时阻止插件加载、删除重复上下文调用后仍保留
    多余权限，或测试替身掩盖生产环境的 capability 授权失败。
    """

    manifest_path = Path(__file__).parents[1] / "_manifest.json"
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["dependencies"] == []
    assert "maisaka.context.append" not in manifest["capabilities"]
    assert "chat.get_group_streams" in manifest["capabilities"]
    assert "maisaka.proactive.trigger" in manifest["capabilities"]


def test_manifest_and_default_config_versions_match_source_constants() -> None:
    """确保发布版本在 Manifest、配置默认值和源码常量之间保持一致。

    预期 Manifest 与默认 ``plugin.version`` 等于 ``PLUGIN_VERSION``，默认
    ``config_version`` 等于承载最近配置变更的 ``CONFIG_VERSION``。source
    Planner 触发开关应默认关闭，避免升级后静默改变消息处理行为。该测试
    防止遗漏发布元数据、配置版本或新字段默认值。
    """

    manifest_path = Path(__file__).parents[1] / "_manifest.json"
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    default_config = ForwardMessagesAutoPlugin.build_default_config()

    assert manifest["version"] == PLUGIN_VERSION
    assert default_config["plugin"]["version"] == PLUGIN_VERSION
    assert default_config["plugin"]["config_version"] == CONFIG_VERSION
    assert "view_cache_ttl_seconds" not in default_config["behavior"]
    assert "dedupe_ttl_seconds" not in default_config["behavior"]
    assert default_config["behavior"]["view_failure_fallback_threshold"] == 2
    assert default_config["behavior"]["trigger_source_planner"] is False
