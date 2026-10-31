"""Breeze TTS integration for MaiBot 0.12.2."""
import base64
from typing import List, Tuple, Type

from src.common.logger import get_logger
from src.plugin_system.apis.plugin_register_api import register_plugin
from src.plugin_system.base.base_action import BaseAction, ActionActivationType
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.component_types import ChatMode, ComponentInfo
from src.plugin_system.base.config_types import ConfigField, ConfigLayout, ConfigTab
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
    plugin_version = "1.1.0"
    plugin_author = "MoonlyCake"
    enable_plugin = True
    config_file_name = "config.toml"
    dependencies = []
    python_dependencies = [PythonDependency(package_name="aiohttp", install_name="aiohttp")]
    config_section_descriptions = {"plugin": "插件配置", "general": "通用设置", "breeze": "Breeze TTS 服务设置"}
    config_layout = ConfigLayout(type="tabs", tabs=[
        ConfigTab(id="general", title="基本设置", sections=["plugin", "general"]),
        ConfigTab(id="service", title="语音服务", sections=["breeze"]),
    ])
    config_schema = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="启用插件", label="启用 Breeze TTS", order=1),
            "config_version": ConfigField(type=str, default="1.0.0", description="配置版本", hidden=True),
        },
        "general": {
            "action_enabled": ConfigField(type=bool, default=True, description="启用语音 Action", label="按需语音回复", hint="允许麦麦在用户明确要求语音时选择使用；并非每条回复都会转语音。", order=1),
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
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        result = []
        if self.get_config("general.action_enabled", True):
            result.append((BreezeAction.get_action_info(), BreezeAction))
        if self.get_config("general.command_enabled", True):
            result.append((BreezeCommand.get_command_info(), BreezeCommand))
        return result
