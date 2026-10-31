"""Breeze TTS integration for MaiBot 0.12.2."""
import base64
import asyncio
import hashlib
from copy import deepcopy
import math
import random
import re
import time
import weakref
from pathlib import Path
from typing import List, Tuple, Type

from src.common.data_models.database_data_model import DatabaseMessages
from src.common.logger import get_logger
from maim_message import Seg
from src.chat.message_receive.chat_stream import get_chat_manager
from src.chat.utils.utils import is_bot_self
from src.plugin_system.apis.plugin_register_api import register_plugin
from src.plugin_system.base.base_action import BaseAction, ActionActivationType
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.base_events_handler import BaseEventHandler
from src.plugin_system.base.component_types import ChatMode, ComponentInfo, EventType
from src.plugin_system.base.config_types import ConfigField, ConfigLayout, ConfigTab
from src.plugin_system import PythonDependency
from src.plugin_system.apis import generator_api, message_api

from .breeze_client import BreezeError, synthesize
from .voice_guard import claim_request, finish_random_usage, is_admin, reserve_usage, scope_allowed

logger = get_logger("breeze_tts_0122")
_GUARD_DB = Path(__file__).resolve().parent / "voice_guard_data" / "usage.sqlite3"
_RANDOM_TASKS = weakref.WeakKeyDictionary()
_RAW_COMMAND_PATTERN = r"^\s*/breeze(?:\s+(?P<text>[\s\S]+?))?\s*$"


async def _release_cancelled_random(reservation, scope, request_id):
    """The DB thread may commit after its caller was cancelled."""
    try:
        allowed, _ = await reservation
        if allowed:
            await finish_random_usage(_GUARD_DB, scope, request_id, success=False)
    except Exception as exc:
        logger.warning(f"Breeze random reservation cleanup failed: {type(exc).__name__}")


def _mentions_only_bot(segment, platform):
    """Reject untrusted or other-user mentions that the 0.12.2 parser strips."""
    if segment is None:
        return False
    kind = getattr(segment, "type", None)
    data = getattr(segment, "data", None)
    if kind == "seglist":
        return isinstance(data, list) and all(_mentions_only_bot(part, platform) for part in data)
    if kind == "mention_bot":
        return True
    if kind != "at":
        return bool(isinstance(kind, str) and kind and not kind.startswith(("at", "mention")))
    if isinstance(data, dict):
        target_platform = str(data.get("platform") or "").strip()
        if target_platform and target_platform.lower() != platform.lower():
            return False
        target = data.get("target_user_id") or data.get("user_id") or data.get("qq")
    else:
        target = data
    if not isinstance(target, (str, int)) or isinstance(target, bool) or not str(target).strip():
        return False
    try:
        return bool(is_bot_self(platform, str(target).strip()))
    except Exception:
        return False


def _raw_command_text(segment):
    """Reconstruct only literal text and mentions, never VLM/ASR output."""
    if segment is None:
        return None
    kind = getattr(segment, "type", None)
    data = getattr(segment, "data", None)
    if kind == "text":
        return data if isinstance(data, str) else None
    if kind in ("at", "mention_bot"):
        return ""
    if kind != "seglist" or not isinstance(data, list):
        return None
    parts = [_raw_command_text(part) for part in data]
    if any(part is None for part in parts):
        return None
    # MessageRecv joins nonempty child results with spaces.
    return " ".join(part for part in parts if part)


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


def _voice_denied(text):
    if not isinstance(text, str):
        return False
    denial = r"(?:不要|不用|不必|无需|不需要|不想|不希望|不愿|没必要|别|禁止|关闭|取消|拒绝).{0,8}(?:语音|声音|朗读|读出来|读给我听|说给我听)|(?:语音|声音|朗读).{0,8}(?:不要|不用|别|关闭|取消)|\b(?:don't|dont|do not|never|without|no|not|stop)\b.{0,16}\b(?:voice|aloud|speak|read)\b"
    text_only = r"(?:只|仅).{0,5}(?:文字|文本|打字)|(?:用|以).{0,3}(?:文字|文本).{0,5}(?:回答|回复)|(?:打字|文字|文本).{0,4}(?:回答|回复)|\b(?:text only|only text|reply in text|type only)\b"
    return any(re.search(denial, clause) or re.search(text_only, clause)
               for clause in re.split(r"[，,。.!！?？；;\n]", text.casefold()))


def _fresh_voice_message(message, stream_id, platform, *, command_enabled=True):
    """Planner targets may come from old history; require a recent identified message."""
    if message is None:
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
        segment = getattr(latest, "message_segment", None)
        raw_text = _raw_command_text(segment)
        natural = (_explicit_voice_request(getattr(message, "processed_plain_text", ""))
                   and _explicit_voice_request(raw_text) and not _voice_denied(raw_text))
        # Some adapters expose a rendered @ prefix in processed_plain_text;
        # only the verified raw Seg may authorize a slash command.
        command_match = re.match(_RAW_COMMAND_PATTERN, raw_text, re.IGNORECASE) if raw_text else None
        command = bool(command_enabled and command_match and (command_match.group("text") or "").strip())
        return bool(latest_id == str(message.message_id).strip() and latest_sender == sender
                    and latest_platform == platform and raw_text is not None
                    and _mentions_only_bot(segment, platform) and (natural or command))
    except Exception:
        # Unknown live context is not a safe reason to spend or send voice.
        return False


class VoiceSender:
    async def _generate_context_reply(self, target):
        """Generate an answer against the host's actual conversation target."""
        ok, result = await asyncio.wait_for(generator_api.generate_reply(
            chat_stream=getattr(self, "chat_stream", None) or self.message.chat_stream, reply_message=target,
            reply_reason="用户请求用语音回答问题", enable_tool=False,
            enable_splitter=False, enable_chinese_typo=False,
            request_type="breeze_tts", from_plugin=True,
        ), timeout=120)
        if not ok or result is None:
            return None
        parts = getattr(result, "processed_output", None)
        if isinstance(parts, list):
            answer = "\n".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
            if answer:
                return answer
        reply_set = getattr(result, "reply_set", None)
        reply_data = getattr(reply_set, "reply_data", [])
        texts = [part.content.strip() for part in reply_data
                 if isinstance(getattr(part, "content", None), str) and part.content.strip()
                 and str(getattr(getattr(part, "content_type", None), "value", getattr(part, "content_type", ""))) == "text"]
        return "\n".join(texts) or None

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

    def _is_admin(self, platform):
        """Only trust the sender supplied by the verified host message."""
        incoming = getattr(self, "message", None)
        if incoming is not None:
            user = getattr(getattr(incoming, "message_info", None), "user_info", None)
        else:
            user = getattr(getattr(self, "action_message", None), "user_info", None)
        user_platform = str(getattr(user, "platform", "") or "").strip()
        if user_platform and user_platform.lower() != platform.lower():
            return False
        user_id = getattr(user, "user_id", None)
        return is_admin(platform, user_id, self.get_config("guard.admin_users", []))

    def _settings(self):
        fields = {
            "backend": "self_hosted", "api_url": "", "api_key": "",
            "voice_id": "", "model": "", "language_code": "", "instruction": "", "ref_audio": "",
            "ref_text": "", "cfg_scale": 1.0, "seed": -1, "timeout": 120,
        }
        return {key: self.get_config("breeze." + key, default) for key, default in fields.items()}

    async def _speak(self, text, request_id=None, *, pre_reserved=False):
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
        if not pre_reserved:
            allowed, reason = await self._reserve(scope, request_id)
            if not allowed:
                return False, self._limit_message(reason)
        try:
            audio = await synthesize(text, self._settings())
            if not isinstance(audio, bytes) or len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
                raise ValueError("invalid WAV")
            if len(audio) > 10 * 1024 * 1024:
                return False, "生成的音频过大，请缩短文字"
        except BreezeError as exc:
            logger.warning(f"Breeze TTS: {exc}")
            return False, "语音合成失败，请检查服务、配置和麦麦日志"
        except Exception as exc:
            # Credentials, paths, and service response bodies must not enter logs.
            logger.warning(f"Breeze TTS failed: {type(exc).__name__}")
            return False, "语音合成失败，请检查服务、配置和麦麦日志"
        try:
            sent = await self.send_custom(message_type="voice", content=base64.b64encode(audio).decode("ascii"))
        except Exception as exc:
            # Delivery may have happened before an exception reached us.
            logger.warning(f"Breeze TTS delivery outcome unknown: {type(exc).__name__}")
            return None, "语音发送状态未知"
        return (True, "语音已发送") if sent else (False, "语音发送失败，请检查适配器")

    @staticmethod
    def _limit_message(reason):
        return {"cooldown": "语音请求过于频繁，请稍后再试", "daily_limit": "今日语音次数已达上限",
                "random_daily_limit": "本群今日随机语音次数已达上限",
                "duplicate": "此条语音请求已处理", "invalid_config": "语音限制配置无效",
                "storage_error": "语音用量检查失败"}.get(reason, "语音用量检查失败")

    async def _reserve(self, scope, request_id, *, is_random=False):
        try:
            return await reserve_usage(
                _GUARD_DB, f"{scope[0]}:{scope[1]}",
                self.get_config("guard.cooldown_seconds", 60),
                self.get_config("guard.daily_max_requests", 0),
                request_id=request_id, bypass_limits=self._is_admin(scope[0]),
                random_daily_max_requests=(self.get_config("guard.random_daily_max_requests", 5)
                    if self.get_config("guard.random_daily_limit_enabled", False) else 0) if is_random else None,
            )
        except Exception as exc:
            logger.warning(f"Breeze TTS usage guard failed: {type(exc).__name__}")
            return False, "storage_error"


class BreezeAction(VoiceSender, BaseAction):
    action_name = "breeze_tts_action"
    action_description = "在用户要求语音时，将回复内容合成为语音"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["语音", "声音", "朗读", "读出来", "念出来", "说出来", "给我听", "voice", "aloud", "/breeze"]
    keyword_case_sensitive = False
    mode_enable = ChatMode.ALL
    parallel_action = False
    associated_types = ["text"]
    action_parameters = {"text": "要以语音回复给用户的完整文字"}
    action_require = ["用户明确要求语音回复、朗读，或发送 /breeze 问题时使用", "text 必须是你生成的回答，不要照读用户的问题"]

    async def execute(self):
        data = getattr(self, "action_data", None)
        text = data.get("text", "") if isinstance(data, dict) else ""
        text = text.strip() if isinstance(text, str) else ""
        scope = self._scope()
        target = getattr(self, "action_message", None)
        fresh_voice = bool(scope is not None and _fresh_voice_message(
            target, scope[1], scope[0], command_enabled=self.get_config("general.command_enabled", True)))
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
        if not claim_ok or scope is None or not scope[2] or not fresh_voice:
            logger.info("Breeze Action skipped speech: request or scope not verified")
            if not await self.send_text(text or "抱歉，暂时无法生成回复，请稍后再试。"):
                return False, "文字回复发送失败"
            await self.store_action_info(action_build_into_prompt=True,
                                         action_prompt_display="已发送文字回复",
                                         action_done=True)
            return True, "已发送文字回复"
        pre_reserved = False
        if not text:
            allowed, reason = await self._reserve(scope, "voice:" + message_id)
            if not allowed:
                logger.info(f"Breeze Action reply generation skipped: {reason}")
                if not await self.send_text("抱歉，暂时无法生成语音回复，请稍后再试。"):
                    return False, "文字回复发送失败"
                return False, self._limit_message(reason)
            pre_reserved = True
            try:
                text = await self._generate_context_reply(target)
            except Exception as exc:
                logger.warning(f"Breeze TTS reply generation failed: {type(exc).__name__}")
                text = None
            if not text:
                if not await self.send_text("抱歉，暂时无法生成回复，请稍后再试。"):
                    return False, "文字回复发送失败"
                return False, "回复生成失败，已发送文字提示"
        ok, message = await self._speak(text, request_id="voice:" + message_id, pre_reserved=pre_reserved)
        if ok is False:
            logger.info(f"Breeze Action speech unavailable: {message}")
        if ok is False and not await self.send_text(text):
            return False, "语音及文字回复发送失败"
        await self.store_action_info(action_build_into_prompt=True,
                                     action_prompt_display="已发送语音回复" if ok else ("已发送文字回复" if ok is False else "语音发送状态未知"),
                                     action_done=True)
        return True, "已发送语音" if ok else (("已发送文字回复；" if ok is False else "") + message)


class BreezeCommand(VoiceSender, BaseCommand):
    command_name = "breeze_tts_command"
    command_description = "让麦麦回答问题并尝试发送语音"
    command_pattern = r"^\s*(?:\[@[^\]\n]{1,128}\]\s*)?/breeze(?:\s+(?P<text>[\s\S]+?))?\s*$"
    command_help = "/breeze <问题>：让麦麦回答并发送语音"
    command_examples = ["/breeze 你在做什么？"]

    async def _answer(self, question, scope, request_id):
        """Generate against the actual sender and stream without requiring DB storage."""
        incoming = self.message
        info = incoming.message_info
        user = getattr(info, "user_info", None)
        sender = str(getattr(user, "user_id", "") or "").strip()
        if not sender:
            return None
        group = getattr(info, "group_info", None)
        target = DatabaseMessages(
            message_id=request_id,
            time=float(getattr(info, "time", time.time()) or time.time()),
            chat_id=scope[1], processed_plain_text=question,
            user_id=sender, user_nickname=str(getattr(user, "user_nickname", "") or ""),
            user_platform=scope[0], chat_info_stream_id=scope[1], chat_info_platform=scope[0],
            chat_info_group_id=str(getattr(group, "group_id", "") or "") or None,
            chat_info_group_name=str(getattr(group, "group_name", "") or "") or None,
        )
        return await self._generate_context_reply(target)

    async def execute(self):
        text = (self.matched_groups.get("text") or "").strip()
        incoming = getattr(self, "message", None)
        scope = self._scope()
        if scope is None:
            if incoming is not None:
                incoming.is_command = False
            return False, "无法确认聊天范围", 0
        if not _mentions_only_bot(getattr(incoming, "message_segment", None), scope[0]):
            # Host parsing drops @other; let unrelated mentions proceed normally.
            incoming.is_command = False
            return True, "未提及本机器人", 0
        raw_text = _raw_command_text(getattr(incoming, "message_segment", None))
        raw_match = re.match(_RAW_COMMAND_PATTERN, raw_text, re.IGNORECASE) if raw_text is not None else None
        if not raw_match or (raw_match.group("text") or "").strip() != text:
            incoming.is_command = False
            return True, "不是纯文字语音命令", 0
        if not scope[2]:
            return False, "当前聊天未启用语音", 2
        if not text or text.lower() == "help":
            await self.send_text(self.command_help)
            return True, "已显示帮助", 2
        inbound = getattr(incoming, "message_info", None)
        request_id = str(getattr(inbound, "message_id", "") or "").strip()
        if not request_id:
            if self.get_config("general.send_errors", True):
                await self.send_text("无法识别请求消息，语音未生成")
            return False, "无法识别请求消息", 2
        limit = self.get_config("general.max_text_length", 500)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5000:
            return False, "文字长度配置无效", 2
        if len(text) > limit:
            await self.send_text("问题超过单次文字长度限制，请缩短后重试。")
            return False, "问题过长", 2
        try:
            claimed, reason = await claim_request(_GUARD_DB, f"{scope[0]}:{scope[1]}", request_id)
            if not claimed:
                if reason != "duplicate" and self.get_config("general.send_errors", True):
                    await self.send_text("语音用量检查失败，请检查日志。")
                return reason == "duplicate", "此条请求已处理" if reason == "duplicate" else "语音用量检查失败", 2
            allowed, reason = await self._reserve(scope, "voice:" + request_id)
            if not allowed:
                message = self._limit_message(reason)
                if self.get_config("general.send_errors", True):
                    await self.send_text(message)
                return False, message, 2
            answer = await self._answer(text, scope, request_id)
        except Exception as exc:
            logger.warning(f"Breeze TTS reply generation failed: {type(exc).__name__}")
            answer = None
        if not answer:
            await self.send_text("抱歉，暂时无法生成回复，请稍后再试。")
            return False, "回复生成失败", 2
        ok, message = await self._speak(answer, request_id="voice:" + request_id, pre_reserved=True)
        if ok is False and not await self.send_text(answer):
            return False, "语音及文字回复发送失败", 2
        return True, "已发送语音" if ok else (("已发送文字回复；" if ok is False else "") + message), 2


class _RandomEvent(BaseEventHandler):
    intercept_message = True

    @staticmethod
    def _task():
        return asyncio.current_task()


class RandomReplyStarted(_RandomEvent):
    event_type = EventType.POST_LLM
    handler_name = "breeze_random_started"
    handler_description = "记录普通回复生成"

    async def execute(self, message):
        task = self._task()
        if task is not None:
            _RANDOM_TASKS.pop(task, None)
            prompt = getattr(message, "llm_prompt", None)
            if message and message.stream_id and isinstance(prompt, str) and prompt:
                _RANDOM_TASKS[task] = (message.stream_id, time.monotonic(), None,
                                       hashlib.sha256(prompt.encode()).digest())
        return True, True, None, None, None


class RandomReplyGenerated(_RandomEvent):
    event_type = EventType.AFTER_LLM
    handler_name = "breeze_random_generated"
    handler_description = "校验普通回复生成结果"

    async def execute(self, message):
        task = self._task()
        mark = _RANDOM_TASKS.get(task)
        text = getattr(message, "llm_response_content", None)
        prompt = getattr(message, "llm_prompt", None)
        if (task is not None and mark and message and message.stream_id == mark[0]
                and time.monotonic() - mark[1] < 120 and isinstance(text, str) and text.strip()
                and isinstance(prompt, str) and hashlib.sha256(prompt.encode()).digest() == mark[3]):
            _RANDOM_TASKS[task] = (mark[0], mark[1], text.strip(), mark[3])
        elif task is not None:
            _RANDOM_TASKS.pop(task, None)
        return True, True, None, None, None


class RandomReplySend(_RandomEvent):
    event_type = EventType.POST_SEND
    handler_name = "breeze_random_send"
    handler_description = "将已确认的普通群聊回复按概率转为语音"

    async def execute(self, message):
        task = self._task()
        mark = _RANDOM_TASKS.pop(task, None) if task is not None else None
        unchanged = (True, True, None, None, None)
        if (not self.get_config("breeze.random_enabled", False) or not mark or not message
                or message.stream_id != mark[0] or time.monotonic() - mark[1] >= 120
                or not isinstance(mark[2], str) or not message.is_group_message):
            return unchanged
        probability = self.get_config("breeze.random_probability", 0.1)
        if (isinstance(probability, bool) or not isinstance(probability, (float, int))
                or not math.isfinite(probability) or not 0 <= probability <= 1
                or random.random() >= probability):
            return unchanged
        info = message.message_base_info
        platform = str(info.get("platform") or "").strip() if isinstance(info, dict) else ""
        group = str(info.get("group_id") or "").strip() if isinstance(info, dict) else ""
        if not platform or not group or not scope_allowed(True, group, self.get_config("guard.allowed_groups", []), False, platform=platform):
            return unchanged
        segments = message.message_segments
        if not isinstance(segments, list) or len(segments) != 2:
            return unchanged
        replies = [s for s in segments if getattr(s, "type", None) == "reply"]
        texts = [s for s in segments if getattr(s, "type", None) == "text"]
        if len(replies) != 1 or len(texts) != 1:
            return unchanged
        target_id = getattr(replies[0], "data", None)
        text = getattr(texts[0], "data", None)
        if (not isinstance(target_id, str) or not target_id.strip() or not isinstance(text, str)
                or not text.strip() or text.strip() != mark[2]):
            return unchanged
        max_length = self.get_config("general.max_text_length", 500)
        if not isinstance(max_length, int) or isinstance(max_length, bool) or not 1 <= len(text.strip()) <= min(max_length, 5000):
            return unchanged
        try:
            now = time.time()
            targets = await asyncio.wait_for(asyncio.to_thread(message_api.get_messages_by_time_in_chat,
                chat_id=message.stream_id, start_time=now - 300, end_time=now,
                limit=100, limit_mode="latest", filter_mai=True), timeout=4)
            targets = [m for m in targets if str(getattr(m, "message_id", "")) == target_id]
            if len(targets) != 1:
                return unchanged
            target = targets[0]
            user = getattr(target, "user_info", None)
            chat = getattr(target, "chat_info", None)
            sender = str(getattr(user, "user_id", "") or "").strip()
            user_platform = str(getattr(user, "platform", "") or "").strip()
            target_group = getattr(chat, "group_info", None)
            if (not sender or is_bot_self(platform, sender) or str(getattr(target, "chat_id", "")) != message.stream_id
                    or (user_platform and user_platform.lower() != platform.lower())
                    or str(getattr(chat, "platform", "") or "").lower() != platform.lower()
                    or str(getattr(target_group, "group_id", "") or "") != group):
                return unchanged
            request = str(getattr(target, "processed_plain_text", "") or "")
            if _explicit_voice_request(request) or re.search(r"(?:^|\s)/breeze(?:\s|$)", request, re.I):
                return unchanged
            if _voice_denied(request):
                return unchanged
            scope = f"{platform}:{message.stream_id}"
            request_id = "random:" + target_id
            random_limit = (self.get_config("guard.random_daily_max_requests", 5)
                            if self.get_config("guard.random_daily_limit_enabled", False) else 0)
            reservation = asyncio.create_task(reserve_usage(_GUARD_DB, scope, self.get_config("guard.cooldown_seconds", 60),
                self.get_config("guard.daily_max_requests", 0), request_id=request_id,
                bypass_limits=is_admin(platform, sender, self.get_config("guard.admin_users", [])),
                random_daily_max_requests=random_limit))
            try:
                allowed, _ = await asyncio.shield(reservation)
            except asyncio.CancelledError:
                asyncio.create_task(_release_cancelled_random(reservation, scope, request_id))
                raise
            if not allowed:
                return unchanged
            success = False
            try:
                settings = VoiceSender._settings(self)
                audio = await synthesize(text.strip(), settings)
                if (not isinstance(audio, bytes) or len(audio) < 44 or len(audio) > 10 * 1024 * 1024
                        or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE"):
                    return unchanged
                updated = message.deepcopy()
                updated.modify_message_segments([replies[0], Seg(type="voice", data=base64.b64encode(audio).decode("ascii"))], suppress_warning=True)
                success = True
                return True, True, None, None, updated
            finally:
                await asyncio.shield(finish_random_usage(_GUARD_DB, scope, request_id, success=success))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"Breeze random voice failed: {type(exc).__name__}")
            return unchanged


@register_plugin
class BreezeTTSPlugin(BasePlugin):
    plugin_name = "breeze_tts_0122"
    plugin_description = "Breeze TTS 语音合成插件（MaiBot 0.12.2）"
    plugin_version = "1.4.1"
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
            "config_version": ConfigField(type=str, default="1.4.0", description="配置版本", hidden=True),
        },
        "general": {
            "action_enabled": ConfigField(type=bool, default=True, description="启用语音 Action", label="允许语音 Action", hint="需同时启用范围与用量中的自然语言语音。此项变动后重载插件。", order=1),
            "command_enabled": ConfigField(type=bool, default=True, description="启用 /breeze 命令", label="启用 /breeze 命令", hint="在聊天中输入 /breeze 加问题，麦麦会生成人设回答并尝试语音回复。", order=2),
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
            "random_enabled": ConfigField(type=bool, default=False, description="随机语音回复", label="随机语音回复", hint="默认关闭；开启后重载插件。仅有可靠来源及引用目标的群聊普通回复可转换。", order=13),
            "random_probability": ConfigField(type=float, default=0.1, description="随机语音概率", label="随机语音概率", input_type="number", min=0.0, max=1.0, step=0.05, hint="0 不触发；1 表示每条符合条件的回复都尝试。", order=14),
        },
        "guard": {
            "natural_enabled": ConfigField(type=bool, default=False, description="自然语言语音回复", label="自然语言语音回复", hint="默认关闭；仅当用户明确要求语音且聊天已授权时，允许模型选择语音 Action。修改后重载插件。", order=1),
            "allowed_groups": ConfigField(type=list, default=[], description="允许使用语音的群", label="允许的群", item_type="string", hint="空列表不允许任何群；可填 qq:群号，其他平台须填 平台:群号。命令和自然语言均受限。", order=2),
            "allow_private": ConfigField(type=bool, default=False, description="允许私聊语音", label="允许私聊", hint="默认关闭；开启后私聊也受用量与冷却限制。", order=3),
            "admin_users": ConfigField(type=list, default=[], description="管理员账号列表", label="管理员账号", item_type="string", hint="仅豁免冷却和每日语音上限；仍受聊天范围、重复请求和平台余额限制。QQ 可填纯号码，其他平台填 平台:用户ID。", order=4),
            "cooldown_seconds": ConfigField(type=int, default=60, description="同一会话冷却时间（秒）", label="每会话冷却（秒）", input_type="number", min=0, max=86400, step=1, hint="默认 60 秒；0 表示关闭冷却。", order=5),
            "random_daily_limit_enabled": ConfigField(type=bool, default=False, description="启用随机语音每日群限额", label="随机语音每日限额", hint="默认关闭。开启后每个群独立计数；主动要求语音不占此额度。", order=7),
            "random_daily_max_requests": ConfigField(type=int, default=5, description="每群每天随机语音次数", label="每群每天随机语音次数", input_type="number", min=0, max=10000, step=1, hint="开启每日限额后生效；0 表示不限。", order=8),
            "daily_max_requests": ConfigField(type=int, default=0, description="每天最多合成次数", label="每日合成次数", input_type="number", min=0, max=10000, step=1, hint="所有聊天和两种触发方式共享；按运行机器本地日期，0 表示不限。发起前计数，失败不退。", order=6),
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
        if self.get_config("breeze.random_enabled", False):
            for handler in (RandomReplyStarted, RandomReplyGenerated, RandomReplySend):
                result.append((handler.get_handler_info(), handler))
        return result
