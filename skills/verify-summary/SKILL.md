---
name: verify-summary
description: Verify that a paper's stored summary is grounded in its actual text. Checks for hallucinated claims not supported by the paper content. Can verify a single paper or all papers in the library.
context: fork
---

# Verify Summary

Verify paper summaries are grounded in actual paper text: $ARGUMENTS

## Instructions

### Determine scope

- If the argument is a bibtex key (e.g., "smith_2024_causal"), verify that single paper.
- If the argument is "all" or empty, verify all papers with status "ready" by running
  each verification as a parallel subagent (Step 2).

### Step 1: Get paper list

If verifying all papers, call `list_papers` with `status="ready"` to get all papers
that have summaries. Extract the bibtex keys.

### Step 2: Verify each paper

For each paper, launch a `general-purpose` subagent using the Task tool. Launch
**all subagents in a single message** so they run concurrently.

Each subagent prompt should be (fill in bibtex_key):

> Verify that the stored summary for paper "{bibtex_key}" is grounded in its text.
>
> 1. Call `get_paper_metadata` with bibtex_key "{bibtex_key}" to get the stored
>    summary and keywords.
> 2. Call `read_paper` with `bibtex_key`, `start_line=1`, `end_line=500` to get
>    the first chunk and the total line count.
> 3. If the paper has more than 500 lines, call `read_paper` for all remaining
>    chunks **in parallel** (500-line chunks).
> 4. For each claim in the summary (main_contribution, methodology, findings,
>    limitations, section_summaries, key_tables, key_figures), check whether
>    it is supported by the paper text.
> 5. Return a verification report with:
>    - `bibtex_key`: the paper's key
>    - `status`: "pass" if all claims are grounded, "fail" if any are not,
>      "partial" if the paper text is incomplete or illegible in places
>    - `issues`: array of objects, each with:
>      - `field`: which summary field has the issue (e.g., "findings")
>      - `claim`: the specific claim that is not grounded
>      - `reason`: why it appears ungrounded (e.g., "not mentioned in paper text",
>        "paper says X but summary says Y", "section not present in markdown")
>    - `notes`: any other observations (e.g., "PDF conversion quality is poor",
>      "some tables are garbled")
>
> Be strict: if a specific number, result, or factual claim in the summary cannot
> be found in the paper text, flag it. General paraphrasing is fine as long as the
> meaning is faithful to the source text. If you cannot find a claim in the text
> but it seems plausible, still flag it -- err on the side of caution.

### Step 3: Report results

Once all subagents return, compile a report:

1. **Overall**: X of Y papers passed, Z had issues
2. **Issues found**: For each paper with issues, list:
   - Paper title and bibtex key
   - Each flagged claim and why it failed
3. **Recommendations**: If any papers have issues, suggest re-summarizing them
   with `/add-paper` (which will re-read and re-summarize from the paper text).

If a paper's summary needs to be regenerated, the user can:
1. Read the paper with `read_paper`
2. Generate a new summary
3. Store it with `store_summary`
