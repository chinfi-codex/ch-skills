"""推送渠道适配器。

每个渠道模块暴露同一个契约（notify_send.py 按此分发，测试也按此 mock）：

    send(markdown, title, *, dry_run=False, **opts) -> dict
    # {"ok": bool, "info": str, ...渠道自有键（response/target 等）}

配置缺失（token 没配、CLI 不存在、目标没法定）抛 ``ConfigError``，由入口统一
折算成退出码 2；发送失败（远端拒绝、进程非零退出）返回 ``ok=False``。
模块自身定位 _shared 共享包，不依赖入口先摆好 sys.path。
"""
from __future__ import annotations


class ConfigError(RuntimeError):
    """渠道前置条件不满足——不是发送失败，是根本没到能发的那一步。"""
