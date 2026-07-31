"""内部模块依赖方向的架构回归测试。"""

from __future__ import annotations

import ast
from pathlib import Path


def _resolve_import(module_name: str, node: ast.ImportFrom) -> str:
    """把相对 ``from`` 导入解析为包内绝对模块名。

    Args:
        module_name: 当前源码文件对应的完整 Python 模块名。
        node: AST 中待解析的 ``ImportFrom`` 节点。

    Returns:
        可用于判断领域边界的绝对模块名；无法解析的空模块返回空字符串。
    """

    imported_module = node.module or ""
    if node.level == 0:
        return imported_module
    current_parts = module_name.split(".")
    base_parts = current_parts[: -node.level]
    if imported_module:
        base_parts.extend(imported_module.split("."))
    return ".".join(base_parts)


def test_domain_packages_follow_one_way_import_boundaries() -> None:
    """验证 core、source 与 target 遵守单向依赖边界。

    ``core`` 不得依赖外层领域或应用服务，``source`` 与 ``target`` 不得互相
    导入，两个领域也不得反向依赖 ``request``、``runtime`` 或调试统计。
    该测试防止后续功能把跨域协调重新下沉，导致循环依赖和目录再次扁平化。
    """

    package_root = Path(__file__).resolve().parents[1] / "forward_messages_auto"
    forbidden_roots = {
        "core": {"source", "target", "request", "runtime", "debug_stats"},
        "source": {"target", "request", "runtime", "debug_stats"},
        "target": {"source", "request", "runtime", "debug_stats"},
    }

    violations: list[str] = []
    for domain, forbidden in forbidden_roots.items():
        for source_path in sorted((package_root / domain).glob("*.py")):
            module_name = f"forward_messages_auto.{domain}.{source_path.stem}"
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            imported_modules = [
                _resolve_import(module_name, node) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            ]
            imported_modules.extend(
                alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
            )
            for imported_module in imported_modules:
                prefix = "forward_messages_auto."
                if not imported_module.startswith(prefix):
                    continue
                imported_root = imported_module.removeprefix(prefix).split(".", 1)[0]
                if imported_root in forbidden:
                    violations.append(f"{source_path.relative_to(package_root)} -> {imported_module}")

    assert violations == []
