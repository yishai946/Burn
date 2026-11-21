"""Playlist scraping, disc planning, downloading and IMAPI2 burning."""
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import urllib.request
import wave

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
GAP = 2                # seconds of silence the CD format puts before each track
DEFAULT_CAP = 79 * 60  # usable seconds on an "80 min" disc (1 min safety margin)
MP3_KBPS = 192         # data-disc MP3s are constant bitrate, so size = seconds * MP3_KBPS * 125
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
try:  # Python's own CA list can miss current issuers (GitHub failed); certifi's bundle is kept current
    import certifi
    SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL = ssl.create_default_context()
VERSION = "1.7.0"             # bump for every release; build.bat tags the GitHub release with it
REPO = "yishai946/Burn"        # GitHub repo whose releases the in-app updater checks
FROZEN = getattr(sys, "frozen", False)  # running as the packaged Burner.exe
RES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))  # bundled files (index.html, ffmpeg.exe)
# packaged: per-user data in %APPDATA%\Burner; from source: next to the code, as before
DATA = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Burner") if FROZEN else os.path.dirname(os.path.abspath(__file__))
os.makedirs(DATA, exist_ok=True)
FFMPEG = os.path.join(RES, "ffmpeg.exe")  # bundled in the package; from source, yt-dlp finds ffmpeg on PATH


# ---------- errors shown to the user ----------
HEBREW = re.compile(r"[֐-׿]")
NET = "אין חיבור לאינטרנט, או שהשרת לא עונה. בדוק את החיבור ונסה שוב."
BURN_HELP = "נסה דיסק CD-R ריק אחר. אם זה חוזר, נתק וחבר את הצורב ונסה שוב."
# (words in the English error text, Hebrew message) – checked in order, first match wins
_KNOWN = [
    (("sign in to confirm", "not a bot"), "יוטיוב חסם זמנית את ההורדות מהמחשב הזה. נסה שוב בעוד כמה דקות."),
    (("video unavailable", "private video", "has been removed", "not available in your country"), "השיר לא זמין להורדה ביוטיוב."),
    (("no search results",), "השיר לא נמצא ביוטיוב."),
    (("requested format is not available",), "לא נמצאה ביוטיוב גרסת שמע של השיר."),
    (("ffmpeg", "ffprobe"), "רכיב המרת השמע חסר או פגום. התקן את Burner מחדש."),
    (("http error 429", "too many requests"), "נשלחו יותר מדי בקשות. חכה כמה דקות ונסה שוב."),
    (("http error 403", "forbidden"), "השרת סירב לבקשה. נסה שוב בעוד כמה דקות."),
    (("http error 404", "not found"), "הכתובת לא נמצאה. בדוק שהקישור נכון ושהפלייליסט ציבורי."),
    (("http error 5", "service unavailable", "bad gateway"), "השירות לא זמין כרגע. נסה שוב מאוחר יותר."),
    (("certificate", "ssl"), "החיבור המאובטח נכשל. בדוק שהשעון של המחשב מכוון, ונסה שוב."),
    (("getaddrinfo", "name or service", "nodename", "timed out", "timeout", "connection", "network", "urlopen error", "unreachable"), NET),
    (("no media", "media is not present", "medium not present"), "אין דיסק בצורב. הכנס דיסק CD-R ריק ונסה שוב."),
    (("not blank", "is not empty", "media is not supported", "incompatible"), "הדיסק בצורב לא ריק או לא מתאים. הכנס דיסק CD-R ריק."),
    (("upside down",), "נראה שהדיסק הוכנס הפוך. הפוך אותו ונסה שוב."),
    (("write protect",), "הדיסק מוגן מפני כתיבה. הכנס דיסק CD-R ריק אחר."),
    (("not enough space", "too large", "insufficient space", "exceeds"), "השירים לא נכנסים בדיסק. בטל כמה שירים ונסה שוב."),
    (("busy", "in use", "locked", "becoming ready"), "הצורב עסוק כרגע (אולי תוכנה אחרת משתמשת בו). חכה רגע ונסה שוב."),
]


def friendly_error(e):
    """A clear Hebrew message for any exception; the original goes to the log for debugging."""
    import traceback
    print("".join(traceback.format_exception(type(e), e, e.__traceback__)).strip(), file=sys.stderr, flush=True)
    text = str(e)
    if HEBREW.search(text):
        return text  # already written for the user
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "אין מספיק מקום פנוי בכונן של המחשב. פנה מקום ונסה שוב."
    if isinstance(e, PermissionError):
        return "אין הרשאה לגשת לקובץ. סגור תוכנות אחרות שמשתמשות בו ונסה שוב."
    is_com = type(e).__name__ == "com_error"  # pywintypes.com_error from the Windows burning API
    if is_com:  # its readable part is the description inside the error details
        text = " ".join(str(x) for x in e.args if x)
    low = text.lower()
    for words, msg in _KNOWN:
        if any(w in low for w in words):
            return msg
    if is_com:
        code = e.args[0] if e.args else ""
        return f"הצריבה נכשלה. {BURN_HELP} (קוד: {code & 0xFFFFFFFF:#010x})" if isinstance(code, int) else f"הצריבה נכשלה. {BURN_HELP}"
    return "אירעה שגיאה לא צפויה. נסה שוב, ואם זה חוזר, שלח את הקובץ burner.log מהתיקייה %APPDATA%\Burner."


# ---------- playlist scraping ----------
def _get(url):
    url = urllib.parse.quote(url, safe=":/?&=%#@+~,;")  # Hebrew etc. in the path
    return urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=20, context=SSL).read().decode("utf-8", "replace")


def _walk(o, out):
    if isinstance(o, dict):
        if o.get("contentDescriptor", {}).get("kind") == "song" and "artistName" in o and "duration" in o:
            out.append(o)
        for v in o.values():
            _walk(v, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, out)


def _bidi(s):
    return re.sub(r"[‎‏‪-‮⁦-⁩]", "", s)


def _add_apple_previews(url, tracks):
    """30s preview links from Apple's public iTunes lookup API (one batch call)."""
    sf = re.search(r"music\.apple\.com/(\w\w)/", url)
    ids = ",".join(t["id"] for t in tracks if t["id"])
    try:
        res = json.loads(_get(f"https://itunes.apple.com/lookup?id={ids}&country={sf.group(1) if sf else 'us'}"))["results"]
        info = {str(x.get("trackId")): x for x in res}
    except Exception:
        info = {}  # previews and artwork are optional
    for t in tracks:
        x = info.get(t["id"], {})
        t["preview"], t["art"] = x.get("previewUrl"), x.get("artworkUrl100")


def _apple(url):
    html = _get(url)
    m = re.search(r'id="serialized-server-data"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("לא הצלחתי לקרוא את דף Apple Music. האם הפלייליסט ציבורי?")
    found = []
    _walk(json.loads(m.group(1)), found)
    seen, tracks = set(), []
    for o in found:
        if o["id"] in seen:
            continue
        seen.add(o["id"])
        tracks.append({"title": _bidi(o["title"]), "artist": _bidi(o["artistName"]), "ms": int(o["duration"]),
                       "id": o["contentDescriptor"].get("identifiers", {}).get("storeAdamID")})
    _add_apple_previews(url, tracks)
    t = re.search(r"<title>(.*?)</title>", html, re.S)
    name = re.split(r" (?:by|-) ", re.sub(r"[‎‏]", "", t.group(1)).strip())[0] if t else "פלייליסט"
    return name, tracks


def _add_spotify_art(tracks):
    """Cover art via Spotify's public oEmbed endpoint (one small request per track, in parallel)."""
    def one(t):
        try:
            tid = t["uri"].split(":")[-1]
            t["art"] = json.loads(_get(f"https://open.spotify.com/oembed?url=https://open.spotify.com/track/{tid}"))["thumbnail_url"]
        except Exception:
            t["art"] = None  # artwork is optional
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, tracks))


def _spotify(url):
    m = re.search(r"(playlist|album)[/:]([A-Za-z0-9]+)", url)
    if not m:
        raise ValueError("קישור Spotify לא מזוהה.")
    html = _get(f"https://open.spotify.com/embed/{m.group(1)}/{m.group(2)}")
    nd = re.search(r"__NEXT_DATA__[^>]*>(.*?)</script>", html, re.S)
    if not nd:
        raise ValueError("לא הצלחתי לקרוא את דף Spotify. האם הפלייליסט ציבורי?")
    ent = json.loads(nd.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    tracks = [
        {"title": t["title"], "artist": t["subtitle"].replace("\xa0", " "), "ms": int(t["duration"]),
         "preview": (t.get("audioPreview") or {}).get("url"), "uri": t.get("uri", "")}
        for t in ent.get("trackList", [])
        if t.get("duration")
    ]
    _add_spotify_art(tracks)
    return ent.get("name") or ent.get("title") or "פלייליסט", tracks


def load_playlist(url):
    url = url.strip()
    try:
        if "music.apple.com" in url:
            name, tracks = _apple(url)
        elif "spotify.com" in url or url.startswith("spotify:"):
            name, tracks = _spotify(url)
        else:
            raise ValueError("קישור לשיר מיוטיוב? הדבק אותו בתיבת החיפוש של שירים." if YT_LINK.search(url)
                             else "הדבק קישור לפלייליסט של Apple Music או Spotify.")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        # the page loaded but holds no playlist: a wrong/deleted link, or a private playlist
        friendly_error(e)  # log the details
        raise ValueError("הפלייליסט לא נמצא. בדוק שהקישור נכון ושהפלייליסט ציבורי.") from e
    if not tracks:
        raise ValueError("לא נמצאו שירים בקישור הזה.")
    return {"name": name, "tracks": tracks}


YT_LINK = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/)([\w-]{11})")


def _yt_song(vid, title, uploader, duration, artist=None, track=None):
    """A song from a YouTube video. preview "yt:<id>": the page asks youtube_audio_url() for it when played."""
    title = title or vid
    m = re.match(r"(.+?)\s+[-–]\s+(.+)", title)  # most music videos are titled "Artist - Song"
    if not (artist and track) and m:
        artist, title = artist or m.group(1), m.group(2)
    artist = artist or re.sub(r"\s*-\s*Topic$", "", uploader or "YouTube")
    title = track or title
    return {"title": title, "artist": artist, "ms": int(duration * 1000), "yt": vid,
            "art": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg", "preview": f"yt:{vid}"}


def youtube_track(vid):
    """A song straight from a YouTube video, for when the name search can't find the right one."""
    with _ydl(skip_download=True) as y:
        x = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False, process=False)
    if not x.get("duration") or x.get("is_live"):
        raise ValueError("זה שידור חי או סרטון בלי אורך ידוע. הדבק קישור לסרטון רגיל.")
    return _yt_song(vid, x.get("title"), x.get("uploader"), x["duration"], x.get("artist"), x.get("track"))


def youtube_audio_url(vid):
    """A direct audio stream for previewing a YouTube song (expires after a few hours)."""
    with _ydl(format="bestaudio[ext=m4a]/bestaudio", skip_download=True) as y:
        return y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)["url"]


def _itunes(q, limit):
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": q, "entity": "song", "limit": limit, "country": "il"})
    return [{"title": x["trackName"], "artist": x["artistName"], "ms": int(x["trackTimeMillis"]),
             "id": str(x["trackId"]), "preview": x.get("previewUrl"), "art": x.get("artworkUrl100")}
            for x in json.loads(_get(url))["results"]
            if x.get("trackName") and x.get("trackTimeMillis")]


def youtube_search(q, n=4):
    """YouTube videos as song suggestions: songs that are only on YouTube (like a clip) aren't in Apple's catalog."""
    with _ydl(extract_flat=True, skip_download=True) as y:
        info = y.extract_info(f"ytsearch{n}:{q}", download=False)
    return [_yt_song(e["id"], e.get("title"), e.get("channel") or e.get("uploader"), e["duration"])
            for e in (info or {}).get("entries") or [] if e and e.get("id") and e.get("duration")]


def search_songs(q, limit=8):
    """Song suggestions for the search box, from Apple's public iTunes Search API, or one song from a YouTube link."""
    q = q.strip()
    m = YT_LINK.search(q)
    if m:
        try:
            return [youtube_track(m.group(1))]
        except Exception as e:  # friendly_error keeps our own Hebrew messages as they are
            raise RuntimeError(friendly_error(e)) from e
    return _itunes(q, limit) if len(q) >= 2 else []


# ---------- updates (GitHub releases) ----------
def _ver(s):
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


def latest_release():
    """The newest GitHub release if it is newer than this build, else None."""
    r = json.loads(_get(f"https://api.github.com/repos/{REPO}/releases/latest"))
    asset = next((a for a in r.get("assets", []) if a["name"].lower().endswith(".exe")), None)
    if not asset or _ver(r["tag_name"]) <= _ver(VERSION):
        return None
    return {"version": r["tag_name"].lstrip("v"), "url": asset["browser_download_url"], "size": asset["size"]}


# ---------- burn history (local JSON file next to the app) ----------
HISTORY = os.path.join(DATA, "history.json")


def _key(t):
    return f"{t['artist']}|{t['title']}".casefold().strip()


def load_history():
    try:
        with open(HISTORY, encoding="utf-8") as f:
            h = json.load(f)  # a corrupt file raises, so it is never silently overwritten
    except FileNotFoundError:
        h = {"playlists": [], "songs": {}}
    if isinstance(h["songs"], list):  # old format: keys only, no playlist name
        h["songs"] = dict.fromkeys(h["songs"])
    if "log" not in h:  # before the activity log: every saved playlist was a finished burn
        h["log"] = [{**p, "status": "burned", "ndiscs": p.get("discs", 1), "discs": [], "songs": []} for p in h["playlists"]]
    return h


def _save_history(h):
    tmp = HISTORY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=1)
    os.replace(tmp, HISTORY)


def log_job(entry):
    """One line in the activity history: a list that was burned, downloaded, cancelled or failed."""
    h = load_history()
    h["log"] = [entry] + h["log"][:299]
    _save_history(h)


def rename_log(k, old, at, name):
    h = load_history()
    e = h["log"][k] if 0 <= k < len(h["log"]) else None
    if not e or e.get("name") != old or e.get("at") != at:
        raise RuntimeError("ההיסטוריה השתנתה בינתיים. פתח אותה שוב ונסה שוב.")
    e["name"] = name
    _save_history(h)


def _burned_keys(e):
    """Song keys a history entry burned, from its "artist – title" lines."""
    return {f"{s.split(' – ', 1)[0]}|{s.split(' – ', 1)[-1]}".casefold().strip()
            for d in e.get("discs", []) if d.get("state") == "burned" for s in d.get("songs", [])}


def delete_log(k, name, at):
    """Remove one history entry; its songs stop counting as burned unless another entry burned them too."""
    h = load_history()
    e = h["log"][k] if 0 <= k < len(h["log"]) else None
    if not e or e.get("name") != name or e.get("at") != at:
        raise RuntimeError("ההיסטוריה השתנתה בינתיים. פתח אותה שוב ונסה שוב.")
    del h["log"][k]
    keep = set().union(*map(_burned_keys, h["log"]))
    for key in _burned_keys(e) - keep:
        h["songs"].pop(key, None)
    _save_history(h)


def mark_burned(tracks):
    songs = load_history()["songs"]
    for t in tracks:
        k = _key(t)
        t["burned"] = k in songs
        t["burned_in"] = songs.get(k)  # playlist name, or None when unknown


def record_burn(tracks, playlist=None):
    """Call only after a disc burned successfully. playlist: {name, url, count}."""
    h = load_history()
    name = (playlist or {}).get("name")
    for t in tracks:
        k = _key(t)
        h["songs"][k] = name or h["songs"].get(k)  # latest burn wins; keep a known name over none
    if playlist:
        h["playlists"] = [playlist] + [p for p in h["playlists"] if not (playlist.get("url") and p.get("url") == playlist.get("url"))]
    _save_history(h)


# ---------- disc planning ----------
def plan_discs(secs, cap=DEFAULT_CAP, gap=GAP):
    """Split tracks (in order) into discs. Returns lists of indices into `secs`.
    Audio: secs are seconds. Data disc: pass file sizes in bytes, cap in bytes, gap=0."""
    discs, cur, used = [], [], 0
    for i, s in enumerate(secs):
        need = s + gap
        if cur and used + need > cap:
            discs.append(cur)
            cur, used = [], 0
        cur.append(i)
        used += need
    if cur:
        discs.append(cur)
    return discs


# ---------- downloading ----------
class _Quiet:
    """yt-dlp logger that stays silent (a windowed exe has no console to write to)."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _ydl(**opts):
    import yt_dlp  # used in-process, so the packaged app needs no separate yt-dlp.exe
    if os.path.exists(FFMPEG):
        opts["ffmpeg_location"] = FFMPEG
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True, "logger": _Quiet(),
                             "socket_timeout": 30, "retries": 3, **opts})  # a stalled connection fails instead of hanging


def _clean(title):
    return re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", title).strip() or title


def _find_video(artist, title, ms):
    # YouTube returns nothing for some phrasings (e.g. transliterated Hebrew), so fall back to other ones
    artist = artist.split(",")[0]
    inner = re.findall(r"[\(\[]([^)\]]+)[\)\]]", title)  # "Halev Sheli (הלב שלי)" -> the original-script name
    queries = dict.fromkeys(q.strip() for q in [f"{artist} {_clean(title)}", *(f"{artist} {x}" for x in inner), f"{artist} {title}", _clean(title)])
    cands = []
    with _ydl(extract_flat=True, skip_download=True) as y:
        for q in queries:
            info = y.extract_info(f"ytsearch5:{q}", download=False)
            cands = [c for c in (info or {}).get("entries") or [] if c and c.get("id")]
            if cands:
                break
    if not cands:
        raise RuntimeError("השיר לא נמצא ביוטיוב.")
    want = ms / 1000
    timed = [c for c in cands if c.get("duration")]
    best = min(timed, key=lambda c: abs(c["duration"] - want)) if timed else cands[0]
    return best["id"]


def download_track(track, dest_base, fmt="audio", on_pct=None):
    """Download one track: fmt "audio" -> 44.1kHz 16-bit stereo WAV, "mp3" -> tagged CBR MP3. Returns the path.
    on_pct(0..1) is called as the audio downloads."""
    vid = track.get("yt") or _find_video(track["artist"], track["title"], track["ms"])  # "yt": pasted YouTube link
    if fmt == "mp3":  # ID3v2.3 tags: what car stereos show; many can't read the newer v2.4
        ext, pp = "mp3", {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(MP3_KBPS)}
        args = ["-ar", "44100", "-id3v2_version", "3", "-metadata", f"title={track['title']}", "-metadata", f"artist={track['artist']}"]
    else:
        ext, pp = "wav", {"key": "FFmpegExtractAudio", "preferredcodec": "wav"}
        args = ["-ar", "44100", "-ac", "2", "-sample_fmt", "s16"]
    def hook(d):
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if on_pct and d.get("status") == "downloading" and total:
            on_pct(min(1, d.get("downloaded_bytes", 0) / total))
    try:
        with _ydl(format="bestaudio/best", noplaylist=True, outtmpl=dest_base + ".%(ext)s", progress_hooks=[hook],
                  postprocessors=[pp], postprocessor_args={"ffmpeg": args}) as y:
            y.download([f"https://www.youtube.com/watch?v={vid}"])
    except Exception as e:
        raise RuntimeError(friendly_error(e)) from e
    path = f"{dest_base}.{ext}"
    if not os.path.exists(path):
        raise RuntimeError("ההורדה לא הסתיימה כמו שצריך. נסה שוב.")
    return path


def wav_seconds(path):
    with wave.open(path) as w:
        return w.getnframes() / w.getframerate()


# ---------- burning (Windows IMAPI2) ----------
def _recorder():
    import win32com.client as w
    master = w.Dispatch("IMAPI2.MsftDiscMaster2")
    if master.Count == 0:
        return None
    rec = w.Dispatch("IMAPI2.MsftDiscRecorder2")
    rec.InitializeDiscRecorder(master.Item(0))
    return rec


def _tao(rec):
    import win32com.client as w
    f = w.Dispatch("IMAPI2.MsftDiscFormat2TrackAtOnce")
    f.Recorder = rec
    f.ClientName = "Burner"
    return f


def burner_status():
    """{state: no_drive|no_disc|not_blank|unsupported|ready, text, cap_s}"""
    import pythoncom
    pythoncom.CoInitialize()
    try:
        rec = _recorder()
    except Exception as e:
        friendly_error(e)  # logged; the short status below is what fits in the header
        return {"state": "no_drive", "text": "רכיב הצריבה של Windows לא זמין במחשב הזה", "cap_s": None, "cap_bytes": None}
    if rec is None:
        return {"state": "no_drive", "text": "לא מחובר צורב", "cap_s": None, "cap_bytes": None}
    name = f"{rec.VendorId.strip()} {rec.ProductId.strip()}".strip()
    try:
        import win32com.client as w
        f = w.Dispatch("IMAPI2.MsftDiscFormat2Data")  # TAO has no media-state property; Data does
        f.Recorder = rec
        if not f.IsRecorderSupported(rec):
            return {"state": "unsupported", "text": f"{name} לא יכול לצרוב דיסקי שמע", "cap_s": None, "cap_bytes": None}
        if not f.IsCurrentMediaSupported(rec):
            return {"state": "no_disc", "text": f"{name} · הכנס דיסק CD-R ריק", "cap_s": None, "cap_bytes": None}
        if not f.CurrentMediaStatus & 2:  # IMAPI_FORMAT2_DATA_MEDIA_STATE_BLANK
            return {"state": "not_blank", "text": f"{name} · הדיסק אינו ריק", "cap_s": None, "cap_bytes": None}
        sectors = int(f.TotalSectorsOnMedia)
        cap = sectors // 75
        return {"state": "ready", "text": f"{name} · דיסק ריק ({cap // 60} דק׳)", "cap_s": cap,
                "cap_bytes": sectors * 2048 * 96 // 100}  # 4% for the ISO/Joliet file system
    except Exception:
        return {"state": "no_disc", "text": f"{name} · הכנס דיסק CD-R ריק", "cap_s": None, "cap_bytes": None}


def burn_disc(wav_paths, on_track=None):
    """Burn WAV files as one finalized audio CD, then eject."""
    import pythoncom
    pythoncom.CoInitialize()
    rec = _recorder()
    f = _tao(rec)
    try:
        f.RequestedWriteSpeed = 16 * 75  # 16x: gentler on old CD players
    except Exception:
        pass
    f.PrepareMedia()
    try:
        for i, p in enumerate(wav_paths):
            if on_track:
                on_track(i)
            with wave.open(p) as w:
                pcm = w.readframes(w.getnframes())
            pcm += b"\0" * (-len(pcm) % 2352)  # CD sectors are 2352 bytes
            s = pythoncom.CreateStreamOnHGlobal()
            s.Write(pcm)
            s.Seek(0, 0)
            f.AddAudioTrack(s)
    finally:
        f.ReleaseMedia()  # closes the disc
    try:
        rec.EjectMedia()
    except Exception:
        pass


def safe_name(s):
    """Strip what Windows file names can't hold."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s).strip(" .")


def disc_name(n, track):
    """File name on a data disc: the number keeps the playlist order (players sort by name)."""
    base = safe_name(f"{n:02d} - {track['artist']} - {track['title']}")
    return base[:60].rstrip(" .") + ".mp3"  # Joliet allows 64 chars


def burn_data_disc(folder, on_start=None):
    """Burn every file in `folder` as one closed ISO9660+Joliet data disc (what MP3 car stereos read), then eject."""
    import pythoncom
    import win32com.client as w
    pythoncom.CoInitialize()
    rec = _recorder()
    f = w.Dispatch("IMAPI2.MsftDiscFormat2Data")
    f.Recorder = rec
    f.ClientName = "Burner"
    f.ForceMediaToBeClosed = True  # one session: older players can't read open multisession discs
    fs = w.Dispatch("IMAPI2FS.MsftFileSystemImage")
    fs.ChooseImageDefaults(rec)
    fs.FileSystemsToCreate = 3  # FsiFileSystemISO9660 | FsiFileSystemJoliet
    fs.VolumeName = "BURNER"
    fs.Root.AddTree(folder, False)  # raises if it doesn't fit, before anything is written
    img = fs.CreateResultImage()
    if on_start:
        on_start()
    f.Write(img.ImageStream)
    try:
        rec.EjectMedia()
    except Exception:
        pass
