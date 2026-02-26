"""Tests for the PDF download pipeline, including SSRN, Cloudflare detection, source ordering, OpenAlex, and content verification."""

from pathlib import Path

import pytest
import httpx
import pymupdf
import respx

from papertrail.config import PapertrailConfig
from papertrail.converter import verify_pdf_content
from papertrail.metadata import (
    MetadataFetcher,
    DownloadResult,
    BROWSER_HEADERS,
    CLOUDFLARE_MARKERS,
    _RateLimiter,
)
from papertrail.models import PaperMetadata, SearchResult


CLOUDFLARE_CHALLENGE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Just a moment...</title></head>
<body>
<div id="challenge-running">
    <div class="cf-browser-verification">
        Checking your browser before accessing papers.ssrn.com.
    </div>
</div>
</body>
</html>
"""

SSRN_ABSTRACT_HTML = """
<html>
<head>
<meta name="citation_title" content="Technological Innovation and Growth">
<meta name="citation_author" content="Kogan, Leonid">
<meta name="citation_author" content="Papanikolaou, Dimitris">
<meta name="citation_pdf_url" content="https://papers.ssrn.com/sol3/Delivery.cfm?abstractid=1234567">
<meta name="citation_publication_date" content="2017/01/15">
<meta name="description" content="We study the effect of technological innovation on growth.">
</head>
<body></body>
</html>
"""

FAKE_PDF_CONTENT = b"%PDF-1.4 fake pdf content for testing"


def _create_test_pdf(path, text_content):
    """Create a minimal PDF with the given text on the first page."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text_content, fontsize=11)
    doc.save(str(path))
    doc.close()


@pytest.fixture
def config():
    return PapertrailConfig(unpaywall_email="test@test.com")


@pytest.fixture
def fetcher(config):
    f = MetadataFetcher(config)
    # Disable curl_cffi so respx can mock the httpx download client
    f._curl_session = None
    return f


class TestCloudflareDetection:
    def test_detects_cloudflare_just_a_moment(self, fetcher):
        response = httpx.Response(
            200,
            text=CLOUDFLARE_CHALLENGE_HTML,
            headers={"content-type": "text/html"},
        )
        assert fetcher._is_cloudflare_challenge(response) is True

    def test_detects_cloudflare_checking_browser(self, fetcher):
        html = "<html><head><title>Checking your browser</title></head><body>Checking your browser before accessing the site.</body></html>"
        response = httpx.Response(
            200, text=html, headers={"content-type": "text/html"}
        )
        assert fetcher._is_cloudflare_challenge(response) is True

    def test_does_not_flag_normal_html(self, fetcher):
        html = "<html><head><title>Paper Abstract</title></head><body>This is a normal page.</body></html>"
        response = httpx.Response(
            200, text=html, headers={"content-type": "text/html"}
        )
        assert fetcher._is_cloudflare_challenge(response) is False

    def test_does_not_flag_pdf_content(self, fetcher):
        response = httpx.Response(
            200,
            content=FAKE_PDF_CONTENT,
            headers={"content-type": "application/pdf"},
        )
        assert fetcher._is_cloudflare_challenge(response) is False

    def test_detects_cf_chl_marker(self, fetcher):
        html = '<html><body><script src="/_cf_chl/scripts/challenge.js"></script></body></html>'
        response = httpx.Response(
            200, text=html, headers={"content-type": "text/html; charset=UTF-8"}
        )
        assert fetcher._is_cloudflare_challenge(response) is True


class TestCandidateUrls:
    def test_arxiv_paper_urls(self, fetcher):
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            arxiv_id="2301.12345",
        )
        urls = fetcher.get_candidate_urls(result)
        assert "https://arxiv.org/pdf/2301.12345" in urls

    def test_arxiv_strips_version(self, fetcher):
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            arxiv_id="2301.12345v3",
        )
        urls = fetcher.get_candidate_urls(result)
        assert "https://arxiv.org/pdf/2301.12345" in urls

    def test_ssrn_paper_urls(self, fetcher):
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            ssrn_id="1234567",
        )
        urls = fetcher.get_candidate_urls(result)
        ssrn_urls = [u for u in urls if "ssrn.com" in u]
        assert len(ssrn_urls) == 1
        assert "1234567" in ssrn_urls[0]

    def test_doi_paper_urls(self, fetcher):
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            doi="10.1257/aer.123",
        )
        urls = fetcher.get_candidate_urls(result)
        assert "https://doi.org/10.1257/aer.123" in urls

    def test_nber_paper_detected_from_url(self, fetcher):
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            url="https://www.nber.org/papers/w25232",
        )
        urls = fetcher.get_candidate_urls(result)
        nber_urls = [u for u in urls if "nber.org/system/files" in u]
        assert len(nber_urls) == 1
        assert "w25232" in nber_urls[0]

    def test_nber_detected_from_open_access_url(self, fetcher):
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            open_access_pdf_url="https://www.nber.org/system/files/working_papers/w25232/w25232.pdf",
        )
        urls = fetcher.get_candidate_urls(result)
        nber_urls = [u for u in urls if "nber.org/system/files" in u]
        assert len(nber_urls) >= 1

    def test_arxiv_url_first(self, fetcher):
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            open_access_pdf_url="https://example.com/paper.pdf",
            doi="10.1257/aer.123",
            arxiv_id="2301.12345",
        )
        urls = fetcher.get_candidate_urls(result)
        assert urls[0] == "https://arxiv.org/pdf/2301.12345"

    def test_no_duplicate_urls(self, fetcher):
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            url="https://doi.org/10.1257/aer.123",
            doi="10.1257/aer.123",
        )
        urls = fetcher.get_candidate_urls(result)
        # The direct URL should not be duplicated if it matches the DOI URL
        doi_urls = [u for u in urls if "doi.org" in u]
        assert len(doi_urls) == 1


class TestDownloadPdf:
    @pytest.mark.asyncio
    async def test_successful_pdf_download(self, fetcher, tmp_path):
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            open_access_pdf_url="https://example.com/paper.pdf",
        )
        dest = tmp_path / "paper.pdf"
        source_pdf = tmp_path / "source.pdf"
        _create_test_pdf(source_pdf, "Test Paper\nSmith\nThis is a research paper about testing with enough content to pass verification checks.")
        pdf_bytes = source_pdf.read_bytes()

        with respx.mock:
            respx.get("https://example.com/paper.pdf").mock(
                return_value=httpx.Response(
                    200,
                    content=pdf_bytes,
                    headers={"content-type": "application/pdf"},
                )
            )
            dl = await fetcher.download_pdf(result, dest)

        assert dl.success is True
        assert dl.pdf_path == dest
        assert dest.read_bytes() == pdf_bytes

    @pytest.mark.asyncio
    async def test_detects_pdf_by_magic_bytes(self, fetcher, tmp_path):
        """Some servers return PDFs with wrong content-type."""
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            open_access_pdf_url="https://example.com/paper",
        )
        dest = tmp_path / "paper.pdf"
        source_pdf = tmp_path / "source.pdf"
        _create_test_pdf(source_pdf, "Test Paper\nSmith\nThis is a research paper about testing with enough content to pass verification checks.")
        pdf_bytes = source_pdf.read_bytes()

        with respx.mock:
            respx.get("https://example.com/paper").mock(
                return_value=httpx.Response(
                    200,
                    content=pdf_bytes,
                    headers={"content-type": "application/octet-stream"},
                )
            )
            dl = await fetcher.download_pdf(result, dest)

        assert dl.success is True

    @pytest.mark.asyncio
    async def test_cloudflare_blocked_ssrn(self, fetcher, tmp_path):
        """SSRN returns a Cloudflare challenge instead of the PDF."""
        result = SearchResult(
            title="Test",
            authors=["Smith"],
            ssrn_id="1234567",
        )
        dest = tmp_path / "paper.pdf"

        with respx.mock:
            respx.get("https://papers.ssrn.com/sol3/Delivery.cfm?abstractid=1234567").mock(
                return_value=httpx.Response(
                    200,
                    text=CLOUDFLARE_CHALLENGE_HTML,
                    headers={"content-type": "text/html; charset=UTF-8"},
                )
            )
            # Unpaywall returns nothing
            respx.get("https://api.unpaywall.org/v2/").mock(
                return_value=httpx.Response(404)
            )
            # PMC returns nothing
            respx.get("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/").mock(
                return_value=httpx.Response(200, json={"records": []})
            )

            dl = await fetcher.download_pdf(result, dest)

        assert dl.success is False
        ssrn_attempts = [a for a in dl.attempts if "ssrn.com" in a.url]
        assert len(ssrn_attempts) == 1
        assert ssrn_attempts[0].cloudflare_blocked is True
        assert len(dl.candidate_urls) > 0

    @pytest.mark.asyncio
    async def test_falls_through_to_next_source(self, fetcher, tmp_path):
        """If first source fails, tries the next one."""
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            open_access_pdf_url="https://example.com/good.pdf",
            url="https://example.com/broken.pdf",
        )
        dest = tmp_path / "paper.pdf"
        source_pdf = tmp_path / "source.pdf"
        _create_test_pdf(source_pdf, "Test Paper\nSmith\nThis is a research paper about testing with enough content to pass verification checks.")
        pdf_bytes = source_pdf.read_bytes()

        with respx.mock:
            # Direct URL tried first (position 3), fails
            respx.get("https://example.com/broken.pdf").mock(
                return_value=httpx.Response(404)
            )
            # Semantic Scholar OA tried next (position 4), succeeds
            respx.get("https://example.com/good.pdf").mock(
                return_value=httpx.Response(
                    200,
                    content=pdf_bytes,
                    headers={"content-type": "application/pdf"},
                )
            )

            dl = await fetcher.download_pdf(result, dest)

        assert dl.success is True
        assert len(dl.attempts) == 2
        assert dl.attempts[0].status_code == 404
        assert dl.attempts[1].status_code == 200

    @pytest.mark.asyncio
    async def test_all_sources_fail(self, fetcher, tmp_path):
        """When all download sources fail, returns structured failure info."""
        result = SearchResult(
            title="Test",
            authors=["Smith"],
            doi="10.1234/fake",
        )
        dest = tmp_path / "paper.pdf"

        with respx.mock:
            # OpenAlex: no results
            respx.get(url__regex=r".*openalex\.org.*").mock(
                return_value=httpx.Response(200, json={
                    "open_access": {}, "locations": []
                })
            )
            # Unpaywall: no results
            respx.get("https://api.unpaywall.org/v2/10.1234/fake").mock(
                return_value=httpx.Response(200, json={"best_oa_location": None, "oa_locations": []})
            )
            # PMC: no results
            respx.get("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/").mock(
                return_value=httpx.Response(200, json={"records": []})
            )
            # DOI redirect: returns HTML
            respx.get("https://doi.org/10.1234/fake").mock(
                return_value=httpx.Response(
                    200,
                    text="<html>Publisher landing page</html>",
                    headers={"content-type": "text/html"},
                )
            )

            dl = await fetcher.download_pdf(result, dest)

        assert dl.success is False
        assert not dest.exists()
        assert len(dl.attempts) > 0

    @pytest.mark.asyncio
    async def test_ssrn_success_when_not_blocked(self, fetcher, tmp_path):
        """SSRN download works when Cloudflare is not active."""
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            ssrn_id="9999999",
        )
        dest = tmp_path / "paper.pdf"
        source_pdf = tmp_path / "source.pdf"
        _create_test_pdf(source_pdf, "Test Paper\nSmith\nThis is a research paper about testing with enough content to pass verification checks.")
        pdf_bytes = source_pdf.read_bytes()

        with respx.mock:
            respx.get("https://papers.ssrn.com/sol3/Delivery.cfm?abstractid=9999999").mock(
                return_value=httpx.Response(
                    200,
                    content=pdf_bytes,
                    headers={"content-type": "application/pdf"},
                )
            )

            dl = await fetcher.download_pdf(result, dest)

        assert dl.success is True

    @pytest.mark.asyncio
    async def test_nber_fallback(self, fetcher, tmp_path):
        """NBER URL is tried when paper URL indicates an NBER working paper."""
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            url="https://www.nber.org/papers/w25232",
        )
        dest = tmp_path / "paper.pdf"
        source_pdf = tmp_path / "source.pdf"
        _create_test_pdf(source_pdf, "Test Paper\nSmith\nThis is a research paper about testing with enough content to pass verification checks.")
        pdf_bytes = source_pdf.read_bytes()

        with respx.mock:
            # The NBER page itself returns HTML
            respx.get("https://www.nber.org/papers/w25232").mock(
                return_value=httpx.Response(
                    200,
                    text="<html>NBER landing page</html>",
                    headers={"content-type": "text/html"},
                )
            )
            # But the direct PDF URL works
            respx.get("https://www.nber.org/system/files/working_papers/w25232/w25232.pdf").mock(
                return_value=httpx.Response(
                    200,
                    content=pdf_bytes,
                    headers={"content-type": "application/pdf"},
                )
            )

            dl = await fetcher.download_pdf(result, dest)

        assert dl.success is True

    @pytest.mark.asyncio
    async def test_unpaywall_source(self, fetcher, tmp_path):
        """Unpaywall provides a working PDF URL."""
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            doi="10.1234/test",
        )
        dest = tmp_path / "paper.pdf"
        source_pdf = tmp_path / "source.pdf"
        _create_test_pdf(source_pdf, "Test Paper\nSmith\nThis is a research paper about testing with enough content to pass verification checks.")
        pdf_bytes = source_pdf.read_bytes()

        with respx.mock:
            # OpenAlex: no results
            respx.get(url__regex=r".*openalex\.org.*").mock(
                return_value=httpx.Response(200, json={
                    "open_access": {}, "locations": []
                })
            )
            respx.get("https://api.unpaywall.org/v2/10.1234/test").mock(
                return_value=httpx.Response(200, json={
                    "best_oa_location": {
                        "url_for_pdf": "https://repository.edu/paper.pdf",
                    },
                    "oa_locations": [],
                })
            )
            # PMC: no results
            respx.get("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/").mock(
                return_value=httpx.Response(200, json={"records": []})
            )
            respx.get("https://repository.edu/paper.pdf").mock(
                return_value=httpx.Response(
                    200,
                    content=pdf_bytes,
                    headers={"content-type": "application/pdf"},
                )
            )
            # DOI would be tried after but we succeed before
            respx.get("https://doi.org/10.1234/test").mock(
                return_value=httpx.Response(200, text="<html>page</html>",
                                           headers={"content-type": "text/html"})
            )

            dl = await fetcher.download_pdf(result, dest)

        assert dl.success is True
        unpaywall_attempt = [a for a in dl.attempts if "repository.edu" in a.url]
        assert len(unpaywall_attempt) == 1


class TestNberDetection:
    def test_nber_url_with_papers_path(self, fetcher):
        result = SearchResult(
            title="Test", authors=["Smith"],
            url="https://www.nber.org/papers/w25232",
        )
        url = fetcher._get_nber_pdf_url(result)
        assert url == "https://www.nber.org/system/files/working_papers/w25232/w25232.pdf"

    def test_nber_url_from_open_access(self, fetcher):
        result = SearchResult(
            title="Test", authors=["Smith"],
            open_access_pdf_url="https://www.nber.org/system/files/working_papers/w30400/w30400.pdf",
        )
        url = fetcher._get_nber_pdf_url(result)
        assert url is not None
        assert "w30400" in url

    def test_non_nber_url_returns_none(self, fetcher):
        result = SearchResult(
            title="Test", authors=["Smith"],
            url="https://doi.org/10.1257/aer.123",
        )
        url = fetcher._get_nber_pdf_url(result)
        assert url is None

    def test_no_url_returns_none(self, fetcher):
        result = SearchResult(title="Test", authors=["Smith"])
        url = fetcher._get_nber_pdf_url(result)
        assert url is None


class TestBrowserHeaders:
    def test_download_client_has_user_agent(self, fetcher):
        user_agent = fetcher.download_client.headers.get("user-agent")
        assert user_agent is not None
        assert "Mozilla" in user_agent

    def test_api_client_does_not_have_browser_headers(self, fetcher):
        user_agent = fetcher.client.headers.get("user-agent")
        # httpx sets a default user-agent, but it should not be the browser one
        if user_agent:
            assert "Mozilla" not in user_agent


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_rate_limiter_enforces_delay(self):
        import time
        limiter = _RateLimiter(min_interval=0.1)
        start = time.monotonic()
        await limiter.acquire()
        await limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.09  # Allow small timing margin

    @pytest.mark.asyncio
    async def test_rate_limiter_no_delay_on_first_call(self):
        import time
        limiter = _RateLimiter(min_interval=1.0)
        start = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1


class TestSsrnMetadataScraping:
    @pytest.mark.asyncio
    async def test_parses_ssrn_abstract_page(self, fetcher):
        with respx.mock:
            respx.get("https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1234567").mock(
                return_value=httpx.Response(200, text=SSRN_ABSTRACT_HTML, headers={"content-type": "text/html"})
            )
            result = await fetcher.get_ssrn_metadata("1234567")

        assert result is not None
        assert result.title == "Technological Innovation and Growth"
        assert "Kogan, Leonid" in result.authors
        assert "Papanikolaou, Dimitris" in result.authors
        assert result.year == 2017
        assert result.ssrn_id == "1234567"

    @pytest.mark.asyncio
    async def test_ssrn_cloudflare_returns_none(self, fetcher):
        with respx.mock:
            respx.get("https://papers.ssrn.com/sol3/papers.cfm?abstract_id=9999999").mock(
                return_value=httpx.Response(200, text=CLOUDFLARE_CHALLENGE_HTML, headers={"content-type": "text/html"})
            )
            result = await fetcher.get_ssrn_metadata("9999999")

        # Cloudflare page has no citation_title, so returns None
        assert result is None

    @pytest.mark.asyncio
    async def test_ssrn_404_returns_none(self, fetcher):
        with respx.mock:
            respx.get("https://papers.ssrn.com/sol3/papers.cfm?abstract_id=0000000").mock(
                return_value=httpx.Response(404)
            )
            result = await fetcher.get_ssrn_metadata("0000000")

        assert result is None


class TestPmcLookup:
    @pytest.mark.asyncio
    async def test_finds_pmc_article(self, fetcher):
        with respx.mock:
            respx.get("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/").mock(
                return_value=httpx.Response(200, json={
                    "records": [{"pmcid": "PMC7654321", "doi": "10.1234/test"}]
                })
            )
            url = await fetcher._get_pmc_pdf_url("10.1234/test")

        assert url == "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7654321/pdf/"

    @pytest.mark.asyncio
    async def test_no_pmc_article(self, fetcher):
        with respx.mock:
            respx.get("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/").mock(
                return_value=httpx.Response(200, json={"records": [{"doi": "10.1234/test"}]})
            )
            url = await fetcher._get_pmc_pdf_url("10.1234/test")

        assert url is None

    @pytest.mark.asyncio
    async def test_pmc_api_error(self, fetcher):
        with respx.mock:
            respx.get("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/").mock(
                return_value=httpx.Response(500)
            )
            url = await fetcher._get_pmc_pdf_url("10.1234/test")

        assert url is None


CROSSREF_SSRN_RESPONSE = {
    "status": "ok",
    "message": {
        "DOI": "10.2139/ssrn.4631010",
        "title": ["Technology and Labor Displacement: Evidence from Linking Patents with Worker-Level Data"],
        "author": [
            {"given": "Leonid", "family": "Kogan"},
            {"given": "Dimitris", "family": "Papanikolaou"},
            {"given": "Lawrence", "family": "Schmidt"},
            {"given": "Bryan", "family": "Seegmiller"},
        ],
        "published-online": {"date-parts": [[2023, 11, 1]]},
        "abstract": "<jats:p>We examine the impact of technological innovation on workers.</jats:p>",
        "URL": "https://doi.org/10.2139/ssrn.4631010",
        "link": [],
    },
}

CROSSREF_QJE_RESPONSE = {
    "status": "ok",
    "message": {
        "DOI": "10.1093/qje/qjw040",
        "title": ["Technological Innovation, Resource Allocation, and Growth"],
        "author": [
            {"given": "Leonid", "family": "Kogan"},
            {"given": "Dimitris", "family": "Papanikolaou"},
            {"given": "Amit", "family": "Seru"},
            {"given": "Noah", "family": "Stoffman"},
        ],
        "published-print": {"date-parts": [[2017, 5]]},
        "abstract": "We study how innovation affects growth.",
        "URL": "https://doi.org/10.1093/qje/qjw040",
        "link": [
            {
                "URL": "https://academic.oup.com/qje/article-pdf/132/2/665/qjw040.pdf",
                "content-type": "application/pdf",
            }
        ],
    },
}


class TestCrossRefLookup:
    @pytest.mark.asyncio
    async def test_parses_ssrn_paper(self, fetcher):
        with respx.mock:
            respx.get("https://api.crossref.org/works/10.2139/ssrn.4631010").mock(
                return_value=httpx.Response(200, json=CROSSREF_SSRN_RESPONSE)
            )
            result = await fetcher.get_crossref_metadata("10.2139/ssrn.4631010")

        assert result is not None
        assert result.title == "Technology and Labor Displacement: Evidence from Linking Patents with Worker-Level Data"
        assert len(result.authors) == 4
        assert result.authors[0] == "Leonid Kogan"
        assert result.year == 2023
        assert result.doi == "10.2139/ssrn.4631010"
        assert result.ssrn_id == "4631010"
        assert result.source == "crossref"

    @pytest.mark.asyncio
    async def test_parses_published_paper(self, fetcher):
        with respx.mock:
            respx.get("https://api.crossref.org/works/10.1093/qje/qjw040").mock(
                return_value=httpx.Response(200, json=CROSSREF_QJE_RESPONSE)
            )
            result = await fetcher.get_crossref_metadata("10.1093/qje/qjw040")

        assert result is not None
        assert "Resource Allocation" in result.title
        assert result.year == 2017
        assert result.ssrn_id is None
        assert result.open_access_pdf_url is not None
        assert "oup.com" in result.open_access_pdf_url

    @pytest.mark.asyncio
    async def test_strips_html_from_abstract(self, fetcher):
        with respx.mock:
            respx.get("https://api.crossref.org/works/10.2139/ssrn.4631010").mock(
                return_value=httpx.Response(200, json=CROSSREF_SSRN_RESPONSE)
            )
            result = await fetcher.get_crossref_metadata("10.2139/ssrn.4631010")

        assert result is not None
        assert "<jats:p>" not in result.abstract
        assert "We examine" in result.abstract

    @pytest.mark.asyncio
    async def test_crossref_404_returns_none(self, fetcher):
        with respx.mock:
            respx.get("https://api.crossref.org/works/10.9999/fake").mock(
                return_value=httpx.Response(404)
            )
            result = await fetcher.get_crossref_metadata("10.9999/fake")

        assert result is None

    @pytest.mark.asyncio
    async def test_crossref_network_error(self, fetcher):
        with respx.mock:
            respx.get("https://api.crossref.org/works/10.9999/fake").mock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            result = await fetcher.get_crossref_metadata("10.9999/fake")

        assert result is None


class TestExtractDoiFromIdentifier:
    def test_bare_doi(self, fetcher):
        assert fetcher._extract_doi_from_identifier("10.1093/qje/qjw040") == "10.1093/qje/qjw040"

    def test_doi_prefix(self, fetcher):
        assert fetcher._extract_doi_from_identifier("DOI:10.1093/qje/qjw040") == "10.1093/qje/qjw040"

    def test_ssrn_url(self, fetcher):
        doi = fetcher._extract_doi_from_identifier("https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4631010")
        assert doi == "10.2139/ssrn.4631010"

    def test_bare_ssrn_id(self, fetcher):
        doi = fetcher._extract_doi_from_identifier("4631010")
        assert doi == "10.2139/ssrn.4631010"

    def test_arxiv_id_returns_none(self, fetcher):
        assert fetcher._extract_doi_from_identifier("2301.12345") is None

    def test_random_text_returns_none(self, fetcher):
        assert fetcher._extract_doi_from_identifier("some paper title") is None


class TestGetByIdentifierWithCrossRefFallback:
    @pytest.mark.asyncio
    async def test_falls_back_to_crossref_for_ssrn_url(self, fetcher):
        """When Semantic Scholar doesn't have an SSRN paper, CrossRef is tried."""
        with respx.mock:
            respx.get(url__regex=r".*semanticscholar\.org.*").mock(
                return_value=httpx.Response(404)
            )
            respx.get("https://api.crossref.org/works/10.2139/ssrn.4631010").mock(
                return_value=httpx.Response(200, json=CROSSREF_SSRN_RESPONSE)
            )
            result = await fetcher.get_by_identifier("https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4631010")

        assert result is not None
        assert result.title == "Technology and Labor Displacement: Evidence from Linking Patents with Worker-Level Data"
        assert result.source == "crossref"

    @pytest.mark.asyncio
    async def test_falls_back_to_crossref_for_bare_ssrn_id(self, fetcher):
        with respx.mock:
            respx.get(url__regex=r".*semanticscholar\.org.*").mock(
                return_value=httpx.Response(404)
            )
            respx.get("https://api.crossref.org/works/10.2139/ssrn.4631010").mock(
                return_value=httpx.Response(200, json=CROSSREF_SSRN_RESPONSE)
            )
            result = await fetcher.get_by_identifier("4631010")

        assert result is not None
        assert result.ssrn_id == "4631010"

    @pytest.mark.asyncio
    async def test_falls_back_to_crossref_for_doi(self, fetcher):
        with respx.mock:
            respx.get(url__regex=r".*semanticscholar\.org.*").mock(
                return_value=httpx.Response(404)
            )
            respx.get("https://api.crossref.org/works/10.1093/qje/qjw040").mock(
                return_value=httpx.Response(200, json=CROSSREF_QJE_RESPONSE)
            )
            result = await fetcher.get_by_identifier("10.1093/qje/qjw040")

        assert result is not None
        assert result.year == 2017

    @pytest.mark.asyncio
    async def test_semantic_scholar_success_skips_crossref(self, fetcher):
        """When Semantic Scholar has the paper, CrossRef is not called."""
        ss_response = {
            "paperId": "abc123",
            "title": "Test Paper",
            "authors": [{"name": "John Smith"}],
            "year": 2023,
            "abstract": None,
            "externalIds": {"DOI": "10.1234/test"},
            "citationCount": 10,
            "fieldsOfStudy": [],
            "s2FieldsOfStudy": [],
            "openAccessPdf": None,
            "venue": None,
            "journal": None,
            "publicationDate": None,
        }
        with respx.mock:
            respx.get(url__regex=r".*semanticscholar\.org.*").mock(
                return_value=httpx.Response(200, json=ss_response)
            )
            crossref_route = respx.get(url__regex=r".*crossref\.org.*").mock(
                return_value=httpx.Response(200, json=CROSSREF_QJE_RESPONSE)
            )
            result = await fetcher.get_by_identifier("10.1234/test")

        assert result is not None
        assert result.source == "semantic_scholar"
        assert not crossref_route.called


class TestOpenAlexLookup:
    @pytest.mark.asyncio
    async def test_finds_pdf_urls(self, fetcher):
        with respx.mock:
            respx.get(url__regex=r".*openalex\.org.*").mock(
                return_value=httpx.Response(200, json={
                    "open_access": {"oa_url": "https://repository.edu/paper.pdf"},
                    "locations": [
                        {"pdf_url": "https://repository.edu/paper.pdf"},
                        {"pdf_url": "https://other.edu/preprint.pdf"},
                    ],
                })
            )
            urls = await fetcher._get_openalex_pdf_urls("10.1234/test")

        assert len(urls) == 2
        assert "repository.edu" in urls[0]
        assert "other.edu" in urls[1]

    @pytest.mark.asyncio
    async def test_returns_empty_on_404(self, fetcher):
        with respx.mock:
            respx.get(url__regex=r".*openalex\.org.*").mock(
                return_value=httpx.Response(404)
            )
            urls = await fetcher._get_openalex_pdf_urls("10.9999/fake")

        assert urls == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_network_error(self, fetcher):
        with respx.mock:
            respx.get(url__regex=r".*openalex\.org.*").mock(
                side_effect=httpx.ConnectError("timeout")
            )
            urls = await fetcher._get_openalex_pdf_urls("10.1234/test")

        assert urls == []

    @pytest.mark.asyncio
    async def test_deduplicates_urls(self, fetcher):
        with respx.mock:
            respx.get(url__regex=r".*openalex\.org.*").mock(
                return_value=httpx.Response(200, json={
                    "open_access": {"oa_url": "https://repository.edu/paper.pdf"},
                    "locations": [
                        {"pdf_url": "https://repository.edu/paper.pdf"},
                        {"pdf_url": "https://repository.edu/paper.pdf"},
                    ],
                })
            )
            urls = await fetcher._get_openalex_pdf_urls("10.1234/test")

        assert len(urls) == 1

    @pytest.mark.asyncio
    async def test_handles_missing_fields(self, fetcher):
        with respx.mock:
            respx.get(url__regex=r".*openalex\.org.*").mock(
                return_value=httpx.Response(200, json={
                    "open_access": {},
                    "locations": [{"pdf_url": None}, {}],
                })
            )
            urls = await fetcher._get_openalex_pdf_urls("10.1234/test")

        assert urls == []


class TestOpenAlexInDownload:
    @pytest.mark.asyncio
    async def test_openalex_url_tried_in_download(self, fetcher, tmp_path):
        """OpenAlex URLs are tried when other sources fail."""
        result = SearchResult(
            title="Test Paper",
            authors=["Smith"],
            doi="10.1234/test",
        )
        dest = tmp_path / "paper.pdf"
        pdf_path = tmp_path / "source.pdf"
        _create_test_pdf(pdf_path, "Test Paper\nSmith\nSome content here to fill the page")
        pdf_bytes = pdf_path.read_bytes()

        with respx.mock:
            respx.get(url__regex=r".*openalex\.org.*").mock(
                return_value=httpx.Response(200, json={
                    "open_access": {},
                    "locations": [
                        {"pdf_url": "https://repository.edu/paper.pdf"},
                    ],
                })
            )
            respx.get("https://repository.edu/paper.pdf").mock(
                return_value=httpx.Response(
                    200, content=pdf_bytes,
                    headers={"content-type": "application/pdf"},
                )
            )
            respx.get("https://api.unpaywall.org/v2/10.1234/test").mock(
                return_value=httpx.Response(200, json={"best_oa_location": None, "oa_locations": []})
            )
            respx.get("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/").mock(
                return_value=httpx.Response(200, json={"records": []})
            )
            respx.get("https://doi.org/10.1234/test").mock(
                return_value=httpx.Response(200, text="<html>page</html>",
                                           headers={"content-type": "text/html"})
            )

            dl = await fetcher.download_pdf(result, dest)

        assert dl.success is True
        repo_attempts = [a for a in dl.attempts if "repository.edu" in a.url]
        assert len(repo_attempts) == 1


class TestVerifyPdfContent:
    def test_matching_paper(self, tmp_path):
        pdf_path = tmp_path / "paper.pdf"
        _create_test_pdf(pdf_path, "Causal Inference in Economics\nJohn Smith\nAbstract: We study causal methods.")
        result = verify_pdf_content(pdf_path, "Causal Inference in Economics", ["John Smith"])
        assert result["verified"] is True
        assert result["title_similarity"] > 0

    def test_wrong_paper(self, tmp_path):
        pdf_path = tmp_path / "paper.pdf"
        _create_test_pdf(pdf_path, "Machine Learning for Weather Prediction\nJane Doe\nAbstract: We forecast weather.")
        result = verify_pdf_content(pdf_path, "Causal Inference in Economics", ["John Smith"])
        assert result["verified"] is False
        assert result["reason"] != ""

    def test_empty_pdf(self, tmp_path):
        pdf_path = tmp_path / "paper.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(pdf_path))
        doc.close()
        result = verify_pdf_content(pdf_path, "Test Paper", ["Smith"])
        assert result["verified"] is False
        assert "too little text" in result["reason"]

    def test_corrupt_file(self, tmp_path):
        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"not a pdf at all")
        result = verify_pdf_content(pdf_path, "Test Paper", ["Smith"])
        assert result["verified"] is False
        assert "Failed to read" in result["reason"]

    def test_title_with_author_match(self, tmp_path):
        pdf_path = tmp_path / "paper.pdf"
        _create_test_pdf(
            pdf_path,
            "The Macroeconomic Announcement Premium\nHengjie Ai, Ravi Bansal\nWe study announcement returns."
        )
        result = verify_pdf_content(
            pdf_path,
            "Macroeconomic Announcement Premium",
            ["Hengjie Ai", "Ravi Bansal"],
        )
        assert result["verified"] is True

    def test_no_authors_but_strong_title_match(self, tmp_path):
        pdf_path = tmp_path / "paper.pdf"
        _create_test_pdf(
            pdf_path,
            "Causal Inference in Modern Economics\nAnonymous submission\nWe study causal inference in economics using novel approaches."
        )
        result = verify_pdf_content(
            pdf_path,
            "Causal Inference in Modern Economics",
            ["Unknown Author"],
        )
        assert result["verified"] is True


class TestVerificationInDownload:
    @pytest.mark.asyncio
    async def test_rejects_wrong_pdf_and_continues(self, fetcher, tmp_path):
        """If downloaded PDF doesn't match metadata, it's rejected and next source tried."""
        result = SearchResult(
            title="Causal Inference in Economics",
            authors=["John Smith"],
            url="https://example.com/wrong.pdf",
            open_access_pdf_url="https://example.com/correct.pdf",
        )
        dest = tmp_path / "paper.pdf"

        wrong_pdf_path = tmp_path / "wrong.pdf"
        _create_test_pdf(wrong_pdf_path, "Machine Learning for Weather\nJane Doe\nCompletely different paper.")
        wrong_pdf_bytes = wrong_pdf_path.read_bytes()

        correct_pdf_path = tmp_path / "correct.pdf"
        _create_test_pdf(correct_pdf_path, "Causal Inference in Economics\nJohn Smith\nThis is the right paper.")
        correct_pdf_bytes = correct_pdf_path.read_bytes()

        with respx.mock:
            # Direct URL (position 3) returns wrong paper
            respx.get("https://example.com/wrong.pdf").mock(
                return_value=httpx.Response(
                    200, content=wrong_pdf_bytes,
                    headers={"content-type": "application/pdf"},
                )
            )
            # Semantic Scholar OA (position 4) returns correct paper
            respx.get("https://example.com/correct.pdf").mock(
                return_value=httpx.Response(
                    200, content=correct_pdf_bytes,
                    headers={"content-type": "application/pdf"},
                )
            )

            dl = await fetcher.download_pdf(result, dest)

        assert dl.success is True
        assert len(dl.attempts) == 2
        assert "Content mismatch" in dl.attempts[0].error
        assert dl.attempts[1].error is None

    @pytest.mark.asyncio
    async def test_verification_skipped_when_disabled(self, fetcher, tmp_path):
        """verify=False skips content checking."""
        result = SearchResult(
            title="Causal Inference in Economics",
            authors=["John Smith"],
            open_access_pdf_url="https://example.com/paper.pdf",
        )
        dest = tmp_path / "paper.pdf"

        wrong_pdf_path = tmp_path / "wrong.pdf"
        _create_test_pdf(wrong_pdf_path, "Completely Different Paper\nJane Doe")
        wrong_pdf_bytes = wrong_pdf_path.read_bytes()

        with respx.mock:
            respx.get("https://example.com/paper.pdf").mock(
                return_value=httpx.Response(
                    200, content=wrong_pdf_bytes,
                    headers={"content-type": "application/pdf"},
                )
            )

            dl = await fetcher.download_pdf(result, dest, verify=False)

        assert dl.success is True


class TestSearchResultFromMetadata:
    def test_round_trip_fields(self):
        paper = PaperMetadata(
            bibtex_key="smith_2024_causal",
            title="Causal Inference in Economics",
            authors=["John Smith", "Jane Doe"],
            year=2024,
            abstract="We study causal methods.",
            doi="10.1234/test",
            arxiv_id="2401.12345",
            ssrn_id="4567890",
            url="https://example.com/paper",
        )
        sr = SearchResult.from_metadata(paper)
        assert sr.title == paper.title
        assert sr.authors == paper.authors
        assert sr.year == paper.year
        assert sr.abstract == paper.abstract
        assert sr.doi == paper.doi
        assert sr.arxiv_id == paper.arxiv_id
        assert sr.ssrn_id == paper.ssrn_id
        assert sr.url == paper.url

    def test_minimal_metadata(self):
        paper = PaperMetadata(
            bibtex_key="unknown_0_paper",
            title="Untitled",
            authors=[],
        )
        sr = SearchResult.from_metadata(paper)
        assert sr.title == "Untitled"
        assert sr.authors == []
        assert sr.doi is None
        assert sr.arxiv_id is None

    def test_produces_valid_candidate_urls(self, fetcher):
        paper = PaperMetadata(
            bibtex_key="smith_2024_causal",
            title="Causal Inference",
            authors=["John Smith"],
            arxiv_id="2401.12345",
            doi="10.1234/test",
        )
        sr = SearchResult.from_metadata(paper)
        urls = fetcher.get_candidate_urls(sr)
        assert "https://arxiv.org/pdf/2401.12345" in urls
        assert "https://doi.org/10.1234/test" in urls
