# jobhook

English | [中文](README.zh-CN.md)

A small server that tells you when a job running on another machine finishes, and holds on to the result until
you come to collect it.

You start a long job on a remote machine: a training run, a backup, a big download. Your laptop goes to sleep or
leaves the network. The remote machine can't reach your laptop anyway. When the job ends, it reports to jobhook,
which runs on a machine that is always on. jobhook sends you a notification right away and keeps the exit code and
the last few KB of the log. Your laptop picks them up the next time it checks.

- One Python file, standard library only (Python 3.8+). No database: one small JSON file per job.
- Works with plain `curl` from any job, cron entry or CI step.
- Notifies through [Bark](https://github.com/Finb/Bark) (iPhone push) or any shell command you give it
  (ntfy, Telegram, email…).
- Notices jobs that never report back (killed, machine restarted) once their time limit passes.
- The callback needs no password: each job gets a random id that works once.

## How it works

1. **Register.** Before starting a job, your client picks a random id and registers it, with a short note and
   an optional time limit. This needs the admin token.
2. **Call back.** When the job ends, it posts its exit code and the end of its log to `/h/<id>`. No token: the
   id is the credential, and it only works once.
3. **Notify.** jobhook sends one notification: finished, failed with its exit code, or "lost" if the time limit
   passed with no callback. A lost job that calls back later still finishes normally.
4. **Collect.** Your client lists what has finished, takes the results, and acknowledges them. Acknowledged
   records are deleted. Records nobody collects are deleted after 30 days.

Notifications carry the host name, the note (anything that looks like a path is left out), the exit code and how
long the job took. Never the log.

## Install

You need a machine that is always on and that your job machines can reach over HTTPS, with Python 3.8 or newer.

Copy the file over:

```sh
scp jobhook.py server:jobhook/jobhook.py
```

On the server, create `~/.config/jobhook.env` and make it readable only by you (`chmod 600`):

```sh
JOBHOOK_TOKEN=…                  # the admin token: openssl rand -hex 24
JOBHOOK_LISTEN=127.0.0.1:8431
# Bark, if you use it; leave these out otherwise
BARK_URL=https://…
BARK_KEY=…
```

Run it as a systemd user service, `~/.config/systemd/user/jobhook.service`:

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
loginctl enable-linger "$USER"     # keep it running while you're logged out
curl -s http://127.0.0.1:8431/ping
```

Put a reverse proxy with HTTPS in front of it. With Caddy:

```
hook.example.com {
    reverse_proxy 127.0.0.1:8431
}
```

- Turn off the access log for this site. The `/h/<id>` in each callback URL is that job's credential.
- If Caddy runs in a container, `127.0.0.1` is the container itself. Have jobhook listen on the host's address on
  the container network and point the proxy there.
- If the proxy asks for a password (basic auth), leave `/h/` open, or every job would need that password too.
  The admin token travels in its own header (`X-Jobhook-Token`), so it doesn't clash with basic auth.

## Notifications

**Bark.** Set `BARK_URL` and `BARK_KEY`, plus `BARK_USER` and `BARK_PASS` if your Bark server sits behind basic
auth. If Bark runs on the same machine, use its local address (`http://127.0.0.1:<port>`).

**Anything else.** Set `JOBHOOK_NOTIFY` to a shell command. For each notification it gets the title as `$1`, the
body as `$2`, and `JOBHOOK_EVENT` (`done`, `failed` or `lost`), `JOBHOOK_TITLE` and `JOBHOOK_BODY` in its
environment. Wrap the whole value in single quotes, so `$1`, `$2` and the inner quotes reach the command as written:

```sh
# ntfy
JOBHOOK_NOTIFY='curl -fsS -d "$1 · $2" https://ntfy.sh/your-topic'
# Telegram
JOBHOOK_NOTIFY='curl -fsS https://api.telegram.org/bot<bot-token>/sendMessage -d chat_id=<chat-id> --data-urlencode "text=$1 · $2"'
# a script of your own; it can read $JOBHOOK_TITLE, $JOBHOOK_BODY and $JOBHOOK_EVENT
JOBHOOK_NOTIFY=/home/you/bin/notify-me
```

Both can be on at once. Notifications go out on a background thread, so a slow one never delays a callback.
A command gets 30 seconds. A notification that fails is logged and not retried.

## Use it from any job

```sh
URL=https://hook.example.com
TOKEN=…                                   # the same JOBHOOK_TOKEN

# 1. where you start the job: register an id (note, host, time limit in seconds)
id=$(openssl rand -hex 16)
curl -fsS -X PUT -H "X-Jobhook-Token: $TOKEN" \
  -d '{"note": "nightly backup", "host": "nas", "within": 7200}' "$URL/h/$id"

# 2. on the machine that runs it: call back when it ends, with the exit code and the end of the log
./backup.sh >backup.log 2>&1
rc=$?
tail -c 4096 backup.log | curl -fsS -m 30 --retry 5 --retry-delay 60 \
  -X POST --data-binary @- "$URL/h/$id?rc=$rc"

# 3. later, from your laptop: see what finished, then take it
curl -fsS -H "X-Jobhook-Token: $TOKEN" "$URL/hooks"
curl -fsS -X POST -H "X-Jobhook-Token: $TOKEN" "$URL/h/$id/ack"
```

Only steps 1 and 3 need the token, so it never has to be on the job machine. Retrying a callback is safe: a second
one for the same id is ignored.

[aca](https://github.com/cheny-00/aca), a terminal manager for coding agents, uses jobhook for its remote tasks
plugin: it registers, starts the job over ssh, and delivers the result to the agent session that asked for it.

## API

| Request | Token | What it does |
|---|---|---|
| `GET /ping` | no | `{"ok": true, "service": "jobhook", "api": 1, "version": "…"}` |
| `PUT /h/<id>` | yes | Register. Body: `{"note", "host", "within"}`, all optional; `within` is a time limit in seconds (0 or missing: none). Registering a waiting id again updates its note, host and time limit. |
| `POST /h/<id>?rc=N` | no | Call back with exit code `N`. The body is the log; only its last 4 KB are kept. Bodies over 8 MB are refused. |
| `GET /hooks` | yes | `{"now", "hooks": [record, …]}`: everything not collected yet, oldest first |
| `POST /h/<id>/ack` | yes | Collected: delete it. Only for finished jobs. |
| `DELETE /h/<id>` | yes | Forget it, finished or not |

Ids are 16 to 64 characters from `A-Z a-z 0-9 _ -`; use at least 128 random bits (`openssl rand -hex 16`).
The token goes in the `X-Jobhook-Token` header. Errors come back as `{"error": "…"}` with a 4xx status.

A record:

| Field | Meaning |
|---|---|
| `id`, `note`, `host` | as registered |
| `created` | Unix time of the first registration |
| `deadline` | Unix time the time limit runs out; 0 if there is none |
| `state` | `waiting`, `lost` (time limit passed, no callback yet) or `done` |
| `rc`, `ended`, `tail` | once done: exit code, Unix time of the callback, end of the log |

## Settings

All through environment variables:

| Variable | Default | |
|---|---|---|
| `JOBHOOK_TOKEN` | (required) | Admin token, 16+ characters |
| `JOBHOOK_LISTEN` | `127.0.0.1:8431` | Address and port to listen on |
| `JOBHOOK_DIR` | `~/.local/state/jobhook` | Where the records live |
| `JOBHOOK_KEEP_DAYS` | `30` | Delete records nobody has touched for this many days; `0` keeps them forever |
| `JOBHOOK_LANG` | `en` | Language of errors and notifications: `en` or `zh` |
| `JOBHOOK_NOTIFY` | | Shell command run for each notification |
| `BARK_URL`, `BARK_KEY`, `BARK_USER`, `BARK_PASS` | | Bark server, device key, optional basic auth |

## Security

- Callbacks need no token. Ids are random and work once; someone who learns an id can at most finish that job
  first with up to 4 KB of text of their choosing. Treat a callback's log as untrusted text.
- Registering, listing, collecting and deleting need the admin token. It is compared in constant time.
- The server log shows only the first 8 characters of an id. Records are stored readable by the server's user only.

## Upgrading from adm-hub

jobhook used to be called adm-hub. Replace `adm_hub.py` with `jobhook.py` and point the service at it; nothing
else has to change right away. jobhook still reads `ADM_HUB_TOKEN`, `ADM_HUB_LISTEN`, `ADM_HUB_DIR` and the
`X-Adm-Token` header, and keeps using `~/.local/state/adm-hub` when that is where the records are and
`~/.local/state/jobhook` doesn't exist. Switch to the new names when convenient; the old ones will be dropped in
a later version.

## Tests

```sh
python3 -m unittest test_jobhook.py
```
