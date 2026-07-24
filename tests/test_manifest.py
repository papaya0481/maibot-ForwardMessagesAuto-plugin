"""插件 Manifest 的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from forward_messages_auto.config import CONFIG_VERSION, PLUGIN_VERSION
from plugin import ForwardMessagesAutoPlugin


def test_manifest_has_no_adapter_plugin_dependency() -> None:
    """确保插件只声明通用 capabilities，而不把 SnowLuma 适配器建模为强依赖。

    预期 Manifest 的 ``dependencies`` 为空，避免适配器尚未激活时阻止插件加载。
    该测试防止未来因运行环境验证范围而重新加入未被调用的适配器 API 依赖。
    """

    manifest_path = Path(__file__).parents[1] / "_manifest.json"
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["dependencies"] == []


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
