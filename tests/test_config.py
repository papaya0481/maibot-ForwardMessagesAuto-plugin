"""插件配置归一化与恢复的回归测试。"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from forward_messages_auto.config_recovery import LastKnownGoodConfig
from plugin import ForwardMessagesAutoPlugin


def test_invalid_toml_reuses_last_valid_config(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """验证保存中的无效 TOML 不会把已启用插件归一化为默认关闭状态。

    首次归一化保存启用状态和白名单，随后让磁盘文件停留在未闭合数组并让
    Runner 按实际预处理路径传入完整默认配置。期望仍返回旧配置且记录警告；
    该测试防止 Host 把短暂解析失败误判为 ``plugin.enabled=false`` 并卸载
    插件。

    Args:
        tmp_path: pytest 提供的临时目录，用于模拟插件 ``config.toml``。
        caplog: pytest 日志捕获 fixture，用于确认恢复路径对用户可观察。
    """

    config_path = tmp_path / "config.toml"
    plugin = ForwardMessagesAutoPlugin()
    plugin._config_recovery = LastKnownGoodConfig(config_path)
    plugin._set_context(SimpleNamespace(logger=logging.getLogger("test.config-recovery")))
    valid_config = plugin.get_default_config()
    valid_config["plugin"]["enabled"] = True
    valid_config["routing"]["source_groups"] = ["10001"]
    valid_config["routing"]["target_groups"] = ["20001"]
    normalized_config, _changed = plugin.normalize_plugin_config(valid_config)
    config_path.write_text('[routing]\nsource_groups = [\n  "10001"\n', encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        recovered_config, changed = plugin.normalize_plugin_config(plugin.get_default_config())

    assert recovered_config == normalized_config
    assert recovered_config["plugin"]["enabled"] is True
    assert recovered_config["routing"]["source_groups"] == ["10001"]
    assert changed is False
    assert "继续使用最近一次有效配置" in caplog.text


def test_deleted_config_uses_sdk_defaults_instead_of_snapshot(tmp_path: Path) -> None:
    """验证配置重置删除文件后不会错误恢复最近有效快照。

    先记住一份启用配置但不创建磁盘文件，再用完整默认配置模拟 Runner 的
    重置事件。期望 SDK 返回默认关闭且空白名单；该测试防止容错逻辑破坏
    WebUI“重置配置”的既有语义。

    Args:
        tmp_path: pytest 提供的临时目录，用于提供一个不存在的配置路径。
    """

    plugin = ForwardMessagesAutoPlugin()
    plugin._config_recovery = LastKnownGoodConfig(tmp_path / "config.toml")
    valid_config = plugin.get_default_config()
    valid_config["plugin"]["enabled"] = True
    valid_config["routing"]["source_groups"] = ["10001"]
    plugin.normalize_plugin_config(valid_config)

    reset_config, _changed = plugin.normalize_plugin_config(plugin.get_default_config())

    assert reset_config["plugin"]["enabled"] is False
    assert reset_config["routing"]["source_groups"] == []
