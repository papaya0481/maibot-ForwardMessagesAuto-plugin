"""插件 Manifest 的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from forward_messages_auto.config import CONFIG_VERSION, PLUGIN_VERSION
from plugin import ForwardMessagesAutoPlugin


def test_manifest_has_no_adapter_plugin_dependency() -> None:
    """确保 Manifest 不声明适配器依赖或已移除的上下文能力。

    预期 ``dependencies`` 为空、保留主动任务能力但不再申请
    ``maisaka.context.append``。该测试防止适配器尚未激活时阻止插件加载，
    或删除重复上下文调用后仍保留多余权限。
    """

    manifest_path = Path(__file__).parents[1] / "_manifest.json"
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["dependencies"] == []
    assert "maisaka.context.append" not in manifest["capabilities"]
    assert "maisaka.proactive.trigger" in manifest["capabilities"]


def test_manifest_and_default_config_versions_match_source_constants() -> None:
    """确保发布版本在 Manifest、配置默认值和源码常量之间保持一致。

    预期 Manifest 与默认 ``plugin.version`` 等于 ``PLUGIN_VERSION``，默认
    ``config_version`` 等于独立维护的 ``CONFIG_VERSION``。该测试防止仅
    更新插件版本时遗漏发布元数据，或错误连带提升配置版本。
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
