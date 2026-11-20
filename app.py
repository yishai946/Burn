import json
import os
import shutil
import sys
import tempfile
import threading
import time

import core

HERE = core.RES  # index.html and icon.ico (inside the package when frozen)
if sys.stderr is None:  # no console (pythonw / Burner.exe): keep errors in burner.log instead of losing them
    sys.stdout = sys.stderr = open(os.path.join(core.DATA, "burner.log"), "a", encoding="utf-8", buffering=1)

import webview


class Api:
    def __init__(self):
        self._window = None
        self._jobs = []           # the queue, oldest first
        self._next_id = 0
        self._lock = threading.RLock()
        self._wake = threading.Event()
        threading.Thread(target=self._downloader, daemon=True).start()

    # --- called from the UI ---
    def load(self, url):
        try:
            r = core.load_playlist(url)
        except Exception as e:
            return {"ok": False, "msg": core.friendly_error(e)}
        try:
            core.mark_burned(r["tracks"])
        except Exception:
            pass  # history is a hint; a broken file must not block loading
        return {"ok": True, **r}

    def preview(self, url):
        """Fallback for the 30s preview: fetch it here and hand the page a data: URL."""
        import base64, urllib.parse, urllib.request
        if urllib.parse.urlparse(url).hostname not in ("p.scdn.co", "audio-ssl.itunes.apple.com"):
            return None  # only known preview hosts; the page passes this URL in
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=core.H), timeout=20, context=core.SSL)
            kind = r.headers.get_content_type() or "audio/mpeg"
            return f"data:{kind};base64," + base64.b64encode(r.read(5_000_000)).decode()
        except Exception:
            return None

    def search(self, q):
        try:
            tracks = core.search_songs(q)
        except Exception as e:
            return {"ok": False, "msg": core.friendly_error(e)}
        try:
            core.mark_burned(tracks)
        except Exception:
            pass
        return {"ok": True, "tracks": tracks}

    def check_update(self):
        if not core.FROZEN:
            return None  # running from source: nothing to update
        try:
            return core.latest_release()
        except Exception:
            return None  # offline or GitHub unreachable: just skip

    def update(self, info):
        if self.busy():
            return {"ok": False, "msg": "אי אפשר לעדכן בזמן הורדה או צריבה. נסה שוב כשהתור יסתיים."}
        threading.Thread(target=self._update, args=(info,), daemon=True).start()
        return {"ok": True}

    def _update(self, info):
        import subprocess, urllib.request
        if not info["url"].startswith("https://github.com/" + core.REPO + "/releases/download/"):
            return self._emit(phase="update_error", msg="קישור עדכון לא צפוי.")
        path = os.path.join(tempfile.gettempdir(), f"Burner-Setup-{info['version']}.exe")
        try:
            with urllib.request.urlopen(urllib.request.Request(info["url"], headers=core.H), timeout=30, context=core.SSL) as r, open(path, "wb") as f:
                got = 0
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    got += len(chunk)
                    self._emit(phase="update", pct=round(got / info["size"] * 100))
            if got != info["size"]:
                raise RuntimeError("ההורדה לא הושלמה.")
        except Exception as e:
            return self._emit(phase="update_error", msg=core.friendly_error(e))
        self._emit(phase="update_restart")
        # run the installer once this process has exited (it waits for Burner to close), then it reopens Burner
        subprocess.Popen(f'cmd /c ping -n 3 127.0.0.1 >nul & "{path}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART',
                         creationflags=core.NOWIN)
        time.sleep(0.8)
        os._exit(0)

    def history(self):
        try:
            return core.load_history()["playlists"]
        except Exception:
            return []

    def status(self):
        return core.burner_status()

    # --- the queue: lists download in the background, then wait for the user to burn them ---
    # job states: queued -> downloading -> ready -> burning -> ready (next disc) ... -> done | error
    def enqueue(self, tracks, name=None, url=None, fmt="audio"):
        if not tracks:
            return {"ok": False, "msg": "לא נבחרו שירים."}
        with self._lock:
            self._next_id += 1
            job = {"id": self._next_id, "name": name or "פלייליסט", "url": url, "fmt": "mp3" if fmt == "mp3" else "audio",
                   "tracks": tracks, "state": "queued", "i": 0, "n": len(tracks), "current": "", "skipped": [],
                   "discs": [], "disc": 0, "msg": "", "files": [], "tmp": None, "cancel": False}
            self._jobs.append(job)
        self._push()
        self._wake.set()
        return {"ok": True, "id": job["id"]}

    def jobs(self):
        return self._snapshot()

    def burn_job(self, job_id):
        """Burn the next disc of a downloaded list. Needs a blank disc in the drive."""
        with self._lock:
            job = self._job(job_id)
            if not job or job["state"] != "ready":
                return {"ok": False, "msg": "הרשימה הזו לא מוכנה לצריבה."}
            if self.busy_burning():
                return {"ok": False, "msg": "כבר צורבים דיסק אחר. חכה שהצריבה תסתיים."}
            st = core.burner_status()
            if st["state"] != "ready":
                return {"ok": False, "msg": st["text"]}
            job["state"], job["i"], job["msg"] = "burning", 0, ""
        self._push()
        threading.Thread(target=self._burn, args=(job,), daemon=True).start()
        return {"ok": True}

    def cancel_job(self, job_id):
        with self._lock:
            job = self._job(job_id)
            if not job:
                return {"ok": True}
            if job["state"] == "burning":
                return {"ok": False, "msg": "אי אפשר לעצור באמצע צריבה, כדי לא להרוס את הדיסק."}
            if job["state"] == "downloading":
                job["cancel"] = True  # the downloader stops after the current song and removes the list
            else:
                self._remove(job)
        self._push()
        return {"ok": True}

    def busy(self):
        return any(j["state"] in ("downloading", "burning") for j in self._jobs)

    def busy_burning(self):
        return any(j["state"] == "burning" for j in self._jobs)

    # --- worker side ---
    def _job(self, job_id):
        return next((j for j in self._jobs if j["id"] == job_id), None)

    def _remove(self, job):
        if job in self._jobs:
            self._jobs.remove(job)
        if job["tmp"]:
            shutil.rmtree(job["tmp"], ignore_errors=True)

    def _snapshot(self):
        keep = ("id", "name", "fmt", "state", "i", "n", "current", "skipped", "disc", "msg")
        return [{**{k: j[k] for k in keep}, "discs": len(j["discs"]),
                 "disc_songs": len(j["discs"][j["disc"]]) if j["disc"] < len(j["discs"]) else 0} for j in self._jobs]

    def _push(self):
        if self._window:
            try:
                self._window.evaluate_js(f"App.jobs({json.dumps(self._snapshot(), ensure_ascii=False)})")
            except Exception:
                pass  # window closing

    def _emit(self, **kw):
        self._window.evaluate_js(f"App.progress({json.dumps(kw)})")

    def _downloader(self):
        """Downloads one list at a time, in the order they were added."""
        while True:
            self._wake.wait()
            self._wake.clear()
            while True:
                with self._lock:
                    job = next((j for j in self._jobs if j["state"] == "queued"), None)
                    if not job:
                        break
                    job["state"] = "downloading"
                self._download(job)

    def _download(self, job):
        mp3 = job["fmt"] == "mp3"
        job["tmp"] = tempfile.mkdtemp(prefix="cdburn_")
        try:
            for i, t in enumerate(job["tracks"]):
                if job["cancel"]:
                    with self._lock:
                        self._remove(job)
                    return
                job["i"], job["current"] = i, f"{t['artist']} – {t['title']}"
                self._push()
                try:
                    p = core.download_track(t, os.path.join(job["tmp"], f"{i:03d}"), "mp3" if mp3 else "audio")
                    job["files"].append((t, p, os.path.getsize(p) if mp3 else core.wav_seconds(p)))
                except Exception as e:
                    job["skipped"].append(f"{t['title']} – {core.friendly_error(e)}")
            if not job["files"]:
                job["state"] = "error"
                job["msg"] = "אף שיר לא ירד." + (" הסיבה: " + job["skipped"][0].split(" – ", 1)[-1] if job["skipped"] else "")
                return
            # split for the disc in the drive if there is one (not while it is burning), else a standard 80 min / 700MB disc
            st = {}
            if not self.busy_burning():
                try:
                    st = core.burner_status()
                except Exception:
                    st = {}
            if mp3:
                cap = st.get("cap_bytes") or 700 * 1024 * 1024 * 96 // 100
            else:
                cap = min(core.DEFAULT_CAP, st["cap_s"] - 60) if st.get("cap_s") else core.DEFAULT_CAP
            job["discs"] = core.plan_discs([f[2] for f in job["files"]], cap, 0 if mp3 else core.GAP)
            job["state"], job["current"], job["i"] = "ready", "", 0
        except Exception as e:
            job["state"], job["msg"] = "error", core.friendly_error(e)
        finally:
            self._push()

    def _burn(self, job):
        mp3 = job["fmt"] == "mp3"
        d, idx = job["disc"], job["discs"][job["disc"]]
        files = job["files"]
        try:
            if mp3:  # the disc gets exactly this folder: numbered, readable names
                folder = os.path.join(job["tmp"], f"disc{d + 1}")
                os.makedirs(folder, exist_ok=True)
                for n, k in enumerate(idx, 1):
                    dst = os.path.join(folder, core.disc_name(n, files[k][0]))
                    if not os.path.exists(dst):
                        shutil.copyfile(files[k][1], dst)
                core.burn_data_disc(folder)
            else:
                def on_track(i):
                    job["i"] = i
                    self._push()
                core.burn_disc([files[k][1] for k in idx], on_track)
            try:  # only songs on a disc that finished burning count as burned
                core.record_burn([files[k][0] for k in idx],
                                 {"name": job["name"], "url": job["url"], "count": len(job["tracks"]),
                                  "discs": len(job["discs"]), "at": time.strftime("%Y-%m-%d")})
            except Exception:
                pass
            job["disc"] += 1
            if job["disc"] < len(job["discs"]):
                job["state"] = "ready"  # waits for the next blank disc
            else:
                job["state"] = "done"
                shutil.rmtree(job["tmp"], ignore_errors=True)
                job["tmp"] = None
        except Exception as e:
            job["state"] = "ready"  # the files are still here: the user can retry with another disc
            job["msg"] = "הצריבה נכשלה: " + core.friendly_error(e)
        finally:
            job["i"] = 0
            self._push()


def _focus_running_copy():
    """Two copies would fight over WebView2's data folder (the second one comes up blank).
    If Burner is already open, bring its window forward and return True."""
    import ctypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)  # private instances: don't touch pywebview's ctypes prototypes
    u = ctypes.WinDLL("user32")
    u.FindWindowW.restype = ctypes.c_void_p
    u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    global _mutex
    _mutex = k.CreateMutexW(None, False, "Burner.SingleInstance")  # kept for the process lifetime
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return False
    hwnd = u.FindWindowW(None, "Burner")
    if not hwnd:
        return False
    u.ShowWindow(hwnd, 9)  # SW_RESTORE
    u.SetForegroundWindow(hwnd)
    return True


if __name__ == "__main__" and "--selftest" in sys.argv:
    # build check for the packaged exe: song search + YouTube lookup + bundled ffmpeg, result in burner.log
    try:
        t = core.search_songs("maroon 5 memories")[0]
        print("SELFTEST search ok:", t["title"], "| youtube id:", core._find_video(t["artist"], t["title"], t["ms"]),
              "| ffmpeg bundled:", os.path.exists(core.FFMPEG), flush=True)
        core.latest_release()  # raises if GitHub can't be reached (e.g. certificates)
        print("SELFTEST update check ok", flush=True)
        if os.environ.get("BURNER_SELFTEST_NO_DOWNLOAD"):  # GitHub's servers are blocked by YouTube anyway
            print("SELFTEST download skipped", flush=True)
        else:
            tmp = tempfile.mkdtemp(prefix="burner_selftest_")
            p = core.download_track(t, os.path.join(tmp, "000"))
            print("SELFTEST download ok:", round(core.wav_seconds(p)), "s wav", flush=True)
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        print("SELFTEST failed:", repr(e), flush=True)
    sys.exit(0)

def _drop_page_cache():
    """pywebview serves index.html from a fixed local address, so WebView2's HTTP cache can keep
    showing an old page after an update. Clear only that cache; localStorage (theme, tour) stays."""
    base = os.path.join(os.environ.get("APPDATA", ""), "pywebview", "EBWebView", "Default")
    for d in ("Cache", "Code Cache"):
        shutil.rmtree(os.path.join(base, d), ignore_errors=True)


if __name__ == "__main__":
    import ctypes
    if _focus_running_copy():
        sys.exit(0)
    _drop_page_cache()  # only once we know no other copy is using it
    # own taskbar identity, so Windows doesn't group the window under python and its icon
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Burner.App")
    api = Api()
    api._window = webview.create_window(
        "Burner", os.path.join(HERE, "index.html"),
        js_api=api, width=1200, height=860, min_size=(560, 600), background_color="#0e0e10", maximized=True)
    # private_mode=False keeps localStorage (theme choice) between runs; icon sets the window + taskbar icon
    webview.start(private_mode=False, icon=os.path.join(HERE, "icon.ico"))
    # hard exit once the window is closed: a lingering thread must not leave a windowless
    # process holding WebView2's data folder (that is what made the next launch come up blank)
    os._exit(0)
