import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

import qrcode
from PIL import Image

from .config import private_directory


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


def fitted_template(source, destination, template, name_field):
    """Keep the design archive intact; fit variable names within its existing text box."""
    def visit(elements):
        for el in elements:
            props = el.get("properties", {})
            value = props.get("value", "")
            if el.get("type") == "text" and isinstance(value, str) and re.search(r"{{\s*" + re.escape(name_field) + r"\s*}}", value):
                props["fit"] = "shrink"
            visit(props.get("children", []))
    for frame in template["template_data"]:
        visit(frame["elements"])
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
            values = {s.name_field: name}
            if ticket_tier is not None:
                if s.tier_field not in template["fields"]["properties"]:
                    raise RenderError("The configured tier field is not declared in this template.")
                if not field_is_bound(template, s.tier_field, s.frame):
                    raise RenderError("The configured tier field is declared but no text binds it in this frame.")
                if ticket_tier not in ("Lite", "Regular", "Pro"):
                    raise RenderError("Choose ticket tier Lite, Regular or Pro.")
                values[s.tier_field] = ticket_tier
            if "name" in template["fields"]["properties"]:
                # This required legacy field is separate from the actual displayName binding.
                # Its current maxLength is 40; displayName itself has no such restriction.
                limit = template["fields"]["properties"]["name"].get("maxLength", len(name))
                values["name"] = name[:limit]
            if "ticket_id" in template["fields"]["properties"]:
                values["ticket_id"] = participant_id
            values_path = temp / "values.json"
            values_path.write_text(json.dumps(values, ensure_ascii=False))
            values_path.chmod(0o600)
            render_template = temp / "fitted-card.coat"
            fitted_template(s.template, render_template, template, s.name_field)
            args = [s.bun, str(s.freshcoat / "packages/cli/src/bin.ts"), "render", str(render_template),
                    "--values", str(values_path), "--frame", s.frame, "--scale", "1", "--format", "png", "--out", str(temp)]
            try:
                subprocess.run(args, cwd=s.freshcoat, check=True, capture_output=True, timeout=120)
            except (OSError, subprocess.SubprocessError):
                # FreshCoat error output can contain field values. Do not log it.
                raise RenderError("FreshCoat failed or timed out; check Bun, dependencies and template locally.") from None
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
