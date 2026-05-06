import os
import json
import time
from typing import Any, Dict, Optional

import aiosqlite
from aiohttp import web

from astrbot.api import logger


class NiuNiuAdminServer:
    """
    牛牛管理后台。

    功能：
    1. 商品 CRUD
    2. 兑换码 CRUD
    3. 文案模板 CRUD
    4. 公告管理
    5. Token 鉴权

    安全说明：
    - 所有 /api/* 接口均需要 Authorization: Bearer <token>
    - 静态首页也需要 token 查询参数或前端本地填写 token 后访问 API
    """

    def __init__(
        self,
        db_path: str,
        static_dir: str,
        host: str,
        port: int,
        token: str
    ):
        self.db_path = db_path
        self.static_dir = static_dir
        self.host = host
        self.port = int(port)
        self.token = token
        self.app: Optional[web.Application] = None
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

    async def start(self):
        self.app = web.Application(middlewares=[self.auth_middleware])
        self._setup_routes()

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()

        logger.info(f"牛牛管理后台已启动：http://{self.host}:{self.port}")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            logger.info("牛牛管理后台已停止")

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler):
        """
        简单 Token 鉴权。

        支持：
        1. Authorization: Bearer token
        2. ?token=xxx
        """
        path = request.path

        if path == "/":
            return await handler(request)

        if path.startswith("/static/"):
            return await handler(request)

        if path.startswith("/api/"):
            auth = request.headers.get("Authorization", "")
            token = ""

            if auth.startswith("Bearer "):
                token = auth.replace("Bearer ", "", 1).strip()
            else:
                token = request.query.get("token", "").strip()

            if not self.token or self.token == "please-change-me":
                return web.json_response(
                    {
                        "ok": False,
                        "error": "后台 Token 未修改。请先在插件配置中设置 admin_token。"
                    },
                    status=403
                )

            if token != self.token:
                return web.json_response(
                    {
                        "ok": False,
                        "error": "Unauthorized"
                    },
                    status=401
                )

        return await handler(request)

    def _setup_routes(self):
        assert self.app is not None

        self.app.router.add_get("/", self.index)

        # 商品
        self.app.router.add_get("/api/items", self.list_items)
        self.app.router.add_post("/api/items", self.upsert_item)
        self.app.router.add_delete("/api/items/{item_id}", self.delete_item)

        # 兑换码
        self.app.router.add_get("/api/redeem-codes", self.list_redeem_codes)
        self.app.router.add_post("/api/redeem-codes", self.upsert_redeem_code)
        self.app.router.add_delete("/api/redeem-codes/{code}", self.delete_redeem_code)

        # 文案模板
        self.app.router.add_get("/api/text-templates", self.list_text_templates)
        self.app.router.add_post("/api/text-templates", self.upsert_text_template)
        self.app.router.add_delete("/api/text-templates/{key}", self.delete_text_template)

        # 公告
        self.app.router.add_get("/api/announcements", self.list_announcements)
        self.app.router.add_post("/api/announcements", self.create_announcement)
        self.app.router.add_delete("/api/announcements/{announcement_id}", self.delete_announcement)

        # 用户数据只读，方便运营观察
        self.app.router.add_get("/api/users", self.list_users)

    async def index(self, request: web.Request):
        path = os.path.join(self.static_dir, "index.html")
        if not os.path.exists(path):
            return web.Response(
                text="static/index.html not found",
                content_type="text/plain"
            )
        return web.FileResponse(path)

    async def _json(self, request: web.Request) -> Dict[str, Any]:
        try:
            return await request.json()
        except Exception:
            return {}

    def _ok(self, data: Any = None):
        return web.json_response(
            {
                "ok": True,
                "data": data
            },
            dumps=lambda x: json.dumps(x, ensure_ascii=False)
        )

    def _fail(self, msg: str, status: int = 400):
        return web.json_response(
            {
                "ok": False,
                "error": msg
            },
            status=status,
            dumps=lambda x: json.dumps(x, ensure_ascii=False)
        )

    # ========================
    # 商品管理
    # ========================
    async def list_items(self, request: web.Request):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """
                SELECT item_id, name, item_type, price, effect_json,
                    description, use_mode, enabled, created_at
                FROM shop_items
                ORDER BY created_at DESC
                """
            )
            data = [dict(row) for row in rows]
        return self._ok(data)

    async def upsert_item(self, request: web.Request):
        body = await self._json(request)

        item_id = str(body.get("item_id", "")).strip()
        name = str(body.get("name", "")).strip()
        item_type = str(body.get("item_type", "")).strip()
        price = int(body.get("price", 0))
        description = str(body.get("description", "")).strip()
        enabled = 1 if body.get("enabled", True) else 0
        effect = body.get("effect", {})
        use_mode = str(body.get("use_mode", "instant")).strip()

        if not item_id:
            return self._fail("item_id 不能为空")
        if item_type not in {"prop", "accessory"}:
            return self._fail("item_type 只能是 prop 或 accessory")
        if price < 0:
            return self._fail("price 不能小于 0")
        if use_mode not in {"instant", "inventory"}:
            return self._fail("use_mode 只能是 instant 或 inventory")

        try:
            effect_json = json.dumps(effect, ensure_ascii=False)
        except Exception:
            return self._fail("effect 必须是合法 JSON")

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO shop_items
                (item_id, name, item_type, price, effect_json, description, use_mode, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id)
                DO UPDATE SET
                    name = excluded.name,
                    item_type = excluded.item_type,
                    price = excluded.price,
                    effect_json = excluded.effect_json,
                    description = excluded.description,
                    use_mode = excluded.use_mode,
                    enabled = excluded.enabled
                """,
                (
                    item_id,
                    name,
                    item_type,
                    price,
                    effect_json,
                    description,
                    use_mode,
                    enabled,
                    int(time.time())
                )
            )
            await db.commit()

        return self._ok({"item_id": item_id})

    async def delete_item(self, request: web.Request):
        item_id = request.match_info["item_id"]

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE shop_items SET enabled = 0 WHERE item_id = ?",
                (item_id,)
            )
            await db.commit()

        return self._ok({"item_id": item_id})

    # ========================
    # 兑换码管理
    # ========================
    async def list_redeem_codes(self, request: web.Request):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """
                SELECT code, reward_json, expire_at, max_uses,
                       used_count, enabled, created_at
                FROM redeem_codes
                ORDER BY created_at DESC
                """
            )
            data = [dict(row) for row in rows]
        return self._ok(data)

    async def upsert_redeem_code(self, request: web.Request):
        body = await self._json(request)

        code = str(body.get("code", "")).strip()
        reward = body.get("reward", {})
        expire_at = int(body.get("expire_at", 0))
        max_uses = int(body.get("max_uses", 1))
        enabled = 1 if body.get("enabled", True) else 0

        if not code:
            return self._fail("code 不能为空")
        if expire_at <= int(time.time()):
            return self._fail("expire_at 必须是未来时间戳")
        if max_uses <= 0:
            return self._fail("max_uses 必须大于 0")

        try:
            reward_json = json.dumps(reward, ensure_ascii=False)
        except Exception:
            return self._fail("reward 必须是合法 JSON")

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO redeem_codes
                (code, reward_json, expire_at, max_uses, used_count, enabled, created_at)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(code)
                DO UPDATE SET
                    reward_json = excluded.reward_json,
                    expire_at = excluded.expire_at,
                    max_uses = excluded.max_uses,
                    enabled = excluded.enabled
                """,
                (
                    code,
                    reward_json,
                    expire_at,
                    max_uses,
                    enabled,
                    int(time.time())
                )
            )
            await db.commit()

        return self._ok({"code": code})

    async def delete_redeem_code(self, request: web.Request):
        code = request.match_info["code"]

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE redeem_codes SET enabled = 0 WHERE code = ?",
                (code,)
            )
            await db.commit()

        return self._ok({"code": code})

    # ========================
    # 文案模板管理
    # ========================
    async def list_text_templates(self, request: web.Request):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """
                SELECT key, value_json
                FROM text_templates
                ORDER BY key ASC
                """
            )
            data = [dict(row) for row in rows]
        return self._ok(data)

    async def upsert_text_template(self, request: web.Request):
        body = await self._json(request)

        key = str(body.get("key", "")).strip()
        value = body.get("value", [])

        if not key:
            return self._fail("key 不能为空")
        if not isinstance(value, list):
            return self._fail("value 必须是字符串数组")

        value_json = json.dumps(value, ensure_ascii=False)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO text_templates(key, value_json)
                VALUES (?, ?)
                ON CONFLICT(key)
                DO UPDATE SET value_json = excluded.value_json
                """,
                (key, value_json)
            )
            await db.commit()

        return self._ok({"key": key})

    async def delete_text_template(self, request: web.Request):
        key = request.match_info["key"]

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "DELETE FROM text_templates WHERE key = ?",
                (key,)
            )
            await db.commit()

        return self._ok({"key": key})

    # ========================
    # 公告管理
    # ========================
    async def list_announcements(self, request: web.Request):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """
                SELECT id, content, created_by, created_at
                FROM announcements
                ORDER BY id DESC
                LIMIT 50
                """
            )
            data = [dict(row) for row in rows]
        return self._ok(data)

    async def create_announcement(self, request: web.Request):
        body = await self._json(request)

        content = str(body.get("content", "")).strip()
        created_by = str(body.get("created_by", "admin")).strip()

        if not content:
            return self._fail("content 不能为空")

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO announcements(content, created_by, created_at)
                VALUES (?, ?, ?)
                """,
                (content, created_by, int(time.time()))
            )
            await db.commit()

        return self._ok()

    async def delete_announcement(self, request: web.Request):
        announcement_id = int(request.match_info["announcement_id"])

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "DELETE FROM announcements WHERE id = ?",
                (announcement_id,)
            )
            await db.commit()

        return self._ok({"id": announcement_id})

    # ========================
    # 用户数据只读
    # ========================
    async def list_users(self, request: web.Request):
        group_id = request.query.get("group_id", "").strip()

        sql = """
            SELECT group_id, user_id, nickname, length, girth, hardness,
                   charm, energy, win_rate_buff, debuff_shield_until,
                   event_ignore_count, created_at, updated_at
            FROM users
        """
        params = []

        if group_id:
            sql += " WHERE group_id = ?"
            params.append(group_id)

        sql += " ORDER BY charm DESC LIMIT 200"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(sql, params)
            data = [dict(row) for row in rows]

        return self._ok(data)