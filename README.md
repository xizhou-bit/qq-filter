# qq-filter

QQ 群重要消息本地过滤看板。只读监听群消息，不在任何群里发言；重要的消息存到本地，并按需推送给自己的 WxPusher、QQ 私聊或邮箱。

## 怎么跑

1. 安装 Python 依赖：

   ```
   pip install -r requirements.txt
   ```

2. 启动看板：

   ```
   python dashboard.py
   ```

3. 浏览器打开 http://127.0.0.1:8090

只跑这一个命令就够了：网页服务和 QQ 消息监听在同一个进程里，不需要再单独启动 `qq_filter_route_b.py`。

## 前置准备

- **SnowLuma**：需要你自己下载安装，不随本项目打包或分发。装好后可在看板的「SnowLuma 连接」页填写它的目录，看板会自动拉起；也可以手动先打开 SnowLuma。
- **Python 依赖**：见 `requirements.txt`。
- **DeepSeek Key**：在「模型」页的密钥区填写；也可以改用自定义 OpenAI 兼容接口。
- **WxPusher**：在 WxPusher 创建一个应用，拿到 AppToken 和 UID，并在 WxPusher 里订阅该应用。创建应用时请关闭“允许用户分享”，只推给自己；填完 AppToken 和 UID 后先点测试推送，确认手机能收到再用。

## 看板里怎么配

- **群聊设置**：每个群可选「关键词模式」或「全量 AI 模式」。全量模式会把该群的闲聊也发给模型商，敏感群建议用关键词模式。
- **发言人**：按群或全局设置重要发言人、屏蔽发言人，支持从近期消息里点选自动记 QQ 号。
- **模型**：选择提供商和模型。DeepSeek 使用固定接口地址；自定义 OpenAI 兼容可以手动填写接口地址和模型名。
- **消息提醒**：选择 WxPusher、QQ 私聊、邮件，或只看本地不推送。
- **批量与提醒**：批量间隔（默认 60 分钟）、紧急消息立即提醒、夜间静默时段，都在「消息提醒」页调整。

## 怎么停

- **关浏览器标签或刷新页面**：不会停止监听。看板和 SnowLuma 继续在后台运行，消息继续筛、继续存文件，下次打开历史还在。
- **停止监听**：只关闭 SnowLuma，不再接收新的 QQ 消息；看板本身不退出，历史消息仍可查看，之后可以点「启动 SnowLuma」恢复。
- **退出看板**：先按设置关闭 SnowLuma，再停止看板服务。历史消息已经存到本地文件，下次启动仍可查看。

## 安全提醒

- 密钥（DeepSeek Key、WxPusher AppToken / UID、邮箱密码或授权码等）只填在本地看板里，保存到 gitignored 的 `secrets.json`，不写进 `config.json`，也不进版本库。
- 建议使用小号接入，只读监听，不要在任何群里发言。
- 不要把 `config.json`、`secrets.json`、`*.jsonl`、`snowluma-launch.log` 等运行时文件提交到版本库。
- QQ 私聊推送只发给自己，绝不发群。
- 看板只绑定 `127.0.0.1`，不对局域网或公网开放。

## 文件说明

- `dashboard.py`：看板服务 + QQ 消息监听（单进程）
- `dashboard.html`：看板页面
- `qq_filter_route_b.py`：群消息过滤、AI 精筛、推送逻辑
- `config.example.json`：配置占位示例，不含真实数据
- `config.json`、`secrets.json`、`*.jsonl`、`snowluma-launch.log`：本地运行时文件，已 gitignore
