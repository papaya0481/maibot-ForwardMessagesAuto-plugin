"""麦麦自动跨群转发插件的内部实现。"""

from forward_messages_auto.config import (
    CONFIG_VERSION,
    PLUGIN_VERSION,
    ForwardMessagesAutoConfig,
)
from forward_messages_auto.runtime import ForwardingRuntime

__all__ = [
    "CONFIG_VERSION",
    "PLUGIN_VERSION",
    "ForwardMessagesAutoConfig",
    "ForwardingRuntime",
]
