from pathlib import Path

import pytest

from cms_policy_lab.stage1_parse import sha256_file, validate_page_range


def test_sha256_file(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    sample.write_text("cms", encoding="utf-8")

    assert sha256_file(sample) == (
        "069df57a3a645590c8bd00dfffb2bd94969c691857a20c7936a5e06fa122a8f3"
    )


@pytest.mark.parametrize("start,end", [(0, 1), (3, 2), (1, 11)])
def test_invalid_page_range(start: int, end: int) -> None:
    with pytest.raises(ValueError):
        validate_page_range(start, end, total_pages=10)


def test_valid_page_range() -> None:
    validate_page_range(36, 40, total_pages=338)
