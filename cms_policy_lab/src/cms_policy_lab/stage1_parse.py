"""Stage 1: convert a CMS policy PDF into provenance-preserving Markdown."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from pypdf import PdfReader


DEFAULT_SOURCE_URL = (
    "https://www.cms.gov/regulations-and-guidance/guidance/manuals/"
    "downloads/clm104c32.pdf"
)
DEFAULT_PAGE_START = 36
DEFAULT_PAGE_END = 40


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_pdf(url: str, destination: Path) -> None:
    """Download a PDF once and reject responses that are not actually PDFs."""
    if destination.exists():
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "cms-policy-lab/0.1 (educational project)"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        content_type = response.headers.get_content_type()
        payload = response.read()

    if content_type != "application/pdf" or not payload.startswith(b"%PDF"):
        raise ValueError(f"Expected a PDF response, received {content_type!r}")

    destination.write_bytes(payload)


def validate_page_range(start: int, end: int, total_pages: int) -> None:
    if start < 1 or end < start or end > total_pages:
        raise ValueError(
            f"Invalid page range {start}-{end}; document contains {total_pages} pages"
        )


def export_page_markdown(document: Any, page_number: int) -> str:
    """Export one page and insert an explicit, machine-readable page marker."""
    page_markdown = document.export_to_markdown(page_no=page_number).strip()
    return f"<!-- source-page: {page_number} -->\n\n{page_markdown}\n"


def parse_policy(
    pdf_path: Path,
    output_dir: Path,
    page_start: int,
    page_end: int,
    source_url: str,
) -> dict[str, Any]:
    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)
    validate_page_range(page_start, page_end, total_pages)

    output_dir.mkdir(parents=True, exist_ok=True)
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    pipeline_options = PdfPipelineOptions(
        do_ocr=True,
        do_table_structure=True,
    )
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )
    result = converter.convert(
        source=pdf_path,
        page_range=(page_start, page_end),
    )
    document = result.document

    page_records: list[dict[str, Any]] = []
    combined_pages: list[str] = []
    empty_pages: list[int] = []

    for page_number in range(page_start, page_end + 1):
        page_markdown = export_page_markdown(document, page_number)
        page_body = page_markdown.split("\n", 2)[-1].strip()
        if not page_body:
            empty_pages.append(page_number)

        page_path = pages_dir / f"page-{page_number:04d}.md"
        page_path.write_text(page_markdown, encoding="utf-8")
        combined_pages.append(page_markdown)
        page_records.append(
            {
                "source_page": page_number,
                "markdown_file": str(page_path.relative_to(output_dir)),
                "character_count": len(page_body),
                "sha256": hashlib.sha256(page_body.encode("utf-8")).hexdigest(),
            }
        )

    if empty_pages:
        raise RuntimeError(f"Docling produced empty output for pages: {empty_pages}")

    combined_path = output_dir / "document-with-page-provenance.md"
    combined_path.write_text("\n\n".join(combined_pages), encoding="utf-8")
    document.save_as_json(output_dir / "docling-document.json")

    manifest = {
        "project": "CMS Medicare Policy Intelligence Lab",
        "stage": "1-parse",
        "technology": {
            "docling_version": version("docling"),
            "ocr_enabled": pipeline_options.do_ocr,
            "table_structure_enabled": pipeline_options.do_table_structure,
        },
        "source": {
            "url": source_url,
            "local_file": str(pdf_path),
            "sha256": sha256_file(pdf_path),
            "pdf_total_pages": total_pages,
        },
        "selection": {
            "topic": "Deep Brain Stimulation",
            "source_page_start": page_start,
            "source_page_end": page_end,
            "expected_page_count": page_end - page_start + 1,
            "exported_page_count": len(page_records),
        },
        "quality": {
            "all_selected_pages_exported": len(page_records)
            == page_end - page_start + 1,
            "empty_pages": empty_pages,
        },
        "pages": page_records,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse selected CMS policy pages with Docling."
    )
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument(
        "--pdf",
        type=Path,
        default=Path("data/raw/cms-claims-processing-chapter-32.pdf"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/stage1/deep-brain-stimulation"),
    )
    parser.add_argument("--page-start", type=int, default=DEFAULT_PAGE_START)
    parser.add_argument("--page-end", type=int, default=DEFAULT_PAGE_END)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    download_pdf(args.source_url, args.pdf)
    manifest = parse_policy(
        pdf_path=args.pdf,
        output_dir=args.output_dir,
        page_start=args.page_start,
        page_end=args.page_end,
        source_url=args.source_url,
    )
    print(json.dumps(manifest["quality"], indent=2))
    print(f"Output: {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
