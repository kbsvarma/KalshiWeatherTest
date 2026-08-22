# CMS Policy Intelligence Lab

This small project recreates the five-stage PREVA architecture using public CMS
documents:

1. Parse
2. Retrieve
3. Extract
4. Validate
5. Apply

Stage 1 uses Docling to parse the Deep Brain Stimulation section of the CMS
Medicare Claims Processing Manual, Chapter 32. It produces:

- structure-preserving Markdown;
- one Markdown file per source page;
- explicit page-provenance markers;
- Docling's lossless document JSON;
- a validation manifest containing the source hash and page checks.

The PDF pipeline explicitly enables OCR and table-structure recognition. For a
digital PDF, Docling can use the embedded text layer; OCR remains available for
scanned or image-based pages.

## Setup

```bash
cd cms_policy_lab
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Docling downloads its layout models the first time it processes a PDF.

## Run Stage 1

```bash
.venv/bin/cms-stage1-parse
```

The default run processes source PDF pages 36 through 40, which contain the
Deep Brain Stimulation coverage and billing rules.

## Inspect the result

```bash
sed -n '1,160p' artifacts/stage1/deep-brain-stimulation/document-with-page-provenance.md
python -m json.tool artifacts/stage1/deep-brain-stimulation/manifest.json
```

## Interview explanation

> We used Docling to convert the CMS policy PDF into structured Markdown while
> retaining page-level provenance. Each extracted page was stored separately
> and marked with its original source-page number. A manifest recorded the
> original PDF hash, total page count, selected page range, and extraction
> completeness. That prevented an incomplete or untraceable document from
> entering the RAG and LLM stages.
