import traceback
from typing import List, Optional, Dict, Any, Tuple

from src.chat.message_receive.message import MessageRecv, MessageThinking, MessageSending
from src.chat.message_receive.message import Seg  # Local import needed after move
from src.chat.message_receive.message import UserInfo
from src.chat.message_receive.chat_stream import get_chat_manager
from src.common.logger import get_logger
from src.llm_models.utils_model import LLMRequest
from src.config.config import global_config
from src.chat.utils.utils_image import image_path_to_base64  # Local import needed after move
from src.chat.utils.timer_calculator import Timer  # <--- Import Timer
from src.chat.emoji_system.emoji_manager import get_emoji_manager
from src.chat.focus_chat.heartFC_sender import HeartFCSender
from src.chat.utils.utils import process_llm_response
from src.chat.heart_flow.utils_chat import get_chat_type_and_target_info
from src.chat.message_receive.chat_stream import ChatStream
from src.chat.focus_chat.hfc_utils import parse_thinking_id_to_timestamp
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager
from src.chat.utils.chat_message_builder import build_readable_messages, get_raw_msg_before_timestamp_with_chat
import time
import random
from datetime import datetime
import re
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

logger = get_logger("replyer")


def init_prompt():
    Prompt(
        """
{expression_habits_block}
        
{extra_info_block}

{relation_info_block}

{time_block}
{chat_target}
{chat_info}
{reply_target_block}
{identity}
你需要使用合适的语言习惯和句法，参考聊天内容，组织一条日常且口语化的回复。注意不要复读你说过的话。
{config_expression_style}，请注意不要输出多余内容(包括前后缀，冒号和引号，括号()，表情包，at或 @等 )。只输出回复内容。
{keywords_reaction_prompt}
请不要输出违法违规内容，不要输出色情，暴力，政治相关内容，如有敏感内容，请规避。
不要浮夸，不要夸张修辞，只输出一条回复就好。
现在，你说：
""",
        "default_replyer_prompt",
    )

    Prompt(
        """
{expression_habits_block}
{extra_info_block}
{time_block}
{chat_target}
{chat_info}
现在"{sender_name}"说的:{target_message}。引起了你的注意，你想要发言或者回复这条消息。
{identity}，
你需要使用合适的语法和句法，参考聊天内容，组织一条日常且口语化的回复。注意不要复读你说过的话。

{config_expression_style}，请注意不要输出多余内容(包括前后缀，冒号和引号，括号()，表情包，at或 @等 )。只输出回复内容。
{keywords_reaction_prompt}
请不要输出违法违规内容，不要输出色情，暴力，政治相关内容，如有敏感内容，请规避。
不要浮夸，不要夸张修辞，只输出一条回复就好。
现在，你说：
""",
        "default_replyer_private_prompt",
    )


class DefaultReplyer:
    def __init__(self, chat_stream: ChatStream):
        self.log_prefix = "replyer"
        # TODO: API-Adapter修改标记
        self.express_model = LLMRequest(
            model=global_config.model.replyer_1,
            request_type="focus.replyer",
        )
        self.heart_fc_sender = HeartFCSender()

        self.chat_id = chat_stream.stream_id
        self.chat_stream = chat_stream
        self.is_group_chat, self.chat_target_info = get_chat_type_and_target_info(self.chat_id)

    async def _create_thinking_message(self, anchor_message: Optional[MessageRecv], thinking_id: str):
        """创建思考消息 (尝试锚定到 anchor_message)"""
        if not anchor_message or not anchor_message.chat_stream:
            logger.error(f"{self.log_prefix} 无法创建思考消息，缺少有效的锚点消息或聊天流。")
            return None

        chat = anchor_message.chat_stream
        messageinfo = anchor_message.message_info
        thinking_time_point = parse_thinking_id_to_timestamp(thinking_id)
        bot_user_info = UserInfo(
            user_id=global_config.bot.qq_account,
            user_nickname=global_config.bot.nickname,
            platform=messageinfo.platform,
        )

        thinking_message = MessageThinking(
            message_id=thinking_id,
            chat_stream=chat,
            bot_user_info=bot_user_info,
            reply=anchor_message,  # 回复的是锚点消息
            thinking_start_time=thinking_time_point,
        )
        # logger.debug(f"创建思考消息thinking_message：{thinking_message}")

        await self.heart_fc_sender.register_thinking(thinking_message)
        return None

    async def deal_reply(
        self,
        cycle_timers: dict,
        action_data: Dict[str, Any],
        reasoning: str,
        anchor_message: MessageRecv,
        thinking_id: str,
    ) -> tuple[bool, Optional[List[Tuple[str, str]]]]:
        # 创建思考消息
        await self._create_thinking_message(anchor_message, thinking_id)

        reply = []  # 初始化 reply，防止未定义
        try:
            has_sent_something = False

            # 处理文本部分
            # text_part = action_data.get("text", [])
            # if text_part:
            sent_msg_list = []

            with Timer("生成回复", cycle_timers):
                # 可以保留原有的文本处理逻辑或进行适当调整
                reply = await self.reply(
                    # in_mind_reply=text_part,
                    anchor_message=anchor_message,
                    thinking_id=thinking_id,
                    reason=reasoning,
                    action_data=action_data,
                )

            if reply:
                with Timer("发送消息", cycle_timers):
                    sent_msg_list = await self.send_response_messages(
                        anchor_message=anchor_message,
                        thinking_id=thinking_id,
                        response_set=reply,
                    )
                has_sent_something = True
            else:
                logger.warning(f"{self.log_prefix} 文本回复生成失败")

            if not has_sent_something:
                logger.warning(f"{self.log_prefix} 回复动作未包含任何有效内容")

            return has_sent_something, sent_msg_list

        except Exception as e:
            logger.error(f"回复失败: {e}")
            traceback.print_exc()
            return False, None

        # --- 回复器 (Replier) 的定义 --- #

    async def deal_emoji(
        self,
        anchor_message: MessageRecv,
        thinking_id: str,
        action_data: Dict[str, Any],
        cycle_timers: dict,
    ) -> Optional[List[str]]:
        """
        表情动作处理类
        """

        await self._create_thinking_message(anchor_message, thinking_id)

        try:
            has_sent_something = False
            sent_msg_list = []
            reply = []
            with Timer("选择表情", cycle_timers):
                emoji_keyword = action_data.get("description", [])
                emoji_base64, _description, emotion = await self._choose_emoji(emoji_keyword)
                if emoji_base64:
                    # logger.info(f"选择表情: {_description}")
                    reply.append(("emoji", emoji_base64))
                else:
                    logger.warning(f"{self.log_prefix} 没有找到合适表情")

            if reply:
                with Timer("发送表情", cycle_timers):
                    sent_msg_list = await self.send_response_messages(
                        anchor_message=anchor_message,
                        thinking_id=thinking_id,
                        response_set=reply,
                    )
                has_sent_something = True
            else:
                logger.warning(f"{self.log_prefix} 表情发送失败")

            if not has_sent_something:
                logger.warning(f"{self.log_prefix} 表情发送失败")

            return has_sent_something, sent_msg_list

        except Exception as e:
            logger.error(f"回复失败: {e}")
            traceback.print_exc()
            return False, None

    async def reply(
        self,
        # in_mind_reply: str,
        reason: str,
        anchor_message: MessageRecv,
        thinking_id: str,
        action_data: Dict[str, Any],
    ) -> Optional[List[str]]:
        """
        回复器 (Replier): 核心逻辑，负责生成回复文本。
        (已整合原 HeartFCGenerator 的功能)
        """
        try:
            # 1. 获取情绪影响因子并调整模型温度
            # arousal_multiplier = mood_manager.get_arousal_multiplier()
            # current_temp = float(global_config.model.normal["temp"]) * arousal_multiplier
            # self.express_model.params["temperature"] = current_temp  # 动态调整温度

            reply_to = action_data.get("reply_to", "none")

            sender = ""
            targer = ""
            if ":" in reply_to or "：" in reply_to:
                # 使用正则表达式匹配中文或英文冒号
                parts = re.split(pattern=r"[:：]", string=reply_to, maxsplit=1)
                if len(parts) == 2:
                    sender = parts[0].strip()
                    targer = parts[1].strip()

            identity = action_data.get("identity", "")
            extra_info_block = action_data.get("extra_info_block", "")
            relation_info_block = action_data.get("relation_info_block", "")

            # 3. 构建 Prompt
            with Timer("构建Prompt", {}):  # 内部计时器，可选保留
                prompt = await self.build_prompt_focus(
                    chat_stream=self.chat_stream,  # Pass the stream object
                    # in_mind_reply=in_mind_reply,
                    identity=identity,
                    extra_info_block=extra_info_block,
                    relation_info_block=relation_info_block,
                    reason=reason,
                    sender_name=sender,  # Pass determined name
                    target_message=targer,
                    config_expression_style=global_config.expression.expression_style,
                    action_data=action_data,  # 传递action_data
                )

            # 4. 调用 LLM 生成回复
            content = None
            reasoning_content = None
            model_name = "unknown_model"
            if not prompt:
                logger.error(f"{self.log_prefix}[Replier-{thinking_id}] Prompt 构建失败，无法生成回复。")
                return None

            try:
                with Timer("LLM生成", {}):  # 内部计时器，可选保留
                    logger.info(f"{self.log_prefix}Prompt:\n{prompt}\n")
                    content, (reasoning_content, model_name) = await self.express_model.generate_response_async(prompt)

                    # logger.info(f"prompt: {prompt}")
                    logger.info(f"最终回复: {content}")

            except Exception as llm_e:
                # 精简报错信息
                logger.error(f"{self.log_prefix}LLM 生成失败: {llm_e}")
                return None  # LLM 调用失败则无法生成回复

            processed_response = process_llm_response(content)

            # 5. 处理 LLM 响应
            if not content:
                logger.warning(f"{self.log_prefix}LLM 生成了空内容。")
                return None
            if not processed_response:
                logger.warning(f"{self.log_prefix}处理后的回复为空。")
                return None

            reply_set = []
            for str in processed_response:
                reply_seg = ("text", str)
                reply_set.append(reply_seg)

            return reply_set

        except Exception as e:
            logger.error(f"{self.log_prefix}回复生成意外失败: {e}")
            traceback.print_exc()
            return None

    async def build_prompt_focus(
        self,
        reason,
        chat_stream,
        sender_name,
        # in_mind_reply,
        extra_info_block,
        relation_info_block,
        identity,
        target_message,
        config_expression_style,
        action_data=None,
        # stuation,
    ) -> str:
        is_group_chat = bool(chat_stream.group_info)

        message_list_before_now = get_raw_msg_before_timestamp_with_chat(
            chat_id=chat_stream.stream_id,
            timestamp=time.time(),
            limit=global_config.focus_chat.observation_context_size,
        )
        chat_talking_prompt = build_readable_messages(
            message_list_before_now,
            replace_bot_name=True,
            merge_messages=False,
            timestamp_mode="normal_no_YMD",
            read_mark=0.0,
            truncate=True,
            show_actions=True,
        )

        style_habbits = []
        grammar_habbits = []

        # 使用从处理器传来的选中表达方式
        selected_expressions = action_data.get("selected_expressions", []) if action_data else []

        if selected_expressions:
            logger.info(f"{self.log_prefix} 使用处理器选中的{len(selected_expressions)}个表达方式")
            for expr in selected_expressions:
                if isinstance(expr, dict) and "situation" in expr and "style" in expr:
                    expr_type = expr.get("type", "style")
                    if expr_type == "grammar":
                        grammar_habbits.append(f"当{expr['situation']}时，使用 {expr['style']}")
                    else:
                        style_habbits.append(f"当{expr['situation']}时，使用 {expr['style']}")
        else:
            logger.debug(f"{self.log_prefix} 没有从处理器获得表达方式，将使用空的表达方式")
            # 不再在replyer中进行随机选择，全部交给处理器处理

        style_habbits_str = "\n".join(style_habbits)
        grammar_habbits_str = "\n".join(grammar_habbits)

        # 动态构建expression habits块
        expression_habits_block = ""
        if style_habbits_str.strip():
            expression_habits_block += f"你可以参考以下的语言习惯，如果情景合适就使用，不要盲目使用,不要生硬使用，而是结合到表达中：\n{style_habbits_str}\n\n"
        if grammar_habbits_str.strip():
            expression_habits_block += f"请你根据情景使用以下句法：\n{grammar_habbits_str}\n"

        # 关键词检测与反应
        keywords_reaction_prompt = ""
        try:
            # 处理关键词规则
            for rule in global_config.keyword_reaction.keyword_rules:
                if any(keyword in target_message for keyword in rule.keywords):
                    logger.info(f"检测到关键词规则：{rule.keywords}，触发反应：{rule.reaction}")
                    keywords_reaction_prompt += f"{rule.reaction}，"

            # 处理正则表达式规则
            for rule in global_config.keyword_reaction.regex_rules:
                for pattern_str in rule.regex:
                    try:
                        pattern = re.compile(pattern_str)
                        if result := pattern.search(target_message):
                            reaction = rule.reaction
                            for name, content in result.groupdict().items():
                                reaction = reaction.replace(f"[{name}]", content)
                            logger.info(f"匹配到正则表达式：{pattern_str}，触发反应：{reaction}")
                            keywords_reaction_prompt += reaction + "，"
                            break
                    except re.error as e:
                        logger.error(f"正则表达式编译错误: {pattern_str}, 错误信息: {str(e)}")
                        continue
        except Exception as e:
            logger.error(f"关键词检测与反应时发生异常: {str(e)}", exc_info=True)

        time_block = f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        # logger.debug("开始构建 focus prompt")

        if sender_name:
            reply_target_block = (
                f"现在{sender_name}说的:{target_message}。引起了你的注意，你想要在群里发言或者回复这条消息。"
            )
        elif target_message:
            reply_target_block = f"现在{target_message}引起了你的注意，你想要在群里发言或者回复这条消息。"
        else:
            reply_target_block = "现在，你想要在群里发言或者回复消息。"

        # --- Choose template based on chat type ---
        if is_group_chat:
            template_name = "default_replyer_prompt"
            # Group specific formatting variables (already fetched or default)
            chat_target_1 = await global_prompt_manager.get_prompt_async("chat_target_group1")
            # chat_target_2 = await global_prompt_manager.get_prompt_async("chat_target_group2")

            prompt = await global_prompt_manager.format_prompt(
                template_name,
                expression_habits_block=expression_habits_block,
                chat_target=chat_target_1,
                chat_info=chat_talking_prompt,
                extra_info_block=extra_info_block,
                relation_info_block=relation_info_block,
                time_block=time_block,
                reply_target_block=reply_target_block,
                # bot_name=global_config.bot.nickname,
                # prompt_personality="",
                # reason=reason,
                # in_mind_reply=in_mind_reply,
                keywords_reaction_prompt=keywords_reaction_prompt,
                identity=identity,
                target_message=target_message,
                sender_name=sender_name,
                config_expression_style=config_expression_style,
            )
        else:  # Private chat
            template_name = "default_replyer_private_prompt"
            chat_target_1 = "你正在和人私聊"
            prompt = await global_prompt_manager.format_prompt(
                template_name,
                expression_habits_block=expression_habits_block,
                chat_target=chat_target_1,
                chat_info=chat_talking_prompt,
                extra_info_block=extra_info_block,
                relation_info_block=relation_info_block,
                time_block=time_block,
                reply_target_block=reply_target_block,
                # bot_name=global_config.bot.nickname,
                # prompt_personality="",
                # reason=reason,
                # in_mind_reply=in_mind_reply,
                keywords_reaction_prompt=keywords_reaction_prompt,
                identity=identity,
                target_message=target_message,
                sender_name=sender_name,
                config_expression_style=config_expression_style,
            )

        return prompt

        # --- 发送器 (Sender) --- #

    async def send_response_messages(
        self,
        anchor_message: Optional[MessageRecv],
        response_set: List[Tuple[str, str]],
        thinking_id: str = "",
        display_message: str = "",
    ) -> Optional[MessageSending]:
        """发送回复消息 (尝试锚定到 anchor_message)，使用 HeartFCSender"""
        chat = self.chat_stream
        chat_id = self.chat_id
        if chat is None:
            logger.error(f"{self.log_prefix} 无法发送回复，chat_stream 为空。")
            return None
        if not anchor_message:
            logger.error(f"{self.log_prefix} 无法发送回复，anchor_message 为空。")
            return None

        stream_name = get_chat_manager().get_stream_name(chat_id) or chat_id  # 获取流名称用于日志

        # 检查思考过程是否仍在进行，并获取开始时间
        if thinking_id:
            # print(f"thinking_id: {thinking_id}")
            thinking_start_time = await self.heart_fc_sender.get_thinking_start_time(chat_id, thinking_id)
        else:
            print("thinking_id is None")
            # thinking_id = "ds" + str(round(time.time(), 2))
            thinking_start_time = time.time()

        if thinking_start_time is None:
            logger.error(f"[{stream_name}]replyer思考过程未找到或已结束，无法发送回复。")
            return None

        mark_head = False
        # first_bot_msg: Optional[MessageSending] = None
        reply_message_ids = []  # 记录实际发送的消息ID

        sent_msg_list = []

        for i, msg_text in enumerate(response_set):
            # 为每个消息片段生成唯一ID
            type = msg_text[0]
            data = msg_text[1]

            if global_config.experimental.debug_show_chat_mode and type == "text":
                data += "ᶠ"

            part_message_id = f"{thinking_id}_{i}"
            message_segment = Seg(type=type, data=data)

            if type == "emoji":
                is_emoji = True
            else:
                is_emoji = False
            reply_to = not mark_head

            bot_message = await self._build_single_sending_message(
                anchor_message=anchor_message,
                message_id=part_message_id,
                message_segment=message_segment,
                display_message=display_message,
                reply_to=reply_to,
                is_emoji=is_emoji,
                thinking_id=thinking_id,
                thinking_start_time=thinking_start_time,
            )

            try:
                if not mark_head:
                    mark_head = True
                    # first_bot_msg = bot_message  # 保存第一个成功发送的消息对象
                    typing = False
                else:
                    typing = True

                if type == "emoji":
                    typing = False

                if anchor_message.raw_message:
                    set_reply = True
                else:
                    set_reply = False
                sent_msg = await self.heart_fc_sender.send_message(
                    bot_message, has_thinking=True, typing=typing, set_reply=set_reply
                )

                reply_message_ids.append(part_message_id)  # 记录我们生成的ID

                sent_msg_list.append((type, sent_msg))

            except Exception as e:
                logger.error(f"{self.log_prefix}发送回复片段 {i} ({part_message_id}) 时失败: {e}")
                traceback.print_exc()
                # 这里可以选择是继续发送下一个片段还是中止

        # 在尝试发送完所有片段后，完成原始的 thinking_id 状态
        try:
            await self.heart_fc_sender.complete_thinking(chat_id, thinking_id)

        except Exception as e:
            logger.error(f"{self.log_prefix}完成思考状态 {thinking_id} 时出错: {e}")

        return sent_msg_list

    async def _choose_emoji(self, send_emoji: str):
        """
        选择表情，根据send_emoji文本选择表情，返回表情base64
        """
        emoji_base64 = ""
        description = ""
        emoji_raw = await get_emoji_manager().get_emoji_for_text(send_emoji)
        if emoji_raw:
            emoji_path, description, _emotion = emoji_raw
            emoji_base64 = image_path_to_base64(emoji_path)
            return emoji_base64, description, _emotion
        else:
            return None, None, None

    async def _build_single_sending_message(
        self,
        anchor_message: MessageRecv,
        message_id: str,
        message_segment: Seg,
        reply_to: bool,
        is_emoji: bool,
        thinking_id: str,
        thinking_start_time: float,
        display_message: str,
    ) -> MessageSending:
        """构建单个发送消息"""

        bot_user_info = UserInfo(
            user_id=global_config.bot.qq_account,
            user_nickname=global_config.bot.nickname,
            platform=self.chat_stream.platform,
        )

        # await anchor_message.process()

        bot_message = MessageSending(
            message_id=message_id,  # 使用片段的唯一ID
            chat_stream=self.chat_stream,
            bot_user_info=bot_user_info,
            sender_info=anchor_message.message_info.user_info,
            message_segment=message_segment,
            reply=anchor_message,  # 回复原始锚点
            is_head=reply_to,
            is_emoji=is_emoji,
            thinking_start_time=thinking_start_time,  # 传递原始思考开始时间
            display_message=display_message,
        )

        return bot_message


def weighted_sample_no_replacement(items, weights, k) -> list:
    """
    加权且不放回地随机抽取k个元素。

    参数：
        items: 待抽取的元素列表
        weights: 每个元素对应的权重（与items等长，且为正数）
        k: 需要抽取的元素个数
    返回：
        selected: 按权重加权且不重复抽取的k个元素组成的列表

        如果 items 中的元素不足 k 个，就只会返回所有可用的元素

    实现思路：
        每次从当前池中按权重加权随机选出一个元素，选中后将其从池中移除，重复k次。
        这样保证了：
        1. count越大被选中概率越高
        2. 不会重复选中同一个元素
    """
    selected = []
    pool = list(zip(items, weights))
    for _ in range(min(k, len(pool))):
        total = sum(w for _, w in pool)
        r = random.uniform(0, total)
        upto = 0
        for idx, (item, weight) in enumerate(pool):
            upto += weight
            if upto >= r:
                selected.append(item)
                pool.pop(idx)
                break
    return selected


def find_similar_expressions(input_text: str, expressions: List[Dict], top_k: int = 3) -> List[Dict]:
    """使用TF-IDF和余弦相似度找出与输入文本最相似的top_k个表达方式"""
    if not expressions:
        return []

    # 准备文本数据
    texts = [expr["situation"] for expr in expressions]
    texts.append(input_text)  # 添加输入文本

    # 使用TF-IDF向量化
    vectorizer = TfidfVectorizer()
    tfidf_matrix = vectorizer.fit_transform(texts)

    # 计算余弦相似度
    similarity_matrix = cosine_similarity(tfidf_matrix)

    # 获取输入文本的相似度分数（最后一行）
    scores = similarity_matrix[-1][:-1]  # 排除与自身的相似度

    # 获取top_k的索引
    top_indices = np.argsort(scores)[::-1][:top_k]

    # 获取相似表达
    similar_exprs = []
    for idx in top_indices:
        if scores[idx] > 0:  # 只保留有相似度的
            similar_exprs.append(expressions[idx])

    return similar_exprs


init_prompt()
