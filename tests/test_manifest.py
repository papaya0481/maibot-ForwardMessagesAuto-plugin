"""插件 Manifest 的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def test_manifest_has_no_adapter_plugin_dependency() -> None:
    """确保插件只声明通用 capabilities，而不把 SnowLuma 适配器建模为强依赖。

    预期 Manifest 的 ``dependencies`` 为空，避免适配器尚未激活时阻止插件加载。
    该测试防止未来因运行环境验证范围而重新加入未被调用的适配器 API 依赖。
    """

    manifest_path = Path(__file__).parents[1] / "_manifest.json"
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["dependencies"] == []
