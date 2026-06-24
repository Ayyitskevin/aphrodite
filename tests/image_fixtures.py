from io import BytesIO

from PIL import Image


def _png(width: int, height: int) -> bytes:
    with BytesIO() as buffer:
        Image.new("RGB", (width, height), (255, 255, 255)).save(buffer, format="PNG")
        return buffer.getvalue()


PNG_1X1 = _png(1, 1)
# 10x10 = 100 pixels, tiny on disk — stands in for an image that declares more
# pixels than allowed, so a pixel cap can be exercised without a real gigapixel bomb.
PNG_10X10 = _png(10, 10)
TRUNCATED_PNG = PNG_1X1[:24]
