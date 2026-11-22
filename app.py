import hashlib
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
        self._toaster = None      # Windows notifications, set up on first use
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

    def version(self):
        return core.VERSION

    def images(self, urls):
        """Cover art as data: URLs for the share image (a canvas can't export pictures from other sites). None where it failed."""
        import base64, urllib.parse, urllib.request
        from concurrent.futures import ThreadPoolExecutor
        hosts = (".mzstatic.com", ".scdn.co", ".ytimg.com", ".spotifycdn.com")  # Apple, Spotify and YouTube artwork

        def one(url):
            host = urllib.parse.urlparse(url or "").hostname or ""
            if not host.endswith(hosts):
                return None
            try:
                r = urllib.request.urlopen(urllib.request.Request(url, headers=core.H), timeout=15, context=core.SSL)
                return f"data:{r.headers.get_content_type() or 'image/jpeg'};base64," + base64.b64encode(r.read(2_000_000)).decode()
            except Exception:
                return None
        with ThreadPoolExecutor(8) as ex:
            return list(ex.map(one, urls))

    def save_image(self, name, data_url):
        """Save the share image (PNG) to Downloads and show it in Explorer."""
        import base64, subprocess
        if not data_url.startswith("data:image/png;base64,"):
            return {"ok": False, "msg": "התמונה לא נוצרה כמו שצריך. נסה שוב."}
        base = os.path.join(os.path.expanduser("~"), "Downloads", core.safe_name(name or "") or "Burner")
        path, n = base + ".png", 1
        while os.path.exists(path):
            n += 1
            path = f"{base} ({n}).png"
        try:
            with open(path, "wb") as f:
                f.write(base64.b64decode(data_url.split(",", 1)[1]))
            subprocess.Popen(["explorer", "/select,", path])
        except Exception as e:
            return {"ok": False, "msg": core.friendly_error(e)}
        return {"ok": True, "path": path}

    def yt_audio(self, vid):
        """Preview of a YouTube song: a direct audio URL, or None."""
        if not core.re.fullmatch(r"[\w-]{11}", vid or ""):
            return None
        try:
            return core.youtube_audio_url(vid)
        except Exception as e:
            core.friendly_error(e)  # logged
            return None

    def search_yt(self, q):
        """YouTube videos and playlists, asked for after Apple's results so those show at once. Empty on any failure."""
        q = (q or "").strip()
        if core.YT_LINK.search(q) or core.is_list_link(q) or len(q) < 2:
            return {"tracks": [], "lists": []}
        from concurrent.futures import ThreadPoolExecutor

        def safe(fn):
            try:
                return fn(q)
            except Exception as e:
                core.friendly_error(e)  # logged; Apple's suggestions are enough
                return []
        with ThreadPoolExecutor(2) as ex:
            tracks, lists = ex.map(safe, (core.youtube_search, core.youtube_lists))
        try:
            core.mark_burned(tracks)
        except Exception:
            pass
        return {"tracks": tracks, "lists": lists}

    def search(self, q):
        """Songs and albums from Apple; a pasted link gives its song (YouTube) or its whole playlist."""
        q = (q or "").strip()
        try:
            if core.is_list_link(q):
                r = core.load_playlist(q)
                tracks, lists = [], [{"kind": "list", "src": "url", "url": q, "name": r["name"], "artist": "", "count": len(r["tracks"]),
                                      "art": next((t.get("art") for t in r["tracks"] if t.get("art")), None), "tracks": r["tracks"]}]
            else:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(2) as ex:
                    songs = ex.submit(core.search_songs, q)
                    albums = ex.submit(core.album_search, q) if len(q) >= 2 and not core.YT_LINK.search(q) else None
                tracks = songs.result()
                try:
                    lists = albums.result() if albums else []
                except Exception as e:
                    core.friendly_error(e)  # logged; the songs still show
                    lists = []
        except Exception as e:
            return {"ok": False, "msg": core.friendly_error(e)}
        try:
            core.mark_burned(tracks)
        except Exception:
            pass
        return {"ok": True, "tracks": tracks, "lists": lists}

    def load_list(self, item):
        """Songs of a list picked in the search box (album, YouTube playlist or pasted link)."""
        try:
            r = core.load_list(item or {})
        except Exception as e:
            return {"ok": False, "msg": core.friendly_error(e)}
        try:
            core.mark_burned(r["tracks"])
        except Exception:
            pass
        return {"ok": True, **r}

    def check_update(self, manual=False):
        """manual (the user pressed the button): also say when there's nothing new or the check failed."""
        if not core.FROZEN:
            return {"error": "בדיקת עדכונים עובדת רק ב־Burner המותקן."} if manual else None
        try:
            return core.latest_release() or ({"latest": core.VERSION} if manual else None)
        except Exception as e:
            return {"error": core.friendly_error(e)} if manual else None  # on start: offline is fine, just skip

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
            return core.load_history()["log"]
        except Exception:
            return []

    def history_tracks(self, k, name, at):
        """The songs of a history entry. Older entries kept only "artist – title" lines: each one is looked up again."""
        try:
            e = core.load_history()["log"][k]
            if e.get("name") != name or e.get("at") != at:
                raise RuntimeError("ההיסטוריה השתנתה בינתיים. פתח אותה שוב ונסה שוב.")
            if e.get("tracks"):
                return {"ok": True, "tracks": e["tracks"]}
            lines = [s for d in e.get("discs", []) for s in d.get("songs", [])] or e.get("songs", [])

            def one(line):
                artist, _, title = line.partition(" – ")
                try:
                    found = core.search_songs(f"{artist} {title}")
                except Exception:
                    found = []
                same = lambda a, b: a.casefold().strip() == b.casefold().strip()
                return (next((t for t in found if same(t["title"], title) and same(t["artist"], artist)), None)
                        or next((t for t in found if same(t["artist"], artist)), None)
                        or {"title": title, "artist": artist, "ms": 0})  # not found: the name is still enough to download it
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(8) as ex:
                return {"ok": True, "tracks": list(ex.map(one, lines))}
        except Exception as e:
            return {"ok": False, "msg": core.friendly_error(e)}

    def rename_history(self, k, old, at, name):
        """old/at: the row the page showed, so a list logged meanwhile can't shift the rename to another row."""
        name = (name or "").strip()[:120]
        if not name:
            return {"ok": False, "msg": "השם לא יכול להיות ריק."}
        try:
            core.rename_log(k, old, at, name)
        except Exception as e:
            return {"ok": False, "msg": core.friendly_error(e)}
        return {"ok": True}

    def delete_history(self, k, name, at):
        """name/at: the row the page showed (see rename_history). Its songs stop counting as burned."""
        try:
            core.delete_log(k, name, at)
        except Exception as e:
            return {"ok": False, "msg": core.friendly_error(e)}
        return {"ok": True}

    def status(self):
        return core.burner_status()

    # --- the queue: lists download in the background, then wait for the user to burn them ---
    # job states: queued -> downloading -> ready -> burning -> ready (next disc) ... -> done | error
    def enqueue(self, tracks, name=None, url=None, fmt="audio", only=False, save=True):
        """only: download to a folder in Downloads, no burning. save=False: leave no trace in the history (burning for a friend)."""
        if not tracks:
            return {"ok": False, "msg": "לא נבחרו שירים."}
        with self._lock:
            self._next_id += 1
            job = {"id": self._next_id, "name": name or "פלייליסט", "url": url, "fmt": "mp3" if fmt == "mp3" else "audio",
                   "tracks": tracks, "state": "queued", "i": 0, "n": len(tracks), "current": "", "skipped": [],
                   "discs": [], "disc": 0, "msg": "", "files": [], "tmp": None, "cancel": False, "only": bool(only), "logged": False, "pct": 0, "save": bool(save)}
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
                self._log_cancel(j)
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
                self._log_cancel(job)
                self._remove(job)
        self._push()
        return {"ok": True}

    # --- changing a list that is already in the queue (before its first disc is burned) ---
    EDITABLE = ("queued", "downloading", "ready", "error")
    NO_EDIT = "אפשר לשנות את הרשימה רק לפני שהתחלת לצרוב אותה."

    @staticmethod
    def _skip_of(job, t):
        return next((x for x in job["skipped"] if x.startswith(t["title"] + " – ")), None)

    @staticmethod
    def _has(job, t):
        k = core._key(t)
        return any(core._key(f[0]) == k for f in job["files"])

    def _pending(self, job):
        return [i for i, t in enumerate(job["tracks"]) if not self._has(job, t) and not self._skip_of(job, t)]

    @staticmethod
    def _sort(job):
        order = {core._key(t): n for n, t in enumerate(job["tracks"])}
        job["files"].sort(key=lambda f: order.get(core._key(f[0]), 0))

    def _requeue(self, job):
        """After a change: download what's missing, or re-split the discs if everything is here."""
        if job["state"] not in ("ready", "error"):
            return  # queued/downloading: the downloader picks the change up by itself
        job.update(msg="", logged=False, discs=[])
        if self._pending(job):
            job["state"] = "queued"
            self._wake.set()
        elif job["files"]:
            self._plan(job)
        else:
            job["state"] = "error"

    def update_job(self, job_id, tracks):
        """The editor sends the list's new songs, in order: added, removed or reordered."""
        with self._lock:
            job = self._job(job_id)
            if not job or job["state"] not in self.EDITABLE or job["disc"]:
                return {"ok": False, "msg": self.NO_EDIT}
            if not tracks:
                return {"ok": False, "msg": "לא נשארו שירים ברשימה. כדי לוותר עליה, לחץ בטל בחלון התור."}
            keys = {core._key(t) for t in tracks}
            for f in job["files"]:
                if core._key(f[0]) not in keys:
                    try:
                        os.remove(f[1])
                    except OSError:
                        pass
            job["files"] = [f for f in job["files"] if core._key(f[0]) in keys]
            job["skipped"] = [x for x in job["skipped"] if any(x.startswith(t["title"] + " – ") for t in tracks)]
            job["tracks"], job["n"] = tracks, len(tracks)
            self._sort(job)
            self._requeue(job)
        self._push()
        return {"ok": True}

    def retry_job(self, job_id, title=None):
        """Download skipped songs again (one by title, or all); songs already downloaded are kept."""
        with self._lock:
            job = self._job(job_id)
            if not job or job["state"] not in self.EDITABLE or job["disc"]:
                return {"ok": False, "msg": self.NO_EDIT}
            job["skipped"] = [x for x in job["skipped"] if title is not None and not x.startswith(title + " – ")]
            self._requeue(job)
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
        keep = ("id", "name", "url", "fmt", "only", "state", "i", "n", "current", "skipped", "disc", "msg")
        return [{**{k: j[k] for k in keep}, "discs": len(j["discs"]),
                 "disc_songs": len(j["discs"][j["disc"]]) if j["disc"] < len(j["discs"]) else 0,
                 "tracks": j["tracks"], "pct": j["pct"],
                 "songs": [["done", ""] if self._has(j, t) else ["err", (self._skip_of(j, t) or "").split(" – ", 1)[-1]]
                           if self._skip_of(j, t) else ["dl" if j["state"] == "downloading" and k == j["i"] else "wait", ""]
                           for k, t in enumerate(j["tracks"])],
                 "disc_lists": [[f"{j['files'][k][0]['artist']} – {j['files'][k][0]['title']}" for k in d] for d in j["discs"]]}
                for j in self._jobs]

    QUEUE = os.path.join(core.DATA, "queue.json")
    SAVED = ("id", "name", "url", "fmt", "tracks", "state", "skipped", "discs", "disc", "msg", "files", "tmp", "only", "logged", "save")

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
                j.setdefault("only", False)  # queues saved before download-only existed
                j.setdefault("logged", False)
                j.setdefault("save", True)
                j.update(i=0, n=len(j["tracks"]), current="", cancel=False, pct=0, was_burning=j["state"] == "burning")
                if j["state"] in ("downloading", "queued") or (j["state"] in ("ready", "burning") and self._pending(j)):
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

    # --- Windows notifications: only when the user isn't looking at Burner ---
    def _away(self):
        if self._hidden:
            return True
        import ctypes
        u = ctypes.WinDLL("user32")  # private instance: don't touch pywebview's ctypes prototypes
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(u.GetForegroundWindow(), ctypes.byref(pid))
        return pid.value != os.getpid()

    def _notify(self, title, body, buttons=(), tag=None):
        """buttons: (label, action) pairs; actions are burn:<id> or retry:<id>:<song title, empty for all>."""
        try:
            if not self._away():
                return
            from windows_toasts import InteractableWindowsToaster, Toast, ToastButton
            if not self._toaster:  # Burner.App is the ID on Burner's Start-menu shortcut (installer)
                self._toaster = InteractableWindowsToaster("Burner", notifierAUMID="Burner.App")
            toast = Toast([title, body], on_activated=lambda a: self._toast_action(a.arguments or ""))
            if tag:
                toast.tag = tag  # a newer toast with the same tag replaces the old one
            for label, action in buttons:
                toast.AddAction(ToastButton(label, action))
            self._toaster.show_toast(toast)
        except Exception as e:
            core.friendly_error(e)  # logged; a missing notification must never break the queue

    def _toast_action(self, arg):
        kind, _, rest = arg.partition(":")
        if kind == "burn":
            r = self.burn_job(int(rest))
        elif kind == "retry":
            job_id, _, title = rest.partition(":")
            r = self.retry_job(int(job_id), title or None)
        else:  # the toast itself was clicked
            r = {"ok": True}
        if kind not in ("burn", "retry") or not r["ok"]:
            self._hidden = False
            try:
                self._window.show()
                self._window.restore()
                if not r["ok"]:
                    self._window.evaluate_js(f"modal('לא אפשרי כרגע', {json.dumps(r['msg'], ensure_ascii=False)}, 0, [['אישור', close, true]])")
            except Exception:
                pass

    def _push(self):
        self._save()
        self._show_if_hidden()
        if self._window:
            try:
                self._window.evaluate_js(f"App.jobs({json.dumps(self._snapshot(), ensure_ascii=False)})")
            except Exception:
                pass  # window closing

    def _emit(self, **kw):
        if self._window:
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
            while True:  # the list can change while it downloads, so pick the next missing song each time
                with self._lock:
                    if job["cancel"]:
                        self._log(job, "cancelled")
                        self._remove(job)
                        return
                    todo = self._pending(job)
                    if not todo:
                        self._sort(job)
                        if not job["files"]:
                            job["state"] = "error"
                            job["msg"] = "אף שיר לא ירד." + (" הסיבה: " + job["skipped"][0].split(" – ", 1)[-1] if job["skipped"] else "")
                            self._notify(f"{job['name']}: ההורדה נכשלה", job["msg"], [("נסה שוב", f"retry:{job['id']}:")])
                            return
                        if not job["only"]:
                            self._plan(job)
                            n = len(job["discs"])
                            self._notify(f"{job['name']} מוכנה לצריבה", "הכנס דיסק ריק ולחץ צרוב" + (f" · {n} דיסקים" if n > 1 else "")
                                         + (f" · {len(job['skipped'])} שירים לא ירדו" if job["skipped"] else ""),
                                         [("צרוב" + (" דיסק 1" if n > 1 else ""), f"burn:{job['id']}")])
                        break
                    i = todo[0]
                    t = job["tracks"][i]
                    job["i"], job["current"], job["pct"] = i, f"{t['artist']} – {t['title']}", 0
                self._push()

                def on_pct(p, k=f"{t['artist']}|{t['title']}".lower()):  # the page's keyOf()
                    pct = round(p * 100)
                    if pct - job["pct"] >= 3 or pct == 100:  # small steps only, the page redraws on each one
                        job["pct"] = pct
                        self._emit(phase="dl", id=job["id"], key=k, pct=pct)
                # named by the song, not its place: places change when the list is edited
                base = os.path.join(job["tmp"], hashlib.md5(core._key(t).encode()).hexdigest()[:12])
                try:
                    p = core.download_track(t, base, "mp3" if mp3 else "audio", on_pct)
                    with self._lock:
                        if any(core._key(x) == core._key(t) for x in job["tracks"]):
                            job["files"].append((t, p, os.path.getsize(p) if mp3 else core.wav_seconds(p)))
                        else:  # removed from the list while it downloaded
                            os.remove(p)
                except Exception as e:
                    with self._lock:
                        job["skipped"].append(f"{t['title']} – {core.friendly_error(e)}")
                    sk = job["skipped"]
                    self._notify(f"השיר {t['title']} לא ירד" if len(sk) == 1 else f"{len(sk)} שירים לא ירדו", f"{job['name']} · {core.friendly_error(e)}",
                                 [("נסה שוב", f"retry:{job['id']}:" + (t['title'] if len(sk) == 1 else ""))], tag=f"skip{job['id']}")
            if job["only"]:
                self._export(job)
        except Exception as e:
            job["state"], job["msg"] = "error", core.friendly_error(e)
        finally:
            if job["state"] == "error":
                self._log(job, "failed")
            self._push()

    def _plan(self, job):
        """Split the downloaded songs into discs and mark the list ready.
        Sized for the disc in the drive if there is one (not while it is burning), else a standard 80 min / 700MB disc."""
        mp3 = job["fmt"] == "mp3"
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

    def _log_cancel(self, job):
        """The user dropped a list that hadn't finished: cancelled, or partly burned if some discs were done."""
        if job["state"] in ("queued", "downloading", "ready"):
            self._log(job, "partial" if job["disc"] else "cancelled")

    def _log(self, job, status):
        """Write the list to the activity history once, with what happened to each disc."""
        if job.get("logged") or not job.get("save", True):
            return
        job["logged"] = True
        song = lambda t: f"{t['artist']} – {t['title']}"
        rest = "failed" if status == "failed" else "cancelled"
        discs = [{"songs": [song(job["files"][k][0]) for k in d], "state": "burned" if n < job["disc"] else rest}
                 for n, d in enumerate(job["discs"])]
        songs = [] if discs else [song(f[0]) for f in job["files"]] or [song(t) for t in job["tracks"]]
        try:
            core.log_job({"name": job["name"], "url": job["url"], "fmt": job["fmt"], "status": status,
                          "at": time.strftime("%Y-%m-%d %H:%M"), "count": len(job["tracks"]), "ndiscs": len(discs),
                          "discs": discs, "songs": songs, "skipped": job["skipped"], "msg": job["msg"],
                          "tracks": job["tracks"]})  # so the card can reopen the list
        except Exception as e:
            core.friendly_error(e)  # logged; history is a record, it must not break the queue

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
        job["msg"] = f"נשמר בתיקייה {out}"
        self._notify(f"{job['name']} ירדה", job["msg"])
        self._log(job, "downloaded")
        shutil.rmtree(job["tmp"], ignore_errors=True)
        job["tmp"], job["files"] = None, []
        job["state"], job["current"], job["i"] = "done", "", 0
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
            if job["save"]:
                try:  # only songs on a disc that finished burning count as burned
                    core.record_burn([files[k][0] for k in idx])
                except Exception:
                    pass
            job["disc"] += 1
            if job["disc"] < len(job["discs"]):
                job["state"] = "ready"  # waits for the next blank disc
                self._notify(f"דיסק {job['disc']} מתוך {len(job['discs'])} נצרב", f"{job['name']} · הוצא את הדיסק והכנס דיסק ריק",
                             [(f"צרוב דיסק {job['disc'] + 1}", f"burn:{job['id']}")])
            else:
                job["state"] = "done"
                self._log(job, "burned")
                self._notify(f"{job['name']} נצרבה", f"{len(job['discs'])} דיסקים מוכנים" if len(job["discs"]) > 1 else "הדיסק מוכן")
                with self._lock:
                    self._remove(job)  # burned lists leave the queue; the history keeps the record
        except Exception as e:
            job["state"] = "ready"  # the files are still here: the user can retry with another disc
            job["msg"] = "הצריבה נכשלה: " + core.friendly_error(e)
            self._notify(f"{job['name']}: הצריבה נכשלה", core.friendly_error(e), [("נסה שוב", f"burn:{job['id']}")])
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
