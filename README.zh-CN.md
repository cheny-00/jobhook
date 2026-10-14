# jobhook

[English](README.md) | 中文

一个小服务:别的机器上跑的任务结束时,它马上通知你,并把结果替你收着,等你来取。

你在远端机器上跑一个很久的任务,比如训练、备份、下载大文件。你的笔记本会合盖、会离开网络,远端机器本来也连不到它。
任务结束时,它把结果交给 jobhook。jobhook 跑在一台一直在线的机器上,收到后立刻给你发一条通知,
并保存退出码和日志的最后几 KB。笔记本下次来查时把这些取走。

- 一个 Python 文件,只用标准库(Python 3.8 以上)。不需要数据库:每个任务一个小 JSON 文件。
- 任何任务、cron 定时任务、CI 步骤,用普通的 `curl` 就能接入。
- 通知可以走 [Bark](https://github.com/Finb/Bark)(iPhone 推送),也可以运行你给的任意命令(ntfy、Telegram、邮件……)。
- 超过时限还没回报的任务(被杀掉、机器重启了),它会发现并提醒你。
- 回调不需要密码:每个任务一个随机 id,只能用一次。

## 工作方式

1. **登记。** 开始任务前,客户端生成一个随机 id 登记上去,附上一句说明和可选的时限。这一步需要管理口令。
2. **回调。** 任务结束时,把退出码和日志末尾发到 `/h/<id>`。不需要口令:id 本身就是凭据,而且只能用一次。
3. **通知。** jobhook 发一条通知:跑完了、失败了(带退出码),或者过了时限还没回调(「失联」)。
   失联的任务之后再回调,照样算正常结束。
4. **取走。** 客户端列出已经结束的任务,取走结果,再确认一下。确认过的记录会删除。一直没人取的记录 30 天后删除。

通知里只有机器名、说明(像路径的部分会去掉)、退出码和耗时,不带日志内容。

## 安装

需要一台一直在线、跑任务的机器能通过 HTTPS 访问到的机器,装有 Python 3.8 或更新版本。

把文件拷过去:

```sh
scp jobhook.py 服务器:jobhook/jobhook.py
```

在服务器上建 `~/.config/jobhook.env`,权限设成只有自己能读(`chmod 600`):

```sh
JOBHOOK_TOKEN=…                  # 管理口令:openssl rand -hex 24
JOBHOOK_LISTEN=127.0.0.1:8431
# 用 Bark 的话填这两行,不用就删掉
BARK_URL=https://…
BARK_KEY=…
```

用 systemd 的用户服务运行它,`~/.config/systemd/user/jobhook.service`:

```ini
[Unit]
Description=jobhook

[Service]
EnvironmentFile=%h/.config/jobhook.env
ExecStart=/usr/bin/python3 %h/jobhook/jobhook.py
Restart=on-failure

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload && systemctl --user enable --now jobhook
loginctl enable-linger "$USER"     # 退出登录后也继续运行
curl -s http://127.0.0.1:8431/ping
```

前面放一个带 HTTPS 的反向代理。以 Caddy 为例:

```
hook.example.com {
    reverse_proxy 127.0.0.1:8431
}
```

- 这个站点不要开访问日志。每个回调地址里的 `/h/<id>` 就是那个任务的凭据。
- Caddy 跑在容器里的话,`127.0.0.1` 指的是容器自己。让 jobhook 监听宿主机在容器网络上的地址,反向代理指过去。
- 反向代理如果要求密码(basic auth),给 `/h/` 开个例外,否则每个任务回调时也得带上这个密码。
  管理口令放在单独的请求头 `X-Jobhook-Token` 里,不会和 basic auth 冲突。

## 通知

**Bark。** 设置 `BARK_URL` 和 `BARK_KEY`;Bark 服务前面有 basic auth 的话,再设 `BARK_USER` 和 `BARK_PASS`。
Bark 就在同一台机器上的话,直接写本机地址(`http://127.0.0.1:<端口>`)。

**其他方式。** 把 `JOBHOOK_NOTIFY` 设成一条 shell 命令。每次通知时,标题是 `$1`,正文是 `$2`;
环境变量里还有 `JOBHOOK_EVENT`(`done`、`failed` 或 `lost`)、`JOBHOOK_TITLE` 和 `JOBHOOK_BODY`。
整个值用单引号括起来,这样 `$1`、`$2` 和里面的双引号会原样交给命令:

```sh
# ntfy
JOBHOOK_NOTIFY='curl -fsS -d "$1 · $2" https://ntfy.sh/your-topic'
# Telegram
JOBHOOK_NOTIFY='curl -fsS https://api.telegram.org/bot<bot-token>/sendMessage -d chat_id=<chat-id> --data-urlencode "text=$1 · $2"'
# 你自己的脚本;脚本里可以读 $JOBHOOK_TITLE、$JOBHOOK_BODY、$JOBHOOK_EVENT
JOBHOOK_NOTIFY=/home/you/bin/notify-me
```

两种可以同时开。通知在后台线程里发,慢的通知不会拖住回调。命令最多运行 30 秒。发送失败的通知记一行日志,不重发。

## 在任意任务里使用

```sh
URL=https://hook.example.com
TOKEN=…                                   # 和服务端的 JOBHOOK_TOKEN 一样

# 1. 在启动任务的地方:登记一个 id(说明、机器名、时限,单位秒)
id=$(openssl rand -hex 16)
curl -fsS -X PUT -H "X-Jobhook-Token: $TOKEN" \
  -d '{"note": "nightly backup", "host": "nas", "within": 7200}' "$URL/h/$id"

# 2. 在跑任务的机器上:结束时回调,带上退出码和日志末尾
./backup.sh >backup.log 2>&1
rc=$?
tail -c 4096 backup.log | curl -fsS -m 30 --retry 5 --retry-delay 60 \
  -X POST --data-binary @- "$URL/h/$id?rc=$rc"

# 3. 之后在你的笔记本上:看哪些结束了,取走
curl -fsS -H "X-Jobhook-Token: $TOKEN" "$URL/hooks"
curl -fsS -X POST -H "X-Jobhook-Token: $TOKEN" "$URL/h/$id/ack"
```

只有第 1 步和第 3 步需要口令,所以口令不必放到跑任务的机器上。回调重试是安全的:同一个 id 的第二次回调会被忽略。

[aca](https://github.com/cheny-00/aca)(一个在终端里管理编程 agent 的工具)的远程任务插件就用 jobhook:
它负责登记、通过 ssh 启动任务,再把结果送回发起任务的那个 agent 会话。

## 接口

| 请求 | 口令 | 作用 |
|---|---|---|
| `GET /ping` | 不要 | `{"ok": true, "service": "jobhook", "api": 1, "version": "…"}` |
| `PUT /h/<id>` | 要 | 登记。正文 `{"note", "host", "within"}`,都可以不写;`within` 是时限,单位秒(0 或不写:不限)。对还在等待的 id 再登记一次,会更新说明、机器名和时限。 |
| `POST /h/<id>?rc=N` | 不要 | 回调,退出码 `N`。正文是日志,只保留最后 4 KB;超过 8 MB 的直接拒收。 |
| `GET /hooks` | 要 | `{"now", "hooks": [记录, …]}`:所有还没取走的,按登记时间排序 |
| `POST /h/<id>/ack` | 要 | 取走了:删除。只对已结束的任务有效。 |
| `DELETE /h/<id>` | 要 | 不要了,不管结束没有 |

id 是 16 到 64 个字符,只能用 `A-Z a-z 0-9 _ -`;至少要有 128 位随机性(`openssl rand -hex 16`)。
口令放在 `X-Jobhook-Token` 请求头里。出错时返回 4xx 状态码和 `{"error": "…"}`。

一条记录:

| 字段 | 含义 |
|---|---|
| `id`、`note`、`host` | 登记时给的 |
| `created` | 第一次登记的时间(Unix 时间) |
| `deadline` | 时限到期的时间(Unix 时间);没有时限是 0 |
| `state` | `waiting`(等待中)、`lost`(过了时限还没回调)或 `done`(已结束) |
| `rc`、`ended`、`tail` | 结束后才有:退出码、回调的时间、日志末尾 |

## 设置

全部通过环境变量:

| 变量 | 默认值 | |
|---|---|---|
| `JOBHOOK_TOKEN` | (必填) | 管理口令,至少 16 个字符 |
| `JOBHOOK_LISTEN` | `127.0.0.1:8431` | 监听的地址和端口 |
| `JOBHOOK_DIR` | `~/.local/state/jobhook` | 记录存放的目录 |
| `JOBHOOK_KEEP_DAYS` | `30` | 多少天没有变动的记录会被删除;`0` 表示永远保留 |
| `JOBHOOK_LANG` | `en` | 错误信息和通知的语言:`en` 或 `zh` |
| `JOBHOOK_NOTIFY` | | 每次通知时运行的 shell 命令 |
| `BARK_URL`、`BARK_KEY`、`BARK_USER`、`BARK_PASS` | | Bark 服务地址、设备 key、可选的 basic auth |

## 安全

- 回调不需要口令。id 是随机的,只能用一次;知道某个 id 的人,最多能抢先替那个任务回调一次,塞进不超过 4 KB 的文字。
  所以回调带来的日志要当作不可信的文字看待。
- 登记、列出、取走、删除都需要管理口令,比较口令时用的是固定耗时的比较。
- 服务日志里 id 只显示前 8 个字符。记录文件只有运行服务的用户能读。

## 测试

```sh
python3 -m unittest test_jobhook.py
```

## 许可证

[MIT](LICENSE)
