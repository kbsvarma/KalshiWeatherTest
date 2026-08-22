from pathlib import Path

import pytest

from cms_policy_lab.stage1_parse import download_pdf, sha256_file, validate_page_range


def test_sha256_file(tmp_path: Path) -> None:
    """The audit fingerprint must remain deterministic for identical content."""
    sample = tmp_path / "sample.txt"
    sample.write_text("cms", encoding="utf-8")

    assert sha256_file(sample) == (
        "069df57a3a645590c8bd00dfffb2bd94969c691857a20c7936a5e06fa122a8f3"
    )


def test_cached_non_pdf_is_rejected(tmp_path: Path) -> None:
    """A cached HTML/error payload must not bypass the PDF ingestion guardrail."""
    cached_source = tmp_path / "policy.pdf"
    cached_source.write_text("not a PDF", encoding="utf-8")

    with pytest.raises(ValueError, match="Cached source is not a PDF"):
        download_pdf("https://unused.example/policy.pdf", cached_source)


@pytest.mark.parametrize("start,end", [(0, 1), (3, 2), (1, 11)])
def test_invalid_page_range(start: int, end: int) -> None:
    """Invalid page selections fail before expensive Docling processing begins."""
    with pytest.raises(ValueError):
        validate_page_range(start, end, total_pages=10)


def test_valid_page_range() -> None:
    """The selected Deep Brain Stimulation pages fit the known source PDF."""
    validate_page_range(36, 40, total_pages=338)
