"""
AstrBot 正则消息屏蔽插件

通过正则表达式屏蔽用户发送给 LLM 的消息。
当消息匹配配置的正则表达式时，将阻止该消息发送给 LLM。

作者: AI Assistant
版本: v1.0.2
"""

import re
from typing import AsyncGenerator

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig, logger
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import Plain


class RegexBlockerPlugin(Star):
    """正则消息屏蔽插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._compile_patterns()

    def _compile_patterns(self) -> None:
        """编译正则表达式模式"""
        self.compiled_patterns = []
        patterns = self.config.get("block_patterns", [])

        for pattern in patterns:
            try:
                compiled = re.compile(pattern)
                self.compiled_patterns.append(compiled)
                logger.debug(f"[RegexBlocker] 编译正则表达式: {pattern}")
            except re.error as e:
                logger.warning(
                    f"[RegexBlocker] 正则表达式编译失败: {pattern}, 错误: {e}"
                )

        logger.info(f"[RegexBlocker] 已加载 {len(self.compiled_patterns)} 个屏蔽规则")

    def _is_blocked(self, message: str) -> bool:
        """检查消息是否匹配任何屏蔽规则"""
        if not message:
            return False
        for pattern in self.compiled_patterns:
            if pattern.search(message):
                return True
        return False

    def _get_raw_message_str(self, event: AstrMessageEvent) -> str:
        """
        获取原始消息字符串（包含命令前缀）

        AstrBot 的 message_str 会去掉命令前缀（如 #、/），
        此方法从消息链中重新构建原始消息。
        """
        try:
            message_obj = event.message_obj
            if message_obj and hasattr(message_obj, "message") and message_obj.message:
                # 从消息链中提取所有 Plain 文本
                parts = []
                for component in message_obj.message:
                    if isinstance(component, Plain) and component.text:
                        parts.append(component.text)
                return " ".join(parts)
        except Exception as e:
            logger.debug(f"[RegexBlocker] 获取原始消息失败: {e}")

        # 回退到 message_str
        return event.message_str or ""

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查发送者是否为管理员"""
        try:
            # 获取管理员列表
            admins = self.context.get_registered_commands().get("_admin", [])
            sender_id = event.get_sender_id()
            return str(sender_id) in [str(admin) for admin in admins]
        except Exception:
            return False

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_all_message(self, event: AstrMessageEvent) -> AsyncGenerator:
        """
        拦截所有消息，检查是否需要屏蔽

        使用高优先级确保在其他插件之前执行。
        如果消息匹配屏蔽规则，则停止事件传播。
        """
        # 检查插件是否启用
        if not self.config.get("enabled", True):
            return

        # 获取原始消息（包含命令前缀）
        raw_message = self._get_raw_message_str(event)
        message_str = event.message_str.strip() if event.message_str else ""

        logger.debug(
            f"[RegexBlocker] 检查消息 - message_str: {message_str}, raw: {raw_message}"
        )

        # 检查管理员绕过
        if self.config.get("admin_bypass", True) and self._is_admin(event):
            logger.debug(f"[RegexBlocker] 管理员消息绕过屏蔽: {message_str}")
            return

        # 同时检查原始消息和处理后的消息
        is_blocked = self._is_blocked(raw_message) or self._is_blocked(message_str)

        if is_blocked:
            # 记录日志
            if self.config.get("log_blocked", True):
                sender_name = event.get_sender_name()
                sender_id = event.get_sender_id()
                logger.info(
                    f"[RegexBlocker] 消息已屏蔽 - 发送者: {sender_name}({sender_id}), "
                    f"消息: {raw_message[:50]}"
                )

            # 停止事件传播 - 这会阻止后续所有处理，包括 LLM 请求
            event.stop_event()

            # 发送提示消息（非静默模式）
            if not self.config.get("silent_mode", False):
                block_message = self.config.get(
                    "block_message", "您的消息已被屏蔽，无法发送给 AI。"
                )
                yield event.plain_result(block_message)

            return

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """
        LLM 请求钩子 - 作为第二道防线

        如果消息没有被 event_message_type 拦截，在这里再次检查。
        """
        # 检查插件是否启用
        if not self.config.get("enabled", True):
            return

        # 获取原始消息
        raw_message = self._get_raw_message_str(event)
        message_str = event.message_str.strip() if event.message_str else ""

        # 检查管理员绕过
        if self.config.get("admin_bypass", True) and self._is_admin(event):
            return

        # 同时检查原始消息和处理后的消息
        is_blocked = self._is_blocked(raw_message) or self._is_blocked(message_str)

        if is_blocked:
            if self.config.get("log_blocked", True):
                sender_name = event.get_sender_name()
                sender_id = event.get_sender_id()
                logger.info(
                    f"[RegexBlocker] (LLM钩子) 消息已屏蔽 - 发送者: {sender_name}({sender_id}), "
                    f"消息: {raw_message[:50]}"
                )

            # 清空请求内容，阻止 LLM 调用
            req.prompt = ""
            # 停止事件传播
            event.stop_event()

            # 发送提示消息
            if not self.config.get("silent_mode", False):
                block_message = self.config.get(
                    "block_message", "您的消息已被屏蔽，无法发送给 AI。"
                )
                await event.send(block_message)

    @filter.command("blocker_reload")
    async def reload_patterns(self, event: AstrMessageEvent) -> AsyncGenerator:
        """重新加载屏蔽规则"""
        self._compile_patterns()
        yield event.plain_result(
            f"已重新加载屏蔽规则，当前共有 {len(self.compiled_patterns)} 条规则。"
        )

    @filter.command("blocker_list")
    async def list_patterns(self, event: AstrMessageEvent) -> AsyncGenerator:
        """列出当前所有屏蔽规则"""
        patterns = self.config.get("block_patterns", [])
        if not patterns:
            yield event.plain_result("当前没有设置屏蔽规则。")
            return

        msg = f"当前共有 {len(patterns)} 条屏蔽规则:\n"
        for i, pattern in enumerate(patterns, 1):
            msg += f"{i}. {pattern}\n"
        yield event.plain_result(msg.strip())

    @filter.command("blocker_test")
    async def test_pattern(
        self, event: AstrMessageEvent, test_message: str = ""
    ) -> AsyncGenerator:
        """测试消息是否会被屏蔽"""
        if not test_message:
            yield event.plain_result("请提供要测试的消息，例如: /blocker_test #napcat")
            return

        if self._is_blocked(test_message):
            matched_patterns = []
            for pattern in self.compiled_patterns:
                if pattern.search(test_message):
                    matched_patterns.append(pattern.pattern)
            yield event.plain_result(
                f"消息 '{test_message}' 会被屏蔽。\n"
                f"匹配的规则: {', '.join(matched_patterns)}"
            )
        else:
            yield event.plain_result(f"消息 '{test_message}' 不会被屏蔽。")

    @filter.command("blocker_status")
    async def show_status(self, event: AstrMessageEvent) -> AsyncGenerator:
        """显示插件状态"""
        status = "已启用" if self.config.get("enabled", True) else "已禁用"
        silent = "开启" if self.config.get("silent_mode", False) else "关闭"
        admin_bypass = "开启" if self.config.get("admin_bypass", True) else "关闭"
        patterns_count = len(self.compiled_patterns)

        msg = (
            f"正则消息屏蔽器状态:\n"
            f"- 状态: {status}\n"
            f"- 屏蔽规则数: {patterns_count}\n"
            f"- 静默模式: {silent}\n"
            f"- 管理员绕过: {admin_bypass}"
        )
        yield event.plain_result(msg)

    async def terminate(self) -> None:
        """插件卸载时调用"""
        logger.info("[RegexBlocker] 插件已卸载")
