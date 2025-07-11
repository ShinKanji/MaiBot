from src.chat.focus_chat.observation.chatting_observation import ChattingObservation
from src.chat.focus_chat.observation.observation import Observation
from src.llm_models.utils_model import LLMRequest
from src.config.config import global_config
import time
import traceback
from src.common.logger import get_logger
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager
from src.chat.message_receive.chat_stream import get_chat_manager
from typing import List
from src.chat.focus_chat.observation.working_observation import WorkingMemoryObservation
from src.chat.focus_chat.working_memory.working_memory import WorkingMemory
from src.chat.focus_chat.info.info_base import InfoBase
from json_repair import repair_json
from src.chat.focus_chat.info.workingmemory_info import WorkingMemoryInfo
import asyncio
import json

logger = get_logger("processor")


def init_prompt():
    memory_proces_prompt = """
你的名字是{bot_name}

现在是{time_now}，你正在上网，和qq群里的网友们聊天，以下是正在进行的聊天内容：
{chat_observe_info}

以下是你已经总结的记忆摘要，你可以调取这些记忆查看内容来帮助你聊天，不要一次调取太多记忆，最多调取3个左右记忆：
{memory_str}

观察聊天内容和已经总结的记忆，思考如果有相近的记忆，请合并记忆，输出merge_memory，
合并记忆的格式为[["id1", "id2"], ["id3", "id4"],...]，你可以进行多组合并，但是每组合并只能有两个记忆id，不要输出其他内容

请根据聊天内容选择你需要调取的记忆并考虑是否添加新记忆，以JSON格式输出，格式如下：
```json
{{
    "selected_memory_ids": ["id1", "id2", ...]
    "merge_memory": [["id1", "id2"], ["id3", "id4"],...]
}}
```
"""
    Prompt(memory_proces_prompt, "prompt_memory_proces")


class WorkingMemoryProcessor:
    log_prefix = "工作记忆"

    def __init__(self, subheartflow_id: str):
        self.subheartflow_id = subheartflow_id

        self.llm_model = LLMRequest(
            model=global_config.model.planner,
            request_type="focus.processor.working_memory",
        )

        name = get_chat_manager().get_stream_name(self.subheartflow_id)
        self.log_prefix = f"[{name}] "

    async def process_info(self, observations: List[Observation] = None, *infos) -> List[InfoBase]:
        """处理信息对象

        Args:
            *infos: 可变数量的InfoBase类型的信息对象

        Returns:
            List[InfoBase]: 处理后的结构化信息列表
        """
        working_memory = None
        chat_info = ""
        chat_obs = None
        try:
            for observation in observations:
                if isinstance(observation, WorkingMemoryObservation):
                    working_memory = observation.get_observe_info()
                if isinstance(observation, ChattingObservation):
                    chat_info = observation.get_observe_info()
                    chat_obs = observation
                    # 检查是否有待压缩内容
            if chat_obs and chat_obs.compressor_prompt:
                logger.debug(f"{self.log_prefix} 压缩聊天记忆")
                await self.compress_chat_memory(working_memory, chat_obs)

            # 检查working_memory是否为None
            if working_memory is None:
                logger.debug(f"{self.log_prefix} 没有找到工作记忆观察，跳过处理")
                return []

            all_memory = working_memory.get_all_memories()
            if not all_memory:
                logger.debug(f"{self.log_prefix} 目前没有工作记忆，跳过提取")
                return []

            memory_prompts = []
            for memory in all_memory:
                memory_id = memory.id
                memory_brief = memory.brief
                memory_single_prompt = f"记忆id:{memory_id},记忆摘要:{memory_brief}\n"
                memory_prompts.append(memory_single_prompt)

            memory_choose_str = "".join(memory_prompts)

            # 使用提示模板进行处理
            prompt = (await global_prompt_manager.get_prompt_async("prompt_memory_proces")).format(
                bot_name=global_config.bot.nickname,
                time_now=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                chat_observe_info=chat_info,
                memory_str=memory_choose_str,
            )

            # 调用LLM处理记忆
            content = ""
            try:
                content, _ = await self.llm_model.generate_response_async(prompt=prompt)

                # print(f"prompt: {prompt}---------------------------------")
                # print(f"content: {content}---------------------------------")

                if not content:
                    logger.warning(f"{self.log_prefix} LLM返回空结果，处理工作记忆失败。")
                    return []
            except Exception as e:
                logger.error(f"{self.log_prefix} 执行LLM请求或处理响应时出错: {e}")
                logger.error(traceback.format_exc())
                return []

            # 解析LLM返回的JSON
            try:
                result = repair_json(content)
                if isinstance(result, str):
                    result = json.loads(result)
                if not isinstance(result, dict):
                    logger.error(f"{self.log_prefix} 解析LLM返回的JSON失败，结果不是字典类型: {type(result)}")
                    return []

                selected_memory_ids = result.get("selected_memory_ids", [])
                merge_memory = result.get("merge_memory", [])
            except Exception as e:
                logger.error(f"{self.log_prefix} 解析LLM返回的JSON失败: {e}")
                logger.error(traceback.format_exc())
                return []

            logger.debug(
                f"{self.log_prefix} 解析LLM返回的JSON,selected_memory_ids: {selected_memory_ids}, merge_memory: {merge_memory}"
            )

            # 根据selected_memory_ids，调取记忆
            memory_str = ""
            selected_ids = set(selected_memory_ids)  # 转换为集合以便快速查找

            # 遍历所有记忆
            for memory in all_memory:
                if memory.id in selected_ids:
                    # 选中的记忆显示详细内容
                    memory = await working_memory.retrieve_memory(memory.id)
                    if memory:
                        memory_str += f"{memory.summary}\n"
                else:
                    # 未选中的记忆显示梗概
                    memory_str += f"{memory.brief}\n"

            working_memory_info = WorkingMemoryInfo()
            if memory_str:
                working_memory_info.add_working_memory(memory_str)
                logger.debug(f"{self.log_prefix} 取得工作记忆: {memory_str}")
            else:
                logger.debug(f"{self.log_prefix} 没有找到工作记忆")

            if merge_memory:
                for merge_pairs in merge_memory:
                    memory1 = await working_memory.retrieve_memory(merge_pairs[0])
                    memory2 = await working_memory.retrieve_memory(merge_pairs[1])
                    if memory1 and memory2:
                        asyncio.create_task(self.merge_memory_async(working_memory, merge_pairs[0], merge_pairs[1]))

            return [working_memory_info]
        except Exception as e:
            logger.error(f"{self.log_prefix} 处理观察时出错: {e}")
            logger.error(traceback.format_exc())
            return []

    async def compress_chat_memory(self, working_memory: WorkingMemory, obs: ChattingObservation):
        """压缩聊天记忆

        Args:
            working_memory: 工作记忆对象
            obs: 聊天观察对象
        """
        # 检查working_memory是否为None
        if working_memory is None:
            logger.warning(f"{self.log_prefix} 工作记忆对象为None，无法压缩聊天记忆")
            return

        try:
            summary_result, _ = await self.llm_model.generate_response_async(obs.compressor_prompt)
            if not summary_result:
                logger.debug(f"{self.log_prefix} 压缩聊天记忆失败: 没有生成摘要")
                return

            print(f"compressor_prompt: {obs.compressor_prompt}")
            print(f"summary_result: {summary_result}")

            # 修复并解析JSON
            try:
                fixed_json = repair_json(summary_result)
                summary_data = json.loads(fixed_json)

                if not isinstance(summary_data, dict):
                    logger.error(f"{self.log_prefix} 解析压缩结果失败: 不是有效的JSON对象")
                    return

                theme = summary_data.get("theme", "")
                content = summary_data.get("content", "")

                if not theme or not content:
                    logger.error(f"{self.log_prefix} 解析压缩结果失败: 缺少必要字段")
                    return

                # 创建新记忆
                await working_memory.add_memory(from_source="chat_compress", summary=content, brief=theme)

                logger.debug(f"{self.log_prefix} 压缩聊天记忆成功: {theme} - {content}")

            except Exception as e:
                logger.error(f"{self.log_prefix} 解析压缩结果失败: {e}")
                logger.error(traceback.format_exc())
                return

            # 清理压缩状态
            obs.compressor_prompt = ""
            obs.oldest_messages = []
            obs.oldest_messages_str = ""

        except Exception as e:
            logger.error(f"{self.log_prefix} 压缩聊天记忆失败: {e}")
            logger.error(traceback.format_exc())

    async def merge_memory_async(self, working_memory: WorkingMemory, memory_id1: str, memory_id2: str):
        """异步合并记忆，不阻塞主流程

        Args:
            working_memory: 工作记忆对象
            memory_id1: 第一个记忆ID
            memory_id2: 第二个记忆ID
        """
        # 检查working_memory是否为None
        if working_memory is None:
            logger.warning(f"{self.log_prefix} 工作记忆对象为None，无法合并记忆")
            return

        try:
            merged_memory = await working_memory.merge_memory(memory_id1, memory_id2)
            logger.debug(f"{self.log_prefix} 合并后的记忆梗概: {merged_memory.brief}")
            logger.debug(f"{self.log_prefix} 合并后的记忆内容: {merged_memory.summary}")

        except Exception as e:
            logger.error(f"{self.log_prefix} 异步合并记忆失败: {e}")
            logger.error(traceback.format_exc())


init_prompt()
