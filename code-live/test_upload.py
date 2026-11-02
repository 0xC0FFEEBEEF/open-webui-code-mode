"""The Upload button's endpoint (Feature #31): PUT /u/<chat>/<usig>/file?path=... writes one file into the chat's
project folder under uploads/, folders kept. Its own signature (not the live window's), no way out of uploads/,
size and free-disk limits, CORS for the Open WebUI page, and uploads/.gitignore so git never carries them."""
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from test_live import SECRET, load_module

CHAT = "c0ffee00-1111-2222-3333-444455556666"
ORIGIN = "http://cryoai:8080"


class UploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module(9)   # (no opencode is needed for uploads)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), cls.mod.Handler)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        cls.defaults = (cls.mod.UPLOAD_MAX_FILE, cls.mod.UPLOAD_MIN_FREE)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.mod.PROJECTS_ROOT = self.root
        self.mod.UPLOAD_MAX_FILE, self.mod.UPLOAD_MIN_FREE = self.defaults[0], 0

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def put(self, path, data=b"hello", chat=CHAT, sig=None, method="PUT"):
        sig = sig or self.mod.upload_sig(chat, SECRET)
        url = "%s/u/%s/%s/file?path=%s" % (self.base, chat, sig, urllib.parse.quote(path, safe=""))
        req = urllib.request.Request(url, data=data if method == "PUT" else None, method=method,
                                     headers={"Origin": ORIGIN, "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def chat_dir(self):
        return os.path.join(self.root, "chats", CHAT)

    def test_a_nested_file_lands_under_uploads_with_its_folders(self):
        st, h, body = self.put("data/2024/q1.csv", b"a,b\n1,2\n")
        self.assertEqual(st, 201, body)
        self.assertIn('"path": "uploads/data/2024/q1.csv"', body)
        with open(os.path.join(self.chat_dir(), "uploads", "data", "2024", "q1.csv"), "rb") as fh:
            self.assertEqual(fh.read(), b"a,b\n1,2\n")
        with open(os.path.join(self.chat_dir(), "uploads", ".gitignore")) as fh:
            self.assertIn("*", fh.read().splitlines())
        self.assertEqual(h["Access-Control-Allow-Origin"], ORIGIN)

    def test_a_second_upload_replaces_the_file(self):
        self.assertEqual(self.put("notes.txt", b"one")[0], 201)
        self.assertEqual(self.put("notes.txt", b"two")[0], 201)
        with open(os.path.join(self.chat_dir(), "uploads", "notes.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"two")
        self.assertEqual(sorted(os.listdir(os.path.join(self.chat_dir(), "uploads"))), [".gitignore", "notes.txt"])

    def test_an_empty_file_is_fine(self):
        self.assertEqual(self.put("empty.txt", b"")[0], 201)
        self.assertEqual(os.path.getsize(os.path.join(self.chat_dir(), "uploads", "empty.txt")), 0)

    def test_a_feature_chat_uploads_into_its_workspace(self):
        os.makedirs(os.path.join(self.root, "features", CHAT))
        self.assertEqual(self.put("a.txt")[0], 201)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "features", CHAT, "uploads", "a.txt")))
        self.assertFalse(os.path.exists(self.chat_dir()))

    def test_the_live_window_signature_cannot_upload(self):
        st, _, _ = self.put("a.txt", sig=self.mod.live_sig(CHAT, SECRET))
        self.assertEqual(st, 403)
        st, _, _ = self.put("a.txt", sig=self.mod.upload_sig("other-chat", SECRET))
        self.assertEqual(st, 403)
        self.assertFalse(os.path.exists(self.chat_dir()))

    def test_paths_that_climb_out_or_touch_git_are_refused(self):
        for bad in ("../x", "a/../../x", "/etc/x", ".git/config", "a/.git/hooks/x", "a//b", "./a", "a\\b", ""):
            st, _, body = self.put(bad)
            self.assertEqual(st, 400, (bad, body))
        self.assertFalse(os.path.exists(os.path.join(self.root, "x")))

    def test_a_link_out_of_uploads_is_not_followed(self):
        outside = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(self.chat_dir(), "uploads"))
            os.symlink(outside, os.path.join(self.chat_dir(), "uploads", "out"))
            st, _, _ = self.put("out/sub/x.txt")
            self.assertEqual(st, 400)
            self.assertEqual(os.listdir(outside), [])
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_size_and_disk_limits(self):
        self.mod.UPLOAD_MAX_FILE = 4
        self.assertEqual(self.put("big.bin", b"12345")[0], 413)
        self.mod.UPLOAD_MAX_FILE = self.defaults[0]
        self.mod.UPLOAD_MIN_FREE = 1 << 62
        st, _, body = self.put("a.txt")
        self.assertEqual(st, 507, body)
        self.assertIn("too full", body)

    def test_a_folder_it_cannot_write_is_an_error_answer_not_a_dropped_connection(self):
        os.makedirs(os.path.join(self.root, "chats"))
        os.chmod(os.path.join(self.root, "chats"), 0o555)   # as under the unit's ProtectHome=read-only
        try:
            st, _, body = self.put("a.txt")
            self.assertEqual(st, 500, body)
            self.assertIn("Could not make the folder", body)
        finally:
            os.chmod(os.path.join(self.root, "chats"), 0o755)

    def test_the_preflight_allows_the_put(self):
        sig = self.mod.upload_sig(CHAT, SECRET)
        req = urllib.request.Request("%s/u/%s/%s/file" % (self.base, CHAT, sig), method="OPTIONS",
                                     headers={"Origin": ORIGIN, "Access-Control-Request-Method": "PUT"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 204)
            self.assertEqual(r.headers["Access-Control-Allow-Origin"], ORIGIN)
            self.assertIn("PUT", r.headers["Access-Control-Allow-Methods"])

    def test_the_masked_log_path_hides_the_signature(self):
        sig = self.mod.upload_sig(CHAT, SECRET)
        self.assertEqual(self.mod.mask("/u/%s/%s/file?path=x" % (CHAT, sig)), "/u/%s/***/file" % CHAT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
