"""SnowLuma 适配器配置模型。

与 ``LarkConfig`` / ``BilibiliConfig`` 同级 — 配置完全自治，不污染顶层
``Config`` 模型，全部字段通过 ``adapters[].config`` 透传。
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SnowLumaConfig(BaseModel):
    """SnowLuma 适配器专属配置。

    Attributes
    ----------
    ws_uri:
        SnowLuma 暴露的 OneBot v11 WebSocket 地址。该端口由用户在 SnowLuma
        WebUI 中配置（"OneBot 协议端点"），NcatBot 仅作为客户端连接。
    ws_token:
        WebSocket access_token（在 WebUI 中设置）。空字符串表示未启用鉴权。
    webui_uri:
        SnowLuma WebUI 地址。默认 ``http://localhost:5099``，对应 SnowLuma
        ``config/runtime.json`` 中的 ``webuiPort`` 字段。
    skip_setup:
        ``True`` = Connect 模式，仅连接已运行的 SnowLuma，不做安装/启动；
        ``False`` = Setup 模式（默认），按需自动下载 / 解压 / 启动。
    enable_update_check:
        启动时是否检查 SnowLuma 新版本。默认关闭以避免污染 GitHub API 限额。
    use_lite_package:
        是否优先下载 ``-lite`` 后缀的精简包。
    install_dir:
        安装目录，相对当前工作目录或绝对路径。留空则使用默认 ``./snowluma/``。
    """

    model_config = ConfigDict(extra="allow")

    ws_uri: str = "ws://localhost:3001"
    ws_token: str = ""
    webui_uri: str = "http://localhost:5099"

    skip_setup: bool = False
    enable_update_check: bool = False
    use_lite_package: bool = False
    install_dir: str = ""
    configured: bool = False

    @field_validator("ws_uri")
    @classmethod
    def _validate_ws_uri(cls, v: str) -> str:
        if not v:
            return "ws://localhost:3001"
        if not (v.startswith("ws://") or v.startswith("wss://")):
            return f"ws://{v}"
        return v

    @field_validator("webui_uri")
    @classmethod
    def _validate_webui_uri(cls, v: str) -> str:
        if not v:
            return "http://localhost:5099"
        if not (v.startswith("http://") or v.startswith("https://")):
            return f"http://{v}"
        return v

    # ------------------------- 派生属性 -------------------------

    @property
    def ws_host(self) -> Optional[str]:
        return urllib.parse.urlparse(self.ws_uri).hostname

    @property
    def ws_port(self) -> Optional[int]:
        return urllib.parse.urlparse(self.ws_uri).port

    @property
    def webui_host(self) -> Optional[str]:
        return urllib.parse.urlparse(self.webui_uri).hostname

    @property
    def webui_port(self) -> Optional[int]:
        return urllib.parse.urlparse(self.webui_uri).port

    def get_uri_with_token(self) -> str:
        """返回带 ``access_token`` 查询参数的 WS URI（与 NapCatConfig 同名同义）。"""
        if not self.ws_token:
            return self.ws_uri
        encoded_token = urllib.parse.quote(self.ws_token, safe="")
        return f"{self.ws_uri}?access_token={encoded_token}"


# 给类型检查器一个稳定的导出符号
__all__ = ["SnowLumaConfig"]


# 占位避免 import 警告: Field 在子类中可能用到
_ = Field
