---
name: add-paper
description: Add an academic paper to the papertrail library. Accepts a DOI, arXiv ID, SSRN URL, paper URL, or title search. Runs the full pipeline: find, download, convert, summarize, tag, and store.
context: fork
---

# Add Paper to Library

You are adding a paper to the papertrail library. The user provided: $ARGUMENTS

Follow these steps in order.

## Step 1: Find and ingest the paper

If the argument is a **local file path** (ends in .pdf, or starts with / or ~/):
1. Extract the paper title from the filename (strip author prefixes, underscores, extensions)
2. Call `find_paper` with the title to find the paper's DOI or arXiv ID
3. Call `ingest_paper` with the identifier
4. If the PDF download fails, call `ingest_paper_manual` with the bibtex key and
   `pdf_source_path` set to the absolute path of the local file

If the argument looks like a DOI (contains "10."), arXiv ID (like "2301.12345"),
SSRN URL/ID, or other URL, call `ingest_paper` directly with that identifier.

If the argument is a title or description, first call `find_paper` to search for it.
Pick the best match and call `ingest_paper` with its DOI or arXiv ID.

If `find_paper` returns no results, use web search to find the paper, then try
`ingest_paper` with a DOI or URL from the search results.

## Step 2: Handle download failures

If `ingest_paper` reports that the PDF download failed (status "pending_pdf"):

1. If the user provided a local PDF path, call `ingest_paper_manual` with the bibtex key
   and `pdf_source_path` set to the local path
2. Check if the paper has an arXiv version -- if so, try `ingest_paper` again with the arXiv ID
3. Ask the user to download the PDF manually and tell them the path to place it at
4. Once the PDF is placed, call `ingest_paper_manual` with the bibtex key to continue

## Step 3: Wait for conversion

After ingesting (or after calling `ingest_paper_manual`), poll `conversion_status`
with the bibtex key every 10 seconds until the status is "summarizing" (or "error").
Do not poll more than 30 times.

If status is "error", report the error and stop.

## Step 4: Read, summarize, and fetch tags (in parallel)

Once status is "summarizing", launch these two tasks **in parallel**:

### Task A — Subagent: Read and summarize the paper

Use the Task tool to launch a `general-purpose` subagent with this prompt (fill in
the bibtex key):

> Read the paper with bibtex key "{bibtex_key}" using the `read_paper` MCP tool
> and generate a structured summary.
>
> **IMPORTANT**: Only summarize content that appears in the paper text returned by
> `read_paper`. Do not supplement with outside knowledge. If a section is missing
> or illegible, say so rather than filling in from memory. Every claim in the
> summary must be traceable to the text you read.
>
> 1. Call `read_paper` with `bibtex_key`, `start_line=1`, `end_line=500` to get the
>    first chunk and the total line count (shown in the response header).
> 2. If the paper has more than 500 lines, call `read_paper` for all remaining chunks
>    **in parallel** (e.g., 501-1000, 1001-1500, etc., using 500-line chunks).
> 3. Combine all chunks in order to form the full paper text.
> 4. Generate a JSON summary with these keys:
>    - `main_contribution`: 2-3 sentences on the paper's primary contribution
>    - `methodology`: Description of the methods used
>    - `findings`: Key results and findings
>    - `limitations`: Noted limitations or caveats
>    - `section_summaries`: Object mapping section headers to 1-2 sentence summaries
>    - `key_tables`: Array of objects with `table` and `description` for important tables
>    - `key_figures`: Array of objects with `figure` and `description` for important figures
> 5. Generate 3-8 descriptive keywords for the paper.
> 6. Return ONLY the JSON summary string and the keywords list, nothing else.
>    Format your response as:
>    SUMMARY_JSON: ```json\n{...}\n```
>    KEYWORDS: keyword1, keyword2, keyword3, ...

### Task B — Main agent: Fetch the tag vocabulary

While the subagent is running, call `list_tags` to retrieve the current tag vocabulary.

## Step 5: Assign tags and store summary (in parallel)

Once the subagent returns the summary and keywords, and you have the tag vocabulary:

1. Choose relevant existing tags for this paper based on the summary
2. If the paper covers topics not in the vocabulary, call `add_tags` with new tags
   (include a short description for each new tag)
3. Then call **both of these in parallel**:
   - `tag_paper` with the bibtex key and chosen tags
   - `store_summary` with the bibtex key, the JSON summary string, and the keywords list

## Step 6: Report

Provide a brief report to the user:
- Paper title and bibtex key
- Main contribution (1-2 sentences)
- Tags assigned
- Confirmation that the paper is ready in the library
