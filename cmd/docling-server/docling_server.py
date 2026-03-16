#!/usr/bin/env python3
"""
Simple Docling server compatible with extraction worker API expectations.
Exposes Docling's document conversion as a REST API.
Falls back to pypdf if Docling models are unavailable.
"""

import logging
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Docling Server",
    description="REST API for document conversion using Docling",
    version="1.0.0",
)

# Initialize Docling converter (lazy loading to avoid HF model downloads at startup)
converter = None
use_fallback = False


def get_converter():
    """Get or initialize the Docling converter lazily"""
    global converter, use_fallback
    if converter is not None:
        return converter

    try:
        from docling.document_converter import DocumentConverter

        logger.info("Initializing Docling converter...")
        converter = DocumentConverter()
        logger.info("✓ Docling converter initialized successfully")
        use_fallback = False
        return converter
    except Exception as e:
        logger.warning(f"Docling initialization failed: {e}")
        logger.warning("Falling back to pypdf text extraction")
        use_fallback = True
        # Return a dummy object - we'll handle extraction in the endpoint
        return None


def extract_with_docling(file_path):
    """Extract using Docling converter"""
    try:
        conv = get_converter()
        if conv is None:
            raise RuntimeError("Docling converter not available")

        doc_result = conv.convert(file_path)
        markdown_text = doc_result.document.export_to_markdown()
        num_pages = (
            len(doc_result.document.pages)
            if hasattr(doc_result.document, "pages")
            else 1
        )
        return markdown_text, num_pages
    except Exception as e:
        logger.error(f"Docling extraction failed: {e}")
        raise


def extract_text_from_pdf_raw(file_path):
    """
    Extract text from PDF using raw Python - no external dependencies.
    This uses basic PDF text stream extraction for text-based PDFs.
    """
    try:
        with open(file_path, "rb") as f:
            pdf_data = f.read()

        # Very basic PDF text extraction (works for simple text PDFs)
        # Look for text objects in PDF streams
        import re

        # Simple pattern to extract text from PDF
        # PDF text is often in BT...ET blocks with Tj operators
        text_parts = []
        decoded = ""

        # Try to find text in common PDF formats
        # This is a fallback for text-based PDFs
        try:
            decoded = pdf_data.decode("latin1", errors="ignore")
            # Look for text between BT (Begin Text) and ET (End Text)
            matches = re.findall(r"BT.*?\((.*?)\).*?ET", decoded, re.DOTALL)
            for match in matches:
                # Remove PDF escapes
                text = match.replace("\\n", "\n").replace("\\t", "\t")
                if text.strip():
                    text_parts.append(text)
        except:
            pass

        # Fallback: look for any readable ASCII text in the file
        if not text_parts:
            readable = "".join(
                chr(b) for b in pdf_data if 32 <= b <= 126 or b in (9, 10, 13)
            )
            # Extract lines that look like content
            for line in readable.split("\n"):
                if len(line.strip()) > 10:  # Only meaningful lines
                    text_parts.append(line.strip())

        markdown_text = (
            "\n".join(text_parts) if text_parts else "(Unable to extract text from PDF)"
        )

        # Estimate page count from PDF structure
        pages_match = re.search(r"/Count\s+(\d+)", decoded)
        num_pages = int(pages_match.group(1)) if pages_match else 1

        return markdown_text, num_pages

    except Exception as e:
        logger.error(f"Raw PDF extraction failed: {e}")
        raise


@app.get("/openapi.json")
async def openapi_spec():
    """Health check endpoint - returns OpenAPI spec"""
    return app.openapi()


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok", "service": "docling"}


@app.post("/convert")
async def convert_document(file: UploadFile = File(...)):
    """
    Convert a document to markdown using Docling or fallback PDF extraction.

    Expects: multipart/form-data with 'file' field
    Returns: JSON with 'markdown' field containing extracted text
    """
    try:
        # Read file content
        content = await file.read()
        logger.info(f"Processing file: {file.filename} ({len(content)} bytes)")

        # Write to temp file (both Docling and fallback need file path)
        import tempfile
        import os
        from pathlib import Path

        # Derive file suffix from actual filename
        file_suffix = Path(file.filename).suffix or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_suffix) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            # Try Docling first, fall back to pypdf if needed
            try:
                markdown_text, num_pages = extract_with_docling(tmp_path)
                extraction_method = "docling"
            except Exception as docling_error:
                logger.info(f"Trying fallback extraction: {docling_error}")
                try:
                    markdown_text, num_pages = extract_text_from_pdf_raw(tmp_path)
                    extraction_method = "raw_pdf"
                except Exception as fallback_error:
                    logger.error(f"Both extraction methods failed")
                    raise fallback_error from docling_error

            logger.info(
                f"✓ Extracted {len(markdown_text)} chars using {extraction_method}"
            )

            # Return response in format expected by extraction worker
            return JSONResponse(
                {
                    "markdown": markdown_text,
                    "text": markdown_text,  # Fallback field
                    "num_pages": num_pages,
                    "success": True,
                    "filename": file.filename,
                    "method": extraction_method,
                }
            )

        finally:
            # Clean up temp file
            try:
                os.remove(tmp_path)
            except:
                pass

    except Exception as e:
        logger.error(f"Conversion failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to convert document: {str(e)}"
        )


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "docling-server",
        "version": "1.0.0",
        "endpoints": {
            "health": "/health",
            "openapi": "/openapi.json",
            "convert": "/convert (POST)",
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5001)
