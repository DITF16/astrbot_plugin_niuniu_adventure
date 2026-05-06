import os
import json
import time
import math
import random
import asyncio
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp

from admin_server import NiuNiuAdminServer

DB_DIR = os.path.join("data", "niuniu_game")
DB_PATH = os.path.join(DB_DIR, "niuniu.db")


def now_ts() -> int:
    return int(time.time())


def rnd_float(a: float, b: float, digits: int = 2) -> float:
    return round(random.uniform(a, b), digits)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def fmt(v: float) -> str:
    return f"{v:.2f}"


class NiuNiuPlugin(Star):
    """
    牛牛大乱斗核心插件。

    设计说明：
    1. SQLite 使用 aiosqlite，避免阻塞事件循环。
    2. 涉及属性变化、购买、兑换码领取、战斗结算的地方都使用事务。
    3. 商城道具和趣味文案存库，后续管理后台可以直接修改。
    """

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        os.makedirs(DB_DIR, exist_ok=True)
        self._db_ready = False
        self.admin_server = None

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """
        AstrBot 初始化完成后启动管理后台。

        注意：
        - 必须先初始化数据库，否则后台访问表时可能报错。
        - 管理后台是否启动由配置控制。
        """
        await self._ensure_db()

        enabled = bool(self.config.get("enable_builtin_admin_server", False))
        if not enabled:
            logger.info("牛牛管理后台未启用")
            return

        host = str(self.config.get("admin_server_host", "127.0.0.1"))
        port = int(self.config.get("admin_server_port", 8787))
        token = str(self.config.get("admin_token", "please-change-me"))

        static_dir = os.path.join(os.path.dirname(__file__), "static")

        self.admin_server = NiuNiuAdminServer(
            db_path=DB_PATH,
            static_dir=static_dir,
            host=host,
            port=port,
            token=token
        )

        await self.admin_server.start()

    async def _ensure_db(self):
        if self._db_ready:
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA foreign_keys=ON;")

            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    length REAL NOT NULL,
                    girth REAL NOT NULL,
                    hardness REAL NOT NULL,
                    charm REAL NOT NULL DEFAULT 0,
                    energy INTEGER NOT NULL DEFAULT 100,
                    win_rate_buff REAL NOT NULL DEFAULT 0,
                    debuff_shield_until INTEGER NOT NULL DEFAULT 0,
                    event_ignore_count INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(group_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(group_id, user_id, item_id)
                );

                CREATE TABLE IF NOT EXISTS shop_items (
                    item_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    effect_json TEXT NOT NULL,
                    description TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cooldowns (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_user_id TEXT NOT NULL DEFAULT '',
                    last_ts INTEGER NOT NULL,
                    extra_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(group_id, user_id, action, target_user_id)
                );

                CREATE TABLE IF NOT EXISTS announcements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS redeem_codes (
                    code TEXT PRIMARY KEY,
                    reward_json TEXT NOT NULL,
                    expire_at INTEGER NOT NULL,
                    max_uses INTEGER NOT NULL DEFAULT 1,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS redeem_logs (
                    code TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    used_at INTEGER NOT NULL,
                    PRIMARY KEY(code, group_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS text_templates (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                """
            )

            await self._seed_default_shop(db)
            await self._seed_default_texts(db)
            await db.commit()

        self._db_ready = True
        logger.info("牛牛大乱斗数据库初始化完成")

    async def _seed_default_shop(self, db: aiosqlite.Connection):
        rows = await db.execute_fetchall("SELECT COUNT(*) FROM shop_items")
        if rows and rows[0][0] > 0:
            return

        items = [
            {
                "item_id": "len_small",
                "name": "成长饼干",
                "item_type": "prop",
                "price": 20,
                "effect": {"add": {"length": 1.0}},
                "description": "长度 +1.00"
            },
            {
                "item_id": "girth_small",
                "name": "圆润奶茶",
                "item_type": "prop",
                "price": 20,
                "effect": {"add": {"girth": 0.2}},
                "description": "粗度 +0.20"
            },
            {
                "item_id": "hard_small",
                "name": "钢铁意志",
                "item_type": "prop",
                "price": 20,
                "effect": {"add": {"hardness": 2.0}},
                "description": "硬度 +2.00"
            },
            {
                "item_id": "len_percent",
                "name": "超级成长剂",
                "item_type": "prop",
                "price": 120,
                "effect": {"percent": {"length": 0.08}},
                "description": "长度 +8%"
            },
            {
                "item_id": "win_lucky",
                "name": "幸运小红绳",
                "item_type": "prop",
                "price": 80,
                "effect": {"win_rate_buff": 0.03},
                "description": "永久胜率 +3%，上限会受系统限制"
            },
            {
                "item_id": "debuff_shield",
                "name": "稳态护符",
                "item_type": "prop",
                "price": 100,
                "effect": {"debuff_shield_seconds": 3600},
                "description": "1小时内免除 debuff 事件影响"
            },
            {
                "item_id": "event_ignore",
                "name": "命运回避券",
                "item_type": "prop",
                "price": 60,
                "effect": {"event_ignore_count": 1},
                "description": "无视一次战斗后事件"
            },
            {
                "item_id": "acc_crown",
                "name": "牛牛小王冠",
                "item_type": "accessory",
                "price": 150,
                "effect": {"charm": 20},
                "description": "饰品，魅力值 +20"
            },
            {
                "item_id": "acc_sunglasses",
                "name": "酷炫墨镜",
                "item_type": "accessory",
                "price": 100,
                "effect": {"charm": 12},
                "description": "饰品，魅力值 +12"
            }
        ]

        for item in items:
            await db.execute(
                """
                INSERT OR IGNORE INTO shop_items
                (item_id, name, item_type, price, effect_json, description, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    item["item_id"],
                    item["name"],
                    item["item_type"],
                    item["price"],
                    json.dumps(item["effect"], ensure_ascii=False),
                    item["description"],
                    now_ts()
                )
            )

    async def _seed_default_texts(self, db: aiosqlite.Connection):
        rows = await db.execute_fetchall("SELECT COUNT(*) FROM text_templates")
        if rows and rows[0][0] > 0:
            return

        texts = {
            "register.success": [
                "{nickname}，注册成功！你的牛牛诞生啦：长度 {length}，粗度 {girth}，硬度 {hardness}。"
            ],
            "register.already": [
                "{nickname}，你已经注册过牛牛啦，别想偷偷再领一只哦。"
            ],
            "register.only_group": [
                "不许一个人偷偷玩牛牛噢，请到群聊里来。"
            ],
            "my.not_registered": [
                "{nickname}，你还没有注册牛牛，请先发送“注册牛牛”。"
            ],
            "dajiao.cooldown": [
                "{nickname}，牛牛还在恢复中呢，至少再等 {remain} 秒，心急可吃不了热豆腐呀。"
            ],
            "dajiao.increase": [
                "{nickname}，这一波操作稳得很，随机属性获得提升：{attr} +{change}！"
            ],
            "dajiao.decrease": [
                "{nickname}，哎呀有点用力过猛，随机属性受到了影响：{attr} -{change}，下次悠着点。"
            ],
            "fly.cooldown": [
                "{nickname}，飞飞机小游戏还在冷却中，剩余 {remain} 秒。"
            ],
            "fly.success": [
                "{nickname}，飞行姿态优雅，获得精力 +{energy}！"
            ],
            "compare.not_registered": [
                "{nickname}，你还没有注册牛牛，无法比划哦。"
            ],
            "compare.no_target": [
                "{nickname}，请 @ 一名已注册牛牛的群友进行比划。"
            ],
            "compare.target_not_registered": [
                "{nickname}，对方还没有注册牛牛呢，先让 TA 发送“注册牛牛”吧。"
            ],
            "compare.cooldown": [
                "{nickname}，你刚和这位群友比划过，再等 {remain} 秒吧。"
            ],
            "compare.win": [
                "{winner} 的牛牛气势如虹，战胜了 {loser}！胜率不是百分百，但这次命运站在了胜者这边。"
            ],
            "compare.upset": [
                "奇迹发生！{winner} 在明显劣势下逆袭了 {loser}，群友们都惊呆啦！"
            ],
            "compare.event": [
                "战后事件触发：{nickname} 遭遇「{event_name}」，{effect_desc}"
            ],
            "shop.header": [
                "牛牛商城：\n{items}\n发送：牛牛购买 道具ID"
            ],
            "purchase.success": [
                "{nickname}，购买成功：{item_name}，消耗精力 {price}。"
            ],
            "purchase.no_energy": [
                "{nickname}，精力不足哦，当前精力 {energy}，需要 {price}。"
            ],
            "purchase.not_found": [
                "{nickname}，没有找到这个商品，请发送“牛牛商城”查看。"
            ]
        }

        for key, value in texts.items():
            await db.execute(
                "INSERT OR IGNORE INTO text_templates(key, value_json) VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False))
            )

    async def _text(self, db: aiosqlite.Connection, key: str, **kwargs) -> str:
        rows = await db.execute_fetchall(
            "SELECT value_json FROM text_templates WHERE key = ?",
            (key,)
        )
        if not rows:
            return key.format(**kwargs)
        arr = json.loads(rows[0][0])
        template = random.choice(arr) if isinstance(arr, list) else str(arr)
        return template.format(**kwargs)

    def _group_id(self, event: AstrMessageEvent) -> str:
        return getattr(event.message_obj, "group_id", "") or ""

    def _user_id(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    def _nickname(self, event: AstrMessageEvent) -> str:
        return event.get_sender_name() or self._user_id(event)

    async def _get_user(
        self,
        db: aiosqlite.Connection,
        group_id: str,
        user_id: str
    ) -> Optional[Dict[str, Any]]:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM users WHERE group_id = ? AND user_id = ?",
            (group_id, user_id)
        )
        return dict(rows[0]) if rows else None

    async def _recalc_charm(
        self,
        db: aiosqlite.Connection,
        group_id: str,
        user_id: str
    ):
        user = await self._get_user(db, group_id, user_id)
        if not user:
            return

        base = user["length"] * 0.8 + user["girth"] * 3.0 + user["hardness"] * 0.15

        rows = await db.execute_fetchall(
            """
            SELECT i.item_id, i.count, s.effect_json
            FROM inventory i
            JOIN shop_items s ON i.item_id = s.item_id
            WHERE i.group_id = ? AND i.user_id = ? AND i.item_type = 'accessory'
            """,
            (group_id, user_id)
        )

        accessory_charm = 0
        for item_id, count, effect_json in rows:
            effect = json.loads(effect_json)
            accessory_charm += float(effect.get("charm", 0)) * int(count)

        charm = round(base + accessory_charm, 2)
        await db.execute(
            "UPDATE users SET charm = ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
            (charm, now_ts(), group_id, user_id)
        )

    def _ideal_debuffs(self, user: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        理想比例模型：
        - 理想粗度 ~= 长度 * 0.18
        - 理想硬度 ~= 长度 * 3.0

        debuff：
        - 过短：长度相对粗度/硬度承载不足，提高“被碾压”概率
        - 过细：粗度低于理想值，提高“折断”概率
        - 过软：硬度低于理想值，提高“缠绕”概率
        """
        length = max(float(user["length"]), 0.01)
        girth = max(float(user["girth"]), 0.01)
        hardness = max(float(user["hardness"]), 0.01)

        debuffs = []

        ideal_girth = length * 0.18
        if girth < ideal_girth * 0.75:
            deviation = 1 - girth / (ideal_girth * 0.75)
            prob = clamp(0.05 + deviation * 0.45, 0.05, 0.50)
            debuffs.append({
                "type": "too_thin",
                "name": "过细",
                "event": "折断",
                "prob": prob,
                "desc": f"粗度偏低，战后有 {prob * 100:.1f}% 概率触发折断，长度减半。"
            })

        ideal_hardness = length * 3.0
        if hardness < ideal_hardness * 0.75:
            deviation = 1 - hardness / (ideal_hardness * 0.75)
            prob = clamp(0.05 + deviation * 0.45, 0.05, 0.50)
            debuffs.append({
                "type": "too_soft",
                "name": "过软",
                "event": "缠绕",
                "prob": prob,
                "desc": f"硬度偏低，战后有 {prob * 100:.1f}% 概率触发缠绕，粗度减半。"
            })

        # 长度如果显著低于由粗度和硬度推导出的承载长度，则视为“过短”
        expected_length_from_girth = girth / 0.18
        expected_length_from_hardness = hardness / 3.0
        expected_length = (expected_length_from_girth + expected_length_from_hardness) / 2

        if length < expected_length * 0.65:
            deviation = 1 - length / (expected_length * 0.65)
            prob = clamp(0.05 + deviation * 0.45, 0.05, 0.50)
            debuffs.append({
                "type": "too_short",
                "name": "过短",
                "event": "被碾压",
                "prob": prob,
                "desc": f"长度承载不足，战后有 {prob * 100:.1f}% 概率触发被碾压，硬度减半。"
            })

        return debuffs

    def _strength(self, user: Dict[str, Any]) -> float:
        length = max(float(user["length"]), 0.01)
        girth = max(float(user["girth"]), 0.01)
        hardness = max(float(user["hardness"]), 0.01)

        # 加权模型，避免单一属性碾压全部玩法。
        return length * 4.5 + girth * 18.0 + hardness * 1.2

    def _win_prob(self, a: Dict[str, Any], b: Dict[str, Any]) -> float:
        sa = self._strength(a)
        sb = self._strength(b)

        # 使用 log ratio + sigmoid，强者大幅提高胜率，但弱者保留小概率爆冷。
        ratio = max(sa / max(sb, 0.01), 0.01)
        raw = 1 / (1 + math.exp(-math.log(ratio) * 2.2))

        raw += float(a.get("win_rate_buff", 0))
        raw -= float(b.get("win_rate_buff", 0)) * 0.5

        return clamp(raw, 0.05, 0.95)

    async def _apply_event_if_any(
        self,
        db: aiosqlite.Connection,
        group_id: str,
        user: Dict[str, Any]
    ) -> Optional[str]:
        current = now_ts()

        if int(user.get("debuff_shield_until", 0)) > current:
            return None

        if int(user.get("event_ignore_count", 0)) > 0:
            await db.execute(
                """
                UPDATE users
                SET event_ignore_count = event_ignore_count - 1, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (current, group_id, user["user_id"])
            )
            return None

        debuffs = self._ideal_debuffs(user)

        # 没有 debuff 也有 1% 随机事件。
        if not debuffs:
            if random.random() > 0.01:
                return None
            event = random.choice(["被碾压", "折断", "缠绕"])
            prob_source = {"event": event}
        else:
            triggered = None
            for d in debuffs:
                if random.random() < d["prob"]:
                    triggered = d
                    break
            if not triggered:
                return None
            event = triggered["event"]
            prob_source = triggered

        if event == "被碾压":
            new_hardness = max(1.0, float(user["hardness"]) * 0.5)
            await db.execute(
                "UPDATE users SET hardness = ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
                (new_hardness, current, group_id, user["user_id"])
            )
            desc = f"硬度变为原来的一半，当前硬度 {fmt(new_hardness)}"
        elif event == "折断":
            new_length = max(1.0, float(user["length"]) * 0.5)
            await db.execute(
                "UPDATE users SET length = ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
                (new_length, current, group_id, user["user_id"])
            )
            desc = f"长度变为原来的一半，当前长度 {fmt(new_length)}"
        else:
            new_girth = max(0.1, float(user["girth"]) * 0.5)
            await db.execute(
                "UPDATE users SET girth = ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
                (new_girth, current, group_id, user["user_id"])
            )
            desc = f"粗度变为原来的一半，当前粗度 {fmt(new_girth)}"

        nickname = user.get("nickname", "某位群友")
        return await self._text(
            db,
            "compare.event",
            nickname=nickname,
            event_name=event,
            effect_desc=desc
        )

    def _extract_at_target(self, event: AstrMessageEvent) -> Optional[str]:
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.At):
                qq = getattr(seg, "qq", None)
                if qq:
                    return str(qq)
        return None

    def _is_super_admin(self, user_id: str) -> bool:
        raw = str(self.config.get("super_admin_ids", "") or "")
        ids = {x.strip() for x in raw.split(",") if x.strip()}
        return user_id in ids

    # ======================
    # 指令：注册牛牛
    # ======================
    @filter.command("注册牛牛")
    async def register(self, event: AstrMessageEvent):
        """注册牛牛，获得初始随机属性。"""
        await self._ensure_db()

        group_id = self._group_id(event)
        if not group_id:
            async with aiosqlite.connect(DB_PATH) as db:
                msg = await self._text(db, "register.only_group")
            yield event.plain_result(msg)
            return

        user_id = self._user_id(event)
        nickname = self._nickname(event)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            user = await self._get_user(db, group_id, user_id)
            if user:
                msg = await self._text(db, "register.already", nickname=nickname)
                await db.commit()
                yield event.plain_result(msg)
                return

            length = rnd_float(8, 16)
            girth = rnd_float(1.5, 3.2)
            hardness = rnd_float(20, 45)
            current = now_ts()

            await db.execute(
                """
                INSERT INTO users
                (group_id, user_id, nickname, length, girth, hardness, charm, energy, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, 100, ?, ?)
                """,
                (group_id, user_id, nickname, length, girth, hardness, current, current)
            )
            await self._recalc_charm(db, group_id, user_id)
            await db.commit()

            msg = await self._text(
                db,
                "register.success",
                nickname=nickname,
                length=fmt(length),
                girth=fmt(girth),
                hardness=fmt(hardness)
            )

        yield event.plain_result(msg)

    # ======================
    # 指令：我的牛牛
    # ======================
    @filter.command("我的牛牛")
    async def my_niuniu(self, event: AstrMessageEvent):
        """查看当前牛牛属性、魅力和 debuff。"""
        await self._ensure_db()

        group_id = self._group_id(event)
        user_id = self._user_id(event)
        nickname = self._nickname(event)

        async with aiosqlite.connect(DB_PATH) as db:
            user = await self._get_user(db, group_id, user_id)
            if not user:
                msg = await self._text(db, "my.not_registered", nickname=nickname)
                yield event.plain_result(msg)
                return

            debuffs = self._ideal_debuffs(user)
            debuff_text = "\n".join([f"- {d['name']}：{d['desc']}" for d in debuffs]) or "暂无 debuff，比例很健康，继续保持。"

            msg = (
                f"{nickname} 的牛牛档案：\n"
                f"长度：{fmt(user['length'])}\n"
                f"粗度：{fmt(user['girth'])}\n"
                f"硬度：{fmt(user['hardness'])}\n"
                f"魅力值：{fmt(user['charm'])}\n"
                f"精力：{user['energy']}\n"
                f"\n养成提示：\n{debuff_text}"
            )

        yield event.plain_result(msg)

    # ======================
    # 指令：牛牛饰品
    # ======================
    @filter.command("牛牛饰品")
    async def accessories(self, event: AstrMessageEvent):
        """查看当前拥有的所有饰品。"""
        await self._ensure_db()

        group_id = self._group_id(event)
        user_id = self._user_id(event)

        async with aiosqlite.connect(DB_PATH) as db:
            rows = await db.execute_fetchall(
                """
                SELECT s.name, i.count, s.description
                FROM inventory i
                JOIN shop_items s ON i.item_id = s.item_id
                WHERE i.group_id = ? AND i.user_id = ? AND i.item_type = 'accessory'
                """,
                (group_id, user_id)
            )

        if not rows:
            yield event.plain_result("你还没有牛牛饰品哦，发送“牛牛商城”看看有什么好东西吧。")
            return

        lines = ["你的牛牛饰品："]
        for name, count, desc in rows:
            lines.append(f"- {name} x{count}：{desc}")

        yield event.plain_result("\n".join(lines))

    # ======================
    # 指令：牛牛商城
    # ======================
    @filter.command("牛牛商城")
    async def shop(self, event: AstrMessageEvent):
        """查看牛牛商城商品。"""
        await self._ensure_db()

        async with aiosqlite.connect(DB_PATH) as db:
            rows = await db.execute_fetchall(
                """
                SELECT item_id, name, item_type, price, description
                FROM shop_items
                WHERE enabled = 1
                ORDER BY price ASC
                """
            )
            items = []
            for item_id, name, item_type, price, desc in rows:
                type_name = "饰品" if item_type == "accessory" else "道具"
                items.append(f"{item_id}｜{name}｜{type_name}｜{price}精力｜{desc}")

            msg = await self._text(db, "shop.header", items="\n".join(items))

        yield event.plain_result(msg)

    # ======================
    # 指令：牛牛购买 道具ID
    # ======================
    @filter.command("牛牛购买")
    async def purchase(self, event: AstrMessageEvent, item_id: str = ""):
        """购买商城中的道具或饰品。用法：牛牛购买 道具ID"""
        await self._ensure_db()

        group_id = self._group_id(event)
        user_id = self._user_id(event)
        nickname = self._nickname(event)

        if not item_id:
            yield event.plain_result("请提供商品 ID，例如：牛牛购买 len_small")
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")

            user = await self._get_user(db, group_id, user_id)
            if not user:
                msg = await self._text(db, "my.not_registered", nickname=nickname)
                await db.commit()
                yield event.plain_result(msg)
                return

            rows = await db.execute_fetchall(
                """
                SELECT item_id, name, item_type, price, effect_json
                FROM shop_items
                WHERE item_id = ? AND enabled = 1
                """,
                (item_id,)
            )
            if not rows:
                msg = await self._text(db, "purchase.not_found", nickname=nickname)
                await db.commit()
                yield event.plain_result(msg)
                return

            item_id, item_name, item_type, price, effect_json = rows[0]
            if int(user["energy"]) < int(price):
                msg = await self._text(
                    db,
                    "purchase.no_energy",
                    nickname=nickname,
                    energy=user["energy"],
                    price=price
                )
                await db.commit()
                yield event.plain_result(msg)
                return

            effect = json.loads(effect_json)

            await db.execute(
                """
                UPDATE users
                SET energy = energy - ?, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (price, now_ts(), group_id, user_id)
            )

            # 饰品进背包，道具默认立即生效。
            if item_type == "accessory":
                await db.execute(
                    """
                    INSERT INTO inventory(group_id, user_id, item_id, item_type, count)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(group_id, user_id, item_id)
                    DO UPDATE SET count = count + 1
                    """,
                    (group_id, user_id, item_id, item_type)
                )
            else:
                await self._apply_item_effect(db, group_id, user_id, effect)

            await self._recalc_charm(db, group_id, user_id)
            await db.commit()

            msg = await self._text(
                db,
                "purchase.success",
                nickname=nickname,
                item_name=item_name,
                price=price
            )

        yield event.plain_result(msg)

    async def _apply_item_effect(
        self,
        db: aiosqlite.Connection,
        group_id: str,
        user_id: str,
        effect: Dict[str, Any]
    ):
        user = await self._get_user(db, group_id, user_id)
        if not user:
            return

        length = float(user["length"])
        girth = float(user["girth"])
        hardness = float(user["hardness"])
        win_rate_buff = float(user.get("win_rate_buff", 0))
        shield_until = int(user.get("debuff_shield_until", 0))
        ignore_count = int(user.get("event_ignore_count", 0))

        add = effect.get("add", {})
        length += float(add.get("length", 0))
        girth += float(add.get("girth", 0))
        hardness += float(add.get("hardness", 0))

        percent = effect.get("percent", {})
        length *= 1 + float(percent.get("length", 0))
        girth *= 1 + float(percent.get("girth", 0))
        hardness *= 1 + float(percent.get("hardness", 0))

        win_rate_buff = clamp(
            win_rate_buff + float(effect.get("win_rate_buff", 0)),
            0,
            0.15
        )

        if "debuff_shield_seconds" in effect:
            shield_until = max(shield_until, now_ts()) + int(effect["debuff_shield_seconds"])

        if "event_ignore_count" in effect:
            ignore_count += int(effect["event_ignore_count"])

        await db.execute(
            """
            UPDATE users
            SET length = ?, girth = ?, hardness = ?, win_rate_buff = ?,
                debuff_shield_until = ?, event_ignore_count = ?, updated_at = ?
            WHERE group_id = ? AND user_id = ?
            """,
            (
                round(length, 2),
                round(girth, 2),
                round(hardness, 2),
                win_rate_buff,
                shield_until,
                ignore_count,
                now_ts(),
                group_id,
                user_id
            )
        )

    # ======================
    # 指令：比划比划 @群友
    # ======================
    @filter.command("比划比划")
    async def compare(self, event: AstrMessageEvent):
        """和 @ 的群友进行牛牛战斗。"""
        await self._ensure_db()

        group_id = self._group_id(event)
        user_id = self._user_id(event)
        nickname = self._nickname(event)
        target_id = self._extract_at_target(event)

        async with aiosqlite.connect(DB_PATH) as db:
            if not target_id or target_id == user_id:
                msg = await self._text(db, "compare.no_target", nickname=nickname)
                yield event.plain_result(msg)
                return

            await db.execute("BEGIN IMMEDIATE")

            user = await self._get_user(db, group_id, user_id)
            target = await self._get_user(db, group_id, target_id)

            if not user:
                msg = await self._text(db, "compare.not_registered", nickname=nickname)
                await db.commit()
                yield event.plain_result(msg)
                return

            if not target:
                msg = await self._text(db, "compare.target_not_registered", nickname=nickname)
                await db.commit()
                yield event.plain_result(msg)
                return

            cd_key = target_id
            cd_rows = await db.execute_fetchall(
                """
                SELECT last_ts FROM cooldowns
                WHERE group_id = ? AND user_id = ? AND action = 'compare' AND target_user_id = ?
                """,
                (group_id, user_id, cd_key)
            )
            current = now_ts()
            cooldown = 600
            if cd_rows and current - int(cd_rows[0][0]) < cooldown:
                remain = cooldown - (current - int(cd_rows[0][0]))
                msg = await self._text(db, "compare.cooldown", nickname=nickname, remain=remain)
                await db.commit()
                yield event.plain_result(msg)
                return

            await db.execute(
                """
                INSERT INTO cooldowns(group_id, user_id, action, target_user_id, last_ts)
                VALUES (?, ?, 'compare', ?, ?)
                ON CONFLICT(group_id, user_id, action, target_user_id)
                DO UPDATE SET last_ts = excluded.last_ts
                """,
                (group_id, user_id, cd_key, current)
            )

            p = self._win_prob(user, target)
            user_win = random.random() < p

            if user_win:
                winner, loser = user, target
            else:
                winner, loser = target, user

            sw = self._strength(winner)
            sl = self._strength(loser)
            ratio = sw / max(sl, 0.01)

            upset = False
            if user_win and self._strength(user) < self._strength(target) * 0.65:
                upset = True
            if (not user_win) and self._strength(target) < self._strength(user) * 0.65:
                upset = True

            reward_factor = 1.0
            penalty_factor = 1.0

            # 弱势方爆冷：奖励更多；强势方翻车：惩罚更大。
            if upset:
                reward_factor = 2.0
                penalty_factor = 1.5
            elif ratio > 1.8:
                reward_factor = 0.6
                penalty_factor = 1.2

            # 属性变化：赢家随机 1~3 个属性增加，输家随机 1~3 个属性减少。
            attrs = ["length", "girth", "hardness"]
            win_attrs = random.sample(attrs, random.randint(1, 3))
            lose_attrs = random.sample(attrs, random.randint(1, 3))

            reward_desc = []
            penalty_desc = []

            for attr in win_attrs:
                delta = self._battle_delta(attr, reward_factor)
                await db.execute(
                    f"UPDATE users SET {attr} = {attr} + ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
                    (delta, current, group_id, winner["user_id"])
                )
                reward_desc.append(f"{self._attr_name(attr)} +{fmt(delta)}")

            for attr in lose_attrs:
                delta = self._battle_delta(attr, penalty_factor)
                min_value = 1.0 if attr != "girth" else 0.1
                await db.execute(
                    f"""
                    UPDATE users
                    SET {attr} = CASE WHEN {attr} - ? < ? THEN ? ELSE {attr} - ? END,
                        updated_at = ?
                    WHERE group_id = ? AND user_id = ?
                    """,
                    (delta, min_value, min_value, delta, current, group_id, loser["user_id"])
                )
                penalty_desc.append(f"{self._attr_name(attr)} -{fmt(delta)}")

            energy_reward = int(random.randint(8, 20) * reward_factor)
            await db.execute(
                """
                UPDATE users
                SET energy = energy + ?, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (energy_reward, current, group_id, winner["user_id"])
            )

            # 重新取一次用户数据，用于事件判定。
            winner_new = await self._get_user(db, group_id, winner["user_id"])
            loser_new = await self._get_user(db, group_id, loser["user_id"])

            event_msgs = []
            for u in [winner_new, loser_new]:
                ev = await self._apply_event_if_any(db, group_id, u)
                if ev:
                    event_msgs.append(ev)

            await self._recalc_charm(db, group_id, winner["user_id"])
            await self._recalc_charm(db, group_id, loser["user_id"])
            await db.commit()

            if upset:
                battle_msg = await self._text(
                    db,
                    "compare.upset",
                    winner=winner["nickname"],
                    loser=loser["nickname"]
                )
            else:
                battle_msg = await self._text(
                    db,
                    "compare.win",
                    winner=winner["nickname"],
                    loser=loser["nickname"]
                )

            msg = (
                f"{battle_msg}\n"
                f"胜者奖励：{', '.join(reward_desc)}，精力 +{energy_reward}\n"
                f"败者变化：{', '.join(penalty_desc)}\n"
                f"本场胜率参考：{nickname} 胜率约 {p * 100:.1f}%"
            )

            if event_msgs:
                msg += "\n\n" + "\n".join(event_msgs)

        yield event.plain_result(msg)

    def _battle_delta(self, attr: str, factor: float) -> float:
        if attr == "length":
            return round(random.uniform(0.3, 1.5) * factor, 2)
        if attr == "girth":
            return round(random.uniform(0.05, 0.35) * factor, 2)
        return round(random.uniform(1.0, 4.0) * factor, 2)

    def _attr_name(self, attr: str) -> str:
        return {
            "length": "长度",
            "girth": "粗度",
            "hardness": "硬度"
        }.get(attr, attr)

    # ======================
    # 指令：打胶
    # ======================
    @filter.command("打胶")
    async def dajiao(self, event: AstrMessageEvent):
        """5分钟冷却的随机属性变化玩法，半小时内次数越多风险越高。"""
        await self._ensure_db()

        group_id = self._group_id(event)
        user_id = self._user_id(event)
        nickname = self._nickname(event)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")

            user = await self._get_user(db, group_id, user_id)
            if not user:
                msg = await self._text(db, "my.not_registered", nickname=nickname)
                await db.commit()
                yield event.plain_result(msg)
                return

            current = now_ts()
            rows = await db.execute_fetchall(
                """
                SELECT last_ts, extra_json FROM cooldowns
                WHERE group_id = ? AND user_id = ? AND action = 'dajiao' AND target_user_id = ''
                """,
                (group_id, user_id)
            )

            cooldown = 300
            recent_times = []
            if rows:
                last_ts, extra_json = rows[0]
                if current - int(last_ts) < cooldown:
                    remain = cooldown - (current - int(last_ts))
                    msg = await self._text(db, "dajiao.cooldown", nickname=nickname, remain=remain)
                    await db.commit()
                    yield event.plain_result(msg)
                    return

                try:
                    recent_times = json.loads(extra_json).get("recent", [])
                except Exception:
                    recent_times = []

            recent_times = [t for t in recent_times if current - int(t) <= 1800]
            recent_times.append(current)

            # 基础奖励 70%，半小时内每多一次，奖励率 -10%，最低 20%。
            reward_prob = clamp(0.70 - max(0, len(recent_times) - 1) * 0.10, 0.20, 0.70)
            is_reward = random.random() < reward_prob

            attr = random.choice(["length", "girth", "hardness"])
            if attr == "length":
                change = rnd_float(0.2, 1.2)
                min_value = 1.0
            elif attr == "girth":
                change = rnd_float(0.05, 0.25)
                min_value = 0.1
            else:
                change = rnd_float(0.8, 3.0)
                min_value = 1.0

            if is_reward:
                await db.execute(
                    f"UPDATE users SET {attr} = {attr} + ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
                    (change, current, group_id, user_id)
                )
                key = "dajiao.increase"
                sign_change = change
            else:
                await db.execute(
                    f"""
                    UPDATE users
                    SET {attr} = CASE WHEN {attr} - ? < ? THEN ? ELSE {attr} - ? END,
                        updated_at = ?
                    WHERE group_id = ? AND user_id = ?
                    """,
                    (change, min_value, min_value, change, current, group_id, user_id)
                )
                key = "dajiao.decrease"
                sign_change = change

            await db.execute(
                """
                INSERT INTO cooldowns(group_id, user_id, action, target_user_id, last_ts, extra_json)
                VALUES (?, ?, 'dajiao', '', ?, ?)
                ON CONFLICT(group_id, user_id, action, target_user_id)
                DO UPDATE SET last_ts = excluded.last_ts, extra_json = excluded.extra_json
                """,
                (
                    group_id,
                    user_id,
                    current,
                    json.dumps({"recent": recent_times}, ensure_ascii=False)
                )
            )

            await self._recalc_charm(db, group_id, user_id)
            await db.commit()

            msg = await self._text(
                db,
                key,
                nickname=nickname,
                attr=self._attr_name(attr),
                change=fmt(sign_change)
            )

        yield event.plain_result(msg)

    # ======================
    # 指令：飞飞机
    # ======================
    @filter.command("飞飞机")
    async def fly(self, event: AstrMessageEvent):
        """参加小游戏获得精力，冷却半小时。"""
        await self._ensure_db()

        group_id = self._group_id(event)
        user_id = self._user_id(event)
        nickname = self._nickname(event)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")

            user = await self._get_user(db, group_id, user_id)
            if not user:
                msg = await self._text(db, "my.not_registered", nickname=nickname)
                await db.commit()
                yield event.plain_result(msg)
                return

            current = now_ts()
            rows = await db.execute_fetchall(
                """
                SELECT last_ts FROM cooldowns
                WHERE group_id = ? AND user_id = ? AND action = 'fly' AND target_user_id = ''
                """,
                (group_id, user_id)
            )

            cooldown = 1800
            if rows and current - int(rows[0][0]) < cooldown:
                remain = cooldown - (current - int(rows[0][0]))
                msg = await self._text(db, "fly.cooldown", nickname=nickname, remain=remain)
                await db.commit()
                yield event.plain_result(msg)
                return

            energy = random.randint(20, 60)
            await db.execute(
                "UPDATE users SET energy = energy + ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
                (energy, current, group_id, user_id)
            )

            await db.execute(
                """
                INSERT INTO cooldowns(group_id, user_id, action, target_user_id, last_ts)
                VALUES (?, ?, 'fly', '', ?)
                ON CONFLICT(group_id, user_id, action, target_user_id)
                DO UPDATE SET last_ts = excluded.last_ts
                """,
                (group_id, user_id, current)
            )

            await db.commit()

            msg = await self._text(db, "fly.success", nickname=nickname, energy=energy)

        yield event.plain_result(msg)

    # ======================
    # 指令：牛牛公告
    # ======================
    @filter.command("牛牛公告")
    async def announcement(self, event: AstrMessageEvent):
        """查看最新牛牛公告。"""
        await self._ensure_db()

        async with aiosqlite.connect(DB_PATH) as db:
            rows = await db.execute_fetchall(
                """
                SELECT content, created_at
                FROM announcements
                ORDER BY id DESC
                LIMIT 1
                """
            )

        if not rows:
            yield event.plain_result("当前还没有牛牛公告，博士……啊不是，管理员还没发布新消息。")
            return

        content, created_at = rows[0]
        yield event.plain_result(f"牛牛公告：\n{content}\n发布时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(created_at))}")

    # ======================
    # 指令：牛牛发送公告 内容
    # ======================
    @filter.command("牛牛发送公告")
    async def send_announcement(self, event: AstrMessageEvent, content: str = ""):
        """超级管理员发送牛牛公告。"""
        await self._ensure_db()

        user_id = self._user_id(event)
        if not self._is_super_admin(user_id):
            yield event.plain_result("权限不足，只有超级管理员可以发送牛牛公告。")
            return

        if not content:
            yield event.plain_result("请输入公告内容，例如：牛牛发送公告 今天商城上新啦！")
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO announcements(content, created_by, created_at)
                VALUES (?, ?, ?)
                """,
                (content, user_id, now_ts())
            )
            await db.commit()

        yield event.plain_result("牛牛公告发送成功。")

    # ======================
    # 指令：牛牛兑换码 兑换码
    # ======================
    @filter.command("牛牛兑换码")
    async def redeem(self, event: AstrMessageEvent, code: str = ""):
        """使用兑换码领取奖励。"""
        await self._ensure_db()

        group_id = self._group_id(event)
        user_id = self._user_id(event)
        nickname = self._nickname(event)

        if not code:
            yield event.plain_result("请输入兑换码，例如：牛牛兑换码 HAPPYNIU")
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")

            user = await self._get_user(db, group_id, user_id)
            if not user:
                msg = await self._text(db, "my.not_registered", nickname=nickname)
                await db.commit()
                yield event.plain_result(msg)
                return

            rows = await db.execute_fetchall(
                """
                SELECT code, reward_json, expire_at, max_uses, used_count, enabled
                FROM redeem_codes
                WHERE code = ?
                """,
                (code,)
            )

            if not rows:
                await db.commit()
                yield event.plain_result("兑换码不存在，牛牛疑惑地歪了歪头。")
                return

            _, reward_json, expire_at, max_uses, used_count, enabled = rows[0]

            if not enabled:
                await db.commit()
                yield event.plain_result("这个兑换码已经被封印啦。")
                return

            if int(expire_at) < now_ts():
                await db.commit()
                yield event.plain_result("这个兑换码已经过期啦。")
                return

            if int(used_count) >= int(max_uses):
                await db.commit()
                yield event.plain_result("这个兑换码已经被领完啦，下次手速快一点。")
                return

            used = await db.execute_fetchall(
                """
                SELECT 1 FROM redeem_logs
                WHERE code = ? AND group_id = ? AND user_id = ?
                """,
                (code, group_id, user_id)
            )
            if used:
                await db.commit()
                yield event.plain_result("你已经使用过这个兑换码啦，不能重复领取哦。")
                return

            reward = json.loads(reward_json)
            desc = await self._apply_reward(db, group_id, user_id, reward)

            await db.execute(
                "UPDATE redeem_codes SET used_count = used_count + 1 WHERE code = ?",
                (code,)
            )
            await db.execute(
                """
                INSERT INTO redeem_logs(code, group_id, user_id, used_at)
                VALUES (?, ?, ?, ?)
                """,
                (code, group_id, user_id, now_ts())
            )

            await self._recalc_charm(db, group_id, user_id)
            await db.commit()

        yield event.plain_result(f"{nickname}，兑换成功！获得：{desc}")

    async def _apply_reward(
        self,
        db: aiosqlite.Connection,
        group_id: str,
        user_id: str,
        reward: Dict[str, Any]
    ) -> str:
        desc = []

        attrs = reward.get("attrs", {})
        if attrs:
            length = float(attrs.get("length", 0))
            girth = float(attrs.get("girth", 0))
            hardness = float(attrs.get("hardness", 0))
            energy = int(attrs.get("energy", 0))

            await db.execute(
                """
                UPDATE users
                SET length = length + ?,
                    girth = girth + ?,
                    hardness = hardness + ?,
                    energy = energy + ?,
                    updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (length, girth, hardness, energy, now_ts(), group_id, user_id)
            )

            if length:
                desc.append(f"长度 +{fmt(length)}")
            if girth:
                desc.append(f"粗度 +{fmt(girth)}")
            if hardness:
                desc.append(f"硬度 +{fmt(hardness)}")
            if energy:
                desc.append(f"精力 +{energy}")

        items = reward.get("items", [])
        for item_id in items:
            rows = await db.execute_fetchall(
                "SELECT item_type, name FROM shop_items WHERE item_id = ?",
                (item_id,)
            )
            if not rows:
                continue

            item_type, name = rows[0]
            await db.execute(
                """
                INSERT INTO inventory(group_id, user_id, item_id, item_type, count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(group_id, user_id, item_id)
                DO UPDATE SET count = count + 1
                """,
                (group_id, user_id, item_id, item_type)
            )
            desc.append(f"{name} x1")

        return "，".join(desc) if desc else "一阵神秘的好运"

    async def terminate(self):
        """插件卸载/停用时调用。"""
        logger.info("牛牛大乱斗插件已停止")