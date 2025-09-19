from pathlib import Path
import json
from typing import Set

from ncatbot.plugin_system import NcatBotPlugin, NcatBotEvent, command_registry, admin_only
from ncatbot.core import GroupMessageEvent, GroupMessage
from ncatbot.utils import get_log

log = get_log("GroupFilter")


class GroupFilter(NcatBotPlugin):
    name = "GroupFilter"
    version = "1.1.0"
    author = "Wuchang325"
    description = "群信息过滤插件"

    DATA_FILE: Path = Path() / "data" / "GroupFilter" / "filter.json"   # JSON 存储路径
    DEFAULT_MODE = "off"                                      # 默认模式  off / white / black

    async def on_load(self) -> None:
        self._load()
        self.config.get("mode", self.DEFAULT_MODE)
        self.event_bus.subscribe("re:.*", self.handle_event, priority=1000)
        log.info(f"GroupFilter 已加载，当前模式: {self.mode}，群列表: {list(self.groups)}")

    async def handle_event(self, event: NcatBotEvent):
        if self.mode == "off":
            return
        if not isinstance(event.data, GroupMessageEvent):
            return
        gid = str(event.data.group_id)
        in_list = gid in self.groups
        if (self.mode == "white" and not in_list) or (self.mode == "black" and in_list):
            event.intercept()

    @admin_only
    @command_registry.command("gf_mode")
    async def cmd_mode(self, _: GroupMessage, mode: str):
        if mode not in {"off", "white", "black"}:
            await _.reply("❌ 模式只能是 off / white / black")
            return
        self.mode = mode
        self._save()
        await _.reply(f"✅ 已切换为 {mode} 模式")

    @admin_only
    @command_registry.command("gf_add")
    async def cmd_add(self, _: GroupMessage, *gids: str):
        try:
            add_set = {str(int(g)) for g in gids}
        except ValueError:
            await _.reply("❌ 群号必须为数字")
            return
        self.groups |= add_set
        self._save()
        await _.reply(f"✅ 已添加 {len(add_set)} 个群，当前共 {len(self.groups)} 个")

    @admin_only
    @command_registry.command("gf_del")
    async def cmd_del(self, _: GroupMessage, *gids: str):
        try:
            del_set = {str(int(g)) for g in gids}
        except ValueError:
            await _.reply("❌ 群号必须为数字")
            return
        self.groups -= del_set
        self._save()
        await _.reply(f"✅ 已移除 {len(del_set)} 个群，当前共 {len(self.groups)} 个")

    @admin_only
    @command_registry.command("gf_list")
    async def cmd_list(self, _: GroupMessage):
        await _.reply(
            f"📄 当前模式：{self.mode}\n"
            f"📄 群列表（{len(self.groups)} 个）：\n"
            + ("\n".join(sorted(self.groups)) or "（空）")
        )
        
    def _load(self):
        if self.DATA_FILE.exists():
            data = json.loads(self.DATA_FILE.read_text(encoding="utf-8"))
            self.mode = data.get("mode", self.DEFAULT_MODE)
            self.groups = set(map(str, data.get("groups", [])))
        else:
            self.mode = self.DEFAULT_MODE
            self.groups = set()
            self._save()

    def _save(self):
        self.DATA_FILE.write_text(
            json.dumps({"mode": self.mode, "groups": sorted(self.groups)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )