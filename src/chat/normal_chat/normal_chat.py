import asyncio
import time
import traceback
from random import random
from typing import List, Optional  # 导入 Optional
from maim_message import UserInfo, Seg
from src.common.logger import get_logger
from src.chat.heart_flow.utils_chat import get_chat_type_and_target_info
from src.manager.mood_manager import mood_manager
from src.chat.message_receive.chat_stream import ChatStream, get_chat_manager
from src.chat.utils.timer_calculator import Timer
from src.chat.utils.prompt_builder import global_prompt_manager
from .normal_chat_generator import NormalChatGenerator
from ..message_receive.message import MessageSending, MessageRecv, MessageThinking, MessageSet
from src.chat.message_receive.message_sender import message_manager
from src.chat.normal_chat.willing.willing_manager import get_willing_manager
from src.chat.normal_chat.normal_chat_utils import get_recent_message_stats
from src.config.config import global_config
from src.chat.focus_chat.planners.action_manager import ActionManager
from src.chat.normal_chat.normal_chat_planner import NormalChatPlanner
from src.chat.normal_chat.normal_chat_action_modifier import NormalChatActionModifier
from src.chat.normal_chat.normal_chat_expressor import NormalChatExpressor
from src.chat.focus_chat.replyer.default_replyer import DefaultReplyer

willing_manager = get_willing_manager()

logger = get_logger("normal_chat")


class NormalChat:
    def __init__(self, chat_stream: ChatStream, interest_dict: dict = None, on_switch_to_focus_callback=None):
        """初始化 NormalChat 实例。只进行同步操作。"""

        self.chat_stream = chat_stream
        self.stream_id = chat_stream.stream_id
        self.stream_name = get_chat_manager().get_stream_name(self.stream_id) or self.stream_id

        # 初始化Normal Chat专用表达器
        self.expressor = NormalChatExpressor(self.chat_stream)
        self.replyer = DefaultReplyer(self.chat_stream)

        # Interest dict
        self.interest_dict = interest_dict

        self.is_group_chat, self.chat_target_info = get_chat_type_and_target_info(self.stream_id)

        self.willing_amplifier = 1
        self.start_time = time.time()

        # Other sync initializations
        self.gpt = NormalChatGenerator()
        self.mood_manager = mood_manager
        self.start_time = time.time()
        self._chat_task: Optional[asyncio.Task] = None
        self._initialized = False  # Track initialization status

        # Planner相关初始化
        self.action_manager = ActionManager()
        self.planner = NormalChatPlanner(self.stream_name, self.action_manager)
        self.action_modifier = NormalChatActionModifier(self.action_manager, self.stream_id, self.stream_name)
        self.enable_planner = global_config.normal_chat.enable_planner  # 从配置中读取是否启用planner

        # 记录最近的回复内容，每项包含: {time, user_message, response, is_mentioned, is_reference_reply}
        self.recent_replies = []
        self.max_replies_history = 20  # 最多保存最近20条回复记录

        # 添加回调函数，用于在满足条件时通知切换到focus_chat模式
        self.on_switch_to_focus_callback = on_switch_to_focus_callback

        self._disabled = False  # 增加停用标志

        logger.debug(f"[{self.stream_name}] NormalChat 初始化完成 (异步部分)。")

    # 改为实例方法
    async def _create_thinking_message(self, message: MessageRecv, timestamp: Optional[float] = None) -> str:
        """创建思考消息"""
        messageinfo = message.message_info

        bot_user_info = UserInfo(
            user_id=global_config.bot.qq_account,
            user_nickname=global_config.bot.nickname,
            platform=messageinfo.platform,
        )

        thinking_time_point = round(time.time(), 2)
        thinking_id = "tid" + str(thinking_time_point)
        thinking_message = MessageThinking(
            message_id=thinking_id,
            chat_stream=self.chat_stream,
            bot_user_info=bot_user_info,
            reply=message,
            thinking_start_time=thinking_time_point,
            timestamp=timestamp if timestamp is not None else None,
        )

        await message_manager.add_message(thinking_message)
        return thinking_id

    # 改为实例方法
    async def _add_messages_to_manager(
        self, message: MessageRecv, response_set: List[str], thinking_id
    ) -> Optional[MessageSending]:
        """发送回复消息"""
        container = await message_manager.get_container(self.stream_id)  # 使用 self.stream_id
        thinking_message = None

        for msg in container.messages[:]:
            if isinstance(msg, MessageThinking) and msg.message_info.message_id == thinking_id:
                thinking_message = msg
                container.messages.remove(msg)
                break

        if not thinking_message:
            logger.warning(f"[{self.stream_name}] 未找到对应的思考消息 {thinking_id}，可能已超时被移除")
            return None

        thinking_start_time = thinking_message.thinking_start_time
        message_set = MessageSet(self.chat_stream, thinking_id)  # 使用 self.chat_stream

        mark_head = False
        first_bot_msg = None
        for msg in response_set:
            if global_config.experimental.debug_show_chat_mode:
                msg += "ⁿ"
            message_segment = Seg(type="text", data=msg)
            bot_message = MessageSending(
                message_id=thinking_id,
                chat_stream=self.chat_stream,  # 使用 self.chat_stream
                bot_user_info=UserInfo(
                    user_id=global_config.bot.qq_account,
                    user_nickname=global_config.bot.nickname,
                    platform=message.message_info.platform,
                ),
                sender_info=message.message_info.user_info,
                message_segment=message_segment,
                reply=message,
                is_head=not mark_head,
                is_emoji=False,
                thinking_start_time=thinking_start_time,
                apply_set_reply_logic=True,
            )
            if not mark_head:
                mark_head = True
                first_bot_msg = bot_message
            message_set.add_message(bot_message)

        await message_manager.add_message(message_set)

        return first_bot_msg

    async def _reply_interested_message(self) -> None:
        """
        后台任务方法，轮询当前实例关联chat的兴趣消息
        通常由start_monitoring_interest()启动
        """
        while True:
            async with global_prompt_manager.async_message_scope(self.chat_stream.context.get_template_name()):
                await asyncio.sleep(0.5)  # 每秒检查一次
                # 检查任务是否已被取消
                if self._chat_task is None or self._chat_task.cancelled():
                    logger.info(f"[{self.stream_name}] 兴趣监控任务被取消或置空，退出")
                    break

                items_to_process = list(self.interest_dict.items())
                if not items_to_process:
                    continue

                # 处理每条兴趣消息
                for msg_id, (message, interest_value, is_mentioned) in items_to_process:
                    try:
                        # 处理消息
                        if time.time() - self.start_time > 300:
                            self.adjust_reply_frequency(duration=300 / 60)
                        else:
                            self.adjust_reply_frequency(duration=(time.time() - self.start_time) / 60)

                        await self.normal_response(
                            message=message,
                            is_mentioned=is_mentioned,
                            interested_rate=interest_value * self.willing_amplifier,
                        )
                    except Exception as e:
                        logger.error(f"[{self.stream_name}] 处理兴趣消息{msg_id}时出错: {e}\n{traceback.format_exc()}")
                    finally:
                        self.interest_dict.pop(msg_id, None)

    # 改为实例方法, 移除 chat 参数
    async def normal_response(self, message: MessageRecv, is_mentioned: bool, interested_rate: float) -> None:
        # 新增：如果已停用，直接返回
        if self._disabled:
            logger.info(f"[{self.stream_name}] 已停用，忽略 normal_response。")
            return

        timing_results = {}
        reply_probability = (
            1.0 if is_mentioned and global_config.normal_chat.mentioned_bot_inevitable_reply else 0.0
        )  # 如果被提及，且开启了提及必回复，则基础概率为1，否则需要意愿判断

        # 意愿管理器：设置当前message信息
        willing_manager.setup(message, self.chat_stream, is_mentioned, interested_rate)

        # 获取回复概率
        # is_willing = False
        # 仅在未被提及或基础概率不为1时查询意愿概率
        if reply_probability < 1:  # 简化逻辑，如果未提及 (reply_probability 为 0)，则获取意愿概率
            # is_willing = True
            reply_probability = await willing_manager.get_reply_probability(message.message_info.message_id)

            if message.message_info.additional_config:
                if "maimcore_reply_probability_gain" in message.message_info.additional_config.keys():
                    reply_probability += message.message_info.additional_config["maimcore_reply_probability_gain"]
                    reply_probability = min(max(reply_probability, 0), 1)  # 确保概率在 0-1 之间

        # 打印消息信息
        mes_name = self.chat_stream.group_info.group_name if self.chat_stream.group_info else "私聊"
        # current_time = time.strftime("%H:%M:%S", time.localtime(message.message_info.time))
        # 使用 self.stream_id
        # willing_log = f"[激活值:{await willing_manager.get_willing(self.stream_id):.2f}]" if is_willing else ""
        logger.info(
            f"[{mes_name}]"
            f"{message.message_info.user_info.user_nickname}:"  # 使用 self.chat_stream
            f"{message.processed_plain_text}[兴趣:{interested_rate:.2f}][回复概率:{reply_probability * 100:.1f}%]"
        )
        do_reply = False
        response_set = None  # 初始化 response_set
        if random() < reply_probability:
            do_reply = True

            # 回复前处理
            await willing_manager.before_generate_reply_handle(message.message_info.message_id)

            thinking_id = await self._create_thinking_message(message)

            logger.debug(f"[{self.stream_name}] 创建捕捉器，thinking_id:{thinking_id}")

            # 如果启用planner，预先修改可用actions（避免在并行任务中重复调用）
            available_actions = None
            if self.enable_planner:
                try:
                    await self.action_modifier.modify_actions_for_normal_chat(
                        self.chat_stream, self.recent_replies, message.processed_plain_text
                    )
                    available_actions = self.action_manager.get_using_actions()
                except Exception as e:
                    logger.warning(f"[{self.stream_name}] 获取available_actions失败: {e}")
                    available_actions = None

            # 定义并行执行的任务
            async def generate_normal_response():
                """生成普通回复"""
                try:
                    return await self.gpt.generate_response(
                        message=message,
                        thinking_id=thinking_id,
                        enable_planner=self.enable_planner,
                        available_actions=available_actions,
                    )
                except Exception as e:
                    logger.error(f"[{self.stream_name}] 回复生成出现错误：{str(e)} {traceback.format_exc()}")
                    return None

            async def plan_and_execute_actions():
                """规划和执行额外动作"""
                if not self.enable_planner:
                    logger.debug(f"[{self.stream_name}] Planner未启用，跳过动作规划")
                    return None

                try:
                    # 获取发送者名称（动作修改已在并行执行前完成）
                    sender_name = self._get_sender_name(message)

                    no_action = {
                        "action_result": {
                            "action_type": "no_action",
                            "action_data": {},
                            "reasoning": "规划器初始化默认",
                            "is_parallel": True,
                        },
                        "chat_context": "",
                        "action_prompt": "",
                    }

                    # 检查是否应该跳过规划
                    if self.action_modifier.should_skip_planning():
                        logger.debug(f"[{self.stream_name}] 没有可用动作，跳过规划")
                        self.action_type = "no_action"
                        return no_action

                    # 执行规划
                    plan_result = await self.planner.plan(message, sender_name)
                    action_type = plan_result["action_result"]["action_type"]
                    action_data = plan_result["action_result"]["action_data"]
                    reasoning = plan_result["action_result"]["reasoning"]
                    is_parallel = plan_result["action_result"].get("is_parallel", False)

                    logger.info(
                        f"[{self.stream_name}] Planner决策: {action_type}, 理由: {reasoning}, 并行执行: {is_parallel}"
                    )
                    self.action_type = action_type  # 更新实例属性
                    self.is_parallel_action = is_parallel  # 新增：保存并行执行标志

                    # 如果规划器决定不执行任何动作
                    if action_type == "no_action":
                        logger.debug(f"[{self.stream_name}] Planner决定不执行任何额外动作")
                        return no_action
                    elif action_type == "change_to_focus_chat":
                        logger.info(f"[{self.stream_name}] Planner决定切换到focus聊天模式")
                        return None

                    # 执行额外的动作（不影响回复生成）
                    action_result = await self._execute_action(action_type, action_data, message, thinking_id)
                    if action_result is not None:
                        logger.info(f"[{self.stream_name}] 额外动作 {action_type} 执行完成")
                    else:
                        logger.warning(f"[{self.stream_name}] 额外动作 {action_type} 执行失败")

                    return {
                        "action_type": action_type,
                        "action_data": action_data,
                        "reasoning": reasoning,
                        "is_parallel": is_parallel,
                    }

                except Exception as e:
                    logger.error(f"[{self.stream_name}] Planner执行失败: {e}")
                    return no_action

            # 并行执行回复生成和动作规划
            self.action_type = None  # 初始化动作类型
            self.is_parallel_action = False  # 初始化并行动作标志
            with Timer("并行生成回复和规划", timing_results):
                response_set, plan_result = await asyncio.gather(
                    generate_normal_response(), plan_and_execute_actions(), return_exceptions=True
                )

            # 处理生成回复的结果
            if isinstance(response_set, Exception):
                logger.error(f"[{self.stream_name}] 回复生成异常: {response_set}")
                response_set = None

            # 处理规划结果（可选，不影响回复）
            if isinstance(plan_result, Exception):
                logger.error(f"[{self.stream_name}] 动作规划异常: {plan_result}")
            elif plan_result:
                logger.debug(f"[{self.stream_name}] 额外动作处理完成: {self.action_type}")

            if not response_set or (
                self.enable_planner
                and self.action_type not in ["no_action", "change_to_focus_chat"]
                and not self.is_parallel_action
            ):
                if not response_set:
                    logger.info(f"[{self.stream_name}] 模型未生成回复内容")
                elif (
                    self.enable_planner
                    and self.action_type not in ["no_action", "change_to_focus_chat"]
                    and not self.is_parallel_action
                ):
                    logger.info(f"[{self.stream_name}] 模型选择其他动作（非并行动作）")
                # 如果模型未生成回复，移除思考消息
                container = await message_manager.get_container(self.stream_id)  # 使用 self.stream_id
                for msg in container.messages[:]:
                    if isinstance(msg, MessageThinking) and msg.message_info.message_id == thinking_id:
                        container.messages.remove(msg)
                        logger.debug(f"[{self.stream_name}] 已移除未产生回复的思考消息 {thinking_id}")
                        break
                # 需要在此处也调用 not_reply_handle 和 delete 吗？
                # 如果是因为模型没回复，也算是一种 "未回复"
                await willing_manager.not_reply_handle(message.message_info.message_id)
                willing_manager.delete(message.message_info.message_id)
                return  # 不执行后续步骤

            # logger.info(f"[{self.stream_name}] 回复内容: {response_set}")

            if self._disabled:
                logger.info(f"[{self.stream_name}] 已停用，忽略 normal_response。")
                return

            # 发送回复 (不再需要传入 chat)
            with Timer("消息发送", timing_results):
                first_bot_msg = await self._add_messages_to_manager(message, response_set, thinking_id)

            # 检查 first_bot_msg 是否为 None (例如思考消息已被移除的情况)
            if first_bot_msg:
                # 记录回复信息到最近回复列表中
                reply_info = {
                    "time": time.time(),
                    "user_message": message.processed_plain_text,
                    "user_info": {
                        "user_id": message.message_info.user_info.user_id,
                        "user_nickname": message.message_info.user_info.user_nickname,
                    },
                    "response": response_set,
                    "is_mentioned": is_mentioned,
                    "is_reference_reply": message.reply is not None,  # 判断是否为引用回复
                    "timing": {k: round(v, 2) for k, v in timing_results.items()},
                }
                self.recent_replies.append(reply_info)
                # 保持最近回复历史在限定数量内
                if len(self.recent_replies) > self.max_replies_history:
                    self.recent_replies = self.recent_replies[-self.max_replies_history :]

                # 检查是否需要切换到focus模式
                if global_config.chat.chat_mode == "auto":
                    if self.action_type == "change_to_focus_chat":
                        logger.info(f"[{self.stream_name}] 检测到切换到focus聊天模式的请求")
                        if self.on_switch_to_focus_callback:
                            await self.on_switch_to_focus_callback()
                        else:
                            logger.warning(f"[{self.stream_name}] 没有设置切换到focus聊天模式的回调函数，无法执行切换")
                        return
                    else:
                        await self._check_switch_to_focus()
                        pass

            # with Timer("关系更新", timing_results):
            #     await self._update_relationship(message, response_set)

            # 回复后处理
            await willing_manager.after_generate_reply_handle(message.message_info.message_id)

        # 输出性能计时结果
        if do_reply and response_set:  # 确保 response_set 不是 None
            timing_str = " | ".join([f"{step}: {duration:.2f}秒" for step, duration in timing_results.items()])
            trigger_msg = message.processed_plain_text
            response_msg = " ".join(response_set)
            logger.info(
                f"[{self.stream_name}]回复消息: {trigger_msg[:30]}... | 回复内容: {response_msg[:30]}... | 计时: {timing_str}"
            )
        elif not do_reply:
            # 不回复处理
            await willing_manager.not_reply_handle(message.message_info.message_id)

        # 意愿管理器：注销当前message信息 (无论是否回复，只要处理过就删除)
        willing_manager.delete(message.message_info.message_id)

    # 改为实例方法, 移除 chat 参数

    async def start_chat(self):
        """启动聊天任务。"""  # Ensure initialized before starting tasks
        self._disabled = False  # 启动时重置停用标志

        if self._chat_task is None or self._chat_task.done():
            # logger.info(f"[{self.stream_name}] 开始处理兴趣消息...")
            polling_task = asyncio.create_task(self._reply_interested_message())
            polling_task.add_done_callback(lambda t: self._handle_task_completion(t))
            self._chat_task = polling_task
        else:
            logger.info(f"[{self.stream_name}] 聊天轮询任务已在运行中。")

    def _handle_task_completion(self, task: asyncio.Task):
        """任务完成回调处理"""
        if task is not self._chat_task:
            logger.warning(f"[{self.stream_name}] 收到未知任务回调")
            return
        try:
            if exc := task.exception():
                logger.error(f"[{self.stream_name}] 任务异常: {exc}")
                traceback.print_exc()
        except asyncio.CancelledError:
            logger.debug(f"[{self.stream_name}] 任务已取消")
        except Exception as e:
            logger.error(f"[{self.stream_name}] 回调处理错误: {e}")
        finally:
            if self._chat_task is task:
                self._chat_task = None
                logger.debug(f"[{self.stream_name}] 任务清理完成")

    # 改为实例方法, 移除 stream_id 参数
    async def stop_chat(self):
        """停止当前实例的兴趣监控任务。"""
        self._disabled = True  # 停止时设置停用标志
        if self._chat_task and not self._chat_task.done():
            task = self._chat_task
            logger.debug(f"[{self.stream_name}] 尝试取消normal聊天任务。")
            task.cancel()
            try:
                await task  # 等待任务响应取消
            except asyncio.CancelledError:
                logger.info(f"[{self.stream_name}] 结束一般聊天模式。")
            except Exception as e:
                # 回调函数 _handle_task_completion 会处理异常日志
                logger.warning(f"[{self.stream_name}] 等待监控任务取消时捕获到异常 (可能已在回调中记录): {e}")
            finally:
                # 确保任务状态更新，即使等待出错 (回调函数也会尝试更新)
                if self._chat_task is task:
                    self._chat_task = None

        # 清理所有未处理的思考消息
        try:
            container = await message_manager.get_container(self.stream_id)
            if container:
                # 查找并移除所有 MessageThinking 类型的消息
                thinking_messages = [msg for msg in container.messages[:] if isinstance(msg, MessageThinking)]
                if thinking_messages:
                    for msg in thinking_messages:
                        container.messages.remove(msg)
                    logger.info(f"[{self.stream_name}] 清理了 {len(thinking_messages)} 条未处理的思考消息。")
        except Exception as e:
            logger.error(f"[{self.stream_name}] 清理思考消息时出错: {e}")
            traceback.print_exc()

    # 获取最近回复记录的方法
    def get_recent_replies(self, limit: int = 10) -> List[dict]:
        """获取最近的回复记录

        Args:
            limit: 最大返回数量，默认10条

        Returns:
            List[dict]: 最近的回复记录列表，每项包含：
                time: 回复时间戳
                user_message: 用户消息内容
                user_info: 用户信息(user_id, user_nickname)
                response: 回复内容
                is_mentioned: 是否被提及(@)
                is_reference_reply: 是否为引用回复
                timing: 各阶段耗时
        """
        # 返回最近的limit条记录，按时间倒序排列
        return sorted(self.recent_replies[-limit:], key=lambda x: x["time"], reverse=True)

    async def _check_switch_to_focus(self) -> None:
        """检查是否满足切换到focus模式的条件"""
        if not self.on_switch_to_focus_callback:
            return  # 如果没有设置回调函数，直接返回
        current_time = time.time()

        time_threshold = 120 / global_config.chat.auto_focus_threshold
        reply_threshold = 6 * global_config.chat.auto_focus_threshold

        one_minute_ago = current_time - time_threshold

        # 统计1分钟内的回复数量
        recent_reply_count = sum(1 for reply in self.recent_replies if reply["time"] > one_minute_ago)
        if recent_reply_count > reply_threshold:
            logger.info(
                f"[{self.stream_name}] 检测到1分钟内回复数量({recent_reply_count})大于{reply_threshold}，触发切换到focus模式"
            )
            try:
                # 调用回调函数通知上层切换到focus模式
                await self.on_switch_to_focus_callback()
            except Exception as e:
                logger.error(f"[{self.stream_name}] 触发切换到focus模式时出错: {e}\n{traceback.format_exc()}")

    def adjust_reply_frequency(self, duration: int = 10):
        """
        调整回复频率
        """
        # 获取最近30分钟内的消息统计

        stats = get_recent_message_stats(minutes=duration, chat_id=self.stream_id)
        bot_reply_count = stats["bot_reply_count"]

        total_message_count = stats["total_message_count"]
        if total_message_count == 0:
            return
        logger.debug(
            f"[{self.stream_name}]({self.willing_amplifier}) 最近{duration}分钟 回复数量: {bot_reply_count}，消息总数: {total_message_count}"
        )

        # 计算回复频率
        _reply_frequency = bot_reply_count / total_message_count

        differ = global_config.normal_chat.talk_frequency - (bot_reply_count / duration)

        # 如果回复频率低于0.5，增加回复概率
        if differ > 0.1:
            mapped = 1 + (differ - 0.1) * 4 / 0.9
            mapped = max(1, min(5, mapped))
            logger.info(
                f"[{self.stream_name}] 回复频率低于{global_config.normal_chat.talk_frequency}，增加回复概率，differ={differ:.3f}，映射值={mapped:.2f}"
            )
            self.willing_amplifier += mapped * 0.1  # 你可以根据实际需要调整系数
        elif differ < -0.1:
            mapped = 1 - (differ + 0.1) * 4 / 0.9
            mapped = max(1, min(5, mapped))
            logger.info(
                f"[{self.stream_name}] 回复频率高于{global_config.normal_chat.talk_frequency}，减少回复概率，differ={differ:.3f}，映射值={mapped:.2f}"
            )
            self.willing_amplifier -= mapped * 0.1

        if self.willing_amplifier > 5:
            self.willing_amplifier = 5
        elif self.willing_amplifier < 0.1:
            self.willing_amplifier = 0.1

    def _get_sender_name(self, message: MessageRecv) -> str:
        """获取发送者名称，用于planner"""
        if message.chat_stream.user_info:
            user_info = message.chat_stream.user_info
            if user_info.user_cardname and user_info.user_nickname:
                return f"[{user_info.user_nickname}][群昵称：{user_info.user_cardname}]"
            elif user_info.user_nickname:
                return f"[{user_info.user_nickname}]"
            else:
                return f"用户({user_info.user_id})"
        return "某人"

    async def _execute_action(
        self, action_type: str, action_data: dict, message: MessageRecv, thinking_id: str
    ) -> Optional[bool]:
        """执行具体的动作，只返回执行成功与否"""
        try:
            # 创建动作处理器实例
            action_handler = self.action_manager.create_action(
                action_name=action_type,
                action_data=action_data,
                reasoning=action_data.get("reasoning", ""),
                cycle_timers={},  # normal_chat使用空的cycle_timers
                thinking_id=thinking_id,
                observations=[],  # normal_chat不使用observations
                expressor=self.expressor,  # 使用normal_chat专用的expressor
                replyer=self.replyer,
                chat_stream=self.chat_stream,
                log_prefix=self.stream_name,
                shutting_down=self._disabled,
            )

            if action_handler:
                # 执行动作
                result = await action_handler.handle_action()
                success = False

                if result and isinstance(result, tuple) and len(result) >= 2:
                    # handle_action返回 (success: bool, message: str)
                    success = result[0]
                elif result:
                    # 如果返回了其他结果，假设成功
                    success = True

                return success

        except Exception as e:
            logger.error(f"[{self.stream_name}] 执行动作 {action_type} 失败: {e}")

        return False

    def set_planner_enabled(self, enabled: bool):
        """设置是否启用planner"""
        self.enable_planner = enabled
        logger.info(f"[{self.stream_name}] Planner {'启用' if enabled else '禁用'}")

    def get_action_manager(self) -> ActionManager:
        """获取动作管理器实例"""
        return self.action_manager
