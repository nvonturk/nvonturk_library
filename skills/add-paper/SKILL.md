---
name: add-paper
description: Add an academic paper to the papertrail library. Accepts a DOI, arXiv ID, SSRN URL, paper URL, or title search. Runs the full pipeline: find, download, convert, summarize, tag, and store.
context: fork
---

# Add Paper to Library

You are adding a paper to the papertrail library. The user provided: $ARGUMENTS

Follow these steps in order.

## Step 0: Detect batch input

If the argument contains multiple numbered citations (e.g., "1. Author (Year)..." or
"1. Author, Title, Journal, Year" with 2+ entries), OR is a path to a `.md` file
containing such a list, this is a **batch import**. Follow the **Batch Flow** section
below instead of Steps 1-7.

Otherwise, continue to Step 1 (single paper flow).

## Step 1: Find and ingest metadata

If the argument is a **local file path** (ends in .pdf, or starts with / or ~/):
1. Extract the paper title from the filename (strip author prefixes, underscores, extensions)
2. Call `find_paper` with the title to find the paper's DOI or arXiv ID
3. Call `ingest_paper` with the identifier (saves metadata only)
4. Then call `download_paper` with the bibtex key and `pdf_source_path` set to the
   absolute path of the local file

If the argument looks like a DOI (contains "10."), arXiv ID (like "2301.12345"),
SSRN URL/ID, or other URL, call `ingest_paper` directly with that identifier.

If the argument is a title or description, first call `find_paper` to search for it.
Pick the best match. Before ingesting, compare the match against what the user asked for:
- Do the authors match (if the user specified authors)?
- Is the title a close match (if the user specified a title)?
- Is the year correct (if the user specified a year)?

If any of these seem off — e.g., different authors, substantially different title,
wrong year — show the user what you found and ask them to confirm before proceeding.
If the match looks correct, call `ingest_paper` with its DOI or arXiv ID.

If `find_paper` returns no results, use web search to find the paper, then try
`ingest_paper` with a DOI or URL from the search results. Apply the same confirmation
check: if the web search result doesn't closely match what the user asked for, confirm
before ingesting.

## Step 2: Download PDF and fetch BibTeX

After `ingest_paper` returns the bibtex key, **skip Tasks A and B if the user
provided a local file path** (the PDF was already provided in Step 1).

Launch all applicable tasks **in parallel**:

### Task A: Automated download

Call `download_paper` with the bibtex key (no pdf_url or pdf_source_path). This runs
the full automated pipeline (arXiv, NBER, Unpaywall, institutional repos, etc.).

### Task B: Web search for PDF

Use WebSearch to find a freely available PDF. Try these queries:
1. "{paper title}" filetype:pdf
2. "{first author last name}" "{first few title words}" PDF

For each search, scan the results for:
- Direct PDF links (ending in .pdf or from known repositories)
- Author faculty/personal websites (these often host working paper PDFs)
- Institutional repositories (repec.org, nber.org, econstor.eu, etc.)

Collect up to 3 promising PDF URLs.

### Task C: Fetch BibTeX citation

Call `fetch_bibtex` with the bibtex key. This fetches the publisher's BibTeX entry
via DOI content negotiation and stores it as `citation.bib`. If it fails (e.g., no
DOI), note it in the final report but don't block the pipeline.

### After Tasks A and B complete:

- If Task A succeeded (status is "converting"), you're done with the PDF step.
- If Task A failed, try each URL from Task B by calling `download_paper` with the
  bibtex key and `pdf_url` set to the URL. Stop as soon as one succeeds.
- If all URLs fail, ask the user to download the PDF manually and tell them
  the path to place it at (`~/.papertrail/papers/{bibtex_key}/paper.pdf`).
- Once the user places the PDF, call `download_paper` with the bibtex key and
  `pdf_source_path` to continue.

## Step 3: Wait for conversion

After the PDF is registered, poll `conversion_status` with the bibtex key every
10 seconds until the status is "summarizing" (or "error").
Do not poll more than 30 times.

If status is "error", report the error and stop.

## Step 4: Read, summarize, and fetch tags (in parallel)

Once status is "summarizing", launch these two tasks **in parallel**:

### Task A — Subagent: Read and summarize the paper

Use the Task tool to launch a `general-purpose` subagent with this prompt (fill in
the bibtex key):

> Read the paper with bibtex key "{bibtex_key}" using the `read_paper` MCP tool
> and generate a structured summary. Also call `get_paper_metadata` to retrieve
> the paper's title, authors, and abstract.
>
> ## Step 1: Read the full paper
>
> 1. Call `read_paper` with `bibtex_key`, `start_line=1`, `end_line=500` to get the
>    first chunk and the total line count (shown in the response header).
> 2. If the paper has more than 500 lines, call `read_paper` for all remaining chunks
>    **in parallel** (e.g., 501-1000, 1001-1500, etc., using 500-line chunks).
> 3. Combine all chunks in order to form the full paper text.
>
> ## Step 2: Verify the document matches the metadata
>
> Before summarizing, check that the document you read is actually the right paper:
> - Does the title in the text match the metadata title?
> - Do the authors match?
> - Is this the full paper, or is it an errata, corrigendum, table of contents,
>   or other supplementary material?
>
> If the document does NOT match the metadata, or is not the full paper, DO NOT
> generate a summary. Instead return:
>   WRONG_DOCUMENT: A brief explanation of what the document actually is
>   (e.g., "This is an errata for the paper, not the paper itself" or
>   "This PDF is a copy of a different paper by the same author")
>
> ## Step 3: Generate the summary
>
> **IMPORTANT**: Only summarize content that appears in the paper text returned by
> `read_paper`. Do not supplement with outside knowledge. If a section is missing
> or illegible, say so rather than filling in from memory.
>
> For every specific factual claim (numbers, percentages, coefficients, named
> results), include a line reference in parentheses, e.g., "(lines 340-342)".
> This is required so claims can be verified against the source text. If you
> cannot find a line number for a claim, do not include the claim.
>
> Generate a JSON summary with these keys:
>    - `main_contribution`: 2-3 sentences on the paper's primary contribution,
>      with line references for key claims
>    - `methodology`: Description of the methods used, with line references
>    - `findings`: Key results and findings, each with line references for
>      specific numbers or results
>    - `limitations`: Noted limitations or caveats
>    - `section_summaries`: Object mapping section headers to 1-2 sentence summaries
>    - `key_tables`: Array of objects with `table` and `description` for important tables
>    - `key_figures`: Array of objects with `figure` and `description` for important figures
>
> Generate 3-8 descriptive keywords for the paper.
>
> Return ONLY the JSON summary string and the keywords list, nothing else.
> Format your response as:
>    SUMMARY_JSON: ```json\n{...}\n```
>    KEYWORDS: keyword1, keyword2, keyword3, ...

### Task B — Main agent: Fetch the tag vocabulary

While the subagent is running, call `list_tags` to retrieve the current tag vocabulary.

## Step 5: Handle wrong document

If the subagent returned `WRONG_DOCUMENT`, report the issue to the user:
- What document was actually ingested (e.g., errata, different paper)
- The bibtex key so they can delete it or provide the correct PDF
- Stop here. Do not store a summary or assign tags.

## Step 6: Assign tags and store summary (in parallel)

Once the subagent returns the summary and keywords, and you have the tag vocabulary:

1. Choose relevant existing tags for this paper based on the summary
2. If the paper covers topics not in the vocabulary, call `add_tags` with new tags
   (include a short description for each new tag)
3. Then call **both of these in parallel**:
   - `tag_paper` with the bibtex key and chosen tags
   - `store_summary` with the bibtex key, the JSON summary string, and the keywords list

## Step 7: Report

Provide a brief report to the user:
- Paper title and bibtex key
- Main contribution (1-2 sentences)
- Tags assigned
- Confirmation that the paper is ready in the library

---

## Batch Flow

This flow handles multiple citations at once. It ingests metadata and downloads PDFs
but **skips summarization and tagging** (too expensive per paper — user can summarize
individually later via `/read-paper`).

### Phase 1: Parse citations

If the input is a `.md` file path, read it first.

Extract structured info from each numbered citation. For each entry, extract:
- **Title**
- **Authors**
- **Year**
- **Identifier** (DOI, SSRN URL/ID, arXiv ID) if embedded in the citation

### Phase 2: Confirm with user

Show the user:
- Total number of citations parsed
- 3-5 sample titles from the list
- A note that summarization will be skipped (can be done later per paper)

Ask them to confirm before proceeding. This catches cases where the input is
malformed or not what the user intended.

### Phase 3: Ingest in batches

Process papers in **batches of 5** (to respect Semantic Scholar rate limits).

For each paper in the batch:
- If it has a DOI, SSRN ID, or arXiv ID → call `ingest_paper` directly
- Otherwise → call `find_paper` with the title. Apply the same match-confirmation
  logic as single-paper mode: check authors, title, and year. If the best match
  seems off, **skip the paper** (don't prompt for each one — log it as "skipped"
  with the reason). If the match looks correct, call `ingest_paper`.

After each batch of ingestions completes, launch **in parallel** for all successfully
ingested papers in that batch:
- `download_paper` with the bibtex key (automated download)
- `fetch_bibtex` with the bibtex key

Track each paper's status: **ingested**, **failed** (with reason), or **skipped**
(match looked wrong).

Report progress to the user after each batch (e.g., "Batch 2/5 done: 4 ingested,
1 skipped").

### Phase 4: Report

Provide a summary table of all papers:

| # | Title | BibTeX Key | Status | PDF | BibTeX |
|---|-------|------------|--------|-----|--------|

Where:
- **Status**: ingested / failed / skipped (with reason for failures and skips)
- **PDF**: found / not found
- **BibTeX**: fetched / not found

End with a note:
- Summarization was skipped for all papers
- Suggest running `/read-paper` for individual papers they want to summarize
- List any papers that need manual PDF downloads
