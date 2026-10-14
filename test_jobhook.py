"""python3 -m unittest test_jobhook.py — starts a real jobhook (temporary directory, random port) and walks
through it the way a client and a job would"""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

import jobhook

# keep the request log out of the test output
jobhook.log = lambda msg: None

TOKEN = "t" * 24
HID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


class Sent:
    """stands in for the Notifier: keeps what would have gone out"""

    def __init__(self):
        self.sent = []

    def send(self, event, title, body):
        self.sent.append((event, title, body))


class JobhookTest(unittest.TestCase):
    lang = "en"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = [1000.0]
        self.notify = Sent()
        store = jobhook.Store(self.dir.name)
        self.app = jobhook.Jobhook(store, TOKEN, self.notify, lang=self.lang, now=lambda: self.clock[0])
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), jobhook.handler(self.app))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.dir.cleanup()

    def call(self, method, path, body=None, admin=True, header="X-Jobhook-Token"):
        # http.client rather than urllib: urllib on macOS asks the system for proxies first
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers={header: TOKEN} if admin else {})
            r = conn.getresponse()
            return r.status, json.loads(r.read())
        finally:
            conn.close()

    def register(self, **meta):
        return self.call("PUT", f"/h/{HID}", json.dumps(meta).encode())

    def sent(self):
        return [(title, body) for _, title, body in self.notify.sent]


class Flow(JobhookTest):
    def test_a_job_goes_from_waiting_to_done_to_collected(self):
        code, rec = self.register(note="pull the 9B model", host="gpu1", within=3600)
        self.assertEqual((code, rec["state"], rec["deadline"]), (200, "waiting", 4600.0))
        self.clock[0] += 125
        # the job calls back without a token: the id is the credential
        code, out = self.call("POST", f"/h/{HID}?rc=0", b"last line\n", admin=False)
        self.assertEqual((code, out), (200, {"ok": True}))
        self.assertEqual(self.notify.sent, [("done", "✅ gpu1 · pull the 9B model", "finished · 2m")])
        code, out = self.call("GET", "/hooks")
        (hook,) = out["hooks"]
        self.assertEqual((hook["state"], hook["rc"], hook["tail"], hook["ended"]), ("done", 0, "last line\n", 1125.0))
        # collected means deleted
        self.assertEqual(self.call("POST", f"/h/{HID}/ack")[0], 200)
        self.assertEqual(self.call("GET", "/hooks")[1]["hooks"], [])
        # a callback now finds nothing
        self.assertEqual(self.call("POST", f"/h/{HID}?rc=0", b"", admin=False)[0], 404)

    def test_a_second_callback_is_ignored_and_a_failure_says_its_code(self):
        self.register(note="run /data/secret/train.py", host="box")
        self.call("POST", f"/h/{HID}?rc=3", b"boom", admin=False)
        code, out = self.call("POST", f"/h/{HID}?rc=0", b"again", admin=False)
        self.assertEqual((code, out.get("already")), (200, True))
        # notified once; anything like a path is left out
        self.assertEqual(self.notify.sent, [("failed", "❌ box · run …", "exit code 3 · 0s")])
        self.assertEqual(self.app.store.get(HID)["tail"], "boom")
        # a finished id can't be registered as waiting again
        self.assertEqual(self.register(note="x"), (409, {"error": "this id has already called back"}))

    def test_only_the_admin_token_registers_lists_and_collects(self):
        for method, path in [("PUT", f"/h/{HID}"), ("GET", "/hooks"), ("POST", f"/h/{HID}/ack"), ("DELETE", f"/h/{HID}")]:
            body = b"{}" if method != "GET" else None
            self.assertEqual(self.call(method, path, body, admin=False), (401, {"error": "wrong token"}), path)
        # ids never registered, and things that aren't ids: no callback
        self.assertEqual(self.call("POST", f"/h/{HID}?rc=0", b"", admin=False)[0], 404)
        self.assertEqual(self.call("POST", "/h/short?rc=0", b"", admin=False)[0], 404)

    def test_ping_says_what_it_is_without_a_token(self):
        code, out = self.call("GET", "/ping", admin=False)
        self.assertEqual((code, out), (200, {"ok": True, "service": "jobhook", "api": 1, "version": jobhook.VERSION}))

    def test_older_aca_remote_still_gets_in(self):
        self.assertEqual(self.call("GET", "/hooks", header="X-Adm-Token")[0], 200)
        self.assertEqual(self.call("GET", "/hooks", header="X-Something-Else")[0], 401)

    def test_past_its_time_it_is_lost_once_and_can_still_finish(self):
        self.register(note="training", host="lab", within=600)
        self.assertEqual(self.app.sweep(), [])
        self.clock[0] += 601
        self.assertEqual([r["id"] for r in self.app.sweep()], [HID])
        self.assertEqual(self.app.sweep(), [])
        self.assertEqual(
            self.notify.sent,
            [("lost", "⏳ lab · training", "no callback after 10m; it may have been killed or its machine restarted")],
        )
        self.assertEqual(self.call("GET", "/hooks")[1]["hooks"][0]["state"], "lost")
        # not finished: can't be collected
        self.assertEqual(self.call("POST", f"/h/{HID}/ack")[0], 409)
        self.call("POST", f"/h/{HID}?rc=0", b"late", admin=False)
        self.assertEqual(self.app.store.get(HID)["state"], "done")
        self.assertEqual(len(self.notify.sent), 2)

    def test_a_long_log_keeps_only_its_end_and_no_time_limit_never_gets_lost(self):
        self.app.keep = 0
        self.register(note="n", host="h")
        self.clock[0] += 10**9
        self.assertEqual(self.app.sweep(), [])
        body = b"x" * 100_000 + b"END"
        self.assertEqual(self.call("POST", f"/h/{HID}?rc=0", body, admin=False)[0], 200)
        tail = self.app.store.get(HID)["tail"]
        # only the last 4 KB
        self.assertEqual(len(tail), jobhook.TAIL_MAX)
        self.assertTrue(tail.endswith("END"))
        # forget it
        self.assertEqual(self.call("DELETE", f"/h/{HID}")[1], {"ok": True, "was": True})
        self.assertEqual(self.call("DELETE", f"/h/{HID}")[1], {"ok": True, "was": False})

    def test_records_nobody_collects_go_after_the_keep_time(self):
        day = 86400
        other = "b" * 32
        self.register(note="n", host="h")
        self.call("PUT", f"/h/{other}", b"{}")
        self.clock[0] += 20 * day
        self.call("POST", f"/h/{HID}?rc=0", b"", admin=False)
        # 31 days after registering: the one that never called back goes, the one that finished on day 20 stays
        self.clock[0] += 11 * day
        self.app.sweep()
        self.assertEqual([r["id"] for r in self.call("GET", "/hooks")[1]["hooks"]], [HID])
        self.clock[0] += 20 * day
        self.app.sweep()
        self.assertEqual(self.call("GET", "/hooks")[1]["hooks"], [])


class Chinese(JobhookTest):
    lang = "zh"

    def test_errors_and_notifications_in_chinese(self):
        self.assertEqual(self.call("GET", "/hooks", admin=False), (401, {"error": "口令不对"}))
        self.register(note="训练", host="lab", within=600)
        self.clock[0] += 601
        self.app.sweep()
        self.call("POST", f"/h/{HID}?rc=2", b"", admin=False)
        self.assertEqual(
            self.sent(),
            [("⏳ lab · 训练", "过了 10m 还没回调,可能被杀了或机器重启了"), ("❌ lab · 训练", "退出码 2 · 10m")],
        )


class Channels(unittest.TestCase):
    def test_a_notify_command_gets_the_title_body_and_event(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            cmd = jobhook.Command({"JOBHOOK_NOTIFY": f'printf "%s|%s|%s" "$1" "$2" "$JOBHOOK_EVENT" > {out}'})
            cmd.send("failed", "❌ box · build", "exit code 1 · 3m")
            with open(out, encoding="utf-8") as f:
                self.assertEqual(f.read(), "❌ box · build|exit code 1 · 3m|failed")
            with self.assertRaises(OSError):
                jobhook.Command({"JOBHOOK_NOTIFY": "echo no >&2; exit 4"}).send("done", "t", "b")

    def test_a_failing_channel_does_not_stop_the_next_one(self):
        got = []

        class Broken:
            def on(self):
                return True

            def send(self, *a):
                raise OSError("down")

        class Works:
            def on(self):
                return True

            def send(self, *a):
                got.append(a)

        class Off:
            def on(self):
                return False

        n = jobhook.Notifier([Broken(), Off(), Works()])
        self.assertEqual(n.names(), ["broken", "works"])
        n.send("done", "t", "b")
        for _ in range(100):
            if got:
                break
            time.sleep(0.02)
        self.assertEqual(got, [("done", "t", "b")])
        # nothing set up: nothing to do
        jobhook.Notifier([Off()]).send("done", "t", "b")


if __name__ == "__main__":
    unittest.main()
