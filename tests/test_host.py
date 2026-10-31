"""Integration with MaiBot 0.12.2 SDK; run from a test host with plugin installed."""
import asyncio
import base64
import io
import re
import wave

from src.plugin_system.core.plugin_manager import plugin_manager

assert plugin_manager._load_plugin_module_file("plugins/breeze_tts_0122/plugin.py")
ok, _ = plugin_manager.load_registered_plugin_classes("breeze_tts_0122")
assert ok, plugin_manager.failed_plugins
instance = plugin_manager.loaded_plugins["breeze_tts_0122"]
schema = instance.get_webui_config_schema()
assert schema["layout"]["type"] == "tabs"
assert {tab["id"] for tab in schema["layout"]["tabs"]} == {"general", "service"}
fields = schema["sections"]["breeze"]["fields"]
assert fields["backend"]["ui_type"] == "select"
assert fields["backend"]["choices"] == ["self_hosted", "cloud"]
assert fields["api_key"]["ui_type"] == "password"
assert fields["instruction"]["ui_type"] == "textarea"
assert schema["sections"]["general"]["fields"]["max_text_length"]["min"] == 1
assert schema["sections"]["plugin"]["fields"]["config_version"]["hidden"] is True
components = {info.name: cls for info, cls in instance.get_plugin_components()}
command_cls = components["breeze_tts_command"]
action_cls = components["breeze_tts_action"]
plugin_globals = command_cls._speak.__globals__

buf = io.BytesIO()
with wave.open(buf, "wb") as audio_file:
    audio_file.setnchannels(1)
    audio_file.setsampwidth(2)
    audio_file.setframerate(16000)
    audio_file.writeframes(b"\x01\x00" * 320)
wav = buf.getvalue()

async def main():
    sent = []
    async def synthesis(text, settings):
        assert text == "你好"
        assert settings["backend"] == "self_hosted"
        return wav
    async def send_custom(**kw):
        sent.append(kw)
        return True
    async def send_text(text):
        sent.append({"error": text})
        return True
    async def store_action_info(**kw):
        sent.append({"stored": kw})
    plugin_globals["synthesize"] = synthesis
    cmd = object.__new__(command_cls)
    cmd.matched_groups = re.match(cmd.command_pattern, "/breeze 你好").groupdict()
    cmd.plugin_config = {}
    cmd.send_custom = send_custom
    cmd.send_text = send_text
    assert (await cmd.execute())[0]
    assert sent[-1]["message_type"] == "voice"
    assert base64.b64decode(sent[-1]["content"]) == wav
    action = object.__new__(action_cls)
    action.action_data = {"text": "你好"}
    action.plugin_config = {}
    action.send_custom = send_custom
    action.send_text = send_text
    action.store_action_info = store_action_info
    assert (await action.execute())[0]
    assert sent[-1]["stored"]["action_done"]
    cmd.matched_groups = {"text": "x" * 501}
    assert not (await cmd.execute())[0]
    assert "超过" in sent[-1]["error"]
    async def fail_send(**kw): return False
    cmd.matched_groups = {"text": "你好"}
    cmd.send_custom = fail_send
    assert not (await cmd.execute())[0]
    async def invalid(*args): return b"invalid"
    plugin_globals["synthesize"] = invalid
    assert not (await cmd.execute())[0]
    print("PASS: MaiBot 0.12.2 plugin registration, WebUI schema, command and action routes, WAV send, limits and errors")

asyncio.run(main())
