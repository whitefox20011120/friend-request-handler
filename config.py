"""好友申请处理插件 — 配置模型。"""

from __future__ import annotations

from typing import ClassVar, List

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件开关"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=True,
        description="是否启用本插件。",
        json_schema_extra={
            "hint": "插件总开关，关闭后不再监听好友申请。",
            "label": "启用插件",
            "order": 0,
        },
    )
    config_version: str = Field(
        default="1.2.0",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本", "order": 99},
    )


class AdminSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "管理员"
    __ui_order__: ClassVar[int] = 1

    admin_qqs: List[str] = Field(
        default_factory=list,
        description="管理员 QQ 号列表。",
        json_schema_extra={
            "hint": "新好友申请会推送到这些 QQ，且只有他们能用 /同意 /拒绝 命令。",
            "label": "管理员 QQ",
            "order": 0,
            "placeholder": "请输入 QQ 号",
        },
    )


class WebhookSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "Webhook 监听"
    __ui_order__: ClassVar[int] = 2

    host: str = Field(
        default="127.0.0.1",
        json_schema_extra={
            "hint": "监听 NapCat HTTP 上报的本地地址，同机填 127.0.0.1 即可。",
            "label": "监听地址",
            "order": 0,
            "placeholder": "127.0.0.1",
        },
    )
    port: int = Field(
        default=18080, ge=1, le=65535,
        json_schema_extra={
            "hint": "监听端口，需与 NapCat HTTP 客户端配置中的端口一致。",
            "label": "监听端口",
            "order": 1,
            "step": 1,
        },
    )
    path: str = Field(
        default="/maibot/friend_request",
        json_schema_extra={
            "hint": "HTTP 路径，NapCat 的 URL 应填 http://host:port/path。",
            "label": "HTTP 路径",
            "order": 2,
            "placeholder": "/maibot/friend_request",
        },
    )
    secret: str = Field(
        default="",
        json_schema_extra={
            "hint": "可选，对应 NapCat HTTP 客户端的 token，留空则不校验签名。",
            "label": "Secret",
            "order": 3,
            "input_type": "password",
        },
    )


class StrategySection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "申请处理策略"
    __ui_order__: ClassVar[int] = 3

    mode: str = Field(
        default="manual",
        json_schema_extra={
            "hint": (
                "manual = 推送给管理员手动审批；"
                "llm = LLM 自动判定是否安全用户；"
                "auto_approve = 无条件自动通过（有风险）。"
            ),
            "label": "处理策略",
            "order": 0,
            "placeholder": "manual / llm / auto_approve",
        },
    )
    model_name: str = Field(
        default="replyer",
        json_schema_extra={
            "hint": "仅 llm 模式生效。留空使用系统默认模型。",
            "label": "LLM 模型名称",
            "order": 1,
            "placeholder": "replyer",
        },
    )
    auto_remark: bool = Field(
        default=True,
        json_schema_extra={
            "hint": "仅 llm 模式生效。LLM 判定通过后自动将对方昵称设为好友备注。",
            "label": "自动设置备注",
            "order": 2,
        },
    )


class WelcomeSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "欢迎语"
    __ui_order__: ClassVar[int] = 4

    messages: List[str] = Field(
        default_factory=lambda: ["你好呀新朋友，欢迎认识我！"],
        json_schema_extra={
            "hint": "通过好友申请后自动私聊发送的内容，按顺序逐条发送（换行用 \\n）。",
            "label": "欢迎语列表",
            "order": 0,
            "placeholder": "请输入欢迎语",
        },
    )


class NoticeSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "通知设置"
    __ui_order__: ClassVar[int] = 5

    send_avatar: bool = Field(
        default=True,
        json_schema_extra={
            "hint": "推送好友申请通知时，在文本上方附带申请方的 QQ 头像。",
            "label": "附带头像",
            "order": 0,
        },
    )
    avatar_size: int = Field(
        default=640, ge=40, le=640,
        json_schema_extra={
            "hint": "头像尺寸（像素），常用值: 100 / 140 / 640。",
            "label": "头像尺寸",
            "order": 1,
            "step": 1,
        },
    )


class FriendRequestHandlerConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    admin: AdminSection = Field(default_factory=AdminSection)
    webhook: WebhookSection = Field(default_factory=WebhookSection)
    strategy: StrategySection = Field(default_factory=StrategySection)
    welcome: WelcomeSection = Field(default_factory=WelcomeSection)
    notice: NoticeSection = Field(default_factory=NoticeSection)