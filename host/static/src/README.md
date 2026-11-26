# 页面源码（手机端 ext.html）

`../ext.html` 是**生成物**，不要直接改它。改这里，再跑：

```bash
python tools/build_page.py          # 重新生成 static/ext.html
python tools/build_page.py --check  # 只检查是不是最新的（CI 会跑）
```

拼出来的还是**一个单文件**，运行方式没变；拆开只是为了让改收件箱的人不用滚过
整个设置页。`page.html` 里三处 `@@JS:key@@` 是内联脚本的占位符，key 对应
`tools/build_page.py` 的 `BLOCKS`。

| 文件 | 管什么 |
|---|---|
| `js/00_boot.js` | 客户端标识 cookie、老内核补丁（最先跑） |
| `js/01_core.js` | 全局状态、`xjX` 请求、计费倒计时、面板切换与手势 |
| `js/02_view.js` | 会话列表、会话页、草稿、媒体查看 |
| `js/03_voice.js` | 语音播报、提醒历史与健康、免打扰 |
| `js/04_net.js` | 网络状态、推送、终端浮层、键盘适配 |
| `js/05_cmd.js` | 命令识别、弹层点选、打开/召回会话 |
| `js/06_settings.js` | 设置页、计费与套餐、版本与更新 |
| `js/07_account.js` | 账号、设备管理、模型切换、删除会话 |
| `js/08_ptt.js` | 按住说话（壳原生录音 / 浏览器 getUserMedia） |
| `js/09_feedback.js` | 问题建议：表单、工单列表与详情 |

拆分点都在顶层声明边界上，拼回去与拆分前逐字节相同（`tests/test_page_build.py`
会挡住「改错地方」的情况）。
