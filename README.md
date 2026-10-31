# Breeze TTS 语音插件 · MaiBot 0.12.2

将 [BreezeBlue](https://breezeblue.ai/) 云端语音或 [Breeze TTS 2 自托管服务](https://github.com/breezeblue-ai/breeze-tts)接入麦麦。使用 `/breeze` 朗读指定文字，也可在用户请求语音时由麦麦选择 Action 合成回复。

> 本仓库针对 **MaiBot 0.12.2**。请使用支持发送语音的适配器。插件不包含模型权重，也不会自动启动语音服务。

## 快速开始：BreezeBlue 云端

1. 将本仓库放入麦麦的 `plugins/breeze_tts_0122` 目录；确保麦麦的 Python 环境中已安装 `aiohttp`。
2. 登录 BreezeBlue，在[开发者页面](https://breezeblue.ai/app/developer/api-keys)创建 API Key；选择音色并获取其 Voice ID。可先听听官网提供的[机灵版](https://breezeblue.ai/zh/voice-library/dafeiyu-clever-girl-voice-vu3nxkd5578wc)或[迷糊版](https://breezeblue.ai/zh/voice-library/dafeiyu-dreamy-girl-voice-v8yud7hz7c9q2)，也可使用自己已保存的音色；参见[官方音色说明](https://docs.breezeblue.ai/concepts/voices)。
3. 启动麦麦生成插件配置。在插件配置中将 `backend` 设为 `cloud`，填入 `voice_id` 和 `api_key`；也可让 `api_key` 留空并在麦麦启动环境中设置 `BREEZE_API_KEY`。`api_url` 和 `model` 留空时使用默认值，中文可将 `language_code` 设为 `zh`。
4. 保存配置并重载插件或重启麦麦，确认插件已启用；在允许测试的聊天中发送 `/breeze 你好，很高兴认识你`。确认语音生成、消息送达和完整播放分别成功。

云端生成会消耗 BreezeBlue 账户额度，网站和 API 共用账户余额；价格及免费额度以[官网当前说明](https://docs.breezeblue.ai/concepts/pricing)为准。插件有单次文字长度限制，**没有每日用量上限**。建议在麦麦的运行环境中设置 `BREEZE_API_KEY` 并将配置中的 `api_key` 留空。密码框只遮挡界面显示，不能保证配置文件、备份或配置接口不含明文；不要上传含密钥的配置或在聊天中发送密钥。

## 安装与配置

在麦麦根目录克隆仓库：

```bash
git clone https://github.com/MoonlyCake/maibot-breeze-tts-0122.git plugins/breeze_tts_0122
```

启动麦麦生成配置并启用插件。在 WebUI 中打开 **插件配置 → Breeze TTS for MaiBot 0.12.2 → 可视化**，在“基本设置”和“语音服务”中修改后保存，再重载插件或重启麦麦；插件须先加载，WebUI 才能取得完整的可视化字段信息。云端和自托管字段都会显示，按所选后端填写即可。也可以修改 `plugins/breeze_tts_0122/config.toml`，修改后重启麦麦。依赖未安装时，在麦麦使用的 Python 环境中执行：

```bash
python -m pip install aiohttp
```

官网示例中，机灵版 Voice ID 为 `voc_u3nxkd5578wc`，迷糊版为 `voc_8yud7hz7c9q2`。请试听后选择。

### 云端配置示例

以下为配置文件中 `[breeze]` 部分的示例；如使用 WebUI，在对应字段填入相同的值即可。先在麦麦启动环境中设置 `BREEZE_API_KEY`，并将 `voice_id` 换成选好的音色 ID。

```toml
[breeze]
backend = "cloud"
api_url = ""
api_key = ""
voice_id = "填写你的音色 ID"
model = ""
language_code = "zh"
```

### 配置项

| 字段 | 用途 |
| --- | --- |
| `action_enabled`、`command_enabled` | 分别启用按需语音 Action 和 `/breeze` 命令。 |
| `max_text_length` | 单次合成的文字长度上限，默认 500。 |
| `send_errors` | 控制是否在聊天中显示合成或发送失败提示。 |
| `backend` | `self_hosted` 为自托管（默认），`cloud` 为 BreezeBlue 云端。 |
| `api_url` | 服务基础地址。留空时分别使用 `http://127.0.0.1:7860` 和 `https://api.breeze.blue`。 |
| `api_key` | 云端 API Key；留空时读取环境变量 `BREEZE_API_KEY`。自托管不用填写。 |
| `voice_id` | 云端必填的音色 ID。 |
| `model`、`language_code` | 可选云端模型和语言代码；留空由服务决定。 |
| `instruction` | 可选的语气、情绪或说话方式描述。 |
| `ref_audio`、`ref_text` | 自托管参考音频的本机路径，以及其中实际说出的文字；两者必须同时填写，也可以同时留空。 |
| `cfg_scale` | 声音指导强度；自托管要求大于 0，云端范围 1–10。 |
| `seed` | 自托管采样种子；`-1` 表示不传该参数，使用服务端默认值。 |
| `timeout` | 单次请求超时秒数。 |

`action_enabled`、`command_enabled` 在插件注册组件时读取；修改后重载插件或重启麦麦以重新注册。参考音频支持 `.wav`、`.mp3`、`.flac`、`.ogg`、`.opus`、`.m4a`、`.aac`、`.aif` 和 `.aiff` 扩展名；实际可解码格式取决于服务端。请使用你有权使用的声音素材。

### 自托管模式

按照 [Breeze TTS 官方项目](https://github.com/breezeblue-ai/breeze-tts)准备运行环境、下载模型并启动其 API。设置 `backend = "self_hosted"`；如果服务与麦麦不在同一台机器，将 `api_url` 改为**麦麦能够访问的**服务地址。`127.0.0.1` 指麦麦进程所在的机器或容器，不一定是运行模型的机器。需要声音克隆时，填写成对的 `ref_audio` 和 `ref_text`。

配置中的 `api_url` 是服务的基础地址，不需要附加语音合成接口路径。插件会将合成文字发送给所配置的服务；使用自托管参考音频时还会发送该音频。

## 使用方式

- **指定朗读：**发送 `/breeze 要朗读的文字`；只发送 `/breeze` 可查看帮助。
- **按需语音回复：**用户明确请求语音或朗读时，麦麦可选择本插件的语音 Action。触发及选择取决于麦麦的对话决策，不保证每条符合条件的消息都会转语音。

使用中请避免同时启用会对同一条回复生成语音的其他 TTS 插件，以免重复发送。能否成功发出和播放语音还取决于麦麦适配器与目标平台。

## 排查问题

| 现象 | 检查内容 |
| --- | --- |
| 插件未出现或未加载 | 目录是否直接包含 `plugin.py` 和 `_manifest.json`，版本是否匹配，`aiohttp` 是否安装在麦麦环境，查看插件加载日志。 |
| 云端合成失败 | 检查 `backend`、API Key、Voice ID、账户额度、模型和网络。环境变量需由启动麦麦的进程继承。 |
| 自托管合成失败 | 检查模型服务是否已启动、`api_url` 能否从麦麦访问、参考音频及文字是否成对填写。 |
| 提示文字过长 | 缩短文字或调整 `max_text_length`；云端仍有服务自身的长度限制。 |
| 合成成功但 QQ 无语音或无法播放 | 查看适配器是否支持语音及其日志，分别检查合成、送达和播放。 |

分享日志前请先去除密钥和其他隐私信息。本插件的自动化测试使用模拟 HTTP 服务；**真实 BreezeBlue 生成及 QQ 发送、播放尚未完成端到端验证**。

## 官方资料与许可

- [BreezeBlue 快速入门](https://docs.breezeblue.ai/quickstart) · [语音合成 API](https://docs.breezeblue.ai/api-reference/text-to-speech/convert-text-to-speech) · [QQ 语音接入指南](https://breezeblue.ai/zh/blog/give-dafeiyu-a-voice-in-qq)
- 插件代码采用 [MIT License](LICENSE)。Breeze TTS 2 模型权重、衍生模型及自托管生成结果受单独的[研究及非商业许可](https://huggingface.co/BreezeBlue/Breeze-TTS-2/blob/main/LICENSE)约束；云端服务适用其自身条款。
