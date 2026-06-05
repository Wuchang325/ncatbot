"""SnowLuma 启动编排。

与 ``napcat.setup.launcher.NapCatLauncher`` 同构，但更轻量：

- **Connect 模式**（``skip_setup: true``）：直接探针 OneBot v11 WebSocket，
  失败抛错。适用于 SnowLuma 由用户/外部进程管理的情况。
- **Setup 模式**（默认）：
  1. 探 WS 是否已通 → 通则跳过准备；
  2. 否则 ``ensure_installed()`` 下载 SnowLuma；
  3. ``platform.start_snowluma()`` 弹 UAC 启动；
  4. 提示用户在 SnowLuma WebUI（默认 5099）配置 OneBot v11 WebSocket
     端点并扫码登录 QQ；
  5. 阻塞等待 WebSocket 通畅（``websocket_timeout`` 秒）。

OneBot 配置写入 / 登录引导**不在本阶段实现**（受 SnowLuma WebUI API 路由
未公开影响），用户需手动在 WebUI 中完成。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional, TYPE_CHECKING

import websockets

from ncatbot.utils import (  # type: ignore[attr-defined]
    NcatBotError,
    get_config_manager,
    get_log,
)

from .installer import SnowLumaInstaller
from .platform import PlatformOps, UnsupportedPlatformError

if TYPE_CHECKING:
    from ..config import SnowLumaConfig

LOG = get_log("SnowLumaLauncher")


class SnowLumaLauncher:
    """SnowLuma 启动编排。

    Parameters
    ----------
    snowluma_config:
        ``SnowLumaConfig`` 实例（由 ``SnowLumaAdapter`` 注入）。
    bot_uin:
        全局 bot QQ 号（仅用于日志，SnowLuma 自管登录态）。
    websocket_timeout:
        WebSocket 等待超时秒数。
    """

    def __init__(
        self,
        snowluma_config: "SnowLumaConfig",
        *,
        bot_uin: str = "",
        websocket_timeout: int = 15,
    ) -> None:
        self._config = snowluma_config
        self._bot_uin = bot_uin
        self._websocket_timeout = websocket_timeout
        self._platform: Optional[PlatformOps] = None
        self._installer: Optional[SnowLumaInstaller] = None

    # ------------------------------------------------------------
    # 平台 / 安装器懒加载
    # ------------------------------------------------------------

    @property
    def platform(self) -> PlatformOps:
        if self._platform is None:
            self._platform = PlatformOps.create(install_dir=self._config.install_dir)
        return self._platform

    def _ensure_installer(self) -> SnowLumaInstaller:
        if self._installer is None:
            self._installer = SnowLumaInstaller(
                self.platform, snowluma_config=self._config
            )
        return self._installer

    # ------------------------------------------------------------
    # WebSocket 探针
    # ------------------------------------------------------------

    async def _test_websocket(self, log_failure: bool = False) -> Optional[int]:
        """连接一次 WS，成功返回 ``self_id``（可能为 ``None``，因 SnowLuma
        在未登录时也可能不下发 lifecycle）。"""
        uri = self._config.get_uri_with_token()
        try:
            async with websockets.connect(uri, open_timeout=5) as ws:
                # SnowLuma 应当下发 OneBot v11 lifecycle / heartbeat 事件
                # 这里读一帧拿 self_id；若超时认为暂未推送也算"WS 在线"
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    data = json.loads(raw)
                    if data.get("status") == "failed":
                        retcode = data.get("retcode")
                        message = data.get("message", "未知错误")
                        if retcode == 1403:
                            raise NcatBotError(
                                "SnowLuma WebSocket Token 校验失败 (1403)", False
                            )
                        raise NcatBotError(
                            f"SnowLuma WebSocket 连接失败: {message}", False
                        )
                    return data.get("self_id")
                except asyncio.TimeoutError:
                    # WS 通了但暂无事件推送，认为服务在线
                    return None
        except NcatBotError:
            raise
        except Exception as e:
            if log_failure:
                LOG.warning("测试 SnowLuma WebSocket 连接失败: %s", e)
            return None

    async def is_service_ok(self, timeout: int = 0, show_info: bool = True) -> bool:
        """WS 是否真正可用 — 不仅握手成功，还要能稳定接收事件帧。

        SnowLuma 在 token 校验失败 / 未登录 / OneBot 服务未启用时，会先完成
        WebSocket 握手再立刻发 ``{"status":"failed"}`` 并 close。如果只判 connect
        成功就放行，BotClient 监听层会陷入"重连成功 → 服务端关闭 → 再重连"的
        死循环。所以这里走 ``_test_websocket()`` 读首帧 + 校验 retcode。

        - 首帧是合法 OneBot 事件 → 返回 True
        - 2 秒内没收到任何帧（可能是心跳间隔较长但服务正常）→ 也返回 True
        - 收到 status=failed 或服务器主动 close（连接异常）→ 返回 False，
          retcode=1403（鉴权失败）则继续向上抛 ``NcatBotError`` 终止启动。
        """
        if timeout == 0:
            try:
                # _test_websocket: TimeoutError 路径返回 None 也算"在线"，
                # 真正失败 (服务器拒绝 / 主动 close) 时返回 None 但前面会抛
                # NcatBotError; 普通连不上则在 except 里返回 None。
                # 这里我们用一个内部更严格的探针来区分这两种 None。
                ok = await self._strict_probe()
                if not ok and show_info:
                    LOG.debug("SnowLuma WebSocket 严格探针未通过")
                return ok
            except NcatBotError:
                # 鉴权 / 协议失败：直接抛给上层，避免轮询死循环
                raise
            except Exception as e:
                if show_info:
                    LOG.debug("SnowLuma WebSocket 探针异常: %s", e)
                return False

        start = time.time()
        warned_5 = False
        warned_10 = False
        while True:
            try:
                if await self.is_service_ok(show_info=False):
                    return True
            except NcatBotError:
                # 配置类错误立刻终止轮询
                raise
            elapsed = time.time() - start
            if not warned_5 and elapsed >= 5:
                LOG.warning("SnowLuma WebSocket 已等待 5s 仍未就绪...")
                warned_5 = True
            if not warned_10 and elapsed >= 10:
                LOG.warning(
                    "SnowLuma WebSocket 已等待 10s 仍未就绪 — "
                    "请确认已在 SnowLuma WebUI 中配置 OneBot v11 WebSocket "
                    "并扫码登录 QQ"
                )
                warned_10 = True
            if time.time() - start >= timeout:
                return False
            await asyncio.sleep(0.5)

    async def _strict_probe(self) -> bool:
        """握手 + 读一帧 + 校验稳定性：

        - 握手失败 → False
        - 首帧 status=failed → 抛 NcatBotError (上层终止启动)
        - 2 秒内有合法事件帧 → True
        - 2 秒内无帧但连接仍开启 → True (心跳间隔较长的合法情况)
        - 2 秒内连接被服务端关闭且未发任何帧 → False (典型 token 错误/未启用)
        """
        from websockets.exceptions import ConnectionClosed

        uri = self._config.get_uri_with_token()
        try:
            async with websockets.connect(uri, open_timeout=5) as ws:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    # 静默 2s — 仍开着即视为正常
                    return True
                except ConnectionClosed:
                    # 握手后立刻被关：典型 token 错 / OB11 未启用
                    return False
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    return True
                if data.get("status") == "failed":
                    retcode = data.get("retcode")
                    message = data.get("message", "未知错误")
                    if retcode == 1403:
                        raise NcatBotError(
                            "SnowLuma WebSocket Token 校验失败 (1403) — "
                            "请检查 ws_token 是否与 SnowLuma WebUI 中"
                            "OneBot v11 端点的 access_token 一致",
                            False,
                        )
                    raise NcatBotError(f"SnowLuma WebSocket 连接被拒: {message}", False)
                return True
        except NcatBotError:
            raise
        except Exception:
            return False

    async def wait_for_service(self, timeout: int = 15) -> None:
        if not await self.is_service_ok(timeout):
            raise NcatBotError(
                f"等待 SnowLuma WebSocket 服务超时 ({timeout}s)。"
                "请检查 SnowLuma 是否已启动、OneBot v11 端点是否启用、"
                "ws_uri / ws_token 是否一致。"
            )
        LOG.info("SnowLuma WebSocket 服务连接成功")

    # ------------------------------------------------------------
    # 模式入口
    # ------------------------------------------------------------

    async def _connect_only(self) -> None:
        LOG.info("Connect 模式 — 直接连接 SnowLuma: %s", self._config.ws_uri)
        if not await self.is_service_ok():
            raise NcatBotError(
                f"无法连接 SnowLuma WebSocket ({self._config.ws_uri})，"
                "请确认 SnowLuma 已启动并已在 WebUI 中启用 OneBot v11 端点。"
            )
        LOG.info("SnowLuma 服务连接成功")

    async def _setup_and_connect(self) -> bool:
        """Setup 模式入口。

        Returns
        -------
        bool
            ``True`` 表示用户在交互阶段修改了 ws_uri / ws_token，
            调用方（adapter）应将配置写回 config.yaml。
        """
        LOG.debug("Setup 模式 — 检查 SnowLuma 状态...")

        # 已就绪：跳过启动
        if await self.is_service_ok():
            LOG.info("SnowLuma WebSocket 已在线，跳过环境准备")
            return False

        # 平台校验
        try:
            _ = self.platform
        except UnsupportedPlatformError:
            raise NcatBotError(
                "当前操作系统暂不支持 SnowLuma 自动启动，请使用 skip_setup: true。"
            )

        # 安装/更新
        installer = self._ensure_installer()
        skip_confirm = self._effective_skip_confirm()
        if not installer.ensure_installed(skip_confirm=skip_confirm):
            raise NcatBotError("SnowLuma 安装/更新失败")

        # 启动
        self.platform.start_snowluma()
        LOG.info("SnowLuma 已启动")

        # 已有配置时直接探针；首次配置时先快速探一次
        config_changed = False
        has_prior_config = self._config.configured or bool(self._config.ws_token)
        if has_prior_config:
            # 有历史配置：直接进入探针验证（带重试）
            pass
        else:
            # 首次配置：快速探一次，通了就不打扰用户
            try:
                if await self._strict_probe():
                    LOG.info("SnowLuma WebSocket 服务连接成功")
                    return False
            except NcatBotError:
                raise
            except Exception:
                pass
            await self._guide_and_collect_config()
            config_changed = True

        # 探针验证（带重试）
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if await self._strict_probe():
                    self._config.configured = True
                    LOG.info("SnowLuma WebSocket 服务连接成功")
                    return config_changed
            except NcatBotError:
                # Token 鉴权失败等配置错误：也允许重配
                pass

            if attempt < max_retries - 1:
                remaining = max_retries - attempt - 1
                LOG.warning("WebSocket 连接失败，还剩 %d 次重试机会", remaining)
                await self._guide_and_collect_config()
                config_changed = True

        raise NcatBotError(
            "SnowLuma WebSocket 连接失败，已用完重试次数。"
            "请检查 SnowLuma WebUI 中 OneBot v11 配置后重新启动。"
        )

    async def _guide_and_collect_config(self) -> None:
        """打印 WebUI 引导，等待用户按回车后收集 ws_uri / ws_token。"""
        loop = asyncio.get_event_loop()

        webui_uri = self._config.webui_uri
        LOG.warning("=" * 60)
        LOG.warning("请在浏览器打开 SnowLuma WebUI:")
        LOG.warning("    %s", webui_uri)
        LOG.warning("步骤:")
        LOG.warning("  1. 设置/确认登录密码")
        LOG.warning("  2. 在 OneBot v11 控制台启用 WebSocket 服务")
        LOG.warning("  3. 设置 access_token (可留空)")
        LOG.warning("  4. 扫码登录目标 QQ 账号")
        LOG.warning("=" * 60)

        await loop.run_in_executor(None, lambda: input("\n完成后按回车继续..."))

        # 收集 WebSocket 地址
        default_uri = self._config.ws_uri
        ws_uri = await loop.run_in_executor(
            None,
            lambda: input(
                f"请输入 WebSocket 地址 (回车使用默认 {default_uri}): "
            ).strip(),
        )
        if ws_uri:
            self._config.ws_uri = ws_uri

        # 收集 access_token
        ws_token = await loop.run_in_executor(
            None,
            lambda: input("请输入 access_token (无密码直接回车): ").strip(),
        )
        if ws_token:
            self._config.ws_token = ws_token

    # ------------------------------------------------------------
    # 主入口 / 工具
    # ------------------------------------------------------------

    async def launch(self) -> bool:
        """根据 ``skip_setup`` 切换 connect / setup 流程。

        Returns
        -------
        bool
            ``True`` 表示用户在交互阶段修改了配置，调用方应持久化。
        """
        if self._config.skip_setup:
            await self._connect_only()
            return False
        else:
            return await self._setup_and_connect()

    def stop(self) -> None:
        """供 ``BotClient.shutdown()`` 调用，停止本地 SnowLuma 进程。"""
        try:
            self.platform.stop_snowluma()
        except UnsupportedPlatformError:
            pass

    # ------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------

    @staticmethod
    def _effective_skip_confirm() -> bool:
        """与 napcat 一致 — 复用 ``effective_skip_napcat_install_confirm`` 全局
        总开关；本阶段未单独引入 ``skip_snowluma_install_confirm`` 顶层字段，
        如有需要可在 ``Config`` 中追加。"""
        try:
            mgr = get_config_manager()
            getter = getattr(mgr, "effective_skip_napcat_install_confirm", None)
            if callable(getter):
                return bool(getter())
            # 兼容直接读字段
            return bool(getattr(mgr.config, "skip_napcat_install_confirm", False))
        except Exception:
            return False


__all__ = ["SnowLumaLauncher"]
