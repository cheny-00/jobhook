#!/usr/bin/env python3
"""jobhook: a mailbox for the results of jobs that run somewhere else. One file, Python standard library only.

Run it on a machine that is always on. Before you start a job, register a random id. When the job ends, it posts
its exit code and the end of its log to that id. jobhook notifies you right away (Bark, or any command you give it),
and keeps the result until your client comes to collect it. Your laptop can be asleep or offline in the meantime.

The life of an id:
  waiting   registered (PUT), no callback yet
  lost      past the time limit given at registration with no callback (kill -9 and reboots never call back);
            you are notified once. A callback that arrives later still finishes it
  done      the job called back (POST, with exit code and log tail); you are notified once
  (gone)    the client collected it (ack) or deleted it, or nobody touched it for JOBHOOK_KEEP_DAYS

API:
  GET    /ping            alive?  {"ok", "service", "api", "version"}
  PUT    /h/<id>          register        admin token   {"note", "host", "within": seconds}
  POST   /h/<id>?rc=N     call back       no token: the id is the credential (random, used once). Body = log tail
  GET    /hooks           not collected   admin token
  POST   /h/<id>/ack      collected       admin token
  DELETE /h/<id>          forget it       admin token

The admin token goes in the X-Jobhook-Token header, not Authorization: a reverse proxy in front may use that
for its own basic auth.

Environment:
  JOBHOOK_TOKEN       admin token, required (16+ characters: openssl rand -hex 24)
  JOBHOOK_LISTEN      address to listen on, default 127.0.0.1:8431 (put a reverse proxy in front)
  JOBHOOK_DIR         where records live, default ~/.local/state/jobhook
  JOBHOOK_KEEP_DAYS   delete records nobody touched for this many days, default 30; 0 keeps them forever
  JOBHOOK_LANG        language of errors and notifications: en (default) or zh
  JOBHOOK_NOTIFY      shell command run for each notification; gets the title and body as $1 and $2,
                      and JOBHOOK_EVENT (done / failed / lost), JOBHOOK_TITLE, JOBHOOK_BODY in its environment
  BARK_URL BARK_KEY [BARK_USER BARK_PASS]   push to a Bark server; leave unset for no Bark

Older versions were called adm-hub. For servers set up back then, this one still reads ADM_HUB_TOKEN,
ADM_HUB_LISTEN, ADM_HUB_DIR and the X-Adm-Token header, and keeps using ~/.local/state/adm-hub if that is
where the records are. These will go away in a later version.
"""

import base64
import hmac
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

VERSION = "0.1.0"
API = 1

ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
# keep only this much of a callback body; a body longer than BODY_MAX is refused unread
BODY_MAX = 8 * 1024 * 1024
TAIL_MAX = 4 * 1024
# how often to look for jobs past their time limit and for records past their keep time
SWEEP = 30
# notifications leave out anything that looks like a path: they say what ran, not where
PATHLIKE = re.compile(r"(~|/)[\w.\-/]{4,}")
# a notification command gets this long before it is killed
NOTIFY_TIMEOUT = 30


def clean(text, limit=60):
    t = PATHLIKE.sub("…", text or "").strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def span(secs):
    """how long: 45s / 12m / 2h13m / 3d4h"""
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        h, m = divmod(secs // 60, 60)
        return f"{h}h{m}m" if m else f"{h}h"
    d, h = divmod(secs // 3600, 24)
    return f"{d}d{h}h" if h else f"{d}d"


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, file=sys.stderr, flush=True)


class Store:
    """one JSON file per id, written to a temporary file first and renamed into place"""

    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        os.makedirs(root, exist_ok=True)

    def _path(self, hid):
        return os.path.join(self.root, hid + ".json")

    def get(self, hid):
        try:
            with open(self._path(hid), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def put(self, rec):
        tmp = self._path(rec["id"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, self._path(rec["id"]))

    def drop(self, hid):
        try:
            os.remove(self._path(hid))
            return True
        except OSError:
            return False

    def all(self):
        out = []
        for name in sorted(os.listdir(self.root)):
            if name.endswith(".json"):
                rec = self.get(name[:-5])
                if rec:
                    out.append(rec)
        return sorted(out, key=lambda r: r.get("created", 0))


class Bark:
    def __init__(self, env):
        self.url = (env.get("BARK_URL") or "").rstrip("/")
        self.key = env.get("BARK_KEY") or ""
        self.user, self.pw = env.get("BARK_USER"), env.get("BARK_PASS")

    def on(self):
        return bool(self.url and self.key)

    def send(self, _event, title, body):
        payload = json.dumps(
            {"device_key": self.key, "title": title, "body": body, "group": "jobhook", "level": "active"}
        ).encode()
        req = urllib.request.Request(
            self.url + "/push",
            data=payload,
            method="POST",
            # some front ends block urllib's default user agent
            headers={"Content-Type": "application/json", "User-Agent": f"jobhook/{VERSION}"},
        )
        if self.user and self.pw:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{self.user}:{self.pw}".encode()).decode())
        with urllib.request.urlopen(req, timeout=15):
            pass


class Command:
    """JOBHOOK_NOTIFY: any shell command, e.g. a curl to ntfy, Telegram or Slack"""

    def __init__(self, env):
        self.cmd = env.get("JOBHOOK_NOTIFY") or ""

    def on(self):
        return bool(self.cmd)

    def send(self, event, title, body):
        env = dict(os.environ, JOBHOOK_EVENT=event, JOBHOOK_TITLE=title, JOBHOOK_BODY=body)
        r = subprocess.run(
            ["sh", "-c", self.cmd, "jobhook", title, body],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=NOTIFY_TIMEOUT,
        )
        if r.returncode != 0:
            raise OSError(f"exit {r.returncode}: {r.stderr.decode('utf-8', 'replace').strip()[:200]}")


class Notifier:
    """hands each notification to every channel that is set up, one at a time on a thread of its own,
    so a slow channel never holds up a reply. A channel that fails gets one log line"""

    def __init__(self, channels):
        self.channels = [c for c in channels if c.on()]
        self.q = queue.Queue(maxsize=1000)
        if self.channels:
            threading.Thread(target=self._run, daemon=True).start()

    def names(self):
        return [type(c).__name__.lower() for c in self.channels]

    def send(self, event, title, body):
        if not self.channels:
            return
        try:
            self.q.put_nowait((event, title, body))
        except queue.Full:
            log("notification dropped: queue full")

    def _run(self):
        while True:
            event, title, body = self.q.get()
            for c in self.channels:
                try:
                    c.send(event, title, body)
                except Exception as e:  # noqa: BLE001 — one log line, whatever went wrong
                    log(f"{type(c).__name__.lower()} failed: {e}")


class Jobhook:
    def __init__(self, store, token, notify, lang="en", keep_days=30, now=time.time):
        self.store, self.token, self.notify, self.now = store, token, notify, now
        self.zh = lang.startswith("zh")
        self.keep = keep_days * 86400

    def t(self, en, zh):
        return zh if self.zh else en

    def admin(self, given):
        return bool(self.token) and any(hmac.compare_digest((g or "").encode(), self.token.encode()) for g in given)

    def register(self, hid, meta):
        within = meta.get("within")
        within = int(within) if isinstance(within, (int, float)) and within > 0 else 0
        with self.store.lock:
            old = self.store.get(hid)
            if old and old["state"] != "waiting":
                return 409, {"error": self.t("this id has already called back", "这个 id 已经回调过了")}
            now = self.now()
            rec = {
                "id": hid,
                "note": str(meta.get("note") or "")[:200],
                "host": str(meta.get("host") or "")[:80],
                "created": old["created"] if old else now,
                "deadline": now + within if within else 0,
                "state": "waiting",
            }
            self.store.put(rec)
        return 200, rec

    def callback(self, hid, rc, body):
        with self.store.lock:
            rec = self.store.get(hid)
            if not rec:
                return 404, {"error": self.t("no such id", "没有这个 id")}
            if rec["state"] == "done":
                return 200, {"ok": True, "already": True}
            rec.update(state="done", rc=rc, ended=self.now(), tail=body.decode("utf-8", "replace"))
            self.store.put(rec)
        took = span(rec["ended"] - rec["created"])
        ok = rc == 0
        said = self.t("finished", "跑完了") if ok else self.t(f"exit code {rc}", f"退出码 {rc}")
        self.notify.send("done" if ok else "failed", ("✅ " if ok else "❌ ") + self.what(rec), f"{said} · {took}")
        return 200, {"ok": True}

    def what(self, rec):
        return " · ".join(x for x in (rec["host"], clean(rec["note"])) if x) or rec["id"][:8]

    def pending(self):
        return 200, {"now": self.now(), "hooks": self.store.all()}

    def ack(self, hid):
        with self.store.lock:
            rec = self.store.get(hid)
            if rec and rec["state"] != "done":
                return 409, {"error": self.t("not finished yet; to forget it, use DELETE", "还没结束,取不走;不要了用 DELETE")}
            self.store.drop(hid)
        return 200, {"ok": True}

    def cancel(self, hid):
        with self.store.lock:
            return 200, {"ok": True, "was": self.store.drop(hid)}

    def sweep(self):
        """jobs past their time limit become lost (notified once); records nobody touched for keep_days go"""
        late, old = [], 0
        with self.store.lock:
            now = self.now()
            for rec in self.store.all():
                if rec["state"] == "waiting" and rec.get("deadline") and now > rec["deadline"]:
                    rec["state"] = "lost"
                    self.store.put(rec)
                    late.append(rec)
                    continue
                touched = rec.get("ended") or rec.get("deadline") or rec.get("created", now)
                if self.keep and now - touched > self.keep and self.store.drop(rec["id"]):
                    old += 1
        for rec in late:
            limit = span(rec["deadline"] - rec["created"])
            self.notify.send(
                "lost",
                "⏳ " + self.what(rec),
                self.t(
                    f"no callback after {limit}; it may have been killed or its machine restarted",
                    f"过了 {limit} 还没回调,可能被杀了或机器重启了",
                ),
            )
        if old:
            log(f"deleted {old} record(s) untouched for {self.keep // 86400} days")
        return late


def handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = f"jobhook/{VERSION}"

        def log_message(self, format, *args):  # noqa: A002 — the base class names it so
            # the id is a credential: logs keep only its first 8 characters
            line = re.sub(r"/h/([A-Za-z0-9_-]{8})[A-Za-z0-9_-]*", r"/h/\1…", format % args)
            log(f"{self.client_address[0]} {line}")

        def reply(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self):
            """the end of the body (up to TAIL_MAX); None for a body too long to read"""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n > BODY_MAX:
                self.close_connection = True
                return None
            tail = b""
            while n > 0:
                chunk = self.rfile.read(min(n, 65536))
                if not chunk:
                    break
                n -= len(chunk)
                tail = (tail + chunk)[-TAIL_MAX:]
            return tail

        def route(self, method):
            url = urlsplit(self.path)
            parts = [p for p in url.path.split("/") if p]
            body = self.body() if method in ("PUT", "POST") else b""
            if body is None:
                return 413, {"error": app.t("body too long", "正文太长")}
            admin = app.admin((self.headers.get("X-Jobhook-Token"), self.headers.get("X-Adm-Token")))
            denied = 401, {"error": app.t("wrong token", "口令不对")}
            if method == "GET" and parts == ["ping"]:
                return 200, {"ok": True, "service": "jobhook", "api": API, "version": VERSION}
            if method == "GET" and parts == ["hooks"]:
                return app.pending() if admin else denied
            if len(parts) >= 2 and parts[0] == "h" and ID.match(parts[1]):
                hid = parts[1]
                if method == "POST" and len(parts) == 2:
                    try:
                        rc = int(parse_qs(url.query).get("rc", ["0"])[0])
                    except ValueError:
                        rc = -1
                    return app.callback(hid, rc, body)
                if not admin:
                    return denied
                if method == "PUT" and len(parts) == 2:
                    try:
                        meta = json.loads(body or b"{}")
                    except ValueError:
                        return 400, {"error": app.t("body is not JSON", "正文不是 JSON")}
                    return app.register(hid, meta if isinstance(meta, dict) else {})
                if method == "POST" and parts[2:] == ["ack"]:
                    return app.ack(hid)
                if method == "DELETE" and len(parts) == 2:
                    return app.cancel(hid)
            return 404, {"error": app.t("no such path", "没有这个地址")}

        def do_GET(self):
            self.reply(*self.route("GET"))

        def do_PUT(self):
            self.reply(*self.route("PUT"))

        def do_POST(self):
            self.reply(*self.route("POST"))

        def do_DELETE(self):
            self.reply(*self.route("DELETE"))

    return H


def setting(env, name, default=""):
    """JOBHOOK_<name>, or ADM_HUB_<name> from before the rename"""
    return env.get("JOBHOOK_" + name) or env.get("ADM_HUB_" + name) or default


def data_dir(env):
    given = setting(env, "DIR")
    if given:
        return given
    new, old = (os.path.expanduser("~/.local/state/" + n) for n in ("jobhook", "adm-hub"))
    return old if not os.path.exists(new) and os.path.isdir(old) else new


def main():
    env = os.environ
    token = setting(env, "TOKEN")
    if len(token) < 16:
        sys.exit("JOBHOOK_TOKEN is not set (16+ characters): openssl rand -hex 24")
    host, _, port = setting(env, "LISTEN", "127.0.0.1:8431").rpartition(":")
    try:
        keep = int(env.get("JOBHOOK_KEEP_DAYS") or 30)
    except ValueError:
        sys.exit("JOBHOOK_KEEP_DAYS must be a whole number of days")
    root = data_dir(env)
    # records hold ids and log tails: readable by this user only
    os.umask(0o077)
    notify = Notifier([Bark(env), Command(env)])
    app = Jobhook(Store(root), token, notify, lang=env.get("JOBHOOK_LANG") or "en", keep_days=max(0, keep))
    srv = ThreadingHTTPServer((host or "127.0.0.1", int(port)), handler(app))

    def sweeper():
        while True:
            time.sleep(SWEEP)
            try:
                app.sweep()
            except Exception as e:  # noqa: BLE001 — a periodic job: one log line, whatever went wrong
                log(f"sweep: {e}")

    threading.Thread(target=sweeper, daemon=True).start()
    log(
        f"jobhook {VERSION} · listening on {host or '127.0.0.1'}:{port} · records in {root}"
        f" · notify: {', '.join(notify.names()) or 'off'}"
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
