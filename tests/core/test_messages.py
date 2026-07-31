"""Host 合并转发消息解析的回归测试。"""

from __future__ import annotations


from forward_messages_auto.core.messages import ForwardMessageParser
from tests.support import build_forward_message


def test_extract_forward_payload_preserves_nodes_and_binary_data() -> None:
    """验证合并转发解析同时生成上下文段和发送节点。

    期望解析结果保留 ``forward`` 类型、节点昵称以及图片
    ``binary_data_base64``。该测试防止消息规范化过程中丢失发送者信息或
    媒体二进制数据，导致目标群收到不完整内容。
    """

    payload = ForwardMessageParser.extract(build_forward_message())
    assert payload is not None
    segment, messages = payload
    assert segment["type"] == "forward"
    assert messages[0]["nickname"] == "群友甲"
    assert messages[0]["segments"][1]["binary_data_base64"] == "aW1hZ2U="
