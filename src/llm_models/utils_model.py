import re
import copy
import asyncio

from enum import Enum
from rich.traceback import install
from typing import Tuple, List, Dict, Optional, Callable, Any

from src.common.logger import get_logger
from src.config.config import model_config
from src.config.api_ada_configs import APIProvider, ModelInfo, TaskConfig
from .payload_content.message import MessageBuilder, Message
from .payload_content.resp_format import RespFormat
from .payload_content.tool_option import ToolOption, ToolCall, ToolOptionBuilder, ToolParamType
from .model_client.base_client import BaseClient, APIResponse, client_registry
from .utils import compress_messages, llm_usage_recorder
from .exceptions import NetworkConnectionError, ReqAbortException, RespNotOkException, RespParseException

install(extra_lines=3)

logger = get_logger("model_utils")

# 常见Error Code Mapping
error_code_mapping = {
    400: "参数不正确",
    401: "API key 错误，认证失败，请检查 config/model_config.toml 中的配置是否正确",
    402: "账号余额不足",
    403: "需要实名,或余额不足",
    404: "Not Found",
    429: "请求过于频繁，请稍后再试",
    500: "服务器内部故障",
    503: "服务器负载过高",
}


class RequestType(Enum):
    """请求类型枚举"""

    RESPONSE = "response"
    EMBEDDING = "embedding"


class LLMRequest:
    """LLM请求类"""

    def __init__(self, model_set: TaskConfig, request_type: str = "") -> None:
        self.task_name = request_type
        self.model_for_task = model_set
        self.request_type = request_type
        self.model_usage: Dict[str, Tuple[int, int]] = {model: (0, 0) for model in self.model_for_task.model_list}
        """模型使用量记录，用于进行负载均衡，对应为(total_tokens, penalty)，惩罚值是为了能在某个模型请求不给力的时候进行调整"""

        self.pri_in = 0
        self.pri_out = 0

    async def generate_response_for_image(
        self,
        prompt: str,
        image_base64: str,
        image_format: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Tuple[str, Tuple[str, str, Optional[List[ToolCall]]]]:
        """
        为图像生成响应
        Args:
            prompt (str): 提示词
            image_base64 (str): 图像的Base64编码字符串
            image_format (str): 图像格式（如 'png', 'jpeg' 等）
        Returns:
            (Tuple[str, str, str, Optional[List[ToolCall]]]): 响应内容、推理内容、模型名称、工具调用列表
        """
        # 请求体构建
        message_builder = MessageBuilder()
        message_builder.add_text_content(prompt)
        message_builder.add_image_content(image_base64=image_base64, image_format=image_format)
        messages = [message_builder.build()]

        # 模型选择
        model_info, api_provider, client = self._select_model()

        # 请求并处理返回值
        response = await self._execute_request(
            api_provider=api_provider,
            client=client,
            request_type=RequestType.RESPONSE,
            model_info=model_info,
            message_list=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = response.content or ""
        reasoning_content = response.reasoning_content or ""
        tool_calls = response.tool_calls
        # 从内容中提取<think>标签的推理内容（向后兼容）
        if not reasoning_content and content:
            content, extracted_reasoning = self._extract_reasoning(content)
            reasoning_content = extracted_reasoning
        if usage := response.usage:
            llm_usage_recorder.record_usage_to_database(
                model_info=model_info,
                model_usage=usage,
                user_id="system",
                request_type=self.request_type,
                endpoint="/chat/completions",
            )
        return content, (reasoning_content, model_info.name, tool_calls)

    async def generate_response_for_voice(self):
        pass

    async def generate_response_async(
        self,
        prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, Tuple[str, str, Optional[List[ToolCall]]]]:
        """
        异步生成响应
        Args:
            prompt (str): 提示词
            temperature (float, optional): 温度参数
            max_tokens (int, optional): 最大token数
        Returns:
            (Tuple[str, str, str, Optional[List[ToolCall]]]): 响应内容、推理内容、模型名称、工具调用列表
        """
        # 请求体构建
        message_builder = MessageBuilder()
        message_builder.add_text_content(prompt)
        messages = [message_builder.build()]
        tool_built = self._build_tool_options(tools)
        # 模型选择
        model_info, api_provider, client = self._select_model()

        # 请求并处理返回值
        response = await self._execute_request(
            api_provider=api_provider,
            client=client,
            request_type=RequestType.RESPONSE,
            model_info=model_info,
            message_list=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_options=tool_built,
        )
        content = response.content
        reasoning_content = response.reasoning_content or ""
        tool_calls = response.tool_calls
        # 从内容中提取<think>标签的推理内容（向后兼容）
        if not reasoning_content and content:
            content, extracted_reasoning = self._extract_reasoning(content)
            reasoning_content = extracted_reasoning
        if usage := response.usage:
            llm_usage_recorder.record_usage_to_database(
                model_info=model_info,
                model_usage=usage,
                user_id="system",
                request_type=self.request_type,
                endpoint="/chat/completions",
            )
        if not content:
            raise RuntimeError("获取LLM生成内容失败")

        return content, (reasoning_content, model_info.name, tool_calls)

    async def get_embedding(self, embedding_input: str) -> Tuple[List[float], str]:
        """获取嵌入向量
        Args:
            embedding_input (str): 获取嵌入的目标
        Returns:
            (Tuple[List[float], str]): (嵌入向量，使用的模型名称)
        """
        # 无需构建消息体，直接使用输入文本
        model_info, api_provider, client = self._select_model()

        # 请求并处理返回值
        response = await self._execute_request(
            api_provider=api_provider,
            client=client,
            request_type=RequestType.EMBEDDING,
            model_info=model_info,
            embedding_input=embedding_input,
        )

        embedding = response.embedding

        if usage := response.usage:
            llm_usage_recorder.record_usage_to_database(
                model_info=model_info,
                model_usage=usage,
                user_id="system",
                request_type=self.request_type,
                endpoint="/embeddings",
            )

        if not embedding:
            raise RuntimeError("获取embedding失败")

        return embedding, model_info.name

    def _select_model(self) -> Tuple[ModelInfo, APIProvider, BaseClient]:
        """
        根据总tokens和惩罚值选择的模型
        """
        least_used_model_name = min(
            self.model_usage, key=lambda k: self.model_usage[k][0] + self.model_usage[k][1] * 300
        )
        model_info = model_config.get_model_info(least_used_model_name)
        api_provider = model_config.get_provider(model_info.api_provider)
        client = client_registry.get_client_class(api_provider.client_type)(copy.deepcopy(api_provider))
        return model_info, api_provider, client

    async def _execute_request(
        self,
        api_provider: APIProvider,
        client: BaseClient,
        request_type: RequestType,
        model_info: ModelInfo,
        message_list: List[Message] | None = None,
        tool_options: list[ToolOption] | None = None,
        response_format: RespFormat | None = None,
        stream_response_handler: Optional[Callable] = None,
        async_response_parser: Optional[Callable] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        embedding_input: str = "",
    ) -> APIResponse:
        """
        实际执行请求的方法

        包含了重试和异常处理逻辑
        """
        retry_remain = api_provider.max_retry
        compressed_messages: Optional[List[Message]] = None
        while retry_remain > 0:
            try:
                if request_type == RequestType.RESPONSE:
                    assert message_list is not None, "message_list cannot be None for response requests"
                    return await client.get_response(
                        model_info=model_info,
                        message_list=(compressed_messages or message_list),
                        tool_options=tool_options,
                        max_tokens=self.model_for_task.max_tokens if max_tokens is None else max_tokens,
                        temperature=self.model_for_task.temperature if temperature is None else temperature,
                        response_format=response_format,
                        stream_response_handler=stream_response_handler,
                        async_response_parser=async_response_parser,
                        extra_params=model_info.extra_params,
                    )
                elif request_type == RequestType.EMBEDDING:
                    assert embedding_input, "embedding_input cannot be empty for embedding requests"
                    return await client.get_embedding(
                        model_info=model_info,
                        embedding_input=embedding_input,
                        extra_params=model_info.extra_params,
                    )
            except Exception as e:
                logger.debug(f"请求失败: {str(e)}")
                # 处理异常
                total_tokens, penalty = self.model_usage[model_info.name]
                self.model_usage[model_info.name] = (total_tokens, penalty + 1)

                wait_interval, compressed_messages = self._default_exception_handler(
                    e,
                    self.task_name,
                    model_name=model_info.name,
                    remain_try=retry_remain,
                    retry_interval=api_provider.retry_interval,
                    messages=(message_list, compressed_messages is not None) if message_list else None,
                )

                if wait_interval == -1:
                    retry_remain = 0  # 不再重试
                elif wait_interval > 0:
                    logger.info(f"等待 {wait_interval} 秒后重试...")
                    await asyncio.sleep(wait_interval)
            finally:
                # 放在finally防止死循环
                retry_remain -= 1
        logger.error(f"模型 '{model_info.name}' 请求失败，达到最大重试次数 {api_provider.max_retry} 次")
        raise RuntimeError("请求失败，已达到最大重试次数")

    def _default_exception_handler(
        self,
        e: Exception,
        task_name: str,
        model_name: str,
        remain_try: int,
        retry_interval: int = 10,
        messages: Tuple[List[Message], bool] | None = None,
    ) -> Tuple[int, List[Message] | None]:
        """
        默认异常处理函数
        Args:
            e (Exception): 异常对象
            task_name (str): 任务名称
            model_name (str): 模型名称
            remain_try (int): 剩余尝试次数
            retry_interval (int): 重试间隔
            messages (tuple[list[Message], bool] | None): (消息列表, 是否已压缩过)
        Returns:
            (等待间隔（如果为0则不等待，为-1则不再请求该模型）, 新的消息列表（适用于压缩消息）)
        """

        if isinstance(e, NetworkConnectionError):  # 网络连接错误
            return self._check_retry(
                remain_try,
                retry_interval,
                can_retry_msg=f"任务-'{task_name}' 模型-'{model_name}': 连接异常，将于{retry_interval}秒后重试",
                cannot_retry_msg=f"任务-'{task_name}' 模型-'{model_name}': 连接异常，超过最大重试次数，请检查网络连接状态或URL是否正确",
            )
        elif isinstance(e, ReqAbortException):
            logger.warning(f"任务-'{task_name}' 模型-'{model_name}': 请求被中断，详细信息-{str(e.message)}")
            return -1, None  # 不再重试请求该模型
        elif isinstance(e, RespNotOkException):
            return self._handle_resp_not_ok(
                e,
                task_name,
                model_name,
                remain_try,
                retry_interval,
                messages,
            )
        elif isinstance(e, RespParseException):
            # 响应解析错误
            logger.error(f"任务-'{task_name}' 模型-'{model_name}': 响应解析错误，错误信息-{e.message}")
            logger.debug(f"附加内容: {str(e.ext_info)}")
            return -1, None  # 不再重试请求该模型
        else:
            logger.error(f"任务-'{task_name}' 模型-'{model_name}': 未知异常，错误信息-{str(e)}")
            return -1, None  # 不再重试请求该模型

    def _check_retry(
        self,
        remain_try: int,
        retry_interval: int,
        can_retry_msg: str,
        cannot_retry_msg: str,
        can_retry_callable: Callable | None = None,
        **kwargs,
    ) -> Tuple[int, List[Message] | None]:
        """辅助函数：检查是否可以重试
        Args:
            remain_try (int): 剩余尝试次数
            retry_interval (int): 重试间隔
            can_retry_msg (str): 可以重试时的提示信息
            cannot_retry_msg (str): 不可以重试时的提示信息
            can_retry_callable (Callable | None): 可以重试时调用的函数（如果有）
            **kwargs: 其他参数

        Returns:
            (Tuple[int, List[Message] | None]): (等待间隔（如果为0则不等待，为-1则不再请求该模型）, 新的消息列表（适用于压缩消息）)
        """
        if remain_try > 0:
            # 还有重试机会
            logger.warning(f"{can_retry_msg}")
            if can_retry_callable is not None:
                return retry_interval, can_retry_callable(**kwargs)
            else:
                return retry_interval, None
        else:
            # 达到最大重试次数
            logger.warning(f"{cannot_retry_msg}")
            return -1, None  # 不再重试请求该模型

    def _handle_resp_not_ok(
        self,
        e: RespNotOkException,
        task_name: str,
        model_name: str,
        remain_try: int,
        retry_interval: int = 10,
        messages: tuple[list[Message], bool] | None = None,
    ):
        """
        处理响应错误异常
        Args:
            e (RespNotOkException): 响应错误异常对象
            task_name (str): 任务名称
            model_name (str): 模型名称
            remain_try (int): 剩余尝试次数
            retry_interval (int): 重试间隔
            messages (tuple[list[Message], bool] | None): (消息列表, 是否已压缩过)
        Returns:
            (等待间隔（如果为0则不等待，为-1则不再请求该模型）, 新的消息列表（适用于压缩消息）)
        """
        # 响应错误
        if e.status_code in [400, 401, 402, 403, 404]:
            # 客户端错误
            logger.warning(
                f"任务-'{task_name}' 模型-'{model_name}': 请求失败，错误代码-{e.status_code}，错误信息-{e.message}"
            )
            return -1, None  # 不再重试请求该模型
        elif e.status_code == 413:
            if messages and not messages[1]:
                # 消息列表不为空且未压缩，尝试压缩消息
                return self._check_retry(
                    remain_try,
                    0,
                    can_retry_msg=f"任务-'{task_name}' 模型-'{model_name}': 请求体过大，尝试压缩消息后重试",
                    cannot_retry_msg=f"任务-'{task_name}' 模型-'{model_name}': 请求体过大，压缩消息后仍然过大，放弃请求",
                    can_retry_callable=compress_messages,
                    messages=messages[0],
                )
            # 没有消息可压缩
            logger.warning(f"任务-'{task_name}' 模型-'{model_name}': 请求体过大，无法压缩消息，放弃请求。")
            return -1, None
        elif e.status_code == 429:
            # 请求过于频繁
            return self._check_retry(
                remain_try,
                retry_interval,
                can_retry_msg=f"任务-'{task_name}' 模型-'{model_name}': 请求过于频繁，将于{retry_interval}秒后重试",
                cannot_retry_msg=f"任务-'{task_name}' 模型-'{model_name}': 请求过于频繁，超过最大重试次数，放弃请求",
            )
        elif e.status_code >= 500:
            # 服务器错误
            return self._check_retry(
                remain_try,
                retry_interval,
                can_retry_msg=f"任务-'{task_name}' 模型-'{model_name}': 服务器错误，将于{retry_interval}秒后重试",
                cannot_retry_msg=f"任务-'{task_name}' 模型-'{model_name}': 服务器错误，超过最大重试次数，请稍后再试",
            )
        else:
            # 未知错误
            logger.warning(
                f"任务-'{task_name}' 模型-'{model_name}': 未知错误，错误代码-{e.status_code}，错误信息-{e.message}"
            )
            return -1, None

    def _build_tool_options(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[List[ToolOption]]:
        """构建工具选项列表"""
        if not tools:
            return None
        tool_options: List[ToolOption] = []
        for tool in tools:
            tool_legal = True
            tool_options_builder = ToolOptionBuilder()
            tool_options_builder.set_name(tool.get("name", ""))
            tool_options_builder.set_description(tool.get("description", ""))
            parameters: List[Tuple[str, str, str, bool]] = tool.get("parameters", [])
            for param in parameters:
                try:
                    tool_options_builder.add_param(
                        name=param[0],
                        param_type=ToolParamType(param[1]),
                        description=param[2],
                        required=param[3],
                    )
                except ValueError as ve:
                    tool_legal = False
                    logger.error(f"{param[1]} 参数类型错误: {str(ve)}")
                except Exception as e:
                    tool_legal = False
                    logger.error(f"构建工具参数失败: {str(e)}")
            if tool_legal:
                tool_options.append(tool_options_builder.build())
        return tool_options or None

    @staticmethod
    def _extract_reasoning(content: str) -> Tuple[str, str]:
        """CoT思维链提取，向后兼容"""
        match = re.search(r"(?:<think>)?(.*?)</think>", content, re.DOTALL)
        content = re.sub(r"(?:<think>)?.*?</think>", "", content, flags=re.DOTALL, count=1).strip()
        reasoning = match[1].strip() if match else ""
        return content, reasoning
