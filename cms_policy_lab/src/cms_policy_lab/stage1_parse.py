"""Stage 1 of the PREVA pipeline: Parse.

This module converts an authoritative CMS policy PDF into an LLM-ready document
while retaining enough provenance to trace extracted content back to the source.
The processing flow is intentionally linear and auditable:

1. Download and validate the source PDF.
2. Confirm that the requested source-page range exists.
3. Use Docling for layout-aware parsing, OCR, and table reconstruction.
4. Export one Markdown file per original page with an explicit page marker.
5. Fail closed if a requested page is missing or empty.
6. Write a manifest containing source identity, configuration, and quality data.

The resulting Markdown is the input to later retrieval and LLM stages. No model
inference or business-rule extraction occurs here; this stage establishes a
trusted, traceable document representation.
"""

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
    """Return a stable SHA-256 fingerprint without loading the file into memory.

    The fingerprint identifies the exact source document used by a pipeline run.
    It supports audit investigations and prevents two differently revised PDFs
    with the same filename from being treated as the same document version.
    """
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        # A bounded block size allows the same function to handle large contracts.
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_pdf(url: str, destination: Path) -> None:
    """Download the authoritative document and enforce basic file integrity.

    An existing local copy is reused to make repeated development runs fast. A
    new response must declare a PDF media type and contain the PDF magic bytes;
    an HTML error page must never be accepted as policy content.

    Raises:
        ValueError: If the remote response is not a recognizable PDF.
    """
    if destination.exists():
        # The full content hash is recorded later in the run manifest.
        if destination.read_bytes()[:4] != b"%PDF":
            raise ValueError(f"Cached source is not a PDF: {destination}")
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
    """Fail before conversion when the requested source pages cannot exist."""
    if start < 1 or end < start or end > total_pages:
        raise ValueError(
            f"Invalid page range {start}-{end}; document contains {total_pages} pages"
        )


def export_page_markdown(document: Any, page_number: int) -> str:
    """Export one source page with a machine-readable provenance marker.

    Page-by-page export avoids relying on inferred page breaks in a single large
    Markdown string. The HTML comment remains invisible to readers but can be
    parsed by chunking, citation, and evidence-validation components.
    """
    page_markdown = document.export_to_markdown(page_no=page_number).strip()
    return f"<!-- source-page: {page_number} -->\n\n{page_markdown}\n"


def parse_policy(
    pdf_path: Path,
    output_dir: Path,
    page_start: int,
    page_end: int,
    source_url: str,
) -> dict[str, Any]:
    """Convert selected PDF pages and write validated Stage 1 artifacts.

    Args:
        pdf_path: Local copy of the authoritative CMS PDF.
        output_dir: Directory for Markdown, Docling JSON, and the run manifest.
        page_start: First one-based source page to process, inclusive.
        page_end: Last one-based source page to process, inclusive.
        source_url: Canonical source URL recorded for auditability.

    Returns:
        The same manifest dictionary written to ``manifest.json``.

    Raises:
        ValueError: If the requested page range is invalid.
        RuntimeError: If Docling produces no content for a selected page.
    """
    # Guardrail 1: validate against the physical PDF before invoking Docling.
    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)
    validate_page_range(page_start, page_end, total_pages)

    output_dir.mkdir(parents=True, exist_ok=True)
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    # Docling uses the embedded text layer when available and can fall back to
    # OCR for scanned content. Table recognition preserves row/column structure
    # that would otherwise be lost in plain-text extraction.
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

    # Each source page is an independent evidence unit. Keeping separate files
    # makes later citations deterministic and allows a reviewer to inspect only
    # the pages supporting a particular extracted rule.
    page_records: list[dict[str, Any]] = []
    combined_pages: list[str] = []
    empty_pages: list[int] = []

    for page_number in range(page_start, page_end + 1):
        page_markdown = export_page_markdown(document, page_number)

        # Remove the provenance header before calculating content-level quality
        # metrics; a marker alone must not make an empty page appear successful.
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
                # Per-page hashes help detect accidental downstream mutation.
                "sha256": hashlib.sha256(page_body.encode("utf-8")).hexdigest(),
            }
        )

    # Guardrail 2: fail closed. Partial extraction must never silently advance
    # into retrieval or LLM processing as though it represented the full range.
    if empty_pages:
        raise RuntimeError(f"Docling produced empty output for pages: {empty_pages}")

    # The combined Markdown is convenient for human inspection. Docling JSON is
    # retained as the lossless representation for future layout-aware use cases.
    combined_path = output_dir / "document-with-page-provenance.md"
    combined_path.write_text("\n\n".join(combined_pages), encoding="utf-8")
    document.save_as_json(output_dir / "docling-document.json")

    # The manifest is the audit envelope for this run. Downstream stages can
    # verify which source and parser configuration produced their input.
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
    """Build the CLI while keeping production inputs configurable."""
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
    """Run download, conversion, validation, and artifact reporting."""
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
