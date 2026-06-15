"""好友申请处理插件 — 策略处理器。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from .plugin import FriendRequestHandlerPlugin

LLM_PROMPT = (
    '你是一个QQ好友申请审核助手。根据以下申请人信息，判断对方是否为正常用户'
    '（非广告号、非小号、非恶意用户）。如果判断为安全用户请回复"通过"，'
    '否则回复"拒绝"，只需回复这两个词之一，不要附加其他内容。'
)

REMARK_PROMPT = (
    '你是一个QQ好友备注生成助手。根据以下申请人信息（昵称、个性签名、验证消息等），'
    '为对方生成一个简短自然的好友备注，不超过8个字，可以参考对方昵称或验证消息中体现的身份/称呼。'
    '只回复备注本身，不要附加任何解释、引号或标点。'
)


async def handle_manual(plugin: "FriendRequestHandlerPlugin", user_id: str, flag: str, comment: str) -> None:
    admin_qqs = plugin._normalized_admin_qqs()
    if not admin_qqs:
        plugin.ctx.logger.warning("收到好友申请但未配置 admin_qqs，无法推送")
        return

    plugin._pending[user_id] = {"flag": flag, "comment": comment, "nickname": ""}

    notice_text = await build_notice_text(plugin, user_id, "", comment)
    for admin_qq in admin_qqs:
        await plugin._send_private_notice(admin_qq, user_id, notice_text)
    plugin.ctx.logger.info(f"已推送好友申请: user_id={user_id} flag={flag}")


async def handle_llm_decision(plugin: "FriendRequestHandlerPlugin", user_id: str, flag: str, comment: str) -> None:
    info_text = await _build_applicant_info_text(plugin, user_id, comment)
    full_prompt = f"{LLM_PROMPT}\n\n申请人信息：\n{info_text}"

    ok, reply = await _call_llm(plugin, full_prompt)
    if not ok:
        plugin.ctx.logger.warning(f"LLM 调用失败，回退到手动模式: user_id={user_id}")
        await handle_manual(plugin, user_id, flag, comment)
        return

    approved = "通过" in reply
    await plugin._call_napcat(
        "set_friend_add_request",
        {"flag": flag, "approve": approved},
        raise_on_error=False,
    )

    if approved:
        plugin.ctx.logger.info(f"LLM 判定通过好友申请: user_id={user_id}")
        if plugin.config.strategy.auto_remark:
            remark = await _generate_remark(plugin, info_text)
            if not remark:
                remark = await _get_nickname(plugin, user_id)
            if remark:
                await asyncio.sleep(0.5)
                try:
                    await plugin._call_napcat(
                        "set_friend_remark",
                        {"user_id": int(user_id), "remark": remark},
                        raise_on_error=False,
                    )
                    plugin.ctx.logger.info(f"已为 {user_id} 设置备注: {remark}")
                except Exception as exc:
                    plugin.ctx.logger.warning(f"设置好友备注失败: {exc}")
        await _send_welcome(plugin, user_id)
    else:
        plugin.ctx.logger.info(f"LLM 判定拒绝好友申请: user_id={user_id}")


async def handle_auto_approve(plugin: "FriendRequestHandlerPlugin", user_id: str, flag: str, comment: str) -> None:
    await plugin._call_napcat(
        "set_friend_add_request",
        {"flag": flag, "approve": True},
        raise_on_error=False,
    )
    plugin.ctx.logger.info(f"自动通过好友申请: user_id={user_id}")

    admin_qqs = plugin._normalized_admin_qqs()
    if admin_qqs:
        info_text = await _build_info_only_text(plugin, user_id, comment)
        for admin_qq in admin_qqs:
            await plugin._send_private_notice(admin_qq, user_id, info_text)

    await _send_welcome(plugin, user_id)


# ---- 内部辅助 ----


async def _call_llm(plugin: "FriendRequestHandlerPlugin", prompt: str) -> tuple[bool, str]:
    model_name = (plugin.config.strategy.model_name or "").strip()
    try:
        kwargs: Dict[str, Any] = {"prompt": prompt, "temperature": 0.3, "max_tokens": 64}
        if model_name:
            kwargs["model"] = model_name
        result = await plugin.ctx.llm.generate(**kwargs)
    except Exception as e:
        plugin.ctx.logger.error(f"LLM 调用异常: {e}", exc_info=True)
        return False, ""
    if not isinstance(result, dict) or not result.get("success"):
        plugin.ctx.logger.warning(f"LLM 返回失败: {result}")
        return False, ""
    return True, str(result.get("response", "")).strip()


async def _generate_remark(plugin: "FriendRequestHandlerPlugin", info_text: str) -> str:
    full_prompt = f"{REMARK_PROMPT}\n\n申请人信息：\n{info_text}"
    ok, reply = await _call_llm(plugin, full_prompt)
    if not ok:
        return ""
    remark = reply.strip().strip('"').strip("'").strip("「」").strip()
    if len(remark) > 16:
        remark = remark[:16]
    return remark


async def _get_nickname(plugin: "FriendRequestHandlerPlugin", user_id: str) -> str:
    info = await plugin._call_napcat(
        "get_stranger_info",
        {"user_id": int(user_id) if user_id.isdigit() else user_id, "no_cache": True},
    )
    info_data = info.get("data", info) if isinstance(info, dict) else {}
    if not isinstance(info_data, dict):
        return ""
    return str(info_data.get("nickname") or "").strip()


async def _send_welcome(plugin: "FriendRequestHandlerPlugin", user_id: str) -> None:
    await asyncio.sleep(1.0)
    messages = [m.strip() for m in (plugin.config.welcome.messages or []) if m.strip()]
    for i, msg in enumerate(messages):
        try:
            await plugin._send_private_text(user_id, msg)
        except Exception as exc:
            plugin.ctx.logger.warning(f"发送欢迎语失败: {exc}")
        if i < len(messages) - 1:
            await asyncio.sleep(0.5)


def _format_sex(value: Any) -> str:
    text = str(value or "").strip().lower()
    return {"male": "男", "female": "女", "0": "男", "1": "女"}.get(text, "")


async def _build_applicant_info_text(plugin: "FriendRequestHandlerPlugin", user_id: str, comment: str) -> str:
    info = await plugin._call_napcat(
        "get_stranger_info",
        {"user_id": int(user_id) if user_id.isdigit() else user_id, "no_cache": True},
    )
    info_data = info.get("data", info) if isinstance(info, dict) else {}
    if not isinstance(info_data, dict):
        info_data = {}

    lines: List[str] = []

    def add(label: str, value: Any) -> None:
        text = "" if value is None else str(value).strip()
        if not text or text in {"0", "0.0", "unknown"}:
            return
        lines.append(f"{label}: {text}")

    add("QQ号", user_id)
    add("昵称", info_data.get("nickname"))
    add("性别", _format_sex(info_data.get("sex")))
    add("年龄", info_data.get("age"))
    add("等级", info_data.get("level") or info_data.get("qqLevel"))
    add("个性签名", info_data.get("long_nick") or info_data.get("longNick") or info_data.get("sign"))
    add("登录天数", info_data.get("login_days") or info_data.get("loginDays"))
    if comment:
        lines.append(f"验证消息: {comment}")
    return "\n".join(lines)


async def _build_info_only_text(plugin: "FriendRequestHandlerPlugin", user_id: str, comment: str) -> str:
    info = await plugin._call_napcat(
        "get_stranger_info",
        {"user_id": int(user_id) if user_id.isdigit() else user_id, "no_cache": True},
    )
    info_data = info.get("data", info) if isinstance(info, dict) else {}
    if not isinstance(info_data, dict):
        info_data = {}

    lines: List[str] = ["✅ 已自动通过好友申请"]

    def add(label: str, value: Any) -> None:
        text = "" if value is None else str(value).strip()
        if not text or text in {"0", "0.0", "unknown"}:
            return
        lines.append(f"{label}: {text}")

    add("QQ号", user_id)
    add("昵称", info_data.get("nickname"))
    add("性别", _format_sex(info_data.get("sex")))
    add("年龄", info_data.get("age"))
    add("等级", info_data.get("level") or info_data.get("qqLevel"))
    add("个性签名", info_data.get("long_nick") or info_data.get("longNick") or info_data.get("sign"))
    if comment:
        lines.append(f"验证消息: {comment}")
    return "\n".join(lines)


async def build_notice_text(plugin: "FriendRequestHandlerPlugin", user_id: str, fallback_nickname: str, comment: str) -> str:
    info = await plugin._call_napcat(
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
    add("性别", _format_sex(info_data.get("sex")))
    add("年龄", info_data.get("age"))
    add("等级", info_data.get("level") or info_data.get("qqLevel"))
    add("生日", _format_birthday(info_data))
    add("所在地", _format_location(info_data))
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
    lines.append(f"通过申请请发送：/同意 {user_id} [备注]")
    lines.append(f"拒绝申请请发送：/拒绝 {user_id}")
    return "\n".join(lines)


def _format_birthday(info: Dict[str, Any]) -> str:
    year = info.get("birthday_year") or info.get("birthdayYear") or info.get("year")
    month = info.get("birthday_month") or info.get("birthdayMonth") or info.get("month")
    day = info.get("birthday_day") or info.get("birthdayDay") or info.get("day")
    parts = [str(p).strip() for p in (year, month, day) if p not in (None, "", 0, "0")]
    return "-".join(parts)


def _format_location(info: Dict[str, Any]) -> str:
    parts = [
        str(info.get(key) or "").strip()
        for key in ("country", "province", "city", "area")
    ]
    return " ".join([p for p in parts if p and p.lower() != "unknown"])