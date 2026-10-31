<div align="center">

# 🌬️ Breeze TTS 语音插件

**让麦麦开口说话吧！(๑•̀ㅂ•́)و✧**

[![MaiBot 0.12.2](https://img.shields.io/badge/MaiBot-0.12.2-ff9ec7)](https://github.com/MaiM-with-u/MaiBot)
[![Version](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2FMoonlyCake%2Fmaibot-breeze-tts-0122%2Fmain%2F_manifest.json&query=%24.version&label=version&color=a0d8ef)](_manifest.json)
[![License: MIT](https://img.shields.io/badge/license-MIT-c3e88d)](LICENSE)

</div>

把 [BreezeBlue](https://breezeblue.ai/) 云端语音或 [Breeze TTS 2 自托管服务](https://github.com/breezeblue-ai/breeze-tts)接入麦麦。发送 `/breeze 你的问题` 后，麦麦结合当前聊天与人设生成回答并尝试语音回复；开启自然语音后，明确要求语音时也可由麦麦选择回复。

## 🎀 麦麦学会了什么

- 🗣️ **语音问答**：`/breeze 你在做什么？`，先生成回答，再发送语音
- 💬 **按需语音回复**：聊天时明确要求语音，麦麦可以选择用语音回答
- ☁️ **两种声音来源**：BreezeBlue 云端开箱即用，也可以用自己运行的模型服务
- 🛡️ **贴心的小闸门**：聊天范围、会话冷却、每日上限都能管，不怕额度被悄悄吃光

> [!NOTE]
> 本仓库针对 **MaiBot 0.12.2**，请使用支持发送语音的适配器。插件不包含模型权重，也不会自动启动语音服务。

> [!IMPORTANT]
> **从旧版本升级？** 先安全备份旧配置，配置及其备份可能含明文密钥。范围限制默认拒绝所有群聊和私聊，升级后要先在 WebUI 的范围设置中填写允许的群号或启用私聊，否则麦麦会一直沉默哦。自然语音也需要单独开启。

## 🚀 快速开始：BreezeBlue 云端

**第 1 步：把插件带回家。** 在麦麦根目录执行：

```bash
git clone https://github.com/MoonlyCake/maibot-breeze-tts-0122.git plugins/breeze_tts_0122
```

如果麦麦的 Python 环境里还没有 `aiohttp`，再装一下：

```bash
python -m pip install aiohttp
```

**第 2 步：领钥匙、挑声音。** 登录 BreezeBlue，在[开发者页面](https://breezeblue.ai/app/developer/api-keys)创建 API Key，然后选一个音色并记下它的 Voice ID。可以先试听官网的这两位，也可以用自己保存的音色（参见[官方音色说明](https://docs.breezeblue.ai/concepts/voices)）：

| 音色 | Voice ID |
| --- | --- |
| [🦊 机灵版](https://breezeblue.ai/zh/voice-library/dafeiyu-clever-girl-voice-vu3nxkd5578wc) | `voc_u3nxkd5578wc` |
| [☁️ 迷糊版](https://breezeblue.ai/zh/voice-library/dafeiyu-dreamy-girl-voice-v8yud7hz7c9q2) | `voc_8yud7hz7c9q2` |

**第 3 步：填配置。** 启动麦麦生成插件配置并启用插件，在 WebUI 打开 **插件配置 → Breeze TTS for MaiBot 0.12.2 → 可视化**，或者编辑 `plugins/breeze_tts_0122/config.toml`。云端模式的关键部分长这样：

```toml
[breeze]
backend = "cloud"
api_url = ""                        # 留空使用默认地址
api_key = ""                        # 推荐留空，改用环境变量 BREEZE_API_KEY
voice_id = "填写你的音色 ID"
model = ""                          # 留空使用默认模型
language_code = "zh"

[guard]
natural_enabled = false             # 想要自然语音时改成 true
allowed_groups = ["qq:替换为目标群号"]
allow_private = false               # 想在私聊使用时改成 true
admin_users = []                    # 自行填管理员 QQ，免冷却和每日次数限制
cooldown_seconds = 60
daily_max_requests = 0              # 0 表示不限制
```

> 💡 插件要先加载，WebUI 才能取得完整的可视化字段。云端和自托管的字段都会显示，按所选后端填写就好。

**第 4 步：打个招呼。** 在 WebUI 保存后重载插件或重启麦麦；如果改的是 `config.toml`，请重启麦麦。然后在允许测试的聊天里发送：

```text
/breeze 你在做什么？
```

听到声音就成功啦 ٩(ˊᗜˋ*)و 如果没听到，可以分别确认语音生成、消息送达和完整播放这三步。

### 💰 额度与密钥，请认真看一眼

- **费用**：云端生成会消耗 BreezeBlue 账户额度，网站和 API 共用账户余额。价格及免费额度以[官网当前说明](https://docs.breezeblue.ai/concepts/pricing)为准。
- **用量**：插件默认不限制每日语音请求次数，可以在 WebUI 中设置每日上限，普通用户的所有聊天和触发方式共享。BreezeBlue 账户及密钥本身的额度限制另行计算。
- **密钥**：建议在麦麦的运行环境中设置 `BREEZE_API_KEY`，并将配置中的 `api_key` 留空。密码框只遮挡界面显示，不能保证配置文件、备份或配置接口不含明文。不要上传含密钥的配置，也不要在聊天中发送密钥。

## 🎙️ 怎么让麦麦说话

**语音问答**

发送 `/breeze 你的问题`，麦麦按当前聊天上下文与人设回答，再将回答发成语音；也支持先 @ 机器人再发命令。命令须为文字消息，图片、音频和转发消息不能作为命令来源。只发送 `/breeze` 或 `/breeze help` 可查看帮助。

**按需语音回复**

开启 `action_enabled` 和 `guard.natural_enabled` 后，可以试试这样说：

> 用语音回答我 ／ 读给我听

插件会检查当前会话最新、120 秒内用户消息的明确文字意图，“不要语音”这样的话不会触发；图片描述和语音转写不会单独触发。不过最后还是麦麦自己决定要不要选择语音 Action，不保证每次都开口。

宿主报告语音发送成功时只发语音；合成失败或宿主返回失败时发送文字回复。传输中断时，实际送达状态可能与宿主回执不一致，不能保证完全避免重复。

> [!TIP]
> 请避免同时启用会对同一条回复生成语音的其他 TTS 插件，不然麦麦会把话说两遍。能否成功发出和播放语音，还取决于麦麦适配器与目标平台。

## 🛡️ 小闸门是怎么工作的

所有语音路径都受会话范围限制；普通用户同时受冷却和每日次数限制：

- **范围**：群号白名单支持 `qq:123456789` 格式；裸数字只匹配 QQ 群，其他平台需填写 `平台:群号`。无法确定平台的会话一律拒绝。
- **冷却**：按会话计算；配置的管理员豁免冷却与每日次数限制，管理员请求不计入普通用户额度，也不改变普通用户的冷却。
- **每日次数**：由普通用户的所有群、私聊和触发方式共享，按宿主机器的本地日期重置。
- **计数规则**：限额在生成回答或调用语音服务前预占，生成失败也计数。每日上限关闭时仍记录调用，当天之后开启上限时会计入此前调用。
- **记录位置**：插件目录的 `voice_guard_data/usage.sqlite3`，重启后保留，多个独立插件副本不共享。

这些限制不能替代 BreezeBlue 平台或 API Key 自身的额度限制。

## ⚙️ 配置项一览

下面按 WebUI 的三个标签页分组，括号里是 `config.toml` 中的小节名。

### 基本设置（`[plugin]`、`[general]`）

| 字段 | 用途 |
| --- | --- |
| `plugin.enabled` | 启用插件，默认开启。 |
| `action_enabled`、`command_enabled` | 分别启用语音 Action 和 `/breeze` 命令；自然语音还需要开启 `guard.natural_enabled`。 |
| `max_text_length` | 单次合成的文字长度上限，默认 500，可设为 1–5000。 |
| `send_errors` | 控制是否在聊天中显示合成或发送失败提示。 |

### 语音服务（`[breeze]`）

| 字段 | 用途 |
| --- | --- |
| `backend` | `self_hosted` 为自托管（默认），`cloud` 为 BreezeBlue 云端。 |
| `api_url` | 服务基础地址。留空时分别使用 `http://127.0.0.1:7860` 和 `https://api.breeze.blue`。 |
| `api_key` | 云端 API Key；留空时读取环境变量 `BREEZE_API_KEY`。自托管不用填写。 |
| `voice_id` | 云端必填的音色 ID。 |
| `model`、`language_code` | 可选云端模型和语言代码；留空由服务决定。 |
| `instruction` | 可选的语气、情绪或说话方式描述，比如“用自然、温柔的语气说话”。 |
| `ref_audio`、`ref_text` | 自托管参考音频的本机路径，以及其中实际说出的文字；两者必须同时填写，也可以同时留空。 |
| `cfg_scale` | 声音指导强度；自托管要求大于 0，云端范围 1–10。 |
| `seed` | 自托管采样种子；`-1` 表示不传该参数，使用服务端默认值。 |
| `timeout` | 单次请求超时秒数，默认 120。 |

### 范围与用量（`[guard]`）

| 字段 | 用途 |
| --- | --- |
| `natural_enabled` | 自然语言语音回复开关，默认关闭；开启后仍由麦麦决定是否使用语音 Action。 |
| `allowed_groups`、`allow_private` | 允许使用语音的群号列表及私聊开关；默认群列表为空、私聊关闭，即默认不允许任何会话调用。开启后允许各平台、具有效平台信息的私聊。 |
| `admin_users` | 管理员账号列表，默认空；QQ 可填纯号码，其他平台使用 `平台:用户ID`。仅豁免冷却和每日次数，不绕过会话范围、去重或平台额度。 |
| `cooldown_seconds` | 每个会话的语音请求冷却时间，默认 60 秒；设为 0 关闭冷却。 |
| `daily_max_requests` | 每天全插件共享的语音尝试上限，默认 0 表示不限制；设为大于 0 的次数以启用每日上限。 |

`action_enabled`、`command_enabled` 和 `guard.natural_enabled` 在插件注册组件时读取，修改后要重载插件或重启麦麦才能重新注册。

## 🏠 自托管模式

想让声音在自己的机器上诞生？按照 [Breeze TTS 官方项目](https://github.com/breezeblue-ai/breeze-tts)准备运行环境、下载模型并启动其 API，然后设置 `backend = "self_hosted"`。

- **地址**：如果服务与麦麦不在同一台机器，将 `api_url` 改为**麦麦能够访问的**服务地址。`127.0.0.1` 指麦麦进程所在的机器或容器，不一定是运行模型的机器。`api_url` 是服务的基础地址，不需要附加语音合成接口路径。
- **声音克隆**：填写成对的 `ref_audio` 和 `ref_text`。参考音频支持 `.wav`、`.mp3`、`.flac`、`.ogg`、`.opus`、`.m4a`、`.aac`、`.aif` 和 `.aiff` 扩展名，实际可解码格式取决于服务端。请使用你有权使用的声音素材。
- **数据去向**：插件会将合成文字发送给所配置的服务；使用自托管参考音频时还会发送该音频。0.12.2 的宿主发送语音流程可能调用已开启的语音转写服务，产生额外延迟或费用，请检查自己的 ASR 配置。

## 🩺 麦麦不说话了？

| 现象 | 检查内容 |
| --- | --- |
| 插件未出现或未加载 | 目录是否直接包含 `plugin.py` 和 `_manifest.json`，版本是否匹配，`aiohttp` 是否安装在麦麦环境，查看插件加载日志。 |
| 云端合成失败 | 检查 `backend`、API Key、Voice ID、账户额度、模型和网络。环境变量需由启动麦麦的进程继承。 |
| 自托管合成失败 | 检查模型服务是否已启动、`api_url` 能否从麦麦访问、参考音频及文字是否成对填写。 |
| 没有触发语音 | 确认会话在允许范围、Action 和自然语音开关已开启，并检查冷却和每日上限；自然回复仍取决于麦麦的选择。 |
| 提示文字过长 | 缩短文字或调整 `max_text_length`；云端仍有服务自身的长度限制。 |
| 合成成功但 QQ 无语音或无法播放 | 查看适配器是否支持语音及其日志，分别检查合成、送达和播放。 |

分享日志前请先去除密钥和其他隐私信息。

> [!WARNING]
> 本插件的自动化测试使用模拟 HTTP 服务，**真实 BreezeBlue 生成及 QQ 发送、播放尚未完成端到端验证**。

## 🧪 运行测试

在插件仓库目录、已安装 `aiohttp` 的环境中执行：

```bash
python -m unittest discover -s tests -p test_breeze_client.py -v
```

```bash
python -m unittest discover -s tests -p test_voice_guard.py -v
```

宿主集成测试需要隔离的 MaiBot 0.12.2 测试环境。将插件放入该环境的 `plugins/breeze_tts_0122`，然后在宿主根目录运行：

```bash
PYTHONPATH=. python plugins/breeze_tts_0122/tests/test_host.py
```

这些测试使用模拟语音服务或模拟发送，不消耗云端额度，也不发送 QQ 消息。

## 📚 官方资料与许可

- [BreezeBlue 快速入门](https://docs.breezeblue.ai/quickstart) · [语音合成 API](https://docs.breezeblue.ai/api-reference/text-to-speech/convert-text-to-speech) · [QQ 语音接入指南](https://breezeblue.ai/zh/blog/give-dafeiyu-a-voice-in-qq)
- 插件代码采用 [MIT License](LICENSE)。Breeze TTS 2 模型权重、衍生模型及自托管生成结果受单独的[研究及非商业许可](https://huggingface.co/BreezeBlue/Breeze-TTS-2/blob/main/LICENSE)约束；云端服务适用其自身条款。

<div align="center">

祝你和麦麦聊得开心 ♪(´▽｀)

</div>
