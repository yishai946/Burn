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
        self._cancel = False
        self._cont = threading.Event()
        self._busy = False

    # --- called from the UI ---
    def load(self, url):
        try:
            r = core.load_playlist(url)
        except Exception as e:
            return {"ok": False, "msg": str(e)}
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
            return {"ok": False, "msg": str(e)}
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
        if self._busy:
            return {"ok": False, "msg": "אי אפשר לעדכן באמצע צריבה."}
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
            return self._emit(phase="update_error", msg=str(e))
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

    def burn(self, tracks, name=None, url=None):
        if self._busy:
            return {"ok": False, "msg": "כבר צורב."}
        st = core.burner_status()
        if st["state"] != "ready":
            return {"ok": False, "msg": st["text"]}
        self._cancel, self._busy = False, True
        self._cont.clear()
        pl = {"name": name or "Playlist", "url": url, "count": len(tracks), "at": time.strftime("%Y-%m-%d")}
        threading.Thread(target=self._run, args=(tracks, st["cap_s"], pl), daemon=True).start()
        return {"ok": True}

    def proceed(self):
        self._cont.set()

    def abort(self):
        self._cancel = True
        self._cont.set()

    # --- worker ---
    def _emit(self, **kw):
        self._window.evaluate_js(f"App.progress({json.dumps(kw)})")

    def _run(self, tracks, cap_s, pl):
        tmp = tempfile.mkdtemp(prefix="cdburn_")
        try:
            cap = min(core.DEFAULT_CAP, cap_s - 60)
            files, skipped = [], []  # files: (track, path, seconds)
            for i, t in enumerate(tracks):
                if self._cancel:
                    return self._emit(phase="cancelled")
                self._emit(phase="download", i=i, n=len(tracks), name=f"{t['artist']} – {t['title']}")
                try:
                    p = core.download_track(t, os.path.join(tmp, f"{i:03d}"))
                    files.append((t, p, core.wav_seconds(p)))
                except Exception as e:
                    skipped.append(f"{t['title']}: {e}")
            if not files:
                return self._emit(phase="error", msg="אף שיר לא ירד.")
            discs = core.plan_discs([f[2] for f in files], cap)
            for d, idx in enumerate(discs):
                if d > 0:
                    self._emit(phase="insert", disc=d + 1, of=len(discs))
                    while True:  # wait for a blank disc (auto-detects, or the user presses Continue)
                        if self._cancel:
                            return self._emit(phase="cancelled")
                        if core.burner_status()["state"] == "ready":
                            break
                        self._cont.wait(2)
                        self._cont.clear()
                self._emit(phase="burn", disc=d + 1, of=len(discs), i=0, n=len(idx))
                core.burn_disc([files[k][1] for k in idx],
                               lambda i, d=d, n=len(idx): self._emit(phase="burn", disc=d + 1, of=len(discs), i=i, n=n))
                try:  # only songs on a disc that finished burning count as burned
                    pl["discs"] = len(discs)
                    core.record_burn([files[k][0] for k in idx], pl)
                except Exception:
                    pass
            self._emit(phase="done", discs=len(discs), skipped=skipped)
        except Exception as e:
            self._emit(phase="error", msg=str(e))
        finally:
            self._busy = False
            shutil.rmtree(tmp, ignore_errors=True)


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
        tmp = tempfile.mkdtemp(prefix="burner_selftest_")
        p = core.download_track(t, os.path.join(tmp, "000"))
        print("SELFTEST download ok:", round(core.wav_seconds(p)), "s wav", flush=True)
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        print("SELFTEST failed:", repr(e), flush=True)
    sys.exit(0)

if __name__ == "__main__":
    import ctypes
    if _focus_running_copy():
        sys.exit(0)
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
