import base64
import hashlib
import json
import re
import subprocess
import tempfile
import zipfile
from copy import deepcopy
from pathlib import Path

import qrcode
from PIL import Image, ImageFont

from .config import private_directory

NAME_LIMIT = 160
GOOGLE_SANS_DIR = Path(__file__).resolve().parent.parent / "templates/fonts/google-sans"


class RenderError(RuntimeError):
    pass


def read_template(path):
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read("template.json"))


def field_is_bound(template, field, frame_name):
    def visit(elements):
        for element in elements:
            props = element.get("properties", {})
            value = props.get("value", "")
            if element.get("type") == "text" and isinstance(value, str) and re.search(r"{{\s*" + re.escape(field) + r"\s*}}", value):
                return True
            if visit(props.get("children", [])):
                return True
        return False
    return any(visit(frame["elements"]) for frame in template["template_data"] if frame["name"] == frame_name)


def inspect_template(settings):
    template = read_template(settings.template)
    field = settings.name_field
    if field not in template["fields"]["properties"]:
        raise RenderError("Configured name field is not declared in the template.")
    frames = [f for f in template["template_data"] if f["name"] == settings.frame]
    if len(frames) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", settings.frame):
        raise RenderError("Choose a single frame with a simple file-safe name.")
    frame = frames[0]
    width, height = template["width"], template["height"]
    x, y, size = settings.qr_x, settings.qr_y, settings.qr_size
    if size < 160 or x < 0 or y < 0 or x + size > width or y + size > height:
        raise RenderError("QR rectangle is too small or falls outside the card.")
    binding_found = False
    def visit(elements, offset_x=0, offset_y=0):
        nonlocal binding_found
        for el in elements:
            props = el.get("properties", {})
            pos, dims = el.get("pos", {}), el.get("size", {})
            ex, ey = pos.get("x", 0) + offset_x, pos.get("y", 0) + offset_y
            if el.get("type") == "text":
                value = props.get("value", "")
                if isinstance(value, str) and re.search(r"{{\s*" + re.escape(field) + r"\s*}}", value):
                    binding_found = True
                if x < ex + dims.get("width", 0) and x + size > ex and y < ey + dims.get("height", 0) and y + size > ey:
                    raise RenderError("QR rectangle overlaps a text layer; adjust QR_X/Y/SIZE.")
            visit(props.get("children", []), ex, ey)
    visit(frame["elements"])
    if not binding_found:
        raise RenderError("Name field is declared but no text uses its {{field}} binding.")
    return template, (width, height)


def google_sans_files():
    """Only the reviewed, versioned Google Sans faces may satisfy this family."""
    try:
        manifest = json.loads((GOOGLE_SANS_DIR / "manifest.json").read_text())
        files = {}
        for entry in manifest["files"]:
            path = GOOGLE_SANS_DIR / entry["file"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError()
            files[entry["weight"]] = path
        if set(files) != {400, 700}:
            raise ValueError()
        return files
    except (OSError, ValueError, KeyError, TypeError):
        raise RenderError("Pinned Google Sans 400/700 assets are missing or changed; no font substitution allowed.") from None


def assert_no_font_fallback(result):
    """FreshCoat can exit zero after rendering fallback faces or missing glyphs.

    Inspect both streams, but never expose renderer output containing names.
    Fail closed on font/glyph warnings, including undeclared Google guesses.
    """
    for stream in (result.stdout, result.stderr):
        text = stream.decode(errors="replace") if isinstance(stream, bytes) else (stream or "")
        for line in text.splitlines():
            if (re.search(r"\bwarning\b.*\b(fonts?|glyphs?|fallback|boxes)\b", line, re.I)
                    or re.search(r"^\s*(?:no font data|no glyphs|missing[- ](?:fonts?|glyphs?)\b|fallback (?:face|font|glyph)\b)", line, re.I)):
                raise RenderError("FreshCoat reported unresolved fonts or glyph fallback; no card accepted.")


def render_values(template, name_field, name, participant_id, tier_field, tier):
    if not isinstance(name, str) or not 0 < len(name) <= NAME_LIMIT:
        raise RenderError("Card names must contain 1 to 160 characters; names are never truncated.")
    values = {name_field: name}
    if tier is not None:
        values[tier_field] = tier
    if "name" in template["fields"]["properties"]:
        values["name"] = name  # Keep the separate legacy required field complete too.
    if "ticket_id" in template["fields"]["properties"]:
        values["ticket_id"] = participant_id
    return values


def prepared_template(template, name_field, name=None):
    """Adjust only a rendering copy's schema, name fitting and font sources."""
    template = deepcopy(template)
    for key in {name_field, "name"}:
        if key in template["fields"]["properties"]:
            template["fields"]["properties"][key]["maxLength"] = NAME_LIMIT
    local_fonts = None
    def visit(elements):
        nonlocal local_fonts
        for el in elements:
            props = el.get("properties", {})
            value = props.get("value", "")
            font = props.get("font", {})
            if el.get("type") == "text" and font.get("family") == "Google Sans":
                local_fonts = local_fonts or google_sans_files()
            if el.get("type") == "text" and isinstance(value, str) and re.search(r"{{\s*" + re.escape(name_field) + r"\s*}}", value):
                props["fit"] = "shrink"
                if name is not None and local_fonts and font.get("family") == "Google Sans":
                    # FreshCoat shrinks against height before maxLines truncates.
                    # Fit the complete line first with the same static face; a
                    # 2% width allowance covers FreeType/Skia shaping rounding.
                    face = ImageFont.truetype(str(local_fonts[font.get("weight", 400)]), size=font["size"])
                    literal = re.sub(r"{{\s*" + re.escape(name_field) + r"\s*}}", lambda _: name, value)
                    width = face.getlength(literal)
                    available = el["size"]["width"] * 0.98
                    if width > available:
                        font["size"] *= available / width
            visit(props.get("children", []))
    for frame in template["template_data"]:
        visit(frame["elements"])
    if local_fonts:
        descriptor = {"kind": "local", "family": "Google Sans", "files": [
            {"weight": weight, "src": "data:font/ttf;base64," + base64.b64encode(path.read_bytes()).decode("ascii")}
            for weight, path in sorted(local_fonts.items())]}
        template["fonts"] = [f for f in template.get("fonts", []) if f["family"] != "Google Sans"] + [descriptor]
    return template


def fitted_template(source, destination, template, name_field, name=None):
    """Keep the installed design archive byte-for-byte intact."""
    template = prepared_template(template, name_field, name)
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as derived:
        for entry in original.infolist():
            data = json.dumps(template, ensure_ascii=False).encode() if entry.filename == "template.json" else original.read(entry.filename)
            derived.writestr(entry, data)


class CardRenderer:
    def __init__(self, settings):
        self.settings = settings

    def render(self, participant_id, name, token, ticket_tier=None):
        s = self.settings
        if not re.fullmatch(r"[A-Za-z0-9_-]+", participant_id):
            raise RenderError("Unsafe participant filename.")
        template, dimensions = inspect_template(s)
        output = private_directory(s.generated / participant_id)
        # The design archive is never changed. Names go in a private JSON data file,
        # not a shell command; subprocess gets separately supplied arguments.
        with tempfile.TemporaryDirectory(prefix="render-", dir=output) as temp:
            temp = Path(temp)
            if ticket_tier is not None:
                if s.tier_field not in template["fields"]["properties"]:
                    raise RenderError("The configured tier field is not declared in this template.")
                if not field_is_bound(template, s.tier_field, s.frame):
                    raise RenderError("The configured tier field is declared but no text binds it in this frame.")
                if ticket_tier not in ("Lite", "Regular", "Pro"):
                    raise RenderError("Choose ticket tier Lite, Regular or Pro.")
            values = render_values(template, s.name_field, name, participant_id, s.tier_field, ticket_tier)
            values_path = temp / "values.json"
            values_path.write_text(json.dumps(values, ensure_ascii=False))
            values_path.chmod(0o600)
            render_template = temp / "fitted-card.coat"
            fitted_template(s.template, render_template, template, s.name_field, name)
            args = [s.bun, str(s.freshcoat / "packages/cli/src/bin.ts"), "render", str(render_template),
                    "--values", str(values_path), "--frame", s.frame, "--scale", "1", "--format", "png", "--out", str(temp)]
            try:
                result = subprocess.run(args, cwd=s.freshcoat, check=True, capture_output=True, timeout=120)
            except (OSError, subprocess.SubprocessError):
                # FreshCoat error output can contain field values. Do not log it.
                raise RenderError("FreshCoat failed or timed out; check Bun, dependencies and template locally.") from None
            assert_no_font_fallback(result)
            image = Image.open(temp / (s.frame + ".png")).convert("RGB")
            if image.size != dimensions:
                raise RenderError("Unexpected render dimensions; QR coordinates need review.")
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=4)
            qr.add_data(token)
            qr.make(fit=True)
            qr_image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            # Integer module scale keeps every square crisp and the four-module quiet zone white.
            factor = s.qr_size // qr_image.width
            if factor < 3:
                raise RenderError("QR area is too small for a reliably readable ticket.")
            qr_image = qr_image.resize((qr_image.width * factor, qr_image.height * factor), Image.Resampling.NEAREST)
            tile = Image.new("RGB", (s.qr_size, s.qr_size), "white")
            margin = (s.qr_size - qr_image.width) // 2
            tile.paste(qr_image, (margin, margin))
            image.paste(tile, (s.qr_x, s.qr_y))
            final = output / "card.png"
            staging = temp / "card.png"
            image.save(staging)
            staging.chmod(0o600)
            staging.replace(final)
        return final
