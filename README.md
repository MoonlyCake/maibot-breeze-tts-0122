# Breeze TTS for MaiBot 0.12.2

将 Breeze TTS 自托管服务或 Breeze 云服务接入 MaiBot 0.12.2，通过 `/breeze` 命令朗读文字，也可在用户请求语音时由 Action 发送语音。

## 安装

1. 将本仓库内容作为 `plugins/breeze_tts_0122` 文件夹放入 MaiBot 0.12.2。安装插件依赖 `aiohttp`（MaiBot 环境中通常已有）：`pip install aiohttp`。
2. 启动一次 MaiBot 生成 `config.toml`，根据下面说明配置，重启 MaiBot。配置文件含密钥时请勿分享或上传。
3. 确保麦麦使用的适配器支持发送语音。输入 `/breeze 你好，很高兴认识你` 进行测试；`/breeze` 可查看命令帮助。

## 配置

在生成的 `config.toml` 中修改：

- `[general]`：`action_enabled`、`command_enabled` 控制对应组件；`max_text_length` 默认 500 字；`send_errors` 控制聊天内的错误提示。
- `[breeze] backend`：`self_hosted`（默认）或 `cloud`。
- `api_url`：留空自动选择对应后端地址（自托管默认 `http://127.0.0.1:7860`，云端默认 `https://api.breeze.blue`）。[Breeze TTS 官方自托管服务](https://github.com/breezeblue-ai/breeze-tts)须由使用者自行安装、启动并加载模型；麦麦与服务不在同一台机器时需填写麦麦可访问的地址。
- 云端需设置 `voice_id` 和 `api_key`；也可通过环境变量 `BREEZE_API_KEY` 提供密钥。`model` 留空由服务选择模型；`language_code` 可留空。使用云服务前请自行确认其费用和隐私条款，文本会发送至配置的服务。
- 自托管可设置 `ref_audio`（麦麦所在机器的参考音频路径）和 `ref_text`（参考音频实际说出的文字）；按服务要求成对提供。请只使用有权使用的声音素材。`cfg_scale` 须大于 0；云端范围 1–10。`seed = -1` 不指定随机种子，使用自托管服务默认值。
- `instruction` 为可选的语气和声音指导；`timeout` 为请求超时秒数。

插件将返回的音频作为 WAV 编码为 base64，并通过 MaiBot 的 `voice` 消息发送；它依赖运行中的语音服务和适配器，不能保证所有平台均支持。

## 许可

本插件代码按 MIT License 发布。官方 Breeze TTS 的模型权重及自托管模型输出另有许可，限制为研究及非商业用途；使用前请以[官方项目许可](https://github.com/breezeblue-ai/breeze-tts)为准。云服务另受其服务条款约束。
