from io import BytesIO

from PIL import Image


def _png_1x1() -> bytes:
    with BytesIO() as buffer:
        Image.new("RGB", (1, 1), (255, 255, 255)).save(buffer, format="PNG")
        return buffer.getvalue()


PNG_1X1 = _png_1x1()
TRUNCATED_PNG = PNG_1X1[:24]
