"""
核心动作插件

将系统核心动作（reply、no_reply、emoji）转换为新插件系统格式
这是系统的内置插件，提供基础的聊天交互功能
"""

import re
from typing import List, Tuple, Type, Optional

# 导入新插件系统
from src.plugin_system import BasePlugin, register_plugin, BaseAction, ComponentInfo, ActionActivationType, ChatMode
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.base.config_types import ConfigField

# 导入依赖的系统组件
from src.common.logger import get_logger
from src.chat.heart_flow.observation.chatting_observation import ChattingObservation
from src.chat.focus_chat.hfc_utils import create_empty_anchor_message

logger = get_logger("core_actions")

# 常量定义
WAITING_TIME_THRESHOLD = 1200  # 等待新消息时间阈值，单位秒


class ReplyAction(BaseAction):
    """回复动作 - 参与聊天回复"""

    # 激活设置
    focus_activation_type = ActionActivationType.ALWAYS
    normal_activation_type = ActionActivationType.NEVER
    mode_enable = ChatMode.FOCUS
    parallel_action = False

    # 动作基本信息
    action_name = "reply"
    action_description = "参与聊天回复，处理文本和表情的发送"

    # 动作参数定义
    action_parameters = {
        "reply_to": "如果是明确回复某个人的发言，请在reply_to参数中指定，格式：（用户名:发言内容），如果不是，reply_to的值设为none"
    }

    # 动作使用场景
    action_require = ["你想要闲聊或者随便附和", "有人提到你", "如果你刚刚进行了回复，不要对同一个话题重复回应"]

    # 关联类型
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        """执行回复动作"""
        logger.info(f"{self.log_prefix} 决定回复: {self.reasoning}")

        try:
            # 获取聊天观察
            chatting_observation = self._get_chatting_observation()
            if not chatting_observation:
                return False, "未找到聊天观察"

            # 处理回复目标
            anchor_message = await self._resolve_reply_target(chatting_observation)

            # 获取回复器服务
            replyer = self.api.get_service("replyer")
            if not replyer:
                logger.error(f"{self.log_prefix} 未找到回复器服务")
                return False, "回复器服务不可用"

            # 执行回复
            success, reply_set = await replyer.deal_reply(
                cycle_timers=self.cycle_timers,
                action_data=self.action_data,
                anchor_message=anchor_message,
                reasoning=self.reasoning,
                thinking_id=self.thinking_id,
            )

            # 构建回复文本
            reply_text = self._build_reply_text(reply_set)

            # 存储动作记录
            await self.api.store_action_info(
                action_build_into_prompt=False,
                action_prompt_display=reply_text,
                action_done=True,
                thinking_id=self.thinking_id,
                action_data=self.action_data,
            )

            # 重置NoReplyAction的连续计数器
            NoReplyAction.reset_consecutive_count()

            return success, reply_text

        except Exception as e:
            logger.error(f"{self.log_prefix} 回复动作执行失败: {e}")
            return False, f"回复失败: {str(e)}"

    def _get_chatting_observation(self) -> Optional[ChattingObservation]:
        """获取聊天观察对象"""
        observations = self.api.get_service("observations") or []
        for obs in observations:
            if isinstance(obs, ChattingObservation):
                return obs
        return None

    async def _resolve_reply_target(self, chatting_observation: ChattingObservation):
        """解析回复目标消息"""
        reply_to = self.action_data.get("reply_to", "none")

        if ":" in reply_to or "：" in reply_to:
            # 解析回复目标格式：用户名:消息内容
            parts = re.split(pattern=r"[:：]", string=reply_to, maxsplit=1)
            if len(parts) == 2:
                target = parts[1].strip()
                anchor_message = chatting_observation.search_message_by_text(target)
                if anchor_message:
                    chat_stream = self.api.get_service("chat_stream")
                    if chat_stream:
                        anchor_message.update_chat_stream(chat_stream)
                    return anchor_message

        # 创建空锚点消息
        logger.info(f"{self.log_prefix} 未找到锚点消息，创建占位符")
        chat_stream = self.api.get_service("chat_stream")
        if chat_stream:
            return await create_empty_anchor_message(chat_stream.platform, chat_stream.group_info, chat_stream)
        return None

    def _build_reply_text(self, reply_set) -> str:
        """构建回复文本"""
        reply_text = ""
        if reply_set:
            for reply in reply_set:
                reply_type = reply[0]
                data = reply[1]
                if reply_type in ["text", "emoji"]:
                    reply_text += data
        return reply_text


class NoReplyAction(BaseAction):
    """不回复动作，继承时会等待新消息或超时"""

    focus_activation_type = ActionActivationType.ALWAYS
    normal_activation_type = ActionActivationType.NEVER
    mode_enable = ChatMode.FOCUS
    parallel_action = False

    # 动作基本信息
    action_name = "no_reply"
    action_description = "暂时不回复消息，等待新消息或超时"

    # 默认超时时间，将由插件在注册时设置
    waiting_timeout = 1200

    # 连续no_reply计数器
    _consecutive_count = 0

    # 分级等待时间
    _waiting_stages = [10, 60, 600]  # 第1、2、3次的等待时间

    # 动作参数定义
    action_parameters = {}

    # 动作使用场景
    action_require = ["你连续发送了太多消息，且无人回复", "想要暂时不回复"]

    # 关联类型
    associated_types = []

    async def execute(self) -> Tuple[bool, str]:
        """执行不回复动作，等待新消息或超时"""
        try:
            # 增加连续计数
            NoReplyAction._consecutive_count += 1
            count = NoReplyAction._consecutive_count

            # 计算本次等待时间
            timeout = self._calculate_waiting_time(count)

            logger.info(f"{self.log_prefix} 选择不回复(第{count}次连续)，等待新消息中... (超时: {timeout}秒)")

            # 等待新消息或达到时间上限
            result = await self.api.wait_for_new_message(timeout)

            # 如果有新消息或者超时，都不重置计数器，因为可能还会继续no_reply
            return result

        except Exception as e:
            logger.error(f"{self.log_prefix} 不回复动作执行失败: {e}")
            return False, f"不回复动作执行失败: {e}"

    def _calculate_waiting_time(self, consecutive_count: int) -> int:
        """根据连续次数计算等待时间"""
        if consecutive_count <= len(self._waiting_stages):
            # 前3次使用预设时间
            stage_time = self._waiting_stages[consecutive_count - 1]
            # 如果WAITING_TIME_THRESHOLD更小，则使用它
            return min(stage_time, self.waiting_timeout)
        else:
            # 第4次及以后使用WAITING_TIME_THRESHOLD
            return self.waiting_timeout

    @classmethod
    def reset_consecutive_count(cls):
        """重置连续计数器"""
        cls._consecutive_count = 0
        logger.debug("NoReplyAction连续计数器已重置")


class EmojiAction(BaseAction):
    """表情动作 - 发送表情包"""

    # 激活设置
    focus_activation_type = ActionActivationType.LLM_JUDGE
    normal_activation_type = ActionActivationType.RANDOM
    mode_enable = ChatMode.ALL
    parallel_action = True
    random_activation_probability = 0.1  # 默认值，可通过配置覆盖

    # 动作基本信息
    action_name = "emoji"
    action_description = "发送表情包辅助表达情绪"

    # LLM判断提示词
    llm_judge_prompt = """
    判定是否需要使用表情动作的条件：
    1. 用户明确要求使用表情包
    2. 这是一个适合表达强烈情绪的场合
    3. 不要发送太多表情包，如果你已经发送过多个表情包则回答"否"
    
    请回答"是"或"否"。
    """

    # 动作参数定义
    action_parameters = {"description": "文字描述你想要发送的表情包内容"}

    # 动作使用场景
    action_require = ["表达情绪时可以选择使用", "重点：不要连续发，如果你已经发过[表情包]，就不要选择此动作"]

    # 关联类型
    associated_types = ["emoji"]

    async def execute(self) -> Tuple[bool, str]:
        """执行表情动作"""
        logger.info(f"{self.log_prefix} 决定发送表情")

        try:
            # 创建空锚点消息
            anchor_message = await self._create_anchor_message()
            if not anchor_message:
                return False, "无法创建锚点消息"

            # 获取回复器服务
            replyer = self.api.get_service("replyer")
            if not replyer:
                logger.error(f"{self.log_prefix} 未找到回复器服务")
                return False, "回复器服务不可用"

            # 执行表情处理
            success, reply_set = await replyer.deal_emoji(
                cycle_timers=self.cycle_timers,
                action_data=self.action_data,
                anchor_message=anchor_message,
                thinking_id=self.thinking_id,
            )

            # 构建回复文本
            reply_text = self._build_reply_text(reply_set)

            # 重置NoReplyAction的连续计数器
            NoReplyAction.reset_consecutive_count()

            return success, reply_text

        except Exception as e:
            logger.error(f"{self.log_prefix} 表情动作执行失败: {e}")
            return False, f"表情发送失败: {str(e)}"

    async def _create_anchor_message(self):
        """创建锚点消息"""
        chat_stream = self.api.get_service("chat_stream")
        if chat_stream:
            logger.info(f"{self.log_prefix} 为表情包创建占位符")
            return await create_empty_anchor_message(chat_stream.platform, chat_stream.group_info, chat_stream)
        return None

    def _build_reply_text(self, reply_set) -> str:
        """构建回复文本"""
        reply_text = ""
        if reply_set:
            for reply in reply_set:
                reply_type = reply[0]
                data = reply[1]
                if reply_type in ["text", "emoji"]:
                    reply_text += data
        return reply_text


class ChangeToFocusChatAction(BaseAction):
    """切换到专注聊天动作 - 从普通模式切换到专注模式"""

    focus_activation_type = ActionActivationType.NEVER
    normal_activation_type = ActionActivationType.NEVER
    mode_enable = ChatMode.NORMAL
    parallel_action = False

    # 动作基本信息
    action_name = "change_to_focus_chat"
    action_description = "切换到专注聊天，从普通模式切换到专注模式"

    # 动作参数定义
    action_parameters = {}

    # 动作使用场景
    action_require = [
        "你想要进入专注聊天模式",
        "聊天上下文中自己的回复条数较多（超过3-4条）",
        "对话进行得非常热烈活跃",
        "用户表现出深入交流的意图",
        "话题需要更专注和深入的讨论",
    ]

    async def execute(self) -> Tuple[bool, str]:
        """执行切换到专注聊天动作"""
        logger.info(f"{self.log_prefix} 决定切换到专注聊天: {self.reasoning}")

        # 重置NoReplyAction的连续计数器
        NoReplyAction.reset_consecutive_count()

        # 这里只做决策标记，具体切换逻辑由上层管理器处理
        return True, "决定切换到专注聊天模式"


class ExitFocusChatAction(BaseAction):
    """退出专注聊天动作 - 从专注模式切换到普通模式"""

    # 激活设置
    focus_activation_type = ActionActivationType.NEVER
    normal_activation_type = ActionActivationType.NEVER
    mode_enable = ChatMode.FOCUS
    parallel_action = False

    # 动作基本信息
    action_name = "exit_focus_chat"
    action_description = "退出专注聊天，从专注模式切换到普通模式"

    # LLM判断提示词
    llm_judge_prompt = """
    判定是否需要退出专注聊天的条件：
    1. 很长时间没有回复，应该退出专注聊天
    2. 当前内容不需要持续专注关注
    3. 聊天内容已经完成，话题结束
    
    请回答"是"或"否"。
    """

    # 动作参数定义
    action_parameters = {}

    # 动作使用场景
    action_require = [
        "很长时间没有回复，你决定退出专注聊天",
        "当前内容不需要持续专注关注，你决定退出专注聊天",
        "聊天内容已经完成，你决定退出专注聊天",
    ]

    # 关联类型
    associated_types = []

    async def execute(self) -> Tuple[bool, str]:
        """执行退出专注聊天动作"""
        logger.info(f"{self.log_prefix} 决定退出专注聊天: {self.reasoning}")

        try:
            # 标记状态切换请求
            self._mark_state_change()

            # 重置NoReplyAction的连续计数器
            NoReplyAction.reset_consecutive_count()

            status_message = "决定退出专注聊天模式"
            return True, status_message

        except Exception as e:
            logger.error(f"{self.log_prefix} 退出专注聊天动作执行失败: {e}")
            return False, f"退出专注聊天失败: {str(e)}"

    def _mark_state_change(self):
        """标记状态切换请求"""
        # 通过action_data传递状态切换命令
        self.action_data["_system_command"] = "stop_focus_chat"
        logger.info(f"{self.log_prefix} 已标记状态切换命令: stop_focus_chat")


@register_plugin
class CoreActionsPlugin(BasePlugin):
    """核心动作插件

    系统内置插件，提供基础的聊天交互功能：
    - Reply: 回复动作
    - NoReply: 不回复动作
    - Emoji: 表情动作
    """

    # 插件基本信息
    plugin_name = "core_actions"
    plugin_description = "系统核心动作插件，提供基础聊天交互功能"
    plugin_version = "1.0.0"
    plugin_author = "MaiBot团队"
    enable_plugin = True
    config_file_name = "config.toml"

    # 配置节描述
    config_section_descriptions = {
        "plugin": "插件基本信息配置",
        "components": "核心组件启用配置",
        "no_reply": "不回复动作配置",
        "emoji": "表情动作配置",
    }

    # 配置Schema定义
    config_schema = {
        "plugin": {
            "name": ConfigField(type=str, default="core_actions", description="插件名称", required=True),
            "version": ConfigField(type=str, default="1.0.0", description="插件版本号"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "description": ConfigField(
                type=str, default="系统核心动作插件，提供基础聊天交互功能", description="插件描述", required=True
            ),
        },
        "components": {
            "enable_reply": ConfigField(type=bool, default=True, description="是否启用'回复'动作"),
            "enable_no_reply": ConfigField(type=bool, default=True, description="是否启用'不回复'动作"),
            "enable_emoji": ConfigField(type=bool, default=True, description="是否启用'表情'动作"),
            "enable_change_to_focus": ConfigField(type=bool, default=True, description="是否启用'切换到专注模式'动作"),
            "enable_exit_focus": ConfigField(type=bool, default=True, description="是否启用'退出专注模式'动作"),
            "enable_ping_command": ConfigField(type=bool, default=True, description="是否启用'/ping'测试命令"),
            "enable_log_command": ConfigField(type=bool, default=True, description="是否启用'/log'日志命令"),
        },
        "no_reply": {
            "waiting_timeout": ConfigField(
                type=int, default=1200, description="连续不回复时，最长的等待超时时间（秒）"
            ),
            "stage_1_wait": ConfigField(type=int, default=10, description="第1次连续不回复的等待时间（秒）"),
            "stage_2_wait": ConfigField(type=int, default=60, description="第2次连续不回复的等待时间（秒）"),
            "stage_3_wait": ConfigField(type=int, default=600, description="第3次连续不回复的等待时间（秒）"),
        },
        "emoji": {
            "random_probability": ConfigField(
                type=float, default=0.1, description="Normal模式下，随机发送表情的概率（0.0到1.0）", example=0.15
            )
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件包含的组件列表"""

        # --- 从配置动态设置Action/Command ---
        emoji_chance = self.get_config("emoji.random_probability", 0.1)
        EmojiAction.random_activation_probability = emoji_chance

        no_reply_timeout = self.get_config("no_reply.waiting_timeout", 1200)
        NoReplyAction.waiting_timeout = no_reply_timeout

        stage1 = self.get_config("no_reply.stage_1_wait", 10)
        stage2 = self.get_config("no_reply.stage_2_wait", 60)
        stage3 = self.get_config("no_reply.stage_3_wait", 600)
        NoReplyAction._waiting_stages = [stage1, stage2, stage3]

        # --- 根据配置注册组件 ---
        components = []
        if self.get_config("components.enable_reply", True):
            components.append((ReplyAction.get_action_info(), ReplyAction))
        if self.get_config("components.enable_no_reply", True):
            components.append((NoReplyAction.get_action_info(), NoReplyAction))
        if self.get_config("components.enable_emoji", True):
            components.append((EmojiAction.get_action_info(), EmojiAction))
        if self.get_config("components.enable_exit_focus", True):
            components.append((ExitFocusChatAction.get_action_info(), ExitFocusChatAction))
        if self.get_config("components.enable_change_to_focus", True):
            components.append((ChangeToFocusChatAction.get_action_info(), ChangeToFocusChatAction))
        if self.get_config("components.enable_ping_command", True):
            components.append(
                (PingCommand.get_command_info(name="ping", description="测试机器人响应，拦截后续处理"), PingCommand)
            )
        if self.get_config("components.enable_log_command", True):
            components.append(
                (LogCommand.get_command_info(name="log", description="记录消息到日志，不拦截后续处理"), LogCommand)
            )

        return components


# ===== 示例Command组件 =====


class PingCommand(BaseCommand):
    """Ping命令 - 测试响应，拦截消息处理"""

    command_pattern = r"^/ping(\s+(?P<message>.+))?$"
    command_help = "测试机器人响应 - 拦截后续处理"
    command_examples = ["/ping", "/ping 测试消息"]
    intercept_message = True  # 拦截消息，不继续处理

    async def execute(self) -> Tuple[bool, Optional[str]]:
        """执行ping命令"""
        try:
            message = self.matched_groups.get("message", "")
            reply_text = f"🏓 Pong! {message}" if message else "🏓 Pong!"

            await self.send_text(reply_text)
            return True, f"发送ping响应: {reply_text}"

        except Exception as e:
            logger.error(f"Ping命令执行失败: {e}")
            return False, f"执行失败: {str(e)}"


class LogCommand(BaseCommand):
    """日志命令 - 记录消息但不拦截后续处理"""

    command_pattern = r"^/log(\s+(?P<level>debug|info|warn|error))?$"
    command_help = "记录当前消息到日志 - 不拦截后续处理"
    command_examples = ["/log", "/log info", "/log debug"]
    intercept_message = False  # 不拦截消息，继续后续处理

    async def execute(self) -> Tuple[bool, Optional[str]]:
        """执行日志命令"""
        try:
            level = self.matched_groups.get("level", "info")
            user_nickname = self.message.message_info.user_info.user_nickname
            content = self.message.processed_plain_text

            log_message = f"[{level.upper()}] 用户 {user_nickname}: {content}"

            # 根据级别记录日志
            if level == "debug":
                logger.debug(log_message)
            elif level == "warn":
                logger.warning(log_message)
            elif level == "error":
                logger.error(log_message)
            else:
                logger.info(log_message)

            # 不发送回复，让消息继续处理
            return True, f"已记录到{level}级别日志"

        except Exception as e:
            logger.error(f"Log命令执行失败: {e}")
            return False, f"执行失败: {str(e)}"
