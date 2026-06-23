from pathlib import Path

import pytest

from aphrodite.storage import (
    OutputStorageError,
    output_relative_path,
    safe_path_segment,
    write_output_file,
)


def test_output_relative_path_sanitizes_path_segments() -> None:
    path = output_relative_path(
        job_id="../job/../../abc",
        variant_id="../hero banner",
        extension="../jpg",
    )

    assert path == "outputs/job_._._abc/hero_banner.jpg"
    assert ".." not in path
    assert Path(path).parts[0] == "outputs"


def test_safe_path_segment_falls_back_for_empty_values() -> None:
    assert safe_path_segment("../", fallback="fallback") == "fallback"


def test_write_output_file_rejects_paths_outside_media_root(tmp_path: Path) -> None:
    with pytest.raises(OutputStorageError):
        write_output_file(
            media_root=str(tmp_path / "media"),
            relative_path="../escape.jpg",
            content=b"bad",
        )


def test_write_output_file_returns_file_metadata(tmp_path: Path) -> None:
    stored = write_output_file(
        media_root=str(tmp_path / "media"),
        relative_path="outputs/job-1/catalog_square.jpg",
        content=b"deterministic output",
    )

    assert stored.relative_path == "outputs/job-1/catalog_square.jpg"
    assert stored.absolute_path.exists()
    assert stored.bytes == len(b"deterministic output")
    assert len(stored.sha256) == 64
