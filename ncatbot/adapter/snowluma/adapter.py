"""SnowLuma 适配器主类。

完全复用 ``ncatbot.adapter.napcat`` 的 OneBot v11 协议实现：

- ``NapCatWebSocket``：原生 WebSocket 连接管理（仅依赖 URI，与 NapCat 无耦合）
- ``OB11Protocol``：请求-响应匹配、心跳处理
- ``NapCatBotAPI``：``IQQAPIClient`` 的实现（消息 / 群管 / 查询 / 文件 API）
- ``NapCatEventParser``：OneBot v11 事件 → ``BaseEventData`` 模型

SnowLuma 自身是符合 OneBot v11 标准的协议端，因此协议层零差异。
本适配器的全部价值在于 ``setup/`` —— 自动下载、UAC 启动、WebUI 引导。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..base import BaseAdapter
from ncatbot.api import IAPIClient
from ncatbot.utils import (
    format_event_summary,
    get_config_manager,
    get_log,
    resolve_event_log_level,
)

# 复用 napcat 协议层（零修改，避免维护两份 OB11 实现）
from ..napcat.api.bot_api import NapCatBotAPI
from ..napcat.connection.protocol import OB11Protocol
from ..napcat.connection.websocket import NapCatWebSocket
from ..napcat.parser import NapCatEventParser

from .config import SnowLumaConfig
from .setup.launcher import SnowLumaLauncher

LOG = get_log("SnowLumaAdapter")


class SnowLumaAdapter(BaseAdapter):
    """SnowLuma (OneBot v11) 适配器 — 与 napcat 同级、协议复用、setup 自治。"""

    name = "snowluma"
    description = "SnowLuma (OneBot v11) 适配器 — 自带 Node 运行时的独立协议端"
    supported_protocols = ["onebot_v11"]
    platform = "qq"

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        bot_uin: str = "",
        websocket_timeout: int = 15,
    ) -> None:
        super().__init__(
            config=config,
            bot_uin=bot_uin,
            websocket_timeout=websocket_timeout,
        )
        self._config = SnowLumaConfig.model_validate(self._raw_config)
        self._launcher = SnowLumaLauncher(
            self._config,
            bot_uin=self._bot_uin,
            websocket_timeout=self._websocket_timeout,
        )
        self._ws: Optional[NapCatWebSocket] = None
        self._protocol: Optional[OB11Protocol] = None
        self._api: Optional[NapCatBotAPI] = None
        self._parser = NapCatEventParser()

    # ------------------------------------------------------------
    # 对外属性
    # ------------------------------------------------------------

    @property
    def snowluma_config(self) -> SnowLumaConfig:
        """供诊断 / CLI 工具读取的 SnowLumaConfig 实例。"""
        return self._config

    # 兼容 BotClient._stop_configured_napcat_runtimes 的鸭子类型 —
    # 该方法会在 Linux 关闭时遍历适配器查找 ``napcat_config.stop_napcat``，
    # SnowLuma 当前不参与该流程，故不暴露 ``napcat_config`` 属性。

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------

    async def setup(self) -> None:
        config_changed = await self._launcher.launch()
        if config_changed:
            self._save_config()

    def _save_config(self) -> None:
        """将 launcher 更新后的 ws_uri / ws_token 写回 config.yaml。"""
        try:
            mgr = get_config_manager()
            for entry in mgr.config.adapters:
                if entry.type == "snowluma":
                    entry.config["ws_uri"] = self._config.ws_uri
                    entry.config["ws_token"] = self._config.ws_token
                    entry.config["configured"] = True
                    break
            mgr.save()
            LOG.info("SnowLuma 配置已保存到 config.yaml")
        except Exception as e:
            LOG.warning("保存 SnowLuma 配置失败: %s", e)

    async def connect(self) -> None:
        uri = self._config.get_uri_with_token()
        self._ws = NapCatWebSocket(uri)
        await self._ws.connect()
        self._protocol = OB11Protocol(self._ws)
        self._api = NapCatBotAPI(self._protocol)
        self._protocol.set_event_handler(self._on_event)

    async def disconnect(self) -> None:
        if self._protocol is not None:
            self._protocol.cancel_all()
        if self._ws is not None:
            await self._ws.disconnect()
        self._api = self._protocol = self._ws = None

    def stop_managed_runtime(self) -> None:
        """供 ``BotClient`` 在 Linux 平台关闭时调用（与 NapCatAdapter 同名）。"""
        self._launcher.stop()

    async def listen(self) -> None:
        assert self._ws is not None
        assert self._protocol is not None
        await self._ws.listen(self._protocol.on_message)

    def get_api(self) -> IAPIClient:
        if self._api is None:
            raise RuntimeError("SnowLumaAdapter 尚未连接")
        return self._api

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.connected

    # ------------------------------------------------------------
    # CLI init 钩子
    # ------------------------------------------------------------

    @classmethod
    def cli_configure(cls) -> Dict[str, Any]:
        """``ncatbot init`` / ``ncatbot adapter`` 的交互式配置钩子。"""
        import click

        click.echo(click.style("\n— SnowLuma 适配器配置 —", fg="cyan", bold=True))
        click.echo(
            click.style(
                "  SnowLuma 是基于 Node.js 的独立 OneBot v11 协议端，"
                "首次启动需在 WebUI (http://localhost:5099) 中配置 WS 端点并扫码登录。",
                dim=True,
            )
        )

        auto_install = click.confirm(
            "是否现在自动下载并安装 SnowLuma (Windows)?", default=False
        )
        if auto_install:
            cls._cli_install_snowluma()
            click.echo(
                click.style(
                    "  将使用默认连接参数，启动时由 SnowLumaLauncher 启动",
                    dim=True,
                )
            )
            return {
                "ws_uri": "ws://localhost:3001",
                "ws_token": "",
                "webui_uri": "http://localhost:5099",
                "skip_setup": False,
            }

        ws_uri = click.prompt("OneBot WebSocket 地址", default="ws://localhost:3001")
        ws_token = click.prompt(
            "WebSocket Token (可留空)", default="", show_default=False
        )
        webui_uri = click.prompt("WebUI 地址", default="http://localhost:5099")
        skip_setup = click.confirm(
            "skip_setup (已手动管理 SnowLuma 时选 Y)?", default=False
        )
        return {
            "ws_uri": ws_uri,
            "ws_token": ws_token,
            "webui_uri": webui_uri,
            "skip_setup": skip_setup,
        }

    @staticmethod
    def _cli_install_snowluma() -> None:
        """CLI 环境下显式触发安装。"""
        import click

        try:
            from .setup.platform import PlatformOps, UnsupportedPlatformError
            from .setup.installer import SnowLumaInstaller

            try:
                platform_ops = PlatformOps.create()
            except UnsupportedPlatformError:
                click.echo(
                    click.style("当前操作系统暂不支持 SnowLuma 自动安装", fg="red")
                )
                return

            installer = SnowLumaInstaller(platform_ops)
            ok = installer.install(skip_confirm=True)
            if ok:
                click.echo(click.style("SnowLuma 安装成功!", fg="green"))
            else:
                click.echo(click.style("SnowLuma 安装失败，请稍后手动安装", fg="red"))
        except Exception as e:
            click.echo(click.style(f"安装过程出错: {e}", fg="red"))

    # ------------------------------------------------------------
    # 事件回调（与 NapCatAdapter 等价；保留以便日志独立分组）
    # ------------------------------------------------------------

    async def _on_event(self, raw_data: dict) -> None:
        data_model = self._parser.parse(raw_data)
        if data_model is None:
            return

        event_type = data_model.resolve_type()
        logging_config = get_config_manager().config.logging
        log_level = resolve_event_log_level(event_type, logging_config.event_log_levels)

        if log_level is not None:
            if logging_config.event_log_format == "summary":
                summary = format_event_summary(raw_data)
                LOG._log(log_level, summary, (), {})
                s = data_model.model_dump_json()
                if len(s) > 2000:
                    s = s[:2000] + "..."
                LOG._log(
                    logging.DEBUG,
                    f"收到事件 {data_model.post_type.value}: {s}",
                    (),
                    {},
                )
            else:
                s = data_model.model_dump_json()
                if len(s) > 2000:
                    s = s[:2000] + "..."
                LOG._log(
                    log_level,
                    f"收到事件 {data_model.post_type.value}: {s}",
                    (),
                    {},
                )

        if self._event_callback:
            await self._event_callback(data_model)


__all__ = ["SnowLumaAdapter"]
