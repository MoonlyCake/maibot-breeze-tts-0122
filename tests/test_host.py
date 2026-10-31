"""MaiBot 0.12.2 host integration; run in an isolated host with this plugin installed.

All audio generation and sends are mocked; SQLite guard state lives in a temp dir.
"""
import asyncio
import base64
import io
from itertools import count
import re
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import wave

import toml
from src.plugin_system.core.plugin_manager import plugin_manager
import src.plugin_system.base.plugin_base as host_plugin_base

assert plugin_manager._load_plugin_module_file("plugins/breeze_tts_0122/plugin.py")
ok, _ = plugin_manager.load_registered_plugin_classes("breeze_tts_0122")
assert ok, plugin_manager.failed_plugins
instance = plugin_manager.loaded_plugins["breeze_tts_0122"]
schema = instance.get_webui_config_schema()
assert schema["layout"]["type"] == "tabs"
assert {tab["id"] for tab in schema["layout"]["tabs"]} == {"general", "service", "guard"}
fields = schema["sections"]["breeze"]["fields"]
assert fields["backend"]["ui_type"] == "select"
assert fields["backend"]["choices"] == ["self_hosted", "cloud"]
assert fields["api_key"]["ui_type"] == "password"
assert fields["instruction"]["ui_type"] == "textarea"
assert schema["sections"]["general"]["fields"]["max_text_length"]["min"] == 1
assert schema["sections"]["plugin"]["fields"]["config_version"]["hidden"] is True
assert {"natural_enabled", "allowed_groups", "allow_private", "cooldown_seconds", "daily_max_requests"} <= set(schema["sections"]["guard"]["fields"])

# Existing configurations have no guard section: natural Action must remain off.
instance.config = {}
components = {info.name: cls for info, cls in instance.get_plugin_components()}
assert set(components) == {"breeze_tts_command"}
command_cls = components["breeze_tts_command"]
plugin_globals = command_cls._speak.__globals__
instance.config = {"guard": {"natural_enabled": True}}
components = {info.name: cls for info, cls in instance.get_plugin_components()}
assert set(components) == {"breeze_tts_action", "breeze_tts_command"}
action_cls = components["breeze_tts_action"]

buf = io.BytesIO()
with wave.open(buf, "wb") as audio_file:
    audio_file.setnchannels(1)
    audio_file.setsampwidth(2)
    audio_file.setframerate(16000)
    audio_file.writeframes(b"\x01\x00" * 320)
wav = buf.getvalue()


def check_old_config_migration():
    """A real 0.12.2 host upgrade must preserve config without logging secrets."""
    synthetic_key = "fictional-secret-for-migration-test"
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        shutil.copy(Path(instance.plugin_dir) / "_manifest.json", tmpdir / "_manifest.json")
        config_file = tmpdir / "config.toml"
        config_file.write_text(
            '[plugin]\nenabled = true\nconfig_version = "1.0.0"\n'
            '[general]\ncommand_enabled = false\nmax_text_length = 123\n'
            '[breeze]\nbackend = "cloud"\nvoice_id = "example_voice"\n'
            f'api_key = "{synthetic_key}"\n', encoding="utf-8"
        )
        base_log = Mock()
        plugin_log = Mock()
        with patch.object(host_plugin_base, "logger", base_log), patch.dict(plugin_globals, {"logger": plugin_log}):
            migrated_plugin = instance.__class__(plugin_dir=str(tmpdir))
        migrated = toml.loads(config_file.read_text(encoding="utf-8"))
        assert migrated["plugin"]["config_version"] == "1.1.0"
        assert migrated["general"]["command_enabled"] is False
        assert migrated["general"]["max_text_length"] == 123
        assert migrated["breeze"]["backend"] == "cloud"
        assert migrated["breeze"]["api_key"] == synthetic_key
        assert migrated_plugin.config["breeze"]["api_key"] == synthetic_key
        assert migrated["guard"]["natural_enabled"] is False
        assert "daily_max_requests" in migrated["guard"]
        assert synthetic_key not in repr(base_log.mock_calls)
        assert synthetic_key not in repr(plugin_log.mock_calls)


def stream(group="123", platform="qq", stream_id=None):
    return SimpleNamespace(group_info=SimpleNamespace(group_id=group) if group else None,
                           platform=platform, stream_id=stream_id or f"{platform}-{group or 'private'}")


def config(groups=None, cooldown=0, daily=20, allow_private=False):
    return {"guard": {"allowed_groups": groups if groups is not None else ["qq:123"],
                      "allow_private": allow_private, "cooldown_seconds": cooldown,
                      "daily_max_requests": daily, "natural_enabled": True}}


async def main():
    sent = []
    synthesized = []

    async def synthesis(text, settings):
        assert settings["backend"] == "self_hosted"
        synthesized.append(text)
        return wav

    async def send_custom(**kw):
        sent.append(("voice", kw))
        return True

    async def send_text(text):
        sent.append(("text", text))
        return True

    async def store_action_info(**kw):
        sent.append(("stored", kw))

    plugin_globals["synthesize"] = synthesis
    latest_by_stream = {}
    plugin_globals["get_chat_manager"] = lambda: SimpleNamespace(last_messages=latest_by_stream)
    command_ids = count()

    def command(cfg, chat, text="你好", message_id=None):
        obj = object.__new__(command_cls)
        obj.matched_groups = re.match(obj.command_pattern, f"/breeze {text}").groupdict()
        obj.plugin_config = cfg
        if message_id is None:
            message_id = f"command-{next(command_ids)}"
        obj.message = SimpleNamespace(chat_stream=chat,
                                      message_info=SimpleNamespace(message_id=message_id,
                                                                   platform=chat.platform,
                                                                   group_info=chat.group_info))
        obj.send_custom = send_custom
        obj.send_text = send_text
        return obj

    def action(cfg, chat, message_id="msg-1", request="请用语音回答我", answer="你好"):
        obj = object.__new__(action_cls)
        obj.action_data = {"text": answer}
        obj.plugin_config = cfg
        obj.chat_stream = chat
        obj.action_message = SimpleNamespace(processed_plain_text=request, message_id=message_id,
                                             time=time.time(), chat_id=chat.stream_id,
                                             user_info=SimpleNamespace(user_id="a-test-user"),
                                             chat_info=SimpleNamespace(platform=chat.platform,
                                                                       group_info=chat.group_info))
        latest_by_stream[chat.stream_id] = SimpleNamespace(
            message_info=SimpleNamespace(message_id=message_id, platform=chat.platform,
                                         user_info=SimpleNamespace(user_id="a-test-user")))
        obj.send_custom = send_custom
        obj.send_text = send_text
        obj.store_action_info = store_action_info
        return obj

    def reset_db(tmp, name):
        plugin_globals["_GUARD_DB"] = Path(tmp) / name / "usage.sqlite3"
        sent.clear()
        synthesized.clear()

    with tempfile.TemporaryDirectory() as tmp:
        # The slash command still works in explicitly authorized QQ groups.
        reset_db(tmp, "command")
        cmd = command(config(), stream())
        assert (await cmd.execute())[0]
        assert [kind for kind, _ in sent] == ["voice"]
        assert sent[0][1]["message_type"] == "voice"
        assert base64.b64decode(sent[0][1]["content"]) == wav
        assert synthesized == ["你好"]
        # Retried delivery of the same message ID must not trigger another call.
        assert not (await cmd.execute())[0]
        assert len(synthesized) == 1

        # No message ID is not sufficient to safely deduplicate a paid request.
        reset_db(tmp, "no-id")
        no_id = command(config(), stream(), message_id="")
        assert not (await no_id.execute())[0]
        assert not synthesized

        # Bare QQ allowlist must not accidentally authorize another platform.
        reset_db(tmp, "scope")
        denied = command(config(groups=["123"]), stream(platform="discord"))
        assert not (await denied.execute())[0]
        assert not synthesized and all(kind != "voice" for kind, _ in sent)
        sent.clear()
        allowed = command(config(groups=["123"]), stream(platform="qq"))
        assert (await allowed.execute())[0]
        assert len(synthesized) == 1

        # Private chats require their own opt-in; no group allowlist bypass.
        reset_db(tmp, "private-denied")
        assert not (await command(config(), stream(group=None)).execute())[0]
        assert not synthesized and all(kind != "voice" for kind, _ in sent)
        reset_db(tmp, "private-allowed")
        assert (await command(config(allow_private=True), stream(group=None)).execute())[0]
        assert len(synthesized) == 1

        # Incoming group metadata must agree with the resolved stream. A
        # malformed group message must never inherit private-chat permission.
        reset_db(tmp, "group-spoofed-private")
        spoof = command(config(allow_private=True), stream(group=None))
        spoof.message.message_info.group_info = SimpleNamespace(group_id="123")
        assert not (await spoof.execute())[0]
        assert not synthesized and all(kind != "voice" for kind, _ in sent)
        reset_db(tmp, "different-command-group")
        mismatch = command(config(groups=["qq:123"]), stream(group="123"))
        mismatch.message.message_info.group_info = SimpleNamespace(group_id="456")
        assert not (await mismatch.execute())[0]
        assert not synthesized and all(kind != "voice" for kind, _ in sent)
        reset_db(tmp, "missing-command-info")
        missing = command(config(allow_private=True), stream(group=None))
        del missing.message.message_info.group_info
        assert not (await missing.execute())[0]
        assert not synthesized and all(kind != "voice" for kind, _ in sent)
        reset_db(tmp, "different-command-platform")
        mismatch = command(config(groups=["qq:123"]), stream(group="123"))
        mismatch.message.message_info.platform = "discord"
        assert not (await mismatch.execute())[0]
        assert not synthesized and all(kind != "voice" for kind, _ in sent)

        # Planner Action sends text first, then voice, once for an explicit request.
        reset_db(tmp, "action")
        obj = action(config(), stream())
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["text", "voice", "stored"]
        assert sent[0][1] == "你好" and synthesized == ["你好"]
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["text", "voice", "stored"]

        # An expired/non-explicit planner target produces text, not chargeable audio.
        reset_db(tmp, "no-intent")
        obj = action(config(), stream(), request="这段语音真有趣")
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["text", "stored"] and not synthesized
        reset_db(tmp, "action-group-mismatch")
        chat = stream()
        obj = action(config(), chat)
        obj.action_message.chat_info = SimpleNamespace(group_info=SimpleNamespace(group_id="456"))
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["text", "stored"] and not synthesized
        reset_db(tmp, "action-platform-mismatch")
        chat = stream()
        obj = action(config(), chat)
        obj.action_message.chat_info.platform = "discord"
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["text", "stored"] and not synthesized
        reset_db(tmp, "expired")
        obj = action(config(), stream())
        obj.action_message.time = time.time() - 3600
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["text", "stored"] and not synthesized

        # A newer message, a different sender with the same ID, a mismatched
        # platform, or missing live context must not trigger paid speech.
        reset_db(tmp, "newer")
        chat = stream()
        obj = action(config(), chat)
        latest_by_stream[chat.stream_id].message_info.message_id = "newer-message"
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["text", "stored"] and not synthesized
        reset_db(tmp, "different-sender")
        obj = action(config(), chat)
        latest_by_stream[chat.stream_id].message_info.user_info.user_id = "another-user"
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["text", "stored"] and not synthesized
        reset_db(tmp, "different-platform")
        obj = action(config(), chat)
        latest_by_stream[chat.stream_id].message_info.platform = "discord"
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["text", "stored"] and not synthesized
        reset_db(tmp, "no-live-context")
        obj = action(config(), chat)
        latest_by_stream.clear()
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["text", "stored"] and not synthesized
        reset_db(tmp, "own-telegram")
        chat = stream(platform="telegram")
        obj = action(config(groups=["telegram:123"]), chat)
        checked = []
        original_is_bot_self = plugin_globals["is_bot_self"]
        def own_telegram_account(platform, user_id):
            checked.append((platform, user_id))
            return platform == "telegram" and user_id == "a-test-user"
        plugin_globals["is_bot_self"] = own_telegram_account
        try:
            assert (await obj.execute())[0]
        finally:
            plugin_globals["is_bot_self"] = original_is_bot_self
        assert checked == [("telegram", "a-test-user")]
        assert [kind for kind, _ in sent] == ["text", "stored"] and not synthesized

        # TTS timeout/error or delivery failure never drops or duplicates text.
        reset_db(tmp, "timeout")
        async def timeout(*args):
            raise asyncio.TimeoutError()
        plugin_globals["synthesize"] = timeout
        assert (await action(config(), stream()).execute())[0]
        assert [kind for kind, _ in sent] == ["text", "stored"]
        assert sent[0][1] == "你好"
        reset_db(tmp, "invalid")
        async def invalid(*args):
            return b"invalid"
        plugin_globals["synthesize"] = invalid
        assert (await action(config(), stream()).execute())[0]
        assert [kind for kind, _ in sent] == ["text", "stored"]
        reset_db(tmp, "delivery")
        plugin_globals["synthesize"] = synthesis
        obj = action(config(), stream())
        async def fail_send(**kw):
            sent.append(("failed_voice", kw))
            return False
        obj.send_custom = fail_send
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["text", "failed_voice", "stored"]
        reset_db(tmp, "text-delivery")
        obj = action(config(), stream())
        async def fail_text(text):
            sent.append(("failed_text", text))
            return False
        obj.send_text = fail_text
        assert not (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["failed_text"] and not synthesized

        # Rejected scope and exhausted quota retain text; no cloud call is made.
        reset_db(tmp, "action-scope")
        assert (await action(config(groups=[]), stream()).execute())[0]
        assert [kind for kind, _ in sent] == ["text", "stored"] and not synthesized
        reset_db(tmp, "daily")
        assert (await command(config(daily=1), stream()).execute())[0]
        sent.clear()
        assert (await action(config(daily=1), stream(), message_id="later").execute())[0]
        assert [kind for kind, _ in sent] == ["text", "stored"]
        assert synthesized == ["你好"]

        # Shared daily quota remains atomic for concurrent commands in two groups.
        reset_db(tmp, "concurrent")
        cfg = config(groups=["qq:123", "qq:456"], daily=1)
        results = await asyncio.gather(command(cfg, stream("123")).execute(),
                                       command(cfg, stream("456")).execute())
        assert sum(bool(item[0]) for item in results) == 1
        assert len(synthesized) == 1

        # Zero daily limit means unlimited, while cooldown still applies.
        reset_db(tmp, "unlimited")
        cfg = config(daily=0)
        for _ in range(3):
            assert (await command(cfg, stream()).execute())[0]
        assert len(synthesized) == 3
        reset_db(tmp, "cooldown")
        cfg = config(cooldown=60, daily=0)
        assert (await command(cfg, stream()).execute())[0]
        assert not (await command(cfg, stream()).execute())[0]
        assert len(synthesized) == 1

    print("PASS: MaiBot 0.12.2 plugin loading, WebUI schema, scope, concurrent limits, command and text fallback")


check_old_config_migration()
asyncio.run(main())
