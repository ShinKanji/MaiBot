import asyncio
from dataclasses import dataclass
from typing import Callable, Any

from openai import AsyncStream
from openai.types.chat import ChatCompletionChunk, ChatCompletion

from src.config.api_ada_configs import ModelInfo, APIProvider
from ..payload_content.message import Message
from ..payload_content.resp_format import RespFormat
from ..payload_content.tool_option import ToolOption, ToolCall


@dataclass
class UsageRecord:
    """
    使用记录类
    """

    model_name: str
    """模型名称"""

    provider_name: str
    """提供商名称"""

    prompt_tokens: int
    """提示token数"""

    completion_tokens: int
    """完成token数"""

    total_tokens: int
    """总token数"""


@dataclass
class APIResponse:
    """
    API响应类
    """

    content: str | None = None
    """响应内容"""

    reasoning_content: str | None = None
    """推理内容"""

    tool_calls: list[ToolCall] | None = None
    """工具调用 [(工具名称, 工具参数), ...]"""

    embedding: list[float] | None = None
    """嵌入向量"""

    usage: UsageRecord | None = None
    """使用情况 (prompt_tokens, completion_tokens, total_tokens)"""

    raw_data: Any = None
    """响应原始数据"""


class BaseClient:
    """
    基础客户端
    """

    api_provider: APIProvider

    def __init__(self, api_provider: APIProvider):
        self.api_provider = api_provider

    async def get_response(
        self,
        model_info: ModelInfo,
        message_list: list[Message],
        tool_options: list[ToolOption] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        response_format: RespFormat | None = None,
        stream_response_handler: Callable[
            [AsyncStream[ChatCompletionChunk], asyncio.Event | None],
            tuple[APIResponse, tuple[int, int, int]],
        ]
        | None = None,
        async_response_parser: Callable[[ChatCompletion], tuple[APIResponse, tuple[int, int, int]]] | None = None,
        interrupt_flag: asyncio.Event | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> APIResponse:
        """
        获取对话响应
        :param model_info: 模型信息
        :param message_list: 对话体
        :param tool_options: 工具选项（可选，默认为None）
        :param max_tokens: 最大token数（可选，默认为1024）
        :param temperature: 温度（可选，默认为0.7）
        :param response_format: 响应格式（可选，默认为 NotGiven ）
        :param stream_response_handler: 流式响应处理函数（可选）
        :param async_response_parser: 响应解析函数（可选）
        :param interrupt_flag: 中断信号量（可选，默认为None）
        :return: (响应文本, 推理文本, 工具调用, 其他数据)
        """
        raise RuntimeError("This method should be overridden in subclasses")

    async def get_embedding(
        self,
        model_info: ModelInfo,
        embedding_input: str,
        extra_params: dict[str, Any] | None = None,
    ) -> APIResponse:
        """
        获取文本嵌入
        :param model_info: 模型信息
        :param embedding_input: 嵌入输入文本
        :return: 嵌入响应
        """
        raise RuntimeError("This method should be overridden in subclasses")


class ClientRegistry:
    def __init__(self) -> None:
        self.client_registry: dict[str, type[BaseClient]] = {}

    def register_client_class(self, client_type: str):
        """
        注册API客户端类
        :param client_class: API客户端类
        """

        def decorator(cls: type[BaseClient]) -> type[BaseClient]:
            if not issubclass(cls, BaseClient):
                raise TypeError(f"{cls.__name__} is not a subclass of BaseClient")
            self.client_registry[client_type] = cls
            return cls

        return decorator

    def get_client_class(self, client_type: str) -> type[BaseClient]:
        """
        获取注册的API客户端类
        Args:
            client_type: 客户端类型
        Returns:
            type[BaseClient]: 注册的API客户端类
        """
        if client_type not in self.client_registry:
            raise KeyError(f"'{client_type}' 类型的 Client 未注册")
        return self.client_registry[client_type]


client_registry = ClientRegistry()
