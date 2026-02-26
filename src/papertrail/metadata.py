import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession

from papertrail.config import PapertrailConfig
from papertrail.models import SearchResult

logger = logging.getLogger(__name__)

SEMANTIC_SCHOLAR_BASE = "https://api.semanticscholar.org/graph/v1"
ARXIV_API_BASE = "https://export.arxiv.org/api/query"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}

SEMANTIC_SCHOLAR_FIELDS = (
    "paperId,title,authors,year,abstract,venue,externalIds,"
    "citationCount,fieldsOfStudy,s2FieldsOfStudy,"
    "isOpenAccess,openAccessPdf,journal,publicationDate"
)

SKIP_TITLE_WORDS = {"a", "an", "the", "on", "in", "of", "for", "and", "to", "with", "by", "from", "is", "are", "at"}

SSRN_ABSTRACT_PATTERN = re.compile(r"ssrn\.com/abstract=(\d+)")
SSRN_ID_PATTERN = re.compile(r"^\d{5,}$")
ARXIV_ID_PATTERN = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$")
DOI_PATTERN = re.compile(r"^10\.\d{4,}")

UNPAYWALL_BASE = "https://api.unpaywall.org/v2"
CROSSREF_BASE = "https://api.crossref.org/works"
OPENALEX_BASE = "https://api.openalex.org/works"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

CLOUDFLARE_MARKERS = [
    "Just a moment",
    "cf-browser-verification",
    "challenge-platform",
    "_cf_chl",
    "Checking your browser",
    "Cloudflare",
]

NBER_WP_PATTERN = re.compile(r"/working_papers/(w\d+)|nber\.org/papers/(w\d+)")


@dataclass
class DownloadAttempt:
    url: str
    status_code: int | None = None
    content_type: str = ""
    cloudflare_blocked: bool = False
    error: str | None = None


@dataclass
class DownloadResult:
    success: bool
    pdf_path: Path | None = None
    attempts: list[DownloadAttempt] = field(default_factory=list)
    candidate_urls: list[str] = field(default_factory=list)


class _RateLimiter:
    """Enforces a minimum interval between calls (e.g., 1 request/second)."""

    def __init__(self, min_interval: float = 1.0):
        self.min_interval = min_interval
        self._last_request_time: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self._last_request_time = time.monotonic()


class MetadataFetcher:
    def __init__(self, config: PapertrailConfig):
        self.config = config
        self.api_key = config.semantic_scholar_api_key
        self.unpaywall_email = config.unpaywall_email
        self._ss_rate_limiter = _RateLimiter(min_interval=1.0)

        api_headers = {}
        if self.api_key:
            api_headers["x-api-key"] = self.api_key
        proxy = config.http_proxy or None

        self.client = httpx.AsyncClient(
            timeout=30.0, headers=api_headers, follow_redirects=True, proxy=proxy
        )
        self.download_client = httpx.AsyncClient(
            timeout=60.0, headers=dict(BROWSER_HEADERS), follow_redirects=True, proxy=proxy
        )
        self._curl_session = CurlAsyncSession(
            impersonate="chrome",
            timeout=60,
            allow_redirects=True,
            proxy=proxy,
        )

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Search Semantic Scholar and arXiv, merge and deduplicate results."""
        ss_results = await self._search_semantic_scholar(query, limit)
        arxiv_results = await self._search_arxiv(query, limit)
        combined = ss_results + arxiv_results
        return self._deduplicate(combined)[:limit]

    async def get_by_identifier(self, identifier: str) -> SearchResult | None:
        """Look up a paper by DOI, arXiv ID, SSRN ID/URL, or Semantic Scholar URL.

        Tries Semantic Scholar first, then CrossRef as a fallback for DOI-based lookups.
        """
        paper_id = self._normalize_identifier(identifier)
        if paper_id is None:
            return None

        response = await self._ss_get(f"/paper/{paper_id}")
        if response is not None:
            return self._parse_ss_result(response)

        # Fallback to CrossRef for DOI-based identifiers
        doi = self._extract_doi_from_identifier(identifier)
        if doi:
            return await self.get_crossref_metadata(doi)

        return None

    async def get_ssrn_metadata(self, ssrn_id: str) -> SearchResult | None:
        """Scrape metadata from an SSRN abstract page as a fallback."""
        url = f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={ssrn_id}"
        try:
            response = await self.client.get(url, follow_redirects=True)
            if response.status_code != 200:
                return None
        except httpx.HTTPError:
            return None

        html = response.text
        title = self._extract_meta(html, "citation_title")
        authors_raw = self._extract_meta_all(html, "citation_author")
        abstract = self._extract_meta(html, "description")
        date = self._extract_meta(html, "citation_publication_date") or self._extract_meta(html, "citation_online_date")
        doi = self._extract_meta(html, "citation_doi")
        pdf_url = self._extract_meta(html, "citation_pdf_url")

        if not title:
            return None

        year = None
        if date:
            year_match = re.search(r"(\d{4})", date)
            if year_match:
                year = int(year_match.group(1))

        return SearchResult(
            title=title,
            authors=authors_raw or [],
            year=year,
            abstract=abstract,
            doi=doi,
            ssrn_id=ssrn_id,
            url=url,
            open_access_pdf_url=pdf_url,
            source="ssrn",
        )

    async def get_crossref_metadata(self, doi: str) -> SearchResult | None:
        """Look up paper metadata via CrossRef API. Works for any DOI including SSRN."""
        try:
            response = await self.client.get(
                f"{CROSSREF_BASE}/{doi}",
                headers={"Accept": "application/json"},
            )
            if response.status_code != 200:
                return None
            data = response.json().get("message", {})
            return self._parse_crossref_result(data)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("CrossRef lookup failed for %s: %s", doi, exc)
            return None

    def _parse_crossref_result(self, data: dict) -> SearchResult | None:
        """Parse a CrossRef API response into a SearchResult."""
        titles = data.get("title", [])
        if not titles:
            return None
        title = titles[0]

        authors = []
        for author in data.get("author", []):
            given = author.get("given", "")
            family = author.get("family", "")
            if given and family:
                authors.append(f"{given} {family}")
            elif family:
                authors.append(family)

        year = None
        for date_field in ("published-print", "published-online", "created"):
            date_info = data.get(date_field)
            if date_info and date_info.get("date-parts"):
                parts = date_info["date-parts"][0]
                if parts and parts[0]:
                    year = parts[0]
                    break

        abstract = data.get("abstract", "")
        if abstract:
            abstract = re.sub(r"<[^>]+>", "", abstract).strip()

        doi = data.get("DOI")
        ssrn_id = None
        if doi and "ssrn" in doi.lower():
            ssrn_match = re.search(r"ssrn\.(\d+)", doi, re.IGNORECASE)
            if ssrn_match:
                ssrn_id = ssrn_match.group(1)

        pdf_url = None
        for link in data.get("link", []):
            if link.get("content-type") == "application/pdf":
                pdf_url = link.get("URL")
                break

        url = data.get("URL") or (f"https://doi.org/{doi}" if doi else None)

        return SearchResult(
            title=title,
            authors=authors,
            year=year,
            abstract=abstract or None,
            doi=doi,
            ssrn_id=ssrn_id,
            url=url,
            open_access_pdf_url=pdf_url,
            source="crossref",
        )

    def _extract_doi_from_identifier(self, identifier: str) -> str | None:
        """Extract a DOI from a user-provided identifier string."""
        identifier = identifier.strip()
        if DOI_PATTERN.match(identifier):
            return identifier
        if identifier.upper().startswith("DOI:"):
            return identifier[4:]
        # SSRN URL -> SSRN DOI (handle both abstract= and abstract_id=)
        ssrn_match = SSRN_ABSTRACT_PATTERN.search(identifier)
        if ssrn_match:
            return f"10.2139/ssrn.{ssrn_match.group(1)}"
        ssrn_id_match = re.search(r"abstract_id=(\d+)", identifier)
        if ssrn_id_match:
            return f"10.2139/ssrn.{ssrn_id_match.group(1)}"
        # Bare SSRN ID
        if SSRN_ID_PATTERN.match(identifier):
            return f"10.2139/ssrn.{identifier}"
        return None

    def generate_bibtex_key(self, result: SearchResult) -> str:
        """Generate a bibtex key: lastname_year_firstword."""
        first_author_last = "unknown"
        if result.authors:
            last_name = result.authors[0].split()[-1]
            first_author_last = re.sub(r"[^a-z]", "", last_name.lower())

        year = result.year or 0

        title_words = re.sub(r"[^a-z\s]", "", result.title.lower()).split()
        first_word = "paper"
        for word in title_words:
            if word not in SKIP_TITLE_WORDS and len(word) > 1:
                first_word = word
                break

        return f"{first_author_last}_{year}_{first_word}"

    async def generate_unique_key(self, result: SearchResult, db, store=None) -> str:
        """Generate a bibtex key, ensuring uniqueness by appending a suffix if needed.

        Checks both the index (db) and the filesystem (store) to guarantee
        uniqueness even if the index hasn't rebuilt yet.
        """
        base_key = self.generate_bibtex_key(result)
        key = base_key
        suffix = ord("a")
        while True:
            index_exists = await db.check_bibtex_key_exists(key)
            store_exists = store.paper_dir_exists(key) if store else False
            if not index_exists and not store_exists:
                break
            key = f"{base_key}_{chr(suffix)}"
            suffix += 1
            if suffix > ord("z"):
                key = f"{base_key}_{suffix - ord('a')}"
                break
        return key

    def get_candidate_urls(self, result: SearchResult) -> list[str]:
        """Build an ordered list of candidate PDF download URLs for a paper."""
        urls = []

        if result.arxiv_id:
            clean_id = result.arxiv_id.split("v")[0] if "v" in result.arxiv_id else result.arxiv_id
            urls.append(f"https://arxiv.org/pdf/{clean_id}")

        nber_url = self._get_nber_pdf_url(result)
        if nber_url:
            urls.append(nber_url)

        if result.url and result.url not in urls:
            urls.append(result.url)

        if result.open_access_pdf_url and result.open_access_pdf_url not in urls:
            urls.append(result.open_access_pdf_url)

        doi_url = f"https://doi.org/{result.doi}" if result.doi else None
        if doi_url and doi_url not in urls:
            urls.append(doi_url)

        if result.ssrn_id:
            urls.append(f"https://papers.ssrn.com/sol3/Delivery.cfm?abstractid={result.ssrn_id}")

        return urls

    async def download_pdf(
        self, result: SearchResult, dest_path: Path, verify: bool = True
    ) -> DownloadResult:
        """Download the PDF for a paper, trying multiple sources.

        Returns a DownloadResult with success status, path, and details of each attempt.
        The caller can use candidate_urls for manual/browser-based fallback.

        When verify=True, downloaded PDFs are checked against expected title/authors
        to reject wrong papers, errata, or paywall landing pages saved as PDF.

        Order of attempts:
        1. arXiv PDF
        2. NBER working paper PDF
        3. Direct URL
        4. Open access PDF URL (from Semantic Scholar)
        5. PubMed Central
        6. OpenAlex (institutional repositories, preprint servers)
        7. Unpaywall (finds legal open access copies)
        8. DOI redirect (works through institutional VPN/proxy)
        9. SSRN direct download (often Cloudflare-blocked)
        """
        from papertrail.converter import verify_pdf_content

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        urls_to_try = []

        # 1. arXiv — fast, reliable, no paywall
        if result.arxiv_id:
            clean_id = result.arxiv_id.split("v")[0] if "v" in result.arxiv_id else result.arxiv_id
            urls_to_try.append(f"https://arxiv.org/pdf/{clean_id}")

        # 2. NBER working papers
        nber_url = self._get_nber_pdf_url(result)
        if nber_url:
            urls_to_try.append(nber_url)

        # 3. Direct URL (e.g., author website, conference proceedings)
        if result.url and result.url not in urls_to_try:
            urls_to_try.append(result.url)

        # 4. Semantic Scholar open access PDF
        if result.open_access_pdf_url and result.open_access_pdf_url not in urls_to_try:
            urls_to_try.append(result.open_access_pdf_url)

        # 5. PubMed Central
        if result.doi:
            pmc_url = await self._get_pmc_pdf_url(result.doi)
            if pmc_url:
                urls_to_try.append(pmc_url)

        # 6. OpenAlex (institutional repositories, preprint servers)
        if result.doi:
            openalex_urls = await self._get_openalex_pdf_urls(result.doi)
            for oa_url in openalex_urls:
                if oa_url not in urls_to_try:
                    urls_to_try.append(oa_url)

        # 7. Unpaywall
        if result.doi:
            unpaywall_url = await self._get_unpaywall_pdf_url(result.doi)
            if unpaywall_url and unpaywall_url not in urls_to_try:
                urls_to_try.append(unpaywall_url)

        # 8. DOI redirect — works through institutional VPN/proxy
        if result.doi:
            urls_to_try.append(f"https://doi.org/{result.doi}")

        # 9. SSRN — last because frequently Cloudflare-blocked
        if result.ssrn_id:
            urls_to_try.append(f"https://papers.ssrn.com/sol3/Delivery.cfm?abstractid={result.ssrn_id}")

        download_result = DownloadResult(
            success=False,
            candidate_urls=self.get_candidate_urls(result),
        )

        for url in urls_to_try:
            attempt = DownloadAttempt(url=url)
            try:
                response = await self._download_get(url)
                attempt.status_code = response.status_code
                attempt.content_type = response.headers.get("content-type", "")

                if response.status_code == 200 and self._is_cloudflare_challenge(response):
                    attempt.cloudflare_blocked = True
                    attempt.error = "Cloudflare challenge page"
                    download_result.attempts.append(attempt)
                    continue

                if response.status_code == 200 and (
                    "pdf" in attempt.content_type or response.content[:5] == b"%PDF-"
                ):
                    dest_path.write_bytes(response.content)

                    if verify and result.title:
                        verification = await asyncio.to_thread(
                            verify_pdf_content, dest_path, result.title, result.authors
                        )
                        if not verification["verified"]:
                            attempt.error = f"Content mismatch: {verification['reason']}"
                            dest_path.unlink(missing_ok=True)
                            download_result.attempts.append(attempt)
                            continue

                    download_result.success = True
                    download_result.pdf_path = dest_path
                    download_result.attempts.append(attempt)
                    return download_result

                attempt.error = f"Not a PDF: status={response.status_code}, content-type={attempt.content_type}"
            except Exception as exc:
                attempt.error = str(exc)
            download_result.attempts.append(attempt)

        return download_result

    async def _download_get(self, url: str):
        """Fetch a URL for PDF download. Uses curl_cffi (Chrome TLS fingerprint) if available."""
        if self._curl_session:
            try:
                return await self._curl_session.get(
                    url, headers={"Accept": "application/pdf,*/*;q=0.8"}
                )
            except Exception as exc:
                logger.debug("curl_cffi request failed for %s: %s, falling back to httpx", url, exc)
        return await self.download_client.get(
            url, headers={"Accept": "application/pdf,*/*;q=0.8"}
        )

    def _is_cloudflare_challenge(self, response) -> bool:
        """Detect Cloudflare challenge pages in a response."""
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            return False
        snippet = response.text[:2048]
        return any(marker in snippet for marker in CLOUDFLARE_MARKERS)

    def _get_nber_pdf_url(self, result: SearchResult) -> str | None:
        """Extract an NBER working paper PDF URL if the paper is from NBER."""
        sources_to_check = [result.url or "", result.open_access_pdf_url or ""]
        for source in sources_to_check:
            match = NBER_WP_PATTERN.search(source)
            if match:
                wp_id = match.group(1) or match.group(2)
                return f"https://www.nber.org/system/files/working_papers/{wp_id}/{wp_id}.pdf"
        return None

    async def _get_pmc_pdf_url(self, doi: str) -> str | None:
        """Check if a paper is available in PubMed Central."""
        try:
            response = await self.client.get(
                "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/",
                params={"ids": doi, "format": "json", "tool": "papertrail",
                        "email": self.unpaywall_email or ""},
            )
            if response.status_code != 200:
                return None
            data = response.json()
            records = data.get("records", [])
            if records and records[0].get("pmcid"):
                pmcid = records[0]["pmcid"]
                return f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"
            return None
        except (httpx.HTTPError, KeyError):
            return None

    async def _get_openalex_pdf_urls(self, doi: str) -> list[str]:
        """Query OpenAlex for PDF URLs from institutional repositories and publishers."""
        params = {}
        if self.unpaywall_email:
            params["mailto"] = self.unpaywall_email
        try:
            response = await self.client.get(
                f"{OPENALEX_BASE}/https://doi.org/{doi}",
                params=params,
            )
            if response.status_code != 200:
                return []
            data = response.json()
            pdf_urls = []
            oa_info = data.get("open_access", {})
            oa_url = oa_info.get("oa_url")
            if oa_url:
                pdf_urls.append(oa_url)
            for location in data.get("locations", []):
                pdf_url = location.get("pdf_url")
                if pdf_url and pdf_url not in pdf_urls:
                    pdf_urls.append(pdf_url)
            return pdf_urls
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.debug("OpenAlex lookup failed for %s: %s", doi, exc)
            return []

    async def _get_unpaywall_pdf_url(self, doi: str) -> str | None:
        """Query Unpaywall for an open access PDF URL."""
        if not self.unpaywall_email:
            return None
        try:
            response = await self.client.get(
                f"{UNPAYWALL_BASE}/{doi}",
                params={"email": self.unpaywall_email},
            )
            if response.status_code != 200:
                return None
            data = response.json()
            best_oa = data.get("best_oa_location")
            if best_oa and best_oa.get("url_for_pdf"):
                return best_oa["url_for_pdf"]
            # Check all OA locations
            for loc in data.get("oa_locations", []):
                if loc.get("url_for_pdf"):
                    return loc["url_for_pdf"]
            return None
        except (httpx.HTTPError, KeyError):
            return None

    async def close(self) -> None:
        await self.client.aclose()
        await self.download_client.aclose()
        if self._curl_session:
            await self._curl_session.close()

    # --- Private methods ---

    async def _search_semantic_scholar(self, query: str, limit: int) -> list[SearchResult]:
        try:
            await self._ss_rate_limiter.acquire()
            params = {
                "query": query,
                "fields": SEMANTIC_SCHOLAR_FIELDS,
                "limit": min(limit, 100),
            }
            response = await self.client.get(
                f"{SEMANTIC_SCHOLAR_BASE}/paper/search",
                params=params,
            )
            if response.status_code == 429:
                logger.warning("Semantic Scholar rate limited")
                return []
            response.raise_for_status()
            data = response.json()
            return [self._parse_ss_result(item) for item in data.get("data", [])]
        except httpx.HTTPError as exc:
            logger.warning("Semantic Scholar search failed: %s", exc)
            return []

    async def _search_arxiv(self, query: str, limit: int) -> list[SearchResult]:
        try:
            params = {
                "search_query": f"all:{query}",
                "start": 0,
                "max_results": min(limit, 50),
            }
            response = await self.client.get(ARXIV_API_BASE, params=params)
            response.raise_for_status()
            return self._parse_arxiv_xml(response.text)
        except httpx.HTTPError as exc:
            logger.warning("arXiv search failed: %s", exc)
            return []

    async def _ss_get(self, path: str) -> dict | None:
        """Make a GET request to Semantic Scholar and return JSON or None."""
        try:
            await self._ss_rate_limiter.acquire()
            response = await self.client.get(
                f"{SEMANTIC_SCHOLAR_BASE}{path}",
                params={"fields": SEMANTIC_SCHOLAR_FIELDS},
            )
            if response.status_code == 404:
                return None
            if response.status_code == 429:
                logger.warning("Semantic Scholar rate limited")
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            logger.warning("Semantic Scholar request failed: %s", exc)
            return None

    def _normalize_identifier(self, identifier: str) -> str | None:
        """Convert a user-provided identifier into a Semantic Scholar paper ID."""
        identifier = identifier.strip()

        # SSRN URL
        ssrn_match = SSRN_ABSTRACT_PATTERN.search(identifier)
        if ssrn_match:
            # Semantic Scholar indexes some SSRN papers by DOI
            return f"URL:https://papers.ssrn.com/sol3/papers.cfm?abstract_id={ssrn_match.group(1)}"

        # Bare SSRN ID
        if SSRN_ID_PATTERN.match(identifier):
            return f"URL:https://papers.ssrn.com/sol3/papers.cfm?abstract_id={identifier}"

        # arXiv ID
        arxiv_match = ARXIV_ID_PATTERN.match(identifier)
        if arxiv_match:
            return f"ARXIV:{arxiv_match.group(1)}"

        # arXiv URL
        arxiv_url_match = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", identifier)
        if arxiv_url_match:
            return f"ARXIV:{arxiv_url_match.group(1)}"

        # DOI
        if DOI_PATTERN.match(identifier):
            return f"DOI:{identifier}"

        # DOI with prefix
        if identifier.upper().startswith("DOI:"):
            return identifier

        # Semantic Scholar URL
        if "semanticscholar.org" in identifier:
            parts = identifier.rstrip("/").split("/")
            return parts[-1]  # corpus ID or paper hash

        # Generic URL — try as-is
        if identifier.startswith("http"):
            return f"URL:{identifier}"

        # Last resort: treat as a title search — won't work with get_by_identifier
        return None

    def _parse_ss_result(self, data: dict) -> SearchResult:
        authors = [a.get("name", "") for a in data.get("authors", [])]
        external_ids = data.get("externalIds", {}) or {}
        fields = data.get("fieldsOfStudy", []) or []
        s2_fields = data.get("s2FieldsOfStudy", []) or []
        topics = [f["category"] for f in s2_fields if f.get("category")]

        oa_pdf = None
        oa_data = data.get("openAccessPdf")
        if oa_data and isinstance(oa_data, dict):
            oa_pdf = oa_data.get("url")

        doi = external_ids.get("DOI")

        # Extract SSRN ID from externalIds or from DOI
        ssrn_id = None
        if "SSRN" in external_ids:
            ssrn_id = str(external_ids["SSRN"])
        elif doi and "ssrn" in doi.lower():
            ssrn_match = re.search(r"ssrn\.(\d+)", doi, re.IGNORECASE)
            if ssrn_match:
                ssrn_id = ssrn_match.group(1)

        return SearchResult(
            title=data.get("title", ""),
            authors=authors,
            year=data.get("year"),
            abstract=data.get("abstract"),
            doi=doi,
            arxiv_id=external_ids.get("ArXiv"),
            ssrn_id=ssrn_id,
            url=f"https://www.semanticscholar.org/paper/{data.get('paperId', '')}",
            citation_count=data.get("citationCount"),
            topics=topics,
            fields_of_study=fields,
            open_access_pdf_url=oa_pdf,
            source="semantic_scholar",
        )

    def _parse_arxiv_xml(self, xml_text: str) -> list[SearchResult]:
        results = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []

        for entry in root.findall("atom:entry", ARXIV_NS):
            title_el = entry.find("atom:title", ARXIV_NS)
            title = title_el.text.strip().replace("\n", " ") if title_el is not None and title_el.text else ""

            authors = []
            for author_el in entry.findall("atom:author", ARXIV_NS):
                name_el = author_el.find("atom:name", ARXIV_NS)
                if name_el is not None and name_el.text:
                    authors.append(name_el.text.strip())

            abstract_el = entry.find("atom:summary", ARXIV_NS)
            abstract = abstract_el.text.strip().replace("\n", " ") if abstract_el is not None and abstract_el.text else None

            published_el = entry.find("atom:published", ARXIV_NS)
            year = None
            if published_el is not None and published_el.text:
                year_match = re.search(r"(\d{4})", published_el.text)
                if year_match:
                    year = int(year_match.group(1))

            id_el = entry.find("atom:id", ARXIV_NS)
            arxiv_id = None
            if id_el is not None and id_el.text:
                id_match = re.search(r"(\d{4}\.\d{4,5})", id_el.text)
                if id_match:
                    arxiv_id = id_match.group(1)

            doi_el = entry.find("{http://arxiv.org/schemas/atom}doi")
            doi = doi_el.text.strip() if doi_el is not None and doi_el.text else None

            categories = []
            for cat_el in entry.findall("{http://arxiv.org/schemas/atom}primary_category"):
                term = cat_el.get("term")
                if term:
                    categories.append(term)

            results.append(SearchResult(
                title=title,
                authors=authors,
                year=year,
                abstract=abstract,
                doi=doi,
                arxiv_id=arxiv_id,
                url=f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None,
                topics=categories,
                open_access_pdf_url=f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None,
                source="arxiv",
            ))
        return results

    def _deduplicate(self, results: list[SearchResult]) -> list[SearchResult]:
        """Deduplicate results by DOI or arXiv ID, preferring Semantic Scholar."""
        seen_dois: set[str] = set()
        seen_arxiv: set[str] = set()
        unique = []

        # Sort so semantic_scholar comes first
        sorted_results = sorted(results, key=lambda r: 0 if r.source == "semantic_scholar" else 1)

        for result in sorted_results:
            if result.doi and result.doi in seen_dois:
                continue
            if result.arxiv_id and result.arxiv_id in seen_arxiv:
                continue
            if result.doi:
                seen_dois.add(result.doi)
            if result.arxiv_id:
                seen_arxiv.add(result.arxiv_id)
            unique.append(result)
        return unique

    def _extract_meta(self, html: str, name: str) -> str | None:
        """Extract a single meta tag value from HTML."""
        pattern = rf'<meta\s+(?:name|property)="{re.escape(name)}"\s+content="([^"]*)"'
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        # Try reversed order (content before name)
        pattern = rf'<meta\s+content="([^"]*)"\s+(?:name|property)="{re.escape(name)}"'
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return None

    def _extract_meta_all(self, html: str, name: str) -> list[str]:
        """Extract all matching meta tag values from HTML."""
        results = []
        for pattern in [
            rf'<meta\s+(?:name|property)="{re.escape(name)}"\s+content="([^"]*)"',
            rf'<meta\s+content="([^"]*)"\s+(?:name|property)="{re.escape(name)}"',
        ]:
            for match in re.finditer(pattern, html, re.IGNORECASE):
                value = match.group(1).strip()
                if value and value not in results:
                    results.append(value)
        return results
