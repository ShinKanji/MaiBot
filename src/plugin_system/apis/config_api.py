from typing import Any
from src.common.logger import get_logger
from src.config.config import global_config
from src.person_info.person_info import get_person_info_manager

logger = get_logger("config_api")


class ConfigAPI:
    """配置API模块

    提供了配置读取和用户信息获取等功能
    """

    def get_global_config(self, key: str, default: Any = None) -> Any:
        """
        安全地从全局配置中获取一个值。
        插件应使用此方法读取全局配置，以保证只读和隔离性。

        Args:
            key: 配置键名，支持嵌套访问如 "section.subsection.key"
            default: 如果配置不存在时返回的默认值

        Returns:
            Any: 配置值或默认值
        """
        # 支持嵌套键访问
        keys = key.split(".")
        current = global_config

        try:
            for k in keys:
                if hasattr(current, k):
                    current = getattr(current, k)
                else:
                    return default
            return current
        except Exception as e:
            logger.warning(f"获取全局配置 {key} 失败: {e}")
            return default

    def get_config(self, key: str, default: Any = None) -> Any:
        """
        从插件配置中获取值，支持嵌套键访问

        Args:
            key: 配置键名，支持嵌套访问如 "section.subsection.key"
            default: 如果配置不存在时返回的默认值

        Returns:
            Any: 配置值或默认值
        """
        # 获取插件配置
        plugin_config = getattr(self, "_plugin_config", {})
        if not plugin_config:
            return default

        # 支持嵌套键访问
        keys = key.split(".")
        current = plugin_config

        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return default

        return current

    async def get_user_id_by_person_name(self, person_name: str) -> tuple[str, str]:
        """根据用户名获取用户ID

        Args:
            person_name: 用户名

        Returns:
            tuple[str, str]: (平台, 用户ID)
        """
        person_info_manager = get_person_info_manager()
        person_id = person_info_manager.get_person_id_by_person_name(person_name)
        user_id = await person_info_manager.get_value(person_id, "user_id")
        platform = await person_info_manager.get_value(person_id, "platform")
        return platform, user_id

    async def get_person_info(self, person_id: str, key: str, default: Any = None) -> Any:
        """获取用户信息

        Args:
            person_id: 用户ID
            key: 信息键名
            default: 默认值

        Returns:
            Any: 用户信息值或默认值
        """
        person_info_manager = get_person_info_manager()
        return await person_info_manager.get_value(person_id, key, default)
