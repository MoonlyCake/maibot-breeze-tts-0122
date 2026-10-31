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
from unittest.mock import AsyncMock, Mock, patch
import wave

import toml
from maim_message import Seg
from src.chat.message_receive.message import MessageRecv
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
assert {"natural_enabled", "allowed_groups", "allow_private", "cooldown_seconds", "daily_max_requests", "admin_users"} <= set(schema["sections"]["guard"]["fields"])

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
        assert migrated["plugin"]["config_version"] == "1.4.0"
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


def config(groups=None, cooldown=0, daily=20, allow_private=False, admins=None):
    return {"guard": {"allowed_groups": groups if groups is not None else ["qq:123"],
                      "allow_private": allow_private, "cooldown_seconds": cooldown,
                      "daily_max_requests": daily, "natural_enabled": True,
                      "admin_users": admins or []}}


async def main():
    sent = []
    synthesized = []
    generated = []

    async def generate_reply(**kwargs):
        # No real model is called: ensure the host receives the question as a
        # target message, not the command or a fabricated history entry.
        target = kwargs["reply_message"]
        assert kwargs["enable_tool"] is False
        assert kwargs["chat_stream"].stream_id == target.chat_id
        generated.append(target.processed_plain_text)
        return True, SimpleNamespace(processed_output=["你好"], reply_set=None)

    plugin_globals["generator_api"].generate_reply = AsyncMock(side_effect=generate_reply)

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

    def command(cfg, chat, text="你好", message_id=None, sender="a-test-user", segment=None):
        obj = object.__new__(command_cls)
        obj.matched_groups = re.match(obj.command_pattern, f"/breeze {text}").groupdict()
        obj.plugin_config = cfg
        if message_id is None:
            message_id = f"command-{next(command_ids)}"
        obj.message = SimpleNamespace(chat_stream=chat,
                                      message_segment=segment or Seg(type="text", data=f"/breeze {text}"),
                                      message_info=SimpleNamespace(message_id=message_id,
                                                                   platform=chat.platform,
                                                                   group_info=chat.group_info,
                                                                   time=time.time(),
                                                                   user_info=SimpleNamespace(user_id=sender,
                                                                                             user_nickname="tester",
                                                                                             platform=chat.platform)))
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
            message_segment=Seg(type="text", data=request),
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
        generated.clear()

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
        assert (await cmd.execute())[0]
        assert len(synthesized) == 1
        assert generated == ["你好"]

        # A command asks the actual replyer a question and speaks its answer;
        # it never simply speaks the user's original question.
        reset_db(tmp, "answer-not-question")
        cmd = command(config(), stream(), text="你在做什么？")
        assert (await cmd.execute())[0]
        assert generated == ["你在做什么？"] and synthesized == ["你好"]
        assert [kind for kind, _ in sent] == ["voice"]

        reset_db(tmp, "reply-failed")
        original_generator = plugin_globals["generator_api"].generate_reply
        plugin_globals["generator_api"].generate_reply = AsyncMock(return_value=(False, None))
        try:
            assert not (await command(config(daily=1), stream(), text="你在做什么？").execute())[0]
            assert not synthesized and len(sent) == 1 and sent[0][0] == "text"
            # The reservation is deliberately not refunded on a failed model
            # call; the next capped request must not call the model again.
            assert not (await command(config(daily=1), stream(), text="再试一次").execute())[0]
            assert plugin_globals["generator_api"].generate_reply.await_count == 1
        finally:
            plugin_globals["generator_api"].generate_reply = original_generator

        reset_db(tmp, "command-synthesis-failed")
        async def failing_synthesis(*args):
            raise asyncio.TimeoutError()
        plugin_globals["synthesize"] = failing_synthesis
        try:
            assert (await command(config(), stream()).execute())[0]
            assert sent == [("text", "你好")]
        finally:
            plugin_globals["synthesize"] = synthesis

        reset_db(tmp, "command-delivery-failed")
        cmd = command(config(), stream())
        async def command_send_failed(**kwargs):
            sent.append(("failed_voice", kwargs))
            return False
        cmd.send_custom = command_send_failed
        assert (await cmd.execute())[0]
        assert [kind for kind, _ in sent] == ["failed_voice", "text"]

        reset_db(tmp, "command-delivery-unknown")
        cmd = command(config(), stream())
        async def command_send_unknown(**kwargs):
            sent.append(("unknown_voice", kwargs))
            raise TimeoutError()
        cmd.send_custom = command_send_unknown
        assert (await cmd.execute())[0]
        assert [kind for kind, _ in sent] == ["unknown_voice"]

        # Exercise real 0.12.2 Seg preprocessing: bot mention becomes leading
        # whitespace, while an at-other is stripped by the host as well.
        receiver = object.__new__(MessageRecv)
        receiver.is_picid = receiver.is_emoji = receiver.is_voice = False
        mentioned = Seg(type="seglist", data=[Seg(type="mention_bot", data=1.0),
                                               Seg(type="text", data=" /breeze 你好")])
        processed = await receiver._process_message_segments(mentioned)
        assert processed.startswith(" ") and re.match(command_cls.command_pattern, processed)
        reset_db(tmp, "real-mention-bot")
        cmd = command(config(), stream(), segment=mentioned)
        cmd.matched_groups = re.match(cmd.command_pattern, processed).groupdict()
        assert (await cmd.execute())[0] and synthesized == ["你好"]

        original_is_bot_self = plugin_globals["is_bot_self"]
        plugin_globals["is_bot_self"] = lambda platform, sender: platform == "qq" and sender == "9999"
        try:
            other = Seg(type="seglist", data=[Seg(type="at", data={"target_user_id": "8888"}),
                                             Seg(type="text", data=" /breeze 你好")])
            processed = await receiver._process_message_segments(other)
            assert re.match(command_cls.command_pattern, processed)
            reset_db(tmp, "real-mention-other")
            cmd = command(config(), stream(), segment=other)
            cmd.matched_groups = re.match(cmd.command_pattern, processed).groupdict()
            assert (await cmd.execute())[2] == 0 and not synthesized and not generated

            own = Seg(type="seglist", data=[Seg(type="at", data={"target_user_id": "9999"}),
                                           Seg(type="text", data=" /breeze 你好")])
            processed = await receiver._process_message_segments(own)
            reset_db(tmp, "real-mention-self")
            cmd = command(config(), stream(), segment=own)
            cmd.matched_groups = re.match(cmd.command_pattern, processed).groupdict()
            assert (await cmd.execute())[0] and synthesized == ["你好"]

            unknown = Seg(type="seglist", data=[Seg(type="at", data={}),
                                               Seg(type="text", data=" /breeze 你好")])
            reset_db(tmp, "real-mention-unknown")
            assert (await command(config(), stream(), segment=unknown).execute())[2] == 0
            assert not synthesized and not generated
        finally:
            plugin_globals["is_bot_self"] = original_is_bot_self

        # The processed description of an image or voice may contain command
        # text, but only a pure text command may trigger paid operations.
        reset_db(tmp, "vlm-injected-command")
        image_injection = Seg(type="seglist", data=[Seg(type="image", data="mock-image"),
                                                    Seg(type="text", data="normal message")])
        cmd = command(config(), stream(), segment=image_injection)
        assert (await cmd.execute())[2] == 0
        assert not generated and not synthesized and not sent
        assert not plugin_globals["_GUARD_DB"].exists()
        reset_db(tmp, "mixed-attachment-command")
        mixed = Seg(type="seglist", data=[Seg(type="image", data="mock-image"),
                                          Seg(type="text", data="/breeze 你好")])
        cmd = command(config(), stream(), segment=mixed)
        assert (await cmd.execute())[2] == 0
        assert not generated and not synthesized and not sent
        assert not plugin_globals["_GUARD_DB"].exists()
        reset_db(tmp, "mismatched-raw-command")
        cmd = command(config(), stream(), segment=Seg(type="text", data="/breeze 别的问题"))
        assert (await cmd.execute())[2] == 0
        assert not generated and not synthesized and not sent
        assert not plugin_globals["_GUARD_DB"].exists()

        # Text that merely contains a command remains ordinary conversation.
        assert re.match(command_cls.command_pattern, "讨论 /breeze 你好") is None

        # Execute through the real 0.12.2 ChatBot routing function: it marks a
        # regex match as is_command before invoking the plugin. A pass-through
        # case must undo that mark so ordinary chat handling is not suppressed.
        from src.chat.message_receive import bot as host_bot
        host = object.__new__(host_bot.ChatBot)
        info = SimpleNamespace(plugin_name="breeze_tts_0122", name="breeze_tts_command")

        def find_test_command(text):
            match = re.match(command_cls.command_pattern, text)
            return (command_cls, match.groupdict(), info) if match else None

        def real_incoming(message_id, segment):
            msg = MessageRecv({"message_info": {"platform": "qq", "message_id": message_id,
                                                "time": time.time(),
                                                "user_info": {"platform": "qq", "user_id": "271828", "user_nickname": "tester"},
                                                "group_info": {"group_id": "123", "group_name": "test"}},
                               "message_segment": segment.to_dict()})
            msg.update_chat_stream(stream())
            return msg

        with patch.object(host_bot.component_registry, "find_command_by_text", side_effect=find_test_command), \
             patch.object(host_bot.component_registry, "get_plugin_config", return_value=config()), \
             patch.object(host_bot.global_announcement_manager, "get_disabled_chat_commands", return_value=[]), \
             patch.dict(plugin_globals, {"is_bot_self": lambda platform, sender: platform == "qq" and sender == "9999"}):
            for name, segment, forged_processed in (
                ("host-at-other", Seg(type="seglist", data=[Seg(type="at", data={"target_user_id": "8888"}),
                                                           Seg(type="text", data=" /breeze 你好")]), None),
                ("host-image", Seg(type="image", data="mock-image"), "/breeze 你好"),
                ("host-voice", Seg(type="voice", data="mock-voice"), "/breeze 你好"),
                ("host-inconsistent-text", Seg(type="text", data="/breeze 不同的问题"), "/breeze 你好"),
            ):
                reset_db(tmp, name)
                msg = real_incoming(name, segment)
                if forged_processed is None:
                    await msg.process()
                else:
                    msg.processed_plain_text = forged_processed
                assert not msg.is_command
                found, _, continue_processing = await host._process_commands(msg)
                assert found and continue_processing
                assert msg.is_command is False and msg.intercept_message_level == 0
                assert not synthesized and not generated and not sent
                assert not plugin_globals["_GUARD_DB"].exists()

            # Unknown chat scope also returns the message to normal handling.
            reset_db(tmp, "host-missing-scope")
            msg = real_incoming("host-missing-scope", Seg(type="text", data="/breeze 你好"))
            await msg.process()
            msg.chat_stream.platform = "discord"
            found, _, continue_processing = await host._process_commands(msg)
            assert found and continue_processing
            assert msg.is_command is False and msg.intercept_message_level == 0
            assert not synthesized and not generated and not sent

        # Admins bypass cooldown/daily budget, not scope or message dedup;
        # their requests do not consume ordinary users' quota.
        reset_db(tmp, "admin-exemption")
        cfg = config(cooldown=60, daily=1, admins=["314159"])
        normal = command(cfg, stream(), sender="271828")
        assert (await normal.execute())[0] and len(synthesized) == 1
        admin = command(cfg, stream(), sender="314159", message_id="admin-1")
        assert (await admin.execute())[0] and len(synthesized) == 2
        assert (await admin.execute())[0] and len(synthesized) == 2
        assert (await command(cfg, stream(), sender="314159", message_id="admin-2").execute())[0]
        assert len(synthesized) == 3
        assert not (await command(cfg, stream(), sender="271828", message_id="normal-2").execute())[0]
        assert len(synthesized) == 3 and sent[-1][0] == "text"
        assert generated == ["你好", "你好", "你好"]
        reset_db(tmp, "admin-out-of-scope")
        assert not (await command(config(groups=[], admins=["314159"]), stream(), sender="314159").execute())[0]
        assert not synthesized and not generated
        reset_db(tmp, "admin-platform-mismatch")
        cfg = config(daily=1, admins=["314159"])
        assert (await command(cfg, stream(), sender="271828").execute())[0]
        spoof = command(cfg, stream(), sender="314159")
        spoof.message.message_info.user_info.platform = "discord"
        assert not (await spoof.execute())[0]
        assert len(synthesized) == 1 and sent[-1][0] == "text"
        assert generated == ["你好"]

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

        # Planner Action sends only voice on success, once for an explicit request.
        reset_db(tmp, "action")
        obj = action(config(), stream())
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["voice", "stored"]
        assert synthesized == ["你好"]
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["voice", "stored"]

        # An expired/non-explicit planner target produces text, not chargeable audio.
        for kind in ("image", "voice"):
            reset_db(tmp, f"natural-{kind}-injection")
            chat = stream()
            obj = action(config(), chat)
            # The database's processed text might be synthesized by a vision
            # model or speech recognizer; raw attachment isn't user text.
            latest_by_stream[chat.stream_id].message_segment = Seg(type=kind, data="mock-attachment")
            assert (await obj.execute())[0]
            assert [tag for tag, _ in sent] == ["text", "stored"] and not synthesized
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
        assert [kind for kind, _ in sent] == ["failed_voice", "text", "stored"]
        # A send exception leaves delivery unknown; do not blindly duplicate it.
        reset_db(tmp, "delivery-unknown")
        obj = action(config(), stream())
        async def unknown_send(**kw):
            sent.append(("unknown_voice", kw))
            raise RuntimeError("delivery status unavailable")
        obj.send_custom = unknown_send
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["unknown_voice", "stored"]
        reset_db(tmp, "text-delivery")
        obj = action(config(), stream())
        async def fail_text(text):
            sent.append(("failed_text", text))
            return False
        plugin_globals["synthesize"] = timeout
        obj.send_text = fail_text
        assert not (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["failed_text"] and not synthesized
        plugin_globals["synthesize"] = synthesis

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

        # Action-admin identity comes from its verified message, not the LLM.
        reset_db(tmp, "action-admin")
        cfg = config(daily=1, cooldown=60, admins=["qq:a-test-user"])
        assert (await command(cfg, stream(), sender="271828").execute())[0]
        sent.clear()
        obj = action(cfg, stream(), message_id="admin-action")
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["voice", "stored"]
        assert (await obj.execute())[0]
        assert [kind for kind, _ in sent] == ["voice", "stored"]

        # Shared daily quota remains atomic for concurrent commands in two groups.
        reset_db(tmp, "concurrent")
        cfg = config(groups=["qq:123", "qq:456"], daily=1)
        results = await asyncio.gather(command(cfg, stream("123")).execute(),
                                       command(cfg, stream("456")).execute())
        assert sum(kind == "voice" for kind, _ in sent) == 1
        assert sum(kind == "text" for kind, _ in sent) == 1
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
        assert sum(kind == "text" for kind, _ in sent) == 1
        assert len(synthesized) == 1

    print("PASS: MaiBot 0.12.2 plugin loading, WebUI schema, scope, concurrent limits, command and text fallback")


async def check_random_host():
    from src.plugin_system.base.component_types import EventType, MaiMessages
    from src.plugin_system.core.events_manager import events_manager

    cfg = config(groups=["qq:123"], cooldown=0, daily=0, admins=["qq:admin"])
    cfg["breeze"] = {"random_enabled": True, "random_probability": 1.0}
    cfg["guard"].update(random_daily_limit_enabled=True, random_daily_max_requests=1)
    instance.config = cfg
    components = {info.name: cls for info, cls in instance.get_plugin_components()}
    started = components["breeze_random_started"]()
    generated = components["breeze_random_generated"]()
    outgoing = components["breeze_random_send"]()
    for handler in (started, generated, outgoing):
        handler.set_plugin_config(cfg)
        handler.set_plugin_name("breeze_tts_0122")
    globals_ = outgoing.execute.__globals__
    targets = {}
    globals_["message_api"].get_messages_by_time_in_chat = lambda **kw: list(targets.values())
    globals_["is_bot_self"] = lambda platform, user: False
    globals_["synthesize"] = AsyncMock(return_value=wav)
    with tempfile.TemporaryDirectory() as temp:
        globals_["_GUARD_DB"] = Path(temp) / "guard" / "usage.sqlite3"

        async def attempt(mid, sender="someone", *, quoted=True, content="你好", run_started=True, request="嗨"):
            targets[mid] = SimpleNamespace(message_id=mid, chat_id="qq-123", processed_plain_text=request,
                user_info=SimpleNamespace(user_id=sender),
                chat_info=SimpleNamespace(platform="qq", group_info=SimpleNamespace(group_id="123")))
            intro = MaiMessages(stream_id="qq-123", llm_prompt="prompt")
            llm = MaiMessages(stream_id="qq-123", llm_prompt="prompt", llm_response_content=content)
            segments = [Seg(type="reply", data=mid), Seg(type="text", data=content)] if quoted else [Seg(type="text", data=content)]
            event = MaiMessages(stream_id="qq-123", is_group_message=True, message_segments=segments,
                                message_base_info={"platform": "qq", "group_id": "123"})
            if run_started:
                await events_manager._dispatch_intercepting_handler_task(started, EventType.POST_LLM, intro)
            await events_manager._dispatch_intercepting_handler_task(generated, EventType.AFTER_LLM, llm)
            return await events_manager._dispatch_intercepting_handler_task(outgoing, EventType.POST_SEND, event)

        _, result = await attempt("first")
        assert result and [s.type for s in result.message_segments] == ["reply", "voice"]
        _, result = await attempt("second")
        assert result is None  # per-group random budget exhausted
        _, result = await attempt("admin", sender="admin")
        assert result and result.message_segments[-1].type == "voice"  # exemption
        _, result = await attempt("unquoted", sender="admin", quoted=False)
        assert result is None  # no verifiable target
        _, result = await attempt("not-model", run_started=False)
        assert result is None  # other plugin/command sends are untouched
        globals_["_GUARD_DB"] = Path(temp) / "failure" / "usage.sqlite3"
        _, result = await attempt("explicit", request="请用语音回答我")
        assert result is None  # an explicit voice request is not random
        _, result = await attempt("command", request="/breeze 你好")
        assert result is None
        for index, denied in enumerate(("请只用文字回答", "打字回答", "text only", "type only", "不要语音")):
            _, result = await attempt(f"denied-{index}", request=denied)
            assert result is None
        globals_["synthesize"] = AsyncMock(side_effect=ValueError("mock failure"))
        _, result = await attempt("failed")
        assert result is None  # synthesis failure leaves original text intact
        globals_["synthesize"] = AsyncMock(return_value=wav)
        _, result = await attempt("after-failure")
        assert result and result.message_segments[-1].type == "voice"  # failure did not consume random quota
        globals_["synthesize"].assert_awaited()

        # A cancelled DB await can still commit in its worker thread; cleanup
        # must release that reservation after it finishes.
        reserve_started, reserve_continue, released = asyncio.Event(), asyncio.Event(), asyncio.Event()
        real_reserve, real_finish = globals_["reserve_usage"], globals_["finish_random_usage"]
        async def delayed_reserve(*args, **kwargs):
            reserve_started.set()
            await reserve_continue.wait()
            return True, "ok"
        async def checked_finish(*args, **kwargs):
            assert kwargs["success"] is False
            released.set()
            return True, "ok"
        globals_["reserve_usage"], globals_["finish_random_usage"] = delayed_reserve, checked_finish
        pending = asyncio.create_task(attempt("cancel-reservation"))
        await asyncio.wait_for(reserve_started.wait(), 2)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        reserve_continue.set()
        await asyncio.wait_for(released.wait(), 2)
        globals_["reserve_usage"], globals_["finish_random_usage"] = real_reserve, real_finish

        globals_["_GUARD_DB"] = Path(temp) / "cancel-synthesis" / "usage.sqlite3"
        synth_started = asyncio.Event()
        async def stalled_synthesis(*args, **kwargs):
            synth_started.set()
            await asyncio.Event().wait()
        globals_["synthesize"] = stalled_synthesis
        pending = asyncio.create_task(attempt("cancel-synthesis"))
        await asyncio.wait_for(synth_started.wait(), 2)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        globals_["synthesize"] = AsyncMock(return_value=wav)
        _, result = await attempt("after-cancel")
        assert result and result.message_segments[-1].type == "voice"
    print("PASS: 0.12.2 random reply host event, scope, admin and quota")


check_old_config_migration()
asyncio.run(main())
asyncio.run(check_random_host())
