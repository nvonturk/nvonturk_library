from pydantic import BaseModel


class SearchResult(BaseModel):
    """Result from Semantic Scholar, arXiv, or SSRN search."""
    title: str
    authors: list[str]
    year: int | None = None
    abstract: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    ssrn_id: str | None = None
    url: str | None = None
    citation_count: int | None = None
    topics: list[str] = []
    fields_of_study: list[str] = []
    open_access_pdf_url: str | None = None
    source: str = "semantic_scholar"

    @classmethod
    def from_metadata(cls, paper: "PaperMetadata") -> "SearchResult":
        """Reconstruct a SearchResult from stored PaperMetadata."""
        return cls(
            title=paper.title,
            authors=paper.authors,
            year=paper.year,
            abstract=paper.abstract,
            doi=paper.doi,
            arxiv_id=paper.arxiv_id,
            ssrn_id=paper.ssrn_id,
            url=paper.url,
        )


class PaperMetadata(BaseModel):
    """Full metadata for a paper stored in the library."""
    bibtex_key: str
    title: str
    authors: list[str]
    year: int | None = None
    abstract: str | None = None
    journal: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    ssrn_id: str | None = None
    url: str | None = None
    topics: list[str] = []
    tags: list[str] = []
    keywords: list[str] = []
    fields_of_study: list[str] = []
    citation_count: int | None = None
    added_date: str = ""
    status: str = "downloading"
    summary: dict | None = None


class Tag(BaseModel):
    """A tag in the managed vocabulary."""
    tag: str
    description: str | None = None
    paper_count: int = 0
