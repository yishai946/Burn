"""Generates icon.ico (the in-app flame logo) and a Desktop shortcut that launches the app without a console.
Run with --icon-only to just redraw the icon."""
import math, os, subprocess, sys
from PIL import Image, ImageDraw

here = os.path.dirname(os.path.abspath(__file__))
S = 1024                       # draw large, downscale for smooth edges
k = S / 38                     # the header logo is a 38px tile holding a 21px, 24-unit icon
off = (38 - 21) / 2 * k
u = 21 / 24 * k                # one SVG unit in pixels


def cubic(p0, p1, p2, p3, n=300):
    return [tuple((1-t)**3*a + 3*(1-t)**2*t*b + 3*(1-t)*t**2*c + t**3*d for a, b, c, d in zip(p0, p1, p2, p3))
            for t in (i / n for i in range(n + 1))]


# the flame path from index.html: M12 2 c1 3.5 5 5.5 5 10 a5 5 0 0 1-10 0 c0-2 1-3.5 2-4.5 0 2 1 3 2 3 0-3-1-5.5 1-8.5z
pts = cubic((12, 2), (13, 5.5), (17, 7.5), (17, 12))
pts += [(12 + 5 * math.cos(a), 12 + 5 * math.sin(a)) for a in (math.pi * i / 300 for i in range(301))]  # lower half circle
pts += cubic((7, 12), (7, 10), (8, 8.5), (9, 7.5))
pts += cubic((9, 7.5), (9, 9.5), (10, 10.5), (11, 10.5))
pts += cubic((11, 10.5), (11, 7.5), (10, 5), (12, 2))
DY = 2.5                       # the path spans y 2..17; shift it to sit centred (4.5..19.5) in the 24-unit box
px = [(off + x * u, off + (y + DY) * u) for x, y in pts]

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((0, 0, S - 1, S - 1), round(11 * k), fill=(255, 122, 26, 255))   # orange tile, as in the header
w = round(2 * u)                                                                     # stroke-width 2
for x, y in px:                    # stamp a disc at every (dense) point: smooth stroke with round caps and joins
    d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=(26, 14, 4, 255))
ico = os.path.join(here, "icon.ico")
img.resize((256, 256), Image.LANCZOS).save(ico, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
if "--icon-only" in sys.argv:
    sys.exit()

pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if "WindowsApps" in pyw:  # Store Python always shows its own taskbar icon; prefer the regular install
    reg = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe")
    pyw = reg if os.path.exists(reg) else pyw
ps = f'''$s=(New-Object -ComObject WScript.Shell).CreateShortcut("{here}\CD Burner.lnk")
$s.TargetPath="{pyw}"; $s.Arguments='"{os.path.join(here, "app.py")}"'; $s.WorkingDirectory="{here}"; $s.IconLocation="{ico}"; $s.Save()
Copy-Item "{here}\CD Burner.lnk" ([Environment]::GetFolderPath("Desktop")) -Force'''
import base64  # -EncodedCommand keeps non-ASCII Desktop paths (e.g. Hebrew OneDrive) intact
subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", base64.b64encode(ps.encode("utf-16-le")).decode()], check=True)
print("shortcut created on Desktop")
