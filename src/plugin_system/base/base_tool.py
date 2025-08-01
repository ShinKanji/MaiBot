from abc import ABC, abstractmethod
from typing import Any, List, Tuple
from rich.traceback import install

from src.common.logger import get_logger
from src.plugin_system.base.component_types import ComponentType, ToolInfo

install(extra_lines=3)

logger = get_logger("base_tool")


class BaseTool(ABC):
    """所有工具的基类"""

    name: str = ""
    """工具的名称"""
    description: str = ""
    """工具的描述"""
    parameters: List[Tuple[str, str, str, bool]] = []
    """工具的参数定义，为[("param_name", "param_type", "description", required)]"""
    available_for_llm: bool = False
    """是否可供LLM使用"""

    @classmethod
    def get_tool_definition(cls) -> dict[str, Any]:
        """获取工具定义，用于LLM工具调用

        Returns:
            dict: 工具定义字典
        """
        if not cls.name or not cls.description or not cls.parameters:
            raise NotImplementedError(f"工具类 {cls.__name__} 必须定义 name, description 和 parameters 属性")

        return {"name": cls.name, "description": cls.description, "parameters": cls.parameters}

    @classmethod
    def get_tool_info(cls) -> ToolInfo:
        """获取工具信息"""
        if not cls.name or not cls.description or not cls.parameters:
            raise NotImplementedError(f"工具类 {cls.__name__} 必须定义 name, description 和 parameters 属性")

        return ToolInfo(
            name=cls.name,
            tool_description=cls.description,
            enabled=cls.available_for_llm,
            tool_parameters=cls.parameters,
            component_type=ComponentType.TOOL,
        )

    @abstractmethod
    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        """执行工具函数(供llm调用)
           通过该方法，maicore会通过llm的tool call来调用工具
           传入的是json格式的参数，符合parameters定义的格式

        Args:
            function_args: 工具调用参数

        Returns:
            dict: 工具执行结果
        """
        raise NotImplementedError("子类必须实现execute方法")

    async def direct_execute(self, **function_args: dict[str, Any]) -> dict[str, Any]:
        """直接执行工具函数(供插件调用)
           通过该方法，插件可以直接调用工具，而不需要传入字典格式的参数
           插件可以直接调用此方法，用更加明了的方式传入参数
           示例: result = await tool.direct_execute(arg1="参数",arg2="参数2")

           工具开发者可以重写此方法以实现与llm调用差异化的执行逻辑

        Args:
            **function_args: 工具调用参数

        Returns:
            dict: 工具执行结果
        """
        parameter_required = [param[0] for param in self.parameters if param[3]]  # 获取所有必填参数名
        for param_name in parameter_required:
            if param_name not in function_args:
                raise ValueError(f"工具类 {self.__class__.__name__} 缺少必要参数: {param_name}")

        return await self.execute(function_args)
