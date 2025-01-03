import os
from typing import Any, List, Dict, Tuple, Optional
from datetime import datetime
from pathlib import Path
import re

from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import TransferInfo, RefreshMediaItem, ServiceInfo
from app.schemas.types import EventType, MediaType


class AutoStrmCreator(_PluginBase):
    # 插件名称
    plugin_name = "自动STRM创建"
    # 插件描述
    plugin_desc = "媒体入库后自动创建STRM文件并刷新媒体服务器，支持远程媒体路径映射"
    # 插件图标
    plugin_icon = "moose.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "chicring"
    # 作者主页
    author_url = "https://github.com/chicring"
    # 插件配置项ID前缀
    plugin_config_prefix = "autostrm_"
    # 加载顺序
    plugin_order = 21
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _refresh_delay = 0
    _mediaservers = None
    _path_mappings = None
    mediaserver_helper = None

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._refresh_delay = int(config.get('refresh_delay') or 0)
            self._mediaservers = config.get('mediaservers') or []
            self._path_mappings = config.get('path_mappings') or []
        
        # 初始化媒体服务器助手
        self.mediaserver_helper = MediaServerHelper()

    def _parse_mapping_rule(self, rule: str) -> Tuple[str, str, str]:
        """
        解析路径映射规则
        格式：源路径#目标路径#远程URL前缀
        """
        try:
            source_path, target_path, remote_url = rule.split('#')
            return source_path.strip(), target_path.strip(), remote_url.strip()
        except Exception as e:
            logger.error(f"解析路径映射规则失败: {str(e)}")
            return None, None, None

    def _find_matching_rule(self, file_path: str) -> Tuple[str, str, str, str]:
        """
        查找匹配的映射规则
        返回：源路径前缀、目标路径前缀、远程URL前缀、相对路径
        """
        for rule in self._path_mappings:
            # 逐行解析规则
            for line in rule.split('\n'):
                source_path, target_path, remote_url = self._parse_mapping_rule(line)
                if not all([source_path, target_path, remote_url]):
                    continue

                # 移除路径中的通配符
                source_path = source_path.replace('/', '')
                if file_path.startswith(source_path):
                    # 获取相对路径部分
                    relative_path = file_path[len(source_path):].lstrip('/')
                    return source_path, target_path, remote_url, relative_path

        return None, None, None, None

    def create_strm_file(self, file_path: str) -> bool:
        """
        创建STRM文件
        """
        try:
            # 查找匹配的规则
            source_prefix, target_prefix, remote_url, relative_path = self._find_matching_rule(file_path)
            if not all([source_prefix, target_prefix, remote_url, relative_path]):
                logger.warning(f"未找到匹配的路径映射规则: {file_path}")
                return False

            # 构建STRM文件路径
            strm_file_path = os.path.join(target_prefix, relative_path)
            # 修改扩展名为.strm
            strm_file_path = os.path.splitext(strm_file_path)[0] + '.strm'
            
            # 构建远程URL
            remote_file_url = f"{remote_url.rstrip('/')}/{relative_path}"
            
            # 确保目录存在
            os.makedirs(os.path.dirname(strm_file_path), exist_ok=True)
            
            # 写入STRM文件
            with open(strm_file_path, 'w', encoding='utf-8') as f:
                f.write(remote_file_url)
                
            logger.info(f"已创建STRM文件: {strm_file_path}")
            logger.debug(f"STRM文件内容: {remote_file_url}")
            return True
                
        except Exception as e:
            logger.error(f"创建STRM文件失败: {str(e)}")
            return False

    @property
    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        """
        获取媒体服务器信息
        """
        if not self._mediaservers:
            return None

        services = self.mediaserver_helper.get_services(name_filters=self._mediaservers)
        if not services:
            return None

        active_services = {}
        for service_name, service_info in services.items():
            if not service_info.instance.is_inactive():
                active_services[service_name] = service_info

        return active_services if active_services else None

    def refresh_media_server(self, items: List[RefreshMediaItem]):
        """
        刷新媒体服务器
        """
        if not self.service_infos:
            return
        
        for name, service in self.service_infos.items():
            try:
                # Emby
                if self.mediaserver_helper.is_media_server("emby", service=service):
                    service.instance.refresh_library_by_items(items)
                    logger.info(f"Emby媒体库刷新指令已发送")

                # Jellyfin
                if self.mediaserver_helper.is_media_server("jellyfin", service=service):
                    service.instance.refresh_root_library()
                    logger.info(f"Jellyfin媒体库刷新指令已发送")

                # Plex
                if self.mediaserver_helper.is_media_server("plex", service=service):
                    service.instance.refresh_library_by_items(items)
                    logger.info(f"Plex媒体库刷新指令已发送")
                    
            except Exception as e:
                logger.error(f"刷新媒体服务器 {name} 失败: {str(e)}")

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        """
        处理入库完成事件
        """
        if not self._enabled or not self._path_mappings:
            return

        event_info: dict = event.event_data
        if not event_info:
            return
            
        # 获取入库信息    
        transfer_info: TransferInfo = event_info.get("transferinfo")
        if not transfer_info or not transfer_info.target_diritem:
            return

        # 源文件路径
        file_path = transfer_info.target_diritem.path
        if not file_path:
            return

        # 创建STRM文件
        if not self.create_strm_file(file_path):
            return

        # 获取媒体信息用于刷新
        media_info: MediaInfo = event_info.get("mediainfo")
        if not media_info:
            return

        # 刷新媒体库
        items = [
            RefreshMediaItem(
                title=media_info.title,
                year=media_info.year,
                type=media_info.type,
                category=media_info.category,
                target_path=Path(file_path)
            )
        ]
        
        if self._refresh_delay:
            logger.info(f"延迟 {self._refresh_delay} 秒后刷新媒体库... ")
            time.sleep(self._refresh_delay)
            
        self.refresh_media_server(items)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        插件配置页面
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'mediaservers',
                                            'label': '媒体服务器',
                                            'items': [{"title": config.name, "value": config.name}
                                                      for config in self.mediaserver_helper.get_configs().values()]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'path_mappings',
                                            'label': '路径映射规则',
                                            'placeholder': '/ecloud/电影#/link/电影#https://alist.example.com/d',
                                            'rows': 5,
                                            'hint': '格式：源路径#目标路径#远程URL，每行一个规则'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'refresh_delay',
                                            'label': '刷新延迟（秒）',
                                            'placeholder': '0'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '路径映射规则说明：\n'
                                                   '1. 每行一个规则，格式：源路径#目标路径#远程URL\n'
                                                   '2. 例如：/ecloud/电影#/link/电影#https://alist.example.com/d'
                                                   '3. 源路径：媒体文件实际存储的路径\n'
                                                   '4. 目标路径：STRM文件存储的路径\n'
                                                   '5. 远程URL：可访问到媒体文件的远程地址前缀',
                                            'style': 'white-space: pre-line;'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "refresh_delay": 0,
            "path_mappings": "",
            "mediaservers": []
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        停止插件
        """
        pass


# 插件实例
plugin_object = AutoStrmCreator()
