---
name: find-pdfs
description: Find and download PDFs for papers in the library that are missing them (status "pending_pdf"). Searches author websites, institutional repositories, and conference proceedings in parallel.
context: fork
---

# Find PDFs for Pending Papers

Find and download PDFs for papers missing them. $ARGUMENTS

## Step 1: Identify papers needing PDFs

Call `list_papers` to get all papers in the library. Filter to those with status
"pending_pdf".

If the user specified particular bibtex keys (comma-separated), filter to just those.
If no papers need PDFs, report that all papers have PDFs and stop.

## Step 2: Launch parallel PDF searches

For each paper needing a PDF, launch a `general-purpose` subagent using the Task
tool. Launch **all subagents in a single message** so they run concurrently.

Each subagent prompt should be (fill in bibtex_key, title, authors, year, doi):

> Find and download a PDF for this paper:
> - **Title**: {title}
> - **Authors**: {authors}
> - **Year**: {year}
> - **DOI**: {doi}
> - **Bibtex key**: {bibtex_key}
>
> ## Search strategy
>
> Use WebSearch to find a freely available PDF. Try these queries in order:
>
> 1. "{title}" filetype:pdf
> 2. "{first_author_last_name}" "{first three title words}" PDF
> 3. "{first_author_last_name}" "{year}" site:scholar.google.com
>
> ## Evaluating results
>
> For each search, look for:
> - **Direct PDF links**: URLs ending in .pdf or containing /pdf/
> - **Author websites**: Faculty pages at universities often host working papers
> - **Institutional repositories**: repec.org, econstor.eu, nber.org, ideas.repec.org
> - **Conference proceedings**: ASSA, NBER Summer Institute, CEPR, etc.
> - **Preprint servers**: arXiv, SSRN, OSF
>
> Prefer author website and institutional repository PDFs over ResearchGate.
>
> ## Download attempts
>
> For each promising URL, call `download_paper` with bibtex_key "{bibtex_key}"
> and `pdf_url` set to the URL.
>
> If the tool reports success (status changes to "converting"), you are done.
> If it reports failure, try the next URL.
>
> Try up to 5 candidate URLs total. If none work, report which URLs you found.
>
> ## Report
>
> Return one of:
> - SUCCESS: {bibtex_key} - PDF downloaded from {url}
> - FAILED: {bibtex_key} - Tried {n} URLs, none worked. Best candidates: {urls}

## Step 3: Collect results

After all subagents return, collect the results and categorize into successes
and failures.

## Step 4: Report

Present a summary:
- Total papers processed
- PDFs found successfully (with source URLs)
- PDFs not found (with best candidate URLs for manual download)

For papers that still need PDFs, suggest the user:
1. Check if their institution provides access
2. Email the corresponding author
3. Download manually and place at `~/.papertrail/papers/{bibtex_key}/paper.pdf`,
   then call `download_paper` with the bibtex key and `pdf_source_path`
