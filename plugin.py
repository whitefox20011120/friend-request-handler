"""
好友申请处理插件。
by：白狐 & claude
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
from hashlib import sha1
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import web

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Command, MaiBotPlugin

from .config import FriendRequestHandlerConfig
from .handlers import handle_auto_approve, handle_llm_decision, handle_manual


class FriendRequestHandlerPlugin(MaiBotPlugin):
    config_model = FriendRequestHandlerConfig

    _runner: Optional[web.AppRunner]
    _site: Optional[web.BaseSite]
    _pending: Dict[str, Dict[str, Any]]
    _notified_flags: set
    _data_path: str

    async def on_load(self) -> None:
        self._runner = None
        self._site = None
        self._pending = {}
        self._notified_flags = set()

        data_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(data_dir, exist_ok=True)
        self._data_path = os.path.join(data_dir, "state.json")
        self._load_state()

        if self.config.plugin.enabled:
            await self._start_webhook()
        self.ctx.logger.info("好友申请处理插件已加载")

    async def on_unload(self) -> None:
        await self._stop_webhook()
        self._save_state()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        del config_data, version
        await self._stop_webhook()
        if self.config.plugin.enabled:
            await self._start_webhook()

    # ---- Webhook 服务 ----

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

        self.ctx.logger.info(f"好友申请 webhook 已监听: http://{webhook.host}:{webhook.port}{path}")

    async def _stop_webhook(self) -> None:
        site, runner = self._site, self._runner
        self._site = self._runner = None
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
        return web.json_response({})

    def _verify_signature(self, request: web.Request, raw: bytes) -> bool:
        secret = (self.config.webhook.secret or "").strip()
        if not secret:
            return True
        signature = request.headers.get("X-Signature", "")
        if not signature.startswith("sha1="):
            return False
        expected = "sha1=" + hmac.new(secret.encode("utf-8"), raw, sha1).hexdigest()
        return hmac.compare_digest(signature, expected)

    # ---- 申请分流 ----

    async def _on_friend_request(self, payload: Dict[str, Any]) -> None:
        try:
            user_id = str(payload.get("user_id") or "").strip()
            flag = str(payload.get("flag") or "").strip()
            comment = str(payload.get("comment") or "").strip()
            if not user_id or not flag:
                return
            if flag in self._notified_flags:
                return
            self._notified_flags.add(flag)

            mode = (self.config.strategy.mode or "manual").strip().lower()
            if mode == "llm":
                await handle_llm_decision(self, user_id, flag, comment)
            elif mode == "auto_approve":
                await handle_auto_approve(self, user_id, flag, comment)
            else:
                await handle_manual(self, user_id, flag, comment)

            self._save_state()
        except Exception as exc:
            self.ctx.logger.warning(f"处理好友申请失败: {exc}")

    # ---- 命令 ----

    @Command(
        "approve_friend",
        description="管理员同意指定 QQ 的好友申请，可附带备注",
        pattern=r"^/同意\s+(?P<target_qq>\d+)(?:\s+(?P<remark>.+?))?\s*$",
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

    # --- PLACEHOLDER_DECISION ---

    async def _handle_decision(self, approve: bool, stream_id: str, **kwargs: Any) -> tuple:
        if self._is_group_context(kwargs):
            return False, None, False
        sender_qq = self._extract_sender_qq(kwargs)
        if sender_qq is None or sender_qq not in self._normalized_admin_qqs():
            return False, None, False

        matched_groups = kwargs.get("matched_groups") or {}
        target_qq = str(matched_groups.get("target_qq") or "").strip()
        if not target_qq:
            return False, "用法：/同意 <QQ号> [备注] 或 /拒绝 <QQ号>", True

        record = self._pending.get(target_qq)
        if record is None:
            await self._reply(stream_id, f"未找到 QQ {target_qq} 的好友申请，可能已经处理过或 webhook 未收到。")
            return True, None, True

        flag = record.get("flag", "")
        remark = str(matched_groups.get("remark") or "").strip() if approve else ""

        params: Dict[str, Any] = {"flag": flag, "approve": bool(approve)}
        if approve and remark:
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
            if remark:
                await asyncio.sleep(0.5)
                try:
                    await self._call_napcat("set_friend_remark", {"user_id": int(target_qq), "remark": remark}, raise_on_error=False)
                except Exception as exc:
                    self.ctx.logger.warning(f"设置好友备注失败: {exc}")
            await asyncio.sleep(1.0)
            for msg in [m.strip() for m in (self.config.welcome.messages or []) if m.strip()]:
                try:
                    await self._send_private_text(target_qq, msg)
                except Exception as exc:
                    self.ctx.logger.warning(f"发送欢迎语失败: {exc}")
                await asyncio.sleep(0.5)
            remark_tip = f"（备注: {remark}）" if remark else ""
            await self._reply(stream_id, f"已同意 QQ {target_qq} 的好友申请。{remark_tip}")
        else:
            await self._reply(stream_id, f"已拒绝 QQ {target_qq} 的好友申请。")
        return True, None, True

    # ---- 工具方法 ----

    @staticmethod
    def _is_group_context(kwargs: Dict[str, Any]) -> bool:
        base_info = kwargs.get("message_base_info") or {}
        if isinstance(base_info, dict):
            if base_info.get("group_id") or base_info.get("group_info"):
                return True
        return bool(kwargs.get("group_id"))

    def _normalized_admin_qqs(self) -> List[str]:
        return [str(qq).strip() for qq in self.config.admin.admin_qqs if str(qq).strip()]

    @staticmethod
    def _extract_sender_qq(kwargs: Dict[str, Any]) -> Optional[str]:
        base_info = kwargs.get("message_base_info") or {}
        user_info = base_info.get("user_info") if isinstance(base_info, dict) else {}
        sender_qq = kwargs.get("user_id") or (user_info.get("user_id") if isinstance(user_info, dict) else None)
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
            {"user_id": int(user_id) if str(user_id).isdigit() else user_id, "message": [{"type": "text", "data": {"text": text}}]},
            raise_on_error=False,
        )

    async def _send_private_notice(self, admin_qq: str, applicant_qq: str, text: str) -> None:
        if not admin_qq or not text:
            return
        message: List[Dict[str, Any]] = []
        if self.config.notice.send_avatar and applicant_qq:
            size = int(self.config.notice.avatar_size or 640)
            avatar_b64 = await self._fetch_avatar_base64(applicant_qq, size=size)
            if avatar_b64:
                message.append({"type": "image", "data": {"file": f"base64://{avatar_b64}"}})
        message.append({"type": "text", "data": {"text": text}})
        await self._call_napcat(
            "send_private_msg",
            {"user_id": int(admin_qq) if str(admin_qq).isdigit() else admin_qq, "message": message},
            raise_on_error=False,
        )

    async def _fetch_avatar_base64(self, qq: str, size: int = 640, timeout_sec: int = 10) -> Optional[str]:
        urls = [
            f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s={size}",
            f"https://q.qlogo.cn/g?b=qq&nk={qq}&s={size}",
        ]
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for url in urls:
                    try:
                        async with session.get(url) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                if data:
                                    return base64.b64encode(data).decode("utf-8")
                    except Exception as exc:
                        self.ctx.logger.debug(f"头像下载失败 {url}: {exc}")
        except Exception as exc:
            self.ctx.logger.warning(f"头像下载会话错误: {exc}")
        return None

    async def _call_napcat(self, action_name: str, params: Dict[str, Any], raise_on_error: bool = False) -> Any:
        try:
            response = await self.ctx.api.call("adapter.napcat.action.call", action_name=action_name, params=params)
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

    # ---- 持久化 ----

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
        payload = {"pending": self._pending, "notified_flags": sorted(self._notified_flags)}
        try:
            with open(self._data_path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.ctx.logger.warning(f"保存好友申请状态失败: {exc}")


def create_plugin() -> FriendRequestHandlerPlugin:
    return FriendRequestHandlerPlugin()