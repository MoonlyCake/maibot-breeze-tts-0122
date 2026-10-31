"""Breeze TTS integration for MaiBot 0.12.2."""
import base64
from typing import List, Tuple, Type

from src.common.logger import get_logger
from src.plugin_system.apis.plugin_register_api import register_plugin
from src.plugin_system.base.base_action import BaseAction, ActionActivationType
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.component_types import ChatMode, ComponentInfo
from src.plugin_system.base.config_types import ConfigField
from src.plugin_system import PythonDependency

from .breeze_client import BreezeError, synthesize

logger = get_logger("breeze_tts_0122")


class VoiceSender:
    def _settings(self):
        fields = {
            "backend": "self_hosted", "api_url": "", "api_key": "",
            "voice_id": "", "model": "", "language_code": "", "instruction": "", "ref_audio": "",
            "ref_text": "", "cfg_scale": 1.0, "seed": -1, "timeout": 120,
        }
        return {key: self.get_config("breeze." + key, default) for key, default in fields.items()}

    async def _speak(self, text):
        if not isinstance(text, str) or not text.strip():
            return False, "请输入需要朗读的文字"
        text = text.strip()
        limit = self.get_config("general.max_text_length", 500)
        if not isinstance(limit, int) or limit < 1 or limit > 5000:
            return False, "请检查 max_text_length 配置"
        if len(text) > limit:
            return False, f"文字超过 {limit} 字，请缩短后重试"
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
    activation_keywords = ["语音", "朗读", "读出来", "说出来", "voice", "tts"]
    keyword_case_sensitive = False
    mode_enable = ChatMode.ALL
    parallel_action = False
    associated_types = ["text"]
    action_parameters = {"text": "要以语音回复给用户的完整文字"}
    action_require = ["用户明确要求语音回复或朗读时使用", "text 必须是需要说出的具体内容"]

    async def execute(self):
        ok, message = await self._speak(self.action_data.get("text", ""))
        if ok:
            await self.store_action_info(action_build_into_prompt=True,
                                         action_prompt_display="已用语音回复用户",
                                         action_done=True)
        elif self.get_config("general.send_errors", True):
            await self.send_text(message)
        return ok, message


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
        ok, message = await self._speak(text)
        if not ok and self.get_config("general.send_errors", True):
            await self.send_text(message)
        return ok, message, 2


@register_plugin
class BreezeTTSPlugin(BasePlugin):
    plugin_name = "breeze_tts_0122"
    plugin_description = "Breeze TTS 语音合成插件（MaiBot 0.12.2）"
    plugin_version = "1.0.0"
    plugin_author = "MoonlyCake"
    enable_plugin = True
    config_file_name = "config.toml"
    dependencies = []
    python_dependencies = [PythonDependency(package_name="aiohttp", install_name="aiohttp")]
    config_section_descriptions = {"plugin": "插件配置", "general": "通用设置", "breeze": "Breeze TTS 服务设置"}
    config_schema = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="启用插件"),
            "config_version": ConfigField(type=str, default="1.0.0", description="配置版本"),
        },
        "general": {
            "action_enabled": ConfigField(type=bool, default=True, description="启用语音 Action"),
            "command_enabled": ConfigField(type=bool, default=True, description="启用 /breeze 命令"),
            "max_text_length": ConfigField(type=int, default=500, description="单次合成最大字符数"),
            "send_errors": ConfigField(type=bool, default=True, description="向聊天发送失败提示"),
        },
        "breeze": {
            "backend": ConfigField(type=str, default="self_hosted", description="self_hosted 或 cloud"),
            "api_url": ConfigField(type=str, default="", description="服务地址，留空使用所选后端默认地址"),
            "api_key": ConfigField(type=str, default="", description="云服务 API Key，勿公开"),
            "voice_id": ConfigField(type=str, default="", description="云服务音色 ID"),
            "model": ConfigField(type=str, default="", description="云服务模型，留空使用默认值"),
            "language_code": ConfigField(type=str, default="", description="云服务语言代码，可留空"),
            "instruction": ConfigField(type=str, default="", description="可选语气和声音指导"),
            "ref_audio": ConfigField(type=str, default="", description="自托管参考音频路径"),
            "ref_text": ConfigField(type=str, default="", description="参考音频中的文本"),
            "cfg_scale": ConfigField(type=float, default=1.0, description="自托管模型 CFG 参数"),
            "seed": ConfigField(type=int, default=-1, description="自托管随机种子，-1 表示省略并使用服务端默认值"),
            "timeout": ConfigField(type=int, default=120, description="单次请求超时秒数"),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        result = []
        if self.get_config("general.action_enabled", True):
            result.append((BreezeAction.get_action_info(), BreezeAction))
        if self.get_config("general.command_enabled", True):
            result.append((BreezeCommand.get_command_info(), BreezeCommand))
        return result
