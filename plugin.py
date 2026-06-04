"""好友申请处理插件。

功能：
1. 在本地启动一个 HTTP 服务器接收 NapCat 推送的事件上报。
2. 收到 ``post_type=request`` 且 ``request_type=friend`` 的事件时，向所有配置的管理员
   QQ 私聊推送申请人 QQ 资料卡。
3. 管理员发送 ``/同意 <QQ号>`` 或 ``/拒绝 <QQ号>`` 即可处理申请；通过后自动给新好友
   发送配置的欢迎语。
4. 启动后做一次补漏（轮询一次 NapCat 系统消息），避免插件未运行期间漏掉申请。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from hashlib import sha1
from typing import Any, ClassVar, Dict, List, Optional

from aiohttp import web

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    Field,
    MaiBotPlugin,
    PluginConfigBase,
)


# ---------------- 配置模型 ----------------


class PluginSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件设置"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=True,
        description="是否启用本插件。",
        json_schema_extra={"label": "启用插件", "order": 0},
    )
    config_version: str = Field(
        default="1.0.0",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本", "order": 99},
    )


class AdminSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "管理员"
    __ui_order__: ClassVar[int] = 1

    admin_qqs: List[str] = Field(
        default_factory=list,
        description="管理员 QQ 号列表；新好友申请会推送到这些 QQ，且只有他们能用 /同意 /拒绝。",
        json_schema_extra={"label": "管理员 QQ", "order": 0, "placeholder": "请输入 QQ 号"},
    )


class WebhookSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "Webhook"
    __ui_order__: ClassVar[int] = 2

    host: str = Field(
        default="127.0.0.1",
        description="监听 NapCat HTTP 上报的本地地址。",
        json_schema_extra={"label": "监听地址", "order": 0, "placeholder": "127.0.0.1"},
    )
    port: int = Field(
        default=18080, ge=1, le=65535,
        description="监听端口，需要和 NapCat HTTP 客户端配置中的端口一致。",
        json_schema_extra={"label": "监听端口", "order": 1, "step": 1},
    )
    path: str = Field(
        default="/maibot/friend_request",
        description="HTTP 路径，NapCat 的 URL 应填成 http://host:port/path。",
        json_schema_extra={"label": "HTTP 路径", "order": 2, "placeholder": "/maibot/friend_request"},
    )
    secret: str = Field(
        default="",
        description="可选 secret，对应 NapCat 「HTTP 客户端」的 token，留空则不校验。",
        json_schema_extra={"label": "Secret", "order": 3, "input_type": "password"},
    )


class StartupSweepSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "启动补漏"
    __ui_order__: ClassVar[int] = 3

    enabled: bool = Field(
        default=True,
        description="启动时拉一次 NapCat 系统消息，补上插件未运行期间漏掉的申请。",
        json_schema_extra={"label": "启用启动补漏", "order": 0},
    )
    delay_sec: int = Field(
        default=10, ge=0,
        description="启动后等待多少秒再做补漏，给适配器留时间连接 NapCat。",
        json_schema_extra={"label": "启动延迟（秒）", "order": 1, "step": 1},
    )
    fetch_count: int = Field(
        default=20, ge=1, le=200,
        description="单次最多拉取的申请数量。",
        json_schema_extra={"label": "拉取数量", "order": 2, "step": 1},
    )


class WelcomeSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "欢迎语"
    __ui_order__: ClassVar[int] = 4

    message: str = Field(
        default="你好呀新朋友，欢迎认识我！",
        description="通过好友申请后自动私聊给新好友的内容。",
        json_schema_extra={"label": "欢迎语", "order": 0, "placeholder": "请输入欢迎语"},
    )
    remark: str = Field(
        default="",
        description="通过申请时给新好友设置的备注，留空则不设置。",
        json_schema_extra={"label": "好友备注", "order": 1, "placeholder": "可留空"},
    )


class FriendRequestHandlerConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    admin: AdminSection = Field(default_factory=AdminSection)
    webhook: WebhookSection = Field(default_factory=WebhookSection)
    startup_sweep: StartupSweepSection = Field(default_factory=StartupSweepSection)
    welcome: WelcomeSection = Field(default_factory=WelcomeSection)


# ---------------- 插件主体 ----------------


class FriendRequestHandlerPlugin(MaiBotPlugin):
    config_model = FriendRequestHandlerConfig

    _runner: Optional[web.AppRunner]
    _site: Optional[web.BaseSite]
    _sweep_task: Optional[asyncio.Task]
    # user_id(str) -> {"flag": str, "comment": str, "nickname": str}
    _pending: Dict[str, Dict[str, Any]]
    # 已经推送过的 flag，避免 NapCat 重复推送或补漏重复打扰
    _notified_flags: set
    _data_path: str

    async def on_load(self) -> None:
        self._runner = None
        self._site = None
        self._sweep_task = None
        self._pending = {}
        self._notified_flags = set()

        data_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(data_dir, exist_ok=True)
        self._data_path = os.path.join(data_dir, "state.json")
        self._load_state()

        if self.config.plugin.enabled:
            await self._start_webhook()
            self._schedule_startup_sweep()
        self.ctx.logger.info("好友申请处理插件已加载")

    async def on_unload(self) -> None:
        await self._cancel_sweep()
        await self._stop_webhook()
        self._save_state()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        del config_data
        del version

        await self._cancel_sweep()
        await self._stop_webhook()
        if self.config.plugin.enabled:
            await self._start_webhook()
            self._schedule_startup_sweep()

    # ---------------- Webhook 服务 ----------------

    async def _start_webhook(self) -> None:
        webhook = self.config.webhook
        path = webhook.path if webhook.path.startswith("/") else f"/{webhook.path}"

        app = web.Application()
        app.router.add_post(path, self._handle_webhook)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        try:
            self._site = web.TCPSite(self._runner, webhook.host, int(webhook.port))
            await self._site.start()
        except OSError as exc:
            self.ctx.logger.error(
                f"好友申请 webhook 监听失败 host={webhook.host} port={webhook.port}: {exc}"
            )
            await self._stop_webhook()
            return

        self.ctx.logger.info(
            f"好友申请 webhook 已监听: http://{webhook.host}:{webhook.port}{path}"
        )

    async def _stop_webhook(self) -> None:
        site = self._site
        runner = self._runner
        self._site = None
        self._runner = None
        try:
            if site is not None:
                await site.stop()
        except Exception as exc:
            self.ctx.logger.warning(f"停止 webhook 监听失败: {exc}")
        try:
            if runner is not None:
                await runner.cleanup()
        except Exception as exc:
            self.ctx.logger.warning(f"清理 webhook 资源失败: {exc}")

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        raw = await request.read()
        if not self._verify_signature(request, raw):
            return web.Response(status=401, text="invalid signature")

        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return web.Response(status=400, text="invalid json")

        if not isinstance(payload, dict):
            return web.Response(status=400, text="invalid payload")

        post_type = str(payload.get("post_type") or "").strip()
        request_type = str(payload.get("request_type") or "").strip()
        if post_type == "request" and request_type == "friend":
            asyncio.create_task(self._on_friend_request(payload))
        return web.Response(text="ok")

    def _verify_signature(self, request: web.Request, raw: bytes) -> bool:
        secret = (self.config.webhook.secret or "").strip()
        if not secret:
            return True
        signature = request.headers.get("X-Signature", "")
        if not signature.startswith("sha1="):
            return False
        expected = "sha1=" + hmac.new(secret.encode("utf-8"), raw, sha1).hexdigest()
        return hmac.compare_digest(signature, expected)

    async def _on_friend_request(self, payload: Dict[str, Any]) -> None:
        try:
            user_id = str(payload.get("user_id") or "").strip()
            flag = str(payload.get("flag") or "").strip()
            comment = str(payload.get("comment") or "").strip()
            if not user_id or not flag:
                return

            admin_qqs = self._normalized_admin_qqs()
            if not admin_qqs:
                self.ctx.logger.warning("收到好友申请但未配置 admin_qqs，无法推送")
                return

            self._pending[user_id] = {"flag": flag, "comment": comment, "nickname": ""}
            if flag in self._notified_flags:
                return
            self._notified_flags.add(flag)
            self._save_state()

            notice_text = await self._build_notice_text(user_id, "", comment)
            for admin_qq in admin_qqs:
                await self._send_private_text(admin_qq, notice_text)
            self.ctx.logger.info(f"已推送好友申请: user_id={user_id} flag={flag}")
        except Exception as exc:
            self.ctx.logger.warning(f"处理好友申请失败: {exc}")

    # ---------------- 启动补漏 ----------------

    def _schedule_startup_sweep(self) -> None:
        if not self.config.startup_sweep.enabled:
            return
        self._sweep_task = asyncio.create_task(self._startup_sweep(), name="friend_request_handler.sweep")

    async def _cancel_sweep(self) -> None:
        task = self._sweep_task
        self._sweep_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _startup_sweep(self) -> None:
        try:
            await asyncio.sleep(max(0, int(self.config.startup_sweep.delay_sec)))
            response = await self._call_napcat(
                "get_friend_system_msg",
                {"count": int(self.config.startup_sweep.fetch_count)},
            )
            if response is None:
                return
            invitations = self._extract_invitations(response)
            admin_qqs = self._normalized_admin_qqs()
            if not invitations or not admin_qqs:
                return
            for invitation in invitations:
                user_id = self._extract_user_id(invitation)
                flag = self._extract_flag(invitation)
                if not user_id or not flag:
                    continue
                comment = str(
                    invitation.get("comment")
                    or invitation.get("reason")
                    or invitation.get("message")
                    or ""
                ).strip()
                nickname = str(invitation.get("nick") or invitation.get("nickname") or "").strip()
                self._pending[user_id] = {"flag": flag, "comment": comment, "nickname": nickname}
                if flag in self._notified_flags:
                    continue
                self._notified_flags.add(flag)
                notice_text = await self._build_notice_text(user_id, nickname, comment)
                for admin_qq in admin_qqs:
                    await self._send_private_text(admin_qq, notice_text)
            self._save_state()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning(f"启动补漏失败: {exc}")

    # ---------------- 资料组装 ----------------

    @staticmethod
    def _extract_invitations(response: Any) -> List[Dict[str, Any]]:
        if isinstance(response, dict):
            data = response.get("data", response)
        else:
            data = response

        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []

        for key in ("invitationList", "InvitationList", "InvitedRequest", "join_list", "list"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

        if "request_id" in data or "flag" in data or "user_id" in data:
            return [data]
        return []

    @staticmethod
    def _extract_user_id(invitation: Dict[str, Any]) -> str:
        for key in ("user_id", "userUid", "uin", "requesterUin", "from_uin", "fromUin"):
            value = invitation.get(key)
            if value not in (None, "", 0):
                return str(value).strip()
        return ""

    @staticmethod
    def _extract_flag(invitation: Dict[str, Any]) -> str:
        for key in ("flag", "request_id", "requestId", "msg_seq", "msgSeq", "seq"):
            value = invitation.get(key)
            if value not in (None, "", 0):
                return str(value).strip()
        return ""

    async def _build_notice_text(self, user_id: str, fallback_nickname: str, comment: str) -> str:
        info = await self._call_napcat(
            "get_stranger_info",
            {"user_id": int(user_id) if user_id.isdigit() else user_id, "no_cache": True},
        )
        info_data = info.get("data", info) if isinstance(info, dict) else info
        if not isinstance(info_data, dict):
            info_data = {}

        lines: List[str] = ["📩 收到新的好友申请"]

        def add(label: str, value: Any) -> None:
            text = "" if value is None else str(value).strip()
            if not text or text in {"0", "0.0", "unknown"}:
                return
            lines.append(f"{label}: {text}")

        nickname = str(info_data.get("nickname") or fallback_nickname or "").strip()
        add("QQ号", user_id)
        add("昵称", nickname)
        add("性别", self._format_sex(info_data.get("sex")))
        add("年龄", info_data.get("age"))
        add("等级", info_data.get("level") or info_data.get("qqLevel"))
        add("生日", self._format_birthday(info_data))
        add("所在地", self._format_location(info_data))
        add("国家", info_data.get("country"))
        add("学校", info_data.get("school") or info_data.get("eduInfo"))
        add("个性签名", info_data.get("long_nick") or info_data.get("longNick") or info_data.get("sign"))
        add("邮箱", info_data.get("email"))
        add("电话", info_data.get("phoneNum") or info_data.get("phone"))
        add("vip等级", info_data.get("vip_level") or info_data.get("vipLevel"))
        add("登录天数", info_data.get("login_days") or info_data.get("loginDays"))
        if comment:
            lines.append(f"验证消息: {comment}")

        lines.append("")
        lines.append(f"通过申请请发送：/同意 {user_id}")
        lines.append(f"拒绝申请请发送：/拒绝 {user_id}")
        return "\n".join(lines)

    @staticmethod
    def _format_sex(value: Any) -> str:
        text = str(value or "").strip().lower()
        return {"male": "男", "female": "女", "0": "男", "1": "女"}.get(text, "")

    @staticmethod
    def _format_birthday(info: Dict[str, Any]) -> str:
        year = info.get("birthday_year") or info.get("birthdayYear") or info.get("year")
        month = info.get("birthday_month") or info.get("birthdayMonth") or info.get("month")
        day = info.get("birthday_day") or info.get("birthdayDay") or info.get("day")
        parts = [str(p).strip() for p in (year, month, day) if p not in (None, "", 0, "0")]
        return "-".join(parts)

    @staticmethod
    def _format_location(info: Dict[str, Any]) -> str:
        parts = [
            str(info.get(key) or "").strip()
            for key in ("country", "province", "city", "area")
        ]
        return " ".join([p for p in parts if p and p.lower() != "unknown"])

    # ---------------- 命令 ----------------

    @Command(
        "approve_friend",
        description="管理员同意指定 QQ 的好友申请",
        pattern=r"^/同意\s+(?P<target_qq>\d+)\s*$",
    )
    async def handle_approve(self, stream_id: str = "", **kwargs: Any) -> tuple:
        return await self._handle_decision(approve=True, stream_id=stream_id, **kwargs)

    @Command(
        "reject_friend",
        description="管理员拒绝指定 QQ 的好友申请",
        pattern=r"^/拒绝\s+(?P<target_qq>\d+)\s*$",
    )
    async def handle_reject(self, stream_id: str = "", **kwargs: Any) -> tuple:
        return await self._handle_decision(approve=False, stream_id=stream_id, **kwargs)

    async def _handle_decision(self, approve: bool, stream_id: str, **kwargs: Any) -> tuple:
        sender_qq = self._extract_sender_qq(kwargs)
        if sender_qq is None or sender_qq not in self._normalized_admin_qqs():
            return False, None, False

        matched_groups = kwargs.get("matched_groups") or {}
        target_qq = str(matched_groups.get("target_qq") or "").strip()
        if not target_qq:
            return False, "用法：/同意 <QQ号> 或 /拒绝 <QQ号>", True

        record = self._pending.get(target_qq)
        if record is None:
            await self._reply(
                stream_id,
                f"未找到 QQ {target_qq} 的好友申请，可能已经处理过或 webhook 未收到。",
            )
            return True, None, True

        flag = record.get("flag", "")
        params: Dict[str, Any] = {"flag": flag, "approve": bool(approve)}
        if approve:
            remark = (self.config.welcome.remark or "").strip()
            if remark:
                params["remark"] = remark

        try:
            await self._call_napcat("set_friend_add_request", params, raise_on_error=True)
        except Exception as exc:
            await self._reply(stream_id, f"处理失败：{exc}")
            return False, None, True

        self._pending.pop(target_qq, None)
        self._notified_flags.discard(flag)
        self._save_state()

        if approve:
            await asyncio.sleep(1.0)
            welcome = (self.config.welcome.message or "").strip()
            if welcome:
                try:
                    await self._send_private_text(target_qq, welcome)
                except Exception as exc:
                    self.ctx.logger.warning(f"发送欢迎语失败: {exc}")
            await self._reply(stream_id, f"已同意 QQ {target_qq} 的好友申请。")
        else:
            await self._reply(stream_id, f"已拒绝 QQ {target_qq} 的好友申请。")
        return True, None, True

    # ---------------- 辅助 ----------------

    def _normalized_admin_qqs(self) -> List[str]:
        return [str(qq).strip() for qq in self.config.admin.admin_qqs if str(qq).strip()]

    @staticmethod
    def _extract_sender_qq(kwargs: Dict[str, Any]) -> Optional[str]:
        base_info = kwargs.get("message_base_info") or {}
        user_info = base_info.get("user_info") if isinstance(base_info, dict) else {}
        sender_qq = (
            kwargs.get("user_id")
            or (user_info.get("user_id") if isinstance(user_info, dict) else None)
        )
        if sender_qq in (None, ""):
            return None
        return str(sender_qq).strip()

    async def _reply(self, stream_id: str, text: str) -> None:
        if not stream_id or not text:
            return
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:
            self.ctx.logger.warning(f"回复消息失败: {exc}")

    async def _send_private_text(self, user_id: str, text: str) -> None:
        if not user_id or not text:
            return
        await self._call_napcat(
            "send_private_msg",
            {
                "user_id": int(user_id) if str(user_id).isdigit() else user_id,
                "message": [{"type": "text", "data": {"text": text}}],
            },
            raise_on_error=False,
        )

    async def _call_napcat(
        self,
        action_name: str,
        params: Dict[str, Any],
        raise_on_error: bool = False,
    ) -> Any:
        try:
            response = await self.ctx.api.call(
                "adapter.napcat.action.call",
                action_name=action_name,
                params=params,
            )
        except Exception as exc:
            if raise_on_error:
                raise
            self.ctx.logger.debug(f"调用 NapCat 动作 {action_name} 失败: {exc}")
            return None

        if isinstance(response, dict) and str(response.get("status", "")).lower() not in {"", "ok"}:
            error_text = str(response.get("wording") or response.get("message") or response.get("retcode"))
            if raise_on_error:
                raise RuntimeError(f"NapCat 动作 {action_name} 返回错误: {error_text}")
            self.ctx.logger.debug(f"NapCat 动作 {action_name} 返回非 ok 状态: {error_text}")
        return response

    # ---------------- 持久化 ----------------

    def _load_state(self) -> None:
        try:
            with open(self._data_path, "r", encoding="utf-8") as fp:
                payload = json.load(fp)
            pending = payload.get("pending")
            if isinstance(pending, dict):
                self._pending = {str(k): dict(v) for k, v in pending.items() if isinstance(v, dict)}
            notified = payload.get("notified_flags")
            if isinstance(notified, list):
                self._notified_flags = {str(item) for item in notified}
        except FileNotFoundError:
            return
        except Exception as exc:
            self.ctx.logger.warning(f"读取好友申请状态失败: {exc}")

    def _save_state(self) -> None:
        payload = {
            "pending": self._pending,
            "notified_flags": sorted(self._notified_flags),
        }
        try:
            with open(self._data_path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.ctx.logger.warning(f"保存好友申请状态失败: {exc}")


def create_plugin() -> FriendRequestHandlerPlugin:
    return FriendRequestHandlerPlugin()
