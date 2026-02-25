import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Context

from papertrail.config import PapertrailConfig
from papertrail.converter import PdfConverter
from papertrail.database import PaperDatabase
from papertrail.metadata import MetadataFetcher
from papertrail.models import PaperMetadata
from papertrail.sync import sync_pull, sync_pull_if_stale, sync_push, sync_delete
from papertrail.paper_store import PaperStore

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    config = PapertrailConfig.from_env()

    # Sync remote data to local directory
    await sync_pull(config.rclone_remote, config.data_dir)
    sync_state = {"last_pull_time": time.monotonic()}

    config.ensure_directories()

    store = PaperStore(config)
    db = PaperDatabase(config.index_db_path)
    await db.initialize()

    # Phase 1 (blocking): rebuild index from JSON files
    papers = await asyncio.to_thread(store.scan_all_papers)
    tags = await asyncio.to_thread(store.read_tags)
    await db.rebuild_from_papers(papers, tags)
    logger.info("Index rebuilt: %d papers, %d tags", len(papers), len(tags))

    # Phase 2 (background): rebuild fulltext index from paper.md files
    fulltext_ready = asyncio.Event()

    async def rebuild_fulltext_index():
        try:
            paper_texts = []
            for paper in papers:
                content = await asyncio.to_thread(store.read_paper_markdown, paper.bibtex_key)
                if content:
                    paper_texts.append((paper.bibtex_key, content))
            if paper_texts:
                await db.rebuild_fulltext(paper_texts)
            logger.info("Fulltext index rebuilt: %d papers", len(paper_texts))
        except Exception:
            logger.error("Fulltext index rebuild failed", exc_info=True)
        finally:
            fulltext_ready.set()

    asyncio.create_task(rebuild_fulltext_index())

    fetcher = MetadataFetcher(config)
    converter = PdfConverter()

    yield {
        "db": db,
        "config": config,
        "store": store,
        "fetcher": fetcher,
        "converter": converter,
        "fulltext_ready": fulltext_ready,
        "remote": config.rclone_remote,
        "sync_state": sync_state,
    }

    await fetcher.close()
    await db.close()


mcp = FastMCP("papertrail", lifespan=lifespan)


def _get_context(ctx: Context) -> dict:
    return ctx.request_context.lifespan_context


def _format_citation(paper: PaperMetadata) -> str:
    """Format a paper as a human-readable citation: Last (Year) or Last and Last (Year)."""
    year = paper.year or "n.d."
    if not paper.authors:
        return f"Unknown ({year})"
    def last_name(author: str) -> str:
        # Handle "Last, First" and "First Last" formats
        if "," in author:
            return author.split(",")[0].strip()
        return author.split()[-1]
    if len(paper.authors) == 1:
        name = last_name(paper.authors[0])
    elif len(paper.authors) == 2:
        name = f"{last_name(paper.authors[0])} and {last_name(paper.authors[1])}"
    else:
        name = f"{last_name(paper.authors[0])} et al."
    return f"{name} ({year})"


async def _ensure_synced(lc: dict) -> None:
    """Re-pull from remote if the last sync is stale, then rebuild the index."""
    config: PapertrailConfig = lc["config"]
    sync_state = lc["sync_state"]
    new_time = await sync_pull_if_stale(
        lc["remote"], config.data_dir, sync_state["last_pull_time"]
    )
    if new_time != sync_state["last_pull_time"]:
        sync_state["last_pull_time"] = new_time
        db: PaperDatabase = lc["db"]
        store: PaperStore = lc["store"]
        papers = await asyncio.to_thread(store.scan_all_papers)
        tags = await asyncio.to_thread(store.read_tags)
        await db.rebuild_from_papers(papers, tags)
        logger.info("Index refreshed after re-sync: %d papers", len(papers))


async def _push_paper(lc: dict, bibtex_key: str) -> None:
    """Push a paper directory to the remote after a local write."""
    await sync_push(lc["remote"], lc["config"].data_dir, f"papers/{bibtex_key}")


async def _push_tags(lc: dict) -> None:
    """Push tags.json to the remote after a local write."""
    await sync_push(lc["remote"], lc["config"].data_dir, "tags.json")


# ---------------------------------------------------------------------------
# Paper discovery
# ---------------------------------------------------------------------------


@mcp.tool()
async def find_paper(query: str, limit: int = 10, ctx: Context = None) -> str:
    """Search for academic papers by query string.

    Searches Semantic Scholar and arXiv. Returns titles, authors, years,
    citation counts, and identifiers for matching papers.

    Args:
        query: Search terms (e.g., "causal inference machine learning")
        limit: Maximum number of results to return (default 10)
    """
    lc = _get_context(ctx)
    fetcher: MetadataFetcher = lc["fetcher"]
    results = await fetcher.search(query, limit=limit)
    if not results:
        return "No papers found for this query. Try different search terms, or use web search as a fallback."
    lines = []
    for i, r in enumerate(results, 1):
        authors_str = ", ".join(r.authors[:3])
        if len(r.authors) > 3:
            authors_str += " et al."
        identifiers = []
        if r.doi:
            identifiers.append(f"DOI: {r.doi}")
        if r.arxiv_id:
            identifiers.append(f"arXiv: {r.arxiv_id}")
        id_str = " | ".join(identifiers) if identifiers else "no identifier"
        lines.append(
            f"{i}. **{r.title}** ({r.year})\n"
            f"   Authors: {authors_str}\n"
            f"   Citations: {r.citation_count or 'N/A'} | {id_str} | Source: {r.source}"
        )
        if r.abstract:
            truncated = r.abstract[:200] + "..." if len(r.abstract) > 200 else r.abstract
            lines.append(f"   Abstract: {truncated}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Paper ingestion
# ---------------------------------------------------------------------------


@mcp.tool()
async def ingest_paper(identifier: str, ctx: Context = None) -> str:
    """Download a paper and start converting it to markdown.

    Accepts a DOI, arXiv ID, SSRN ID/URL, or paper URL. Downloads the PDF,
    fetches metadata, generates a BibTeX key, and starts background conversion.

    Use conversion_status to check progress after calling this.

    Args:
        identifier: DOI (e.g., "10.1257/aer.2024.001"), arXiv ID (e.g., "2301.12345"),
                    SSRN URL/ID, or direct paper URL
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    config: PapertrailConfig = lc["config"]
    fetcher: MetadataFetcher = lc["fetcher"]
    converter: PdfConverter = lc["converter"]
    store: PaperStore = lc["store"]

    # 1. Look up metadata
    result = await fetcher.get_by_identifier(identifier)

    # Fallback for SSRN if Semantic Scholar doesn't have it
    if result is None:
        import re
        ssrn_match = re.search(r"(?:abstract=|^)(\d{5,})", identifier)
        if ssrn_match:
            ssrn_id = ssrn_match.group(1)
            result = await fetcher.get_crossref_metadata(f"10.2139/ssrn.{ssrn_id}")
            if result is None:
                result = await fetcher.get_ssrn_metadata(ssrn_id)

    if result is None:
        return f"Could not find paper with identifier: {identifier}. Try using find_paper to search by title."

    # 2. Generate unique bibtex key
    bibtex_key = await fetcher.generate_unique_key(result, db, store)
    paper_dir = config.papers_dir / bibtex_key
    paper_dir.mkdir(parents=True, exist_ok=True)

    # 3. Download PDF (before writing metadata)
    pdf_path = paper_dir / "paper.pdf"
    dl = await fetcher.download_pdf(result, pdf_path)

    # 4. Create paper metadata with correct initial status
    initial_status = "converting" if dl.success else "pending_pdf"
    paper = PaperMetadata(
        bibtex_key=bibtex_key,
        title=result.title,
        authors=result.authors,
        year=result.year,
        abstract=result.abstract,
        doi=result.doi,
        arxiv_id=result.arxiv_id,
        ssrn_id=result.ssrn_id,
        url=result.url,
        topics=result.topics,
        fields_of_study=result.fields_of_study,
        citation_count=result.citation_count,
        added_date=datetime.now(UTC).isoformat(),
        status=initial_status,
    )

    # 5. Write JSON source of truth, then update index
    await asyncio.to_thread(store.write_paper_metadata, paper)
    await db.upsert_paper(paper)
    await _push_paper(lc, bibtex_key)

    if not dl.success:
        failure_details = []
        for attempt in dl.attempts:
            detail = f"  - {attempt.url}: "
            if attempt.cloudflare_blocked:
                detail += "blocked by Cloudflare"
            elif attempt.error:
                detail += attempt.error
            else:
                detail += f"status={attempt.status_code}"
            failure_details.append(detail)
        details_text = "\n".join(failure_details) if failure_details else "  No URLs to try"
        return (
            f"Ingested metadata for **{bibtex_key}** but PDF download failed.\n\n"
            f"**Attempts:**\n{details_text}\n\n"
            f"To add the PDF manually, download it and place it at:\n"
            f"  `{pdf_path}`\n\n"
            f"Then call `ingest_paper_manual(\"{bibtex_key}\")` to continue processing."
        )

    # 6. Start background conversion
    md_path = paper_dir / "paper.md"

    async def background_convert():
        try:
            content = await converter.convert(pdf_path, md_path)
            await db.index_fulltext(bibtex_key, content)
            paper.status = "summarizing"
            await asyncio.to_thread(store.write_paper_metadata, paper)
            await db.update_status(bibtex_key, "summarizing")
        except Exception:
            logger.error("Background conversion failed for %s", bibtex_key, exc_info=True)
            paper.status = "error"
            await asyncio.to_thread(store.write_paper_metadata, paper)
            await db.update_status(bibtex_key, "error")
        await _push_paper(lc, bibtex_key)

    asyncio.create_task(background_convert())

    return (
        f"Paper ingested as **{bibtex_key}**\n\n"
        f"- Title: {result.title}\n"
        f"- Authors: {', '.join(result.authors)}\n"
        f"- Year: {result.year}\n\n"
        f"PDF is being converted to markdown in the background. "
        f"Use `conversion_status(\"{bibtex_key}\")` to check progress."
    )


@mcp.tool()
async def conversion_status(bibtex_key: str, ctx: Context = None) -> str:
    """Check the conversion and processing status of a paper.

    Args:
        bibtex_key: The paper's BibTeX key (e.g., "smith_2024_causal")
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    paper = await db.get_paper(bibtex_key)
    if paper is None:
        return f"No paper found with key: {bibtex_key}"
    status_desc = {
        "downloading": "PDF is being downloaded",
        "pending_pdf": "Metadata saved but PDF download failed. Place PDF manually and call ingest_paper_manual.",
        "converting": "PDF is being converted to markdown (this may take a minute)",
        "summarizing": "Conversion complete. Ready for summary generation.",
        "ready": "Fully processed with summary",
        "error": "Something went wrong during processing",
    }
    desc = status_desc.get(paper.status, paper.status)
    return f"**{paper.title}**\nStatus: **{paper.status}** - {desc}"


@mcp.tool()
async def ingest_paper_manual(
    bibtex_key: str,
    pdf_url: str | None = None,
    pdf_source_path: str | None = None,
    ctx: Context = None,
) -> str:
    """Provide a PDF for a paper that failed automatic download.

    The paper must already exist in the library (from a prior ingest_paper call).
    Provide either a direct PDF URL to download from, a local file path, or
    place the PDF at ~/.papertrail/papers/{bibtex_key}/paper.pdf beforehand.

    Args:
        bibtex_key: The paper's BibTeX key
        pdf_url: Optional URL to download the PDF from (e.g., NBER working paper URL)
        pdf_source_path: Optional absolute path to a local PDF file
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    config: PapertrailConfig = lc["config"]
    converter: PdfConverter = lc["converter"]
    fetcher: MetadataFetcher = lc["fetcher"]
    store: PaperStore = lc["store"]

    paper = await asyncio.to_thread(store.read_paper_metadata, bibtex_key)
    if paper is None:
        paper = await db.get_paper(bibtex_key)
    if paper is None:
        return f"No paper found with key: {bibtex_key}"

    paper_dir = config.papers_dir / bibtex_key
    paper_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = paper_dir / "paper.pdf"

    if pdf_url:
        try:
            response = await fetcher._download_get(pdf_url)
            content_type = response.headers.get("content-type", "")
            if response.status_code == 200 and (
                "pdf" in content_type or response.content[:5] == b"%PDF-"
            ):
                pdf_path.write_bytes(response.content)
            else:
                return (
                    f"Failed to download PDF from URL: status={response.status_code}, "
                    f"content-type={content_type}"
                )
        except Exception as exc:
            return f"Failed to download PDF from URL: {exc}"
    elif pdf_source_path:
        import shutil
        source = Path(pdf_source_path)
        if not source.exists():
            return f"Source PDF not found at: {pdf_source_path}"
        shutil.copy2(source, pdf_path)

    if not pdf_path.exists():
        return (
            f"No PDF found at `{pdf_path}`.\n"
            f"Provide a pdf_url, pdf_source_path, or place the PDF there manually."
        )

    # Start conversion
    paper.status = "converting"
    await asyncio.to_thread(store.write_paper_metadata, paper)
    await db.update_status(bibtex_key, "converting")
    md_path = paper_dir / "paper.md"

    async def background_convert():
        try:
            content = await converter.convert(pdf_path, md_path)
            await db.index_fulltext(bibtex_key, content)
            paper.status = "summarizing"
            await asyncio.to_thread(store.write_paper_metadata, paper)
            await db.update_status(bibtex_key, "summarizing")
        except Exception:
            logger.error("Conversion failed for %s", bibtex_key, exc_info=True)
            paper.status = "error"
            await asyncio.to_thread(store.write_paper_metadata, paper)
            await db.update_status(bibtex_key, "error")
        await _push_paper(lc, bibtex_key)

    asyncio.create_task(background_convert())

    return (
        f"PDF registered for **{bibtex_key}**. Converting to markdown in the background.\n"
        f"Use `conversion_status(\"{bibtex_key}\")` to check progress."
    )


# ---------------------------------------------------------------------------
# Reading papers
# ---------------------------------------------------------------------------


@mcp.tool()
async def read_paper(
    bibtex_key: str,
    start_line: int | None = None,
    end_line: int | None = None,
    ctx: Context = None,
) -> str:
    """Read the markdown content of a paper, optionally a specific line range.

    Args:
        bibtex_key: The paper's BibTeX key
        start_line: Optional start line number (1-indexed)
        end_line: Optional end line number (1-indexed, inclusive)
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    config: PapertrailConfig = lc["config"]
    md_path = config.papers_dir / bibtex_key / "paper.md"
    if not md_path.exists():
        return f"No markdown file found for {bibtex_key}. Check conversion_status."
    content = md_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    total_lines = len(lines)
    if start_line is not None or end_line is not None:
        start_idx = (start_line or 1) - 1
        end_idx = end_line or total_lines
        selected = lines[start_idx:end_idx]
        return (
            f"Lines {start_idx + 1}-{min(end_idx, total_lines)} of {total_lines} total:\n\n"
            + "\n".join(selected)
        )
    return f"Total lines: {total_lines}\n\n" + content


@mcp.tool()
async def get_paper_metadata(bibtex_key: str, ctx: Context = None) -> str:
    """Get structured metadata and summary for a specific paper.

    Args:
        bibtex_key: The paper's BibTeX key
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    paper = await db.get_paper(bibtex_key)
    if paper is None:
        return f"No paper found with key: {bibtex_key}"

    paper_tags = await db.get_paper_tags(bibtex_key)

    lines = [
        f"# {paper.title}",
        f"**Key**: {paper.bibtex_key}",
        f"**Authors**: {', '.join(paper.authors)}",
        f"**Year**: {paper.year}",
        f"**Status**: {paper.status}",
    ]
    if paper.journal:
        lines.append(f"**Journal**: {paper.journal}")
    if paper.doi:
        lines.append(f"**DOI**: {paper.doi}")
    if paper.arxiv_id:
        lines.append(f"**arXiv**: {paper.arxiv_id}")
    if paper.ssrn_id:
        lines.append(f"**SSRN**: {paper.ssrn_id}")
    if paper.citation_count is not None:
        lines.append(f"**Citations**: {paper.citation_count}")
    if paper_tags:
        lines.append(f"**Tags**: {', '.join(paper_tags)}")
    if paper.topics:
        lines.append(f"**Topics**: {', '.join(paper.topics)}")
    if paper.keywords:
        lines.append(f"**Keywords**: {', '.join(paper.keywords)}")
    if paper.fields_of_study:
        lines.append(f"**Fields**: {', '.join(paper.fields_of_study)}")
    if paper.abstract:
        lines.append(f"\n**Abstract**: {paper.abstract}")
    if paper.summary:
        lines.append(f"\n**Summary**: ```json\n{json.dumps(paper.summary, indent=2)}\n```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Summary storage
# ---------------------------------------------------------------------------


@mcp.tool()
async def store_summary(
    bibtex_key: str,
    summary: str,
    keywords: list[str] | None = None,
    ctx: Context = None,
) -> str:
    """Store a summary for a paper and set its status to 'ready'.

    Call this after reading a paper's markdown and generating a structured summary.
    The summary must only contain information from the paper's actual text.
    Do not include claims from outside knowledge.

    Args:
        bibtex_key: The paper's BibTeX key
        summary: JSON string with the summary. Should include keys like
                 "main_contribution", "methodology", "findings", "limitations",
                 "section_summaries" (object mapping section names to summaries)
        keywords: Optional list of descriptive keywords for the paper
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    paper = await asyncio.to_thread(store.read_paper_metadata, bibtex_key)
    if paper is None:
        paper = await db.get_paper(bibtex_key)
    if paper is None:
        return f"No paper found with key: {bibtex_key}"

    try:
        summary_data = json.loads(summary) if isinstance(summary, str) else summary
    except json.JSONDecodeError as exc:
        return f"Invalid JSON in summary: {exc}"

    # Update paper metadata (source of truth)
    paper.summary = summary_data
    paper.status = "ready"
    if keywords:
        paper.keywords = keywords
    await asyncio.to_thread(store.write_paper_metadata, paper)
    await asyncio.to_thread(store.write_summary_file, bibtex_key, summary_data)

    # Update index
    await db.store_summary(bibtex_key, summary_data)
    if keywords:
        await db.update_keywords(bibtex_key, keywords)
    await db.update_status(bibtex_key, "ready")
    await _push_paper(lc, bibtex_key)

    return f"Summary stored for **{bibtex_key}**. Status set to 'ready'."


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_library(query: str, limit: int = 20, ctx: Context = None) -> str:
    """Search the paper library by metadata, topics, keywords, and summaries.

    Uses full-text search over paper metadata and summaries. Good for finding
    papers by topic, method, author, or content of summaries.

    Args:
        query: Search query (e.g., "climate risk", "difference-in-differences", "Smith")
        limit: Maximum results to return (default 20)
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    results = await db.search_metadata(query, limit=limit)
    if not results:
        return f"No papers found matching '{query}' in library metadata/summaries."
    lines = []
    for paper in results:
        paper_tags = await db.get_paper_tags(paper.bibtex_key)
        tags_str = f" [{', '.join(paper_tags)}]" if paper_tags else ""
        citation = _format_citation(paper)
        entry = f"- **{citation}**: {paper.title}{tags_str} `{paper.bibtex_key}`"
        if paper.abstract:
            truncated = paper.abstract[:150] + "..." if len(paper.abstract) > 150 else paper.abstract
            entry += f"\n  {truncated}"
        lines.append(entry)
    return "\n\n".join(lines)


@mcp.tool()
async def search_paper_text(query: str, limit: int = 10, ctx: Context = None) -> str:
    """Search over the full text content of papers in the library.

    Uses full-text search over the markdown content of all papers.
    Returns matching snippets with paper keys. Use this when search_library
    doesn't find what you need.

    Args:
        query: Search query to match against paper content
        limit: Maximum results to return (default 10)
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    fulltext_ready: asyncio.Event = lc["fulltext_ready"]

    if not fulltext_ready.is_set():
        return "Fulltext index is still building. Please try again in a moment."

    results = await db.search_fulltext(query, limit=limit)
    if not results:
        return f"No matches for '{query}' in paper full text."
    lines = []
    for r in results:
        paper = await db.get_paper(r["bibtex_key"])
        cite = _format_citation(paper) if paper else r["bibtex_key"]
        lines.append(f"**{cite}** `{r['bibtex_key']}`: ...{r['snippet']}...")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Paper listing
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_papers(
    status: str | None = None,
    tag: str | None = None,
    limit: int = 50,
    ctx: Context = None,
) -> str:
    """List papers in the library, optionally filtered by status or tag.

    Args:
        status: Filter by status (downloading, converting, summarizing, ready, error)
        tag: Filter by tag name
        limit: Maximum number of papers to return (default 50)
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    papers = await db.list_papers(status=status, tag=tag, limit=limit)
    if not papers:
        return "No papers found in the library."
    lines = []
    for paper in papers:
        paper_tags = await db.get_paper_tags(paper.bibtex_key)
        tags_str = f" [{', '.join(paper_tags)}]" if paper_tags else ""
        citation = _format_citation(paper)
        lines.append(
            f"- **{citation}**: {paper.title}{tags_str} `{paper.bibtex_key}` [{paper.status}]"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tag management
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_tags(prefix: str | None = None, ctx: Context = None) -> str:
    """Return all tags in the vocabulary with paper counts.

    Args:
        prefix: Optional prefix to filter tags (e.g., "climate" returns "climate-risk", "climate-finance", etc.)
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    tags = await db.list_tags(prefix=prefix)
    if not tags:
        return "No tags in the vocabulary yet."
    lines = []
    for tag in tags:
        desc = f" - {tag.description}" if tag.description else ""
        lines.append(f"- **{tag.tag}** ({tag.paper_count} papers){desc}")
    return "\n".join(lines)


@mcp.tool()
async def add_tags(tags: str, ctx: Context = None) -> str:
    """Add new tags to the vocabulary.

    Args:
        tags: JSON array of objects with 'tag' (required) and 'description' (optional).
              Example: [{"tag": "causal-inference", "description": "Papers using causal methods"}]
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    try:
        tag_list = json.loads(tags) if isinstance(tags, str) else tags
    except json.JSONDecodeError as exc:
        return f"Invalid JSON: {exc}"

    if not isinstance(tag_list, list):
        return "Expected a JSON array of tag objects."

    # Update source of truth: merge into tags.json
    existing_tags = await asyncio.to_thread(store.read_tags)
    existing_tag_names = {t["tag"] for t in existing_tags}
    for new_tag in tag_list:
        if new_tag["tag"] not in existing_tag_names:
            existing_tags.append(new_tag)
            existing_tag_names.add(new_tag["tag"])
    await asyncio.to_thread(store.write_tags, existing_tags)

    # Update index
    await db.add_tags(tag_list)
    await _push_tags(lc)

    tag_names = [t["tag"] for t in tag_list]
    return f"Added tags: {', '.join(tag_names)}"


@mcp.tool()
async def tag_paper(bibtex_key: str, tags: str, ctx: Context = None) -> str:
    """Associate tags with a paper. Tags must exist in the vocabulary first (use add_tags).

    Args:
        bibtex_key: The paper's BibTeX key
        tags: JSON array of tag names, e.g. ["causal-inference", "macro"]
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    paper = await asyncio.to_thread(store.read_paper_metadata, bibtex_key)
    if paper is None:
        paper = await db.get_paper(bibtex_key)
    if paper is None:
        return f"No paper found with key: {bibtex_key}"

    try:
        tag_list = json.loads(tags) if isinstance(tags, str) else tags
    except json.JSONDecodeError as exc:
        return f"Invalid JSON: {exc}"

    if not isinstance(tag_list, list):
        return "Expected a JSON array of tag names."

    # Check tags exist
    existing_tags = {t.tag for t in await db.list_tags()}
    missing = [t for t in tag_list if t not in existing_tags]
    if missing:
        return f"Tags not in vocabulary: {', '.join(missing)}. Use add_tags first."

    # Update source of truth: append new tags to paper's tag list
    current_tags = set(paper.tags)
    for tag_name in tag_list:
        current_tags.add(tag_name)
    paper.tags = sorted(current_tags)
    await asyncio.to_thread(store.write_paper_metadata, paper)

    # Update index
    await db.tag_paper(bibtex_key, tag_list)
    await _push_paper(lc, bibtex_key)

    return f"Tagged **{bibtex_key}** with: {', '.join(tag_list)}"


# ---------------------------------------------------------------------------
# Paper management
# ---------------------------------------------------------------------------


@mcp.tool()
async def delete_paper(bibtex_key: str, ctx: Context = None) -> str:
    """Delete a paper from the library entirely (both files and index).

    Args:
        bibtex_key: The paper's BibTeX key
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    deleted_from_index = await db.delete_paper(bibtex_key)
    deleted_from_store = await asyncio.to_thread(store.delete_paper_dir, bibtex_key)

    if not deleted_from_index and not deleted_from_store:
        return f"No paper found with key: {bibtex_key}"
    await sync_delete(lc["remote"], f"papers/{bibtex_key}")
    return f"Deleted **{bibtex_key}** from the library."


# ---------------------------------------------------------------------------
# Index rebuild (replaces sync)
# ---------------------------------------------------------------------------


@mcp.tool()
async def rebuild_index(ctx: Context = None) -> str:
    """Force a full rescan of the paper library and rebuild the search index.

    Use this if the index seems stale or after manually adding/modifying files.
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    await db.initialize()

    papers = await asyncio.to_thread(store.scan_all_papers)
    tags = await asyncio.to_thread(store.read_tags)
    await db.rebuild_from_papers(papers, tags)

    paper_texts = []
    for paper in papers:
        content = await asyncio.to_thread(store.read_paper_markdown, paper.bibtex_key)
        if content:
            paper_texts.append((paper.bibtex_key, content))
    if paper_texts:
        await db.rebuild_fulltext(paper_texts)

    fulltext_ready: asyncio.Event = lc["fulltext_ready"]
    fulltext_ready.set()

    return (
        f"Index rebuilt: {len(papers)} papers, {len(tags)} tags, "
        f"{len(paper_texts)} fulltext entries."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
