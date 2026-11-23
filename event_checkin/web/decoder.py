"""Bounded in-memory QR fallback for Safari/Chrome; images are never persisted."""
import base64
import io


def decode_image(value):
    if not value.startswith(("data:image/jpeg;base64,", "data:image/png;base64,")):
        raise ValueError("Use a PNG or JPEG QR image.")
    try:
        raw = base64.b64decode(value.split(",", 1)[1], validate=True)
        if len(raw) > 280000:
            raise ValueError()
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            if not 1 <= width <= 1600 or not 1 <= height <= 1600 or width * height > 2100000:
                raise ValueError()
            image.verify()
        import cv2
        import numpy as np
        frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError()
        content, _, _ = cv2.QRCodeDetector().detectAndDecode(frame)
        return content
    except Exception:
        raise ValueError("Unable to read this image. Use a clear QR image up to 1280px, or search participants.") from None
