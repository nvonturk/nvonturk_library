import asyncio
import logging
from difflib import SequenceMatcher
from pathlib import Path

import pymupdf
import pymupdf4llm

logger = logging.getLogger(__name__)


def verify_pdf_content(
    pdf_path: Path, expected_title: str, expected_authors: list[str]
) -> dict:
    """Extract first-page text and verify it matches the expected paper.

    Returns a dict with:
        - verified: bool (True if content likely matches)
        - first_page_text: str (extracted text, truncated to 500 chars)
        - title_similarity: float (0-1 sequence match score)
        - reason: str (explanation if not verified)
    """
    try:
        doc = pymupdf.open(str(pdf_path))
        if doc.page_count == 0:
            doc.close()
            return {
                "verified": False,
                "first_page_text": "",
                "title_similarity": 0.0,
                "reason": "PDF has no pages",
            }
        first_page_text = doc[0].get_text("text")
        doc.close()
    except Exception as exc:
        return {
            "verified": False,
            "first_page_text": "",
            "title_similarity": 0.0,
            "reason": f"Failed to read PDF: {exc}",
        }

    if len(first_page_text.strip()) < 50:
        return {
            "verified": False,
            "first_page_text": first_page_text,
            "title_similarity": 0.0,
            "reason": "First page has too little text",
        }

    normalized_page = " ".join(first_page_text.lower().split())
    normalized_title = " ".join(expected_title.lower().split())

    title_similarity = SequenceMatcher(
        None, normalized_title, normalized_page[: len(normalized_title) * 3]
    ).ratio()

    title_words = [w for w in normalized_title.split() if len(w) > 3]
    if title_words:
        words_found = sum(1 for w in title_words if w in normalized_page)
        word_match_ratio = words_found / len(title_words)
    else:
        word_match_ratio = 0.0

    author_found = False
    for author in expected_authors[:3]:
        last_name = author.split()[-1].lower() if author.split() else ""
        if last_name and len(last_name) > 2 and last_name in normalized_page:
            author_found = True
            break

    verified = (word_match_ratio >= 0.5 or title_similarity >= 0.4) and (
        author_found or word_match_ratio >= 0.7
    )

    reason = ""
    if not verified:
        reason_parts = []
        if word_match_ratio < 0.5:
            reason_parts.append(f"title word match {word_match_ratio:.0%}")
        if title_similarity < 0.4:
            reason_parts.append(f"title similarity {title_similarity:.0%}")
        if not author_found:
            reason_parts.append("no author name found on first page")
        reason = "Low confidence: " + ", ".join(reason_parts)

    return {
        "verified": verified,
        "first_page_text": first_page_text[:500],
        "title_similarity": title_similarity,
        "reason": reason,
    }


class PdfConverter:
    async def convert(self, pdf_path: Path, output_path: Path) -> str:
        """Convert a PDF to markdown and write to output_path.

        Returns the markdown content.
        """
        markdown_content = await asyncio.to_thread(self._sync_convert, pdf_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown_content, encoding="utf-8")
        return markdown_content

    def _sync_convert(self, pdf_path: Path) -> str:
        """Synchronous conversion using pymupdf4llm."""
        try:
            return pymupdf4llm.to_markdown(str(pdf_path))
        except Exception as exc:
            logger.error("PDF conversion failed for %s: %s", pdf_path, exc)
            raise
