import pytest
from image_fixtures import PNG_1X1, TRUNCATED_PNG

from aphrodite.assets import AssetValidationError, validate_image_upload


def test_validate_png_upload_extracts_metadata() -> None:
    asset = validate_image_upload(
        content=PNG_1X1,
        filename="../mug shot.png",
        declared_content_type="image/png",
        max_bytes=1024,
    )

    assert asset.original_filename == "mug_shot.png"
    assert asset.content_type == "image/png"
    assert asset.extension == "png"
    assert asset.width == 1
    assert asset.height == 1
    assert asset.bytes == len(PNG_1X1)


def test_validate_upload_rejects_unsupported_content() -> None:
    with pytest.raises(AssetValidationError) as exc:
        validate_image_upload(
            content=b"not an image",
            filename="notes.txt",
            declared_content_type="text/plain",
            max_bytes=1024,
        )

    assert exc.value.status_code == 415


def test_validate_upload_rejects_corrupt_image_bytes() -> None:
    with pytest.raises(AssetValidationError) as exc:
        validate_image_upload(
            content=TRUNCATED_PNG,
            filename="mug.png",
            declared_content_type="image/png",
            max_bytes=1024,
        )

    assert exc.value.status_code == 422


def test_validate_upload_rejects_oversized_content() -> None:
    with pytest.raises(AssetValidationError) as exc:
        validate_image_upload(
            content=PNG_1X1,
            filename="mug.png",
            declared_content_type="image/png",
            max_bytes=8,
        )

    assert exc.value.status_code == 413
