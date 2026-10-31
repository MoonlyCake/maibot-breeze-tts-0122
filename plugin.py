"""Breeze TTS integration for MaiBot 0.12.2."""
import base64
from copy import deepcopy
import math
import re
import time
from pathlib import Path
from typing import List, Tuple, Type

from src.common.logger import get_logger
from src.chat.message_receive.chat_stream import get_chat_manager
from src.chat.utils.utils import is_bot_self
from src.plugin_system.apis.plugin_register_api import register_plugin
from src.plugin_system.base.base_action import BaseAction, ActionActivationType
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.component_types import ChatMode, ComponentInfo
from src.plugin_system.base.config_types import ConfigField, ConfigLayout, ConfigTab
from src.plugin_system import PythonDependency

from .breeze_client import BreezeError, synthesize
from .voice_guard import claim_request, reserve_usage, scope_allowed

logger = get_logger("breeze_tts_0122")
_GUARD_DB = Path(__file__).resolve().parent / "voice_guard_data" / "usage.sqlite3"


def _explicit_voice_request(text):
    """Only recognize a request to speak, not a casual mention of voice."""
    if not isinstance(text, str) or not text.strip():
        return False
    text = text.strip().lower()
    negation = re.compile(r"(?:不需要|不希望|没必要|不要|不用|别|不想|无需|拒绝|停止|禁止|关闭|取消).{0,12}(?:语音|声音|朗读|念|读|说)|(?:语音|朗读).{0,8}(?:不要|不用|别|关闭|取消)|(?:don't|dont|do not|no|never|stop|without).{0,20}(?:voice|aloud|speech|read|speak)")
    positive = re.compile(
        r"(?:用|以).{0,4}(?:语音|声音).{0,8}(?:回答|回复|讲|说|念|读|聊|解释|告诉)"
        r"|(?:发|来|给|想听|要听|想要|我要|想来|来段|来个|请发|发段).{0,8}(?:语音|声音)"
        r"|(?:语音|声音|朗读).{0,4}(?:回答|回复|朗读|读出来|念出来)"
        r"|(?:请|帮我|麻烦|给我).{0,6}朗读"
        r"|(?:读|念|说|讲).{0,8}(?:给我听|出来)"
        r"|(?:read|say|speak).{0,20}(?:aloud|voice)"
    )
    parts = re.split(r"[，。！？；,;!?\n]+", text)
    if any(negation.search(part) for part in parts):
        return False
    return any(positive.search(part) for part in parts)


def _fresh_voice_message(message, stream_id, platform):
    """Planner targets may come from old history; require a recent identified message."""
    if message is None or not _explicit_voice_request(getattr(message, "processed_plain_text", "")):
        return False
    if not str(getattr(message, "message_id", "") or ""):
        return False
    sender = str(getattr(getattr(message, "user_info", None), "user_id", "") or "").strip()
    if not sender:
        return False
    try:
        if is_bot_self(platform, sender):
            return False
    except Exception:
        return False
    try:
        timestamp = float(message.time)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(timestamp) or not -5 <= time.time() - timestamp <= 120:
        return False
    target_stream = str(getattr(message, "chat_id", "") or "")
    if not target_stream or target_stream != stream_id:
        return False
    # KEYWORD activates from the entire historical prompt. Only the latest
    # real inbound message may trigger this paid Action, not another member's
    # older message selected by the planner.
    try:
        latest = get_chat_manager().last_messages.get(stream_id)
        info = latest.message_info
        latest_id = str(info.message_id or "").strip()
        latest_sender = str(info.user_info.user_id or "").strip()
        latest_platform = str(info.platform or "").strip()
        return bool(latest_id == str(message.message_id).strip() and latest_sender == sender
                    and latest_platform == platform)
    except Exception:
        # Unknown live context is not a safe reason to spend or send voice.
        return False


class VoiceSender:
    def _scope(self):
        stream = getattr(self, "chat_stream", None)
        if stream is None:
            message = getattr(self, "message", None)
            stream = getattr(message, "chat_stream", None)
        if stream is None or not hasattr(stream, "group_info"):
            return None
        platform = str(getattr(stream, "platform", "") or "").strip()
        stream_id = str(getattr(stream, "stream_id", "") or "").strip()
        if not platform or not stream_id:
            return None
        group_info = stream.group_info
        is_group = group_info is not None
        group_id = str(getattr(group_info, "group_id", "") or "").strip() if is_group else None
        if is_group and not group_id:
            return None
        # Reject contradictory metadata instead of accidentally treating a
        # group as an allowed private chat when a stream is incomplete.
        incoming = getattr(self, "message", None)
        if incoming is not None:
            info = getattr(incoming, "message_info", None)
            if info is None or not hasattr(info, "group_info"):
                return None
            if str(getattr(info, "platform", "") or "").strip().lower() != platform.lower():
                return None
            incoming_group = info.group_info
            if (incoming_group is None) != (group_info is None):
                return None
            if incoming_group is not None and str(getattr(incoming_group, "group_id", "") or "").strip() != group_id:
                return None
        else:
            target = getattr(self, "action_message", None)
            target_chat = getattr(target, "chat_info", None)
            target_platform = str(getattr(target_chat, "platform", "") or "").strip()
            if target_platform and target_platform.lower() != platform.lower():
                return None
            target_group = getattr(target_chat, "group_info", None)
            if target_group is not None:
                target_id = str(getattr(target_group, "group_id", "") or "").strip()
                if not is_group or not target_id or target_id != group_id:
                    return None
        allowed = scope_allowed(is_group, group_id, self.get_config("guard.allowed_groups", []),
                                self.get_config("guard.allow_private", False), platform=platform)
        return (platform, stream_id, allowed)

    def _settings(self):
        fields = {
            "backend": "self_hosted", "api_url": "", "api_key": "",
            "voice_id": "", "model": "", "language_code": "", "instruction": "", "ref_audio": "",
            "ref_text": "", "cfg_scale": 1.0, "seed": -1, "timeout": 120,
        }
        return {key: self.get_config("breeze." + key, default) for key, default in fields.items()}

    async def _speak(self, text, request_id=None):
        if not isinstance(text, str) or not text.strip():
            return False, "请输入需要朗读的文字"
        text = text.strip()
        limit = self.get_config("general.max_text_length", 500)
        if not isinstance(limit, int) or limit < 1 or limit > 5000:
            return False, "请检查 max_text_length 配置"
        if len(text) > limit:
            return False, f"文字超过 {limit} 字，请缩短后重试"
        scope = self._scope()
        if scope is None or not scope[2]:
            return False, "当前聊天未启用语音"
        try:
            allowed, reason = await reserve_usage(
                _GUARD_DB, f"{scope[0]}:{scope[1]}",
                self.get_config("guard.cooldown_seconds", 60),
                self.get_config("guard.daily_max_requests", 0),
                request_id=request_id,
            )
        except Exception as exc:
            logger.warning(f"Breeze TTS usage guard failed: {type(exc).__name__}")
            return False, "语音用量检查失败，请检查日志"
        if not allowed:
            messages = {"cooldown": "语音请求过于频繁，请稍后再试", "daily_limit": "今日语音次数已达上限",
                        "duplicate": "此条语音请求已处理", "invalid_config": "语音限制配置无效",
                        "storage_error": "语音用量检查失败"}
            return False, messages.get(reason, "语音用量检查失败")
        try:
            audio = await synthesize(text, self._settings())
            if not isinstance(audio, bytes) or len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
                raise ValueError("invalid WAV")
            if len(audio) > 10 * 1024 * 1024:
                return False, "生成的音频过大，请缩短文字"
            sent = await self.send_custom(message_type="voice", content=base64.b64encode(audio).decode("ascii"))
            return (True, "语音已发送") if sent else (False, "语音发送失败，请检查适配器")
        except BreezeError as exc:
            logger.warning(f"Breeze TTS: {exc}")
            return False, "语音合成失败，请检查服务、配置和麦麦日志"
        except Exception as exc:
            # Credentials, paths, and service response bodies must not enter logs.
            logger.warning(f"Breeze TTS failed: {type(exc).__name__}")
            return False, "语音合成失败，请检查服务、配置和麦麦日志"


class BreezeAction(VoiceSender, BaseAction):
    action_name = "breeze_tts_action"
    action_description = "在用户要求语音时，将回复内容合成为语音"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["语音", "声音", "朗读", "读出来", "念出来", "说出来", "给我听", "voice", "aloud"]
    keyword_case_sensitive = False
    mode_enable = ChatMode.ALL
    parallel_action = False
    associated_types = ["text"]
    action_parameters = {"text": "要以语音回复给用户的完整文字"}
    action_require = ["用户明确要求语音回复或朗读时使用", "text 必须是需要说出的具体内容"]

    async def execute(self):
        text = self.action_data.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return False, "没有可发送的回复文字"
        text = text.strip()
        scope = self._scope()
        target = getattr(self, "action_message", None)
        fresh_voice = bool(scope is not None and _fresh_voice_message(target, scope[1], scope[0]))
        # A 0.12.2 planner can select the same historical target repeatedly.
        # Claim it before sending text so denied TTS does not spam fallbacks.
        claim_ok = False
        message_id = str(getattr(target, "message_id", "") or "").strip()
        if scope is not None and message_id:
            try:
                claim_ok, claim_reason = await claim_request(_GUARD_DB, f"{scope[0]}:{scope[1]}", message_id)
                if claim_reason == "duplicate":
                    return True, "此条消息已处理"
            except Exception as exc:
                logger.warning(f"Breeze TTS message guard failed: {type(exc).__name__}")
        # This Action is a standalone planner branch: send text before TTS so
        # limits, failed synthesis, and failed voice delivery cannot erase it.
        if not await self.send_text(text):
            return False, "文字回复发送失败"
        if not claim_ok or scope is None or not scope[2] or not fresh_voice:
            await self.store_action_info(action_build_into_prompt=True,
                                         action_prompt_display="已发送文字回复",
                                         action_done=True)
            return True, "已发送文字回复"
        ok, message = await self._speak(text)
        await self.store_action_info(action_build_into_prompt=True,
                                     action_prompt_display="已发送文字及语音回复" if ok else "已发送文字回复",
                                     action_done=True)
        return True, "已发送文字及语音" if ok else "已发送文字回复；" + message


class BreezeCommand(VoiceSender, BaseCommand):
    command_name = "breeze_tts_command"
    command_description = "使用 Breeze TTS 朗读文字"
    command_pattern = r"^/breeze(?:\s+(?P<text>[\s\S]+?))?\s*$"
    command_help = "/breeze <文字>：合成并发送语音"
    command_examples = ["/breeze 你好，很高兴认识你"]

    async def execute(self):
        text = (self.matched_groups.get("text") or "").strip()
        if not text or text.lower() == "help":
            await self.send_text(self.command_help)
            return True, "已显示帮助", 2
        inbound = getattr(getattr(self, "message", None), "message_info", None)
        request_id = str(getattr(inbound, "message_id", "") or "").strip()
        if not request_id:
            if self.get_config("general.send_errors", True):
                await self.send_text("无法识别请求消息，语音未生成")
            return False, "无法识别请求消息", 2
        ok, message = await self._speak(text, request_id=request_id)
        if not ok and self.get_config("general.send_errors", True):
            await self.send_text(message)
        return ok, message, 2


@register_plugin
class BreezeTTSPlugin(BasePlugin):
    plugin_name = "breeze_tts_0122"
    plugin_description = "Breeze TTS 语音合成插件（MaiBot 0.12.2）"
    plugin_version = "1.2.0"
    plugin_author = "MoonlyCake"
    enable_plugin = True
    config_file_name = "config.toml"
    dependencies = []
    python_dependencies = [PythonDependency(package_name="aiohttp", install_name="aiohttp")]
    config_section_descriptions = {"plugin": "插件配置", "general": "通用设置", "breeze": "Breeze TTS 服务设置", "guard": "语音范围和用量限制"}
    config_layout = ConfigLayout(type="tabs", tabs=[
        ConfigTab(id="general", title="基本设置", sections=["plugin", "general"]),
        ConfigTab(id="service", title="语音服务", sections=["breeze"]),
        ConfigTab(id="guard", title="范围与用量", sections=["guard"]),
    ])
    config_schema = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="启用插件", label="启用 Breeze TTS", order=1),
            "config_version": ConfigField(type=str, default="1.1.0", description="配置版本", hidden=True),
        },
        "general": {
            "action_enabled": ConfigField(type=bool, default=True, description="启用语音 Action", label="允许语音 Action", hint="需同时启用范围与用量中的自然语言语音。此项变动后重载插件。", order=1),
            "command_enabled": ConfigField(type=bool, default=True, description="启用 /breeze 命令", label="启用 /breeze 命令", hint="在聊天中输入 /breeze 加需要朗读的文字。", order=2),
            "max_text_length": ConfigField(type=int, default=500, description="单次合成最大字符数", label="单次文字上限", hint="允许 1–5000 字符；过长的文字不会发送给语音服务。", min=1, max=5000, step=1, input_type="number", order=3),
            "send_errors": ConfigField(type=bool, default=True, description="向聊天发送失败提示", label="在聊天中提示失败", hint="关闭后仍可在麦麦日志中查看错误类别。", order=4),
        },
        "breeze": {
            "backend": ConfigField(type=str, default="self_hosted", description="self_hosted 或 cloud", label="服务类型", choices=["self_hosted", "cloud"], hint="cloud 为 BreezeBlue 云端；self_hosted 为自行运行的官方模型服务。", order=1),
            "api_url": ConfigField(type=str, default="", description="服务地址，留空使用所选后端默认地址", label="服务地址", placeholder="留空使用对应后端的默认地址", hint="自托管默认 http://127.0.0.1:7860；云端默认 https://api.breeze.blue。", order=2),
            "api_key": ConfigField(type=str, default="", description="云服务 API Key，勿公开", label="云端 API Key", input_type="password", placeholder="可留空并使用 BREEZE_API_KEY 环境变量", hint="仅界面掩码；填入后配置和其备份均为明文。推荐使用环境变量。", order=3),
            "voice_id": ConfigField(type=str, default="", description="云服务音色 ID", label="云端音色 ID", placeholder="例如 voc_...", hint="云端必填；从 BreezeBlue 音色库选择并复制 ID。", order=4),
            "model": ConfigField(type=str, default="", description="云服务模型，留空使用默认值", label="云端模型", placeholder="留空使用服务默认模型", order=5),
            "language_code": ConfigField(type=str, default="", description="云服务语言代码，可留空", label="云端语言代码", placeholder="例如 zh；留空由服务处理", order=6),
            "instruction": ConfigField(type=str, default="", description="可选语气和声音指导", label="语气与声音指导", input_type="textarea", rows=3, placeholder="例如：用自然、温柔的语气说话", hint="可选；云端与自托管均可使用。", order=7),
            "ref_audio": ConfigField(type=str, default="", description="自托管参考音频路径", label="自托管参考音频路径", placeholder="麦麦所在机器上的文件路径", hint="与参考音频文本同时填写；请使用有权使用的声音。", order=8),
            "ref_text": ConfigField(type=str, default="", description="参考音频中的文本", label="参考音频文本", input_type="textarea", rows=3, hint="填写参考音频中实际说出的完整内容，须与音频同时提供。", order=9),
            "cfg_scale": ConfigField(type=float, default=1.0, description="语音控制强度", label="声音指导强度", input_type="number", min=0.01, step=0.1, hint="自托管需大于 0；云端允许 1–10。", order=10),
            "seed": ConfigField(type=int, default=-1, description="自托管随机种子，-1 表示省略并使用服务端默认值", label="自托管随机种子", input_type="number", min=-1, max=4294967295, step=1, hint="-1 表示不传入种子，使用服务端默认值。", order=11),
            "timeout": ConfigField(type=int, default=120, description="单次请求超时秒数", label="请求超时（秒）", input_type="number", min=1, step=1, hint="模型加载或生成较慢时可适当调大。", order=12),
        },
        "guard": {
            "natural_enabled": ConfigField(type=bool, default=False, description="自然语言语音回复", label="自然语言语音回复", hint="默认关闭；仅当用户明确要求语音且聊天已授权时，允许模型选择语音 Action。修改后重载插件。", order=1),
            "allowed_groups": ConfigField(type=list, default=[], description="允许使用语音的群", label="允许的群", item_type="string", hint="空列表不允许任何群；可填 qq:群号，其他平台须填 平台:群号。命令和自然语言均受限。", order=2),
            "allow_private": ConfigField(type=bool, default=False, description="允许私聊语音", label="允许私聊", hint="默认关闭；开启后私聊也受用量与冷却限制。", order=3),
            "cooldown_seconds": ConfigField(type=int, default=60, description="同一会话冷却时间（秒）", label="每会话冷却（秒）", input_type="number", min=0, max=86400, step=1, hint="默认 60 秒；0 表示关闭冷却。", order=4),
            "daily_max_requests": ConfigField(type=int, default=0, description="每天最多合成次数", label="每日合成次数", input_type="number", min=0, max=10000, step=1, hint="所有聊天和两种触发方式共享；按运行机器本地日期，0 表示不限。发起前计数，失败不退。", order=5),
        },
    }

    def _migrate_config_values(self, old_config, new_config):
        """Preserve known settings without the 0.12.2 host logging secret values."""
        migrated = deepcopy(new_config)
        if not isinstance(old_config, dict):
            return migrated
        for section, defaults in migrated.items():
            old_section = old_config.get(section)
            if not isinstance(defaults, dict) or not isinstance(old_section, dict):
                continue
            for key in defaults:
                if (section, key) != ("plugin", "config_version") and key in old_section:
                    defaults[key] = deepcopy(old_section[key])
        return migrated

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        result = []
        if self.get_config("general.action_enabled", True) and self.get_config("guard.natural_enabled", False):
            result.append((BreezeAction.get_action_info(), BreezeAction))
        if self.get_config("general.command_enabled", True):
            result.append((BreezeCommand.get_command_info(), BreezeCommand))
        return result
