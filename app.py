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
        self._hold = False        # True while the user hasn't said what to do with lists from last time
        self._hidden = False      # window hidden: work goes on in the background
        self._quitting = False
        self._restore()
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
    def enqueue(self, tracks, name=None, url=None, fmt="audio", only=False):
        """only: download to a folder in Downloads, no burning."""
        if not tracks:
            return {"ok": False, "msg": "לא נבחרו שירים."}
        with self._lock:
            self._next_id += 1
            job = {"id": self._next_id, "name": name or "פלייליסט", "url": url, "fmt": "mp3" if fmt == "mp3" else "audio",
                   "tracks": tracks, "state": "queued", "i": 0, "n": len(tracks), "current": "", "skipped": [],
                   "discs": [], "disc": 0, "msg": "", "files": [], "got": [], "tmp": None, "cancel": False, "only": bool(only)}
            self._jobs.append(job)
        self._push()
        self._wake.set()
        return {"ok": True, "id": job["id"]}

    def jobs(self):
        return self._snapshot()

    # --- lists left over from the last run (closed or killed mid-way) ---
    def restored(self):
        return {"count": len(self._jobs) if self._hold else 0,
                "burning": any(j.get("was_burning") for j in self._jobs)}

    def resume_restored(self):
        self._hold = False
        self._wake.set()
        self._push()
        return {"ok": True}

    def clear_restored(self):
        with self._lock:
            for j in list(self._jobs):
                self._remove(j)
            self._hold = False
        self._push()
        return {"ok": True}

    # --- closing the window ---
    def _on_closing(self):
        if self._quitting or not self.busy():
            self._save()
            return True  # nothing running (ready lists are saved and come back next time)
        burning = self.busy_burning()
        threading.Thread(target=lambda: self._window.evaluate_js(f"App.confirmClose({json.dumps(burning)})"), daemon=True).start()
        return False     # keep the window until the user chooses

    def close_choice(self, choice):
        if choice == "background":
            self._hidden = True
            self._window.hide()
        elif choice == "quit":
            self._quitting = True
            self._save()
            self._window.destroy()
        return {"ok": True}

    def _show_if_hidden(self):
        """Bring the window back when a hidden Burner has something for the user (a list ready, or all done)."""
        ready = any(j["state"] == "ready" for j in self._jobs) and not self.busy_burning()
        if self._hidden and (ready or not self.busy()):
            self._hidden = False
            try:
                self._window.show()
                self._window.restore()
            except Exception:
                pass

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
        self._save()

    def _snapshot(self):
        keep = ("id", "name", "fmt", "only", "state", "i", "n", "current", "skipped", "disc", "msg")
        return [{**{k: j[k] for k in keep}, "discs": len(j["discs"]),
                 "disc_songs": len(j["discs"][j["disc"]]) if j["disc"] < len(j["discs"]) else 0} for j in self._jobs]

    QUEUE = os.path.join(core.DATA, "queue.json")
    SAVED = ("id", "name", "url", "fmt", "tracks", "state", "skipped", "discs", "disc", "msg", "files", "got", "tmp", "only")

    def _save(self):
        with self._lock:
            data = [{k: j[k] for k in self.SAVED} for j in self._jobs if j["state"] != "done"]
            try:
                tmp = self.QUEUE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, self.QUEUE)
            except OSError as e:
                core.friendly_error(e)  # logged; the queue still works for this run

    def _restore(self):
        """Lists from last time: downloads resume where they stopped; a burn cut off mid-way must start over."""
        try:
            with open(self.QUEUE, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        for j in saved:
            try:
                j["files"] = [f for f in j["files"] if os.path.exists(f[1])]  # downloaded songs that are still on disk
                j["got"] = [i for i in j["got"] if any(f[1].endswith(f"{i:03d}.wav") or f[1].endswith(f"{i:03d}.mp3") for f in j["files"])]
                j.setdefault("only", False)  # queues saved before download-only existed
                j.update(i=0, n=len(j["tracks"]), current="", cancel=False, was_burning=j["state"] == "burning")
                if j["state"] in ("downloading", "queued") or (j["state"] in ("ready", "burning") and len(j["got"]) + len(j["skipped"]) < j["n"]):
                    j["state"], j["discs"] = "queued", []
                elif j["state"] == "burning":
                    j["state"] = "ready"
                    j["msg"] = "הצריבה נעצרה באמצע, והדיסק כנראה לא שמיש. הכנס דיסק ריק חדש ולחץ צרוב."
                if j["state"] in ("queued", "ready", "error"):
                    self._jobs.append(j)
            except (KeyError, TypeError, IndexError):
                continue  # a damaged entry is skipped, the rest still come back
        self._next_id = max([j["id"] for j in self._jobs], default=0)
        self._hold = bool(self._jobs)

    def _push(self):
        self._save()
        self._show_if_hidden()
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
                    job = None if self._hold else next((j for j in self._jobs if j["state"] == "queued"), None)
                    if not job:
                        break
                    job["state"] = "downloading"
                self._download(job)

    def _download(self, job):
        mp3 = job["fmt"] == "mp3"
        if not job["tmp"]:
            job["tmp"] = os.path.join(core.DATA, "jobs", str(job["id"]))
        os.makedirs(job["tmp"], exist_ok=True)
        try:
            for i, t in enumerate(job["tracks"]):
                if i in job["got"] or any(x.startswith(t["title"] + " – ") for x in job["skipped"]):
                    continue  # done before Burner was closed
                if job["cancel"]:
                    with self._lock:
                        self._remove(job)
                    return
                job["i"], job["current"] = i, f"{t['artist']} – {t['title']}"
                self._push()
                try:
                    p = core.download_track(t, os.path.join(job["tmp"], f"{i:03d}"), "mp3" if mp3 else "audio")
                    job["files"].append((t, p, os.path.getsize(p) if mp3 else core.wav_seconds(p)))
                    job["got"].append(i)
                except Exception as e:
                    job["skipped"].append(f"{t['title']} – {core.friendly_error(e)}")
            job["files"].sort(key=lambda f: next((n for n, t in enumerate(job["tracks"]) if t == f[0]), 0))
            if not job["files"]:
                job["state"] = "error"
                job["msg"] = "אף שיר לא ירד." + (" הסיבה: " + job["skipped"][0].split(" – ", 1)[-1] if job["skipped"] else "")
                return
            if job["only"]:
                self._export(job)
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

    def _export(self, job):
        """Download-only list: move the songs, numbered in order, to Downloads\<list name>, then open that folder."""
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        base = os.path.join(downloads, core.safe_name(job["name"]) or "Burner")
        out, n = base, 1
        while os.path.exists(out):
            n += 1
            out = f"{base} ({n})"
        os.makedirs(out)
        for k, (t, p, _) in enumerate(job["files"], 1):
            shutil.move(p, os.path.join(out, os.path.splitext(core.disc_name(k, t))[0] + os.path.splitext(p)[1]))
        shutil.rmtree(job["tmp"], ignore_errors=True)
        job["tmp"], job["files"] = None, []
        job["state"], job["current"], job["i"], job["msg"] = "done", "", 0, f"נשמר בתיקייה {out}"
        try:
            os.startfile(out)
        except OSError:
            pass  # the path is in the message anyway

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
    api._window.events.closing += api._on_closing
    # private_mode=False keeps localStorage (theme choice) between runs; icon sets the window + taskbar icon
    webview.start(private_mode=False, icon=os.path.join(HERE, "icon.ico"))
    # hard exit once the window is closed: a lingering thread must not leave a windowless
    # process holding WebView2's data folder (that is what made the next launch come up blank)
    os._exit(0)
