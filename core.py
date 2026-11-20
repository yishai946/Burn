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
VERSION = "1.1.1"             # bump for every release; build.bat tags the GitHub release with it
REPO = "yishai946/Burn"        # GitHub repo whose releases the in-app updater checks
FROZEN = getattr(sys, "frozen", False)  # running as the packaged Burner.exe
RES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))  # bundled files (index.html, ffmpeg.exe)
# packaged: per-user data in %APPDATA%\Burner; from source: next to the code, as before
DATA = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Burner") if FROZEN else os.path.dirname(os.path.abspath(__file__))
os.makedirs(DATA, exist_ok=True)
FFMPEG = os.path.join(RES, "ffmpeg.exe")  # bundled in the package; from source, yt-dlp finds ffmpeg on PATH


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
    name = re.split(r" (?:by|-) ", re.sub(r"[‎‏]", "", t.group(1)).strip())[0] if t else "Playlist"
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
    return ent.get("name") or ent.get("title") or "Playlist", tracks


def load_playlist(url):
    url = url.strip()
    if "music.apple.com" in url:
        name, tracks = _apple(url)
    elif "spotify.com" in url or url.startswith("spotify:"):
        name, tracks = _spotify(url)
    else:
        raise ValueError("הדבק קישור לפלייליסט של Apple Music או Spotify.")
    if not tracks:
        raise ValueError("לא נמצאו שירים בקישור הזה.")
    return {"name": name, "tracks": tracks}


def search_songs(q, limit=8):
    """Song suggestions for the search box, from Apple's public iTunes Search API."""
    q = q.strip()
    if len(q) < 2:
        return []
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": q, "entity": "song", "limit": limit, "country": "il"})
    return [{"title": x["trackName"], "artist": x["artistName"], "ms": int(x["trackTimeMillis"]),
             "id": str(x["trackId"]), "preview": x.get("previewUrl"), "art": x.get("artworkUrl100")}
            for x in json.loads(_get(url))["results"]
            if x.get("trackName") and x.get("trackTimeMillis")]


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
    return h


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
    tmp = HISTORY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=1)
    os.replace(tmp, HISTORY)


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
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True, "logger": _Quiet(), **opts})


def _clean(title):
    return re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", title).strip() or title


def _find_video(artist, title, ms):
    with _ydl(extract_flat=True, skip_download=True) as y:
        info = y.extract_info(f"ytsearch5:{artist.split(',')[0]} {_clean(title)}", download=False)
    cands = [c for c in (info or {}).get("entries") or [] if c and c.get("id")]
    if not cands:
        raise RuntimeError("no search results")
    want = ms / 1000
    timed = [c for c in cands if c.get("duration")]
    best = min(timed, key=lambda c: abs(c["duration"] - want)) if timed else cands[0]
    return best["id"]


def download_track(track, dest_base, fmt="audio"):
    """Download one track: fmt "audio" -> 44.1kHz 16-bit stereo WAV, "mp3" -> tagged CBR MP3. Returns the path."""
    vid = _find_video(track["artist"], track["title"], track["ms"])
    if fmt == "mp3":  # ID3v2.3 tags: what car stereos show; many can't read the newer v2.4
        ext, pp = "mp3", {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(MP3_KBPS)}
        args = ["-ar", "44100", "-id3v2_version", "3", "-metadata", f"title={track['title']}", "-metadata", f"artist={track['artist']}"]
    else:
        ext, pp = "wav", {"key": "FFmpegExtractAudio", "preferredcodec": "wav"}
        args = ["-ar", "44100", "-ac", "2", "-sample_fmt", "s16"]
    try:
        with _ydl(format="bestaudio/best", noplaylist=True, outtmpl=dest_base + ".%(ext)s",
                  postprocessors=[pp], postprocessor_args={"ffmpeg": args}) as y:
            y.download([f"https://www.youtube.com/watch?v={vid}"])
    except Exception as e:
        raise RuntimeError(str(e).replace("ERROR: ", "").strip() or "download failed")
    path = f"{dest_base}.{ext}"
    if not os.path.exists(path):
        raise RuntimeError("download failed")
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
        return {"state": "no_drive", "text": f"צריבה לא זמינה ({e})", "cap_s": None, "cap_bytes": None}
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


def disc_name(n, track):
    """File name on a data disc: the number keeps the playlist order (players sort by name)."""
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", f"{n:02d} - {track['artist']} - {track['title']}").strip(" .")
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
