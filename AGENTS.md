# 开发约定

- 本插件针对 MaiBot 0.12.2；保持现有配置可读，遵守宿主插件接口。
- 不提交 API Key、配置文件及备份、参考音频或运行数据。
- 修改语音、触发或限制逻辑时运行相关测试，并区分模拟测试与真实 QQ 验证。
- 常用客户端测试：`python -m unittest discover -s tests -p 'test_breeze_client.py' -v`。
- 在独立分支开发，通过 PR 合并；同步更新 README。
