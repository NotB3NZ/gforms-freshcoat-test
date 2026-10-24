"""Optional asset rebuild: fonttools==4.60.2, pinned upstream TTF as argv[1]."""
import hashlib
import json
import sys
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

directory = Path(__file__).resolve().parent.parent / "templates/fonts/google-sans"
manifest = json.loads((directory / "manifest.json").read_text())
source = Path(sys.argv[1])
assert hashlib.sha256(source.read_bytes()).hexdigest() == manifest["upstream_sha256"]
for entry in manifest["files"]:
    original = TTFont(source, recalcTimestamp=False)
    font = instantiateVariableFont(original, {"wght": entry["weight"], **manifest["derivation"]["axes"]}, inplace=True)
    style = entry["style"]
    for record in font["name"].names:
        if record.nameID in (2, 4, 6, 17):
            value = {2: style, 4: "Google Sans " + style, 6: "GoogleSans-" + style, 17: style}[record.nameID]
            record.string = value.encode(record.getEncoding())
    if entry["weight"] == 700:
        font["OS/2"].fsSelection = (font["OS/2"].fsSelection & ~(1 << 6)) | (1 << 5)
        font["head"].macStyle |= 1
    else:
        font["OS/2"].fsSelection = (font["OS/2"].fsSelection & ~(1 << 5)) | (1 << 6)
        font["head"].macStyle &= ~1
    path = directory / entry["file"]
    font.save(path)
    entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
(directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
