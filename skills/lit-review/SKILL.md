---
name: lit-review
description: Conduct a literature review across papers in the library. Takes a research question, finds relevant papers, reads them in parallel via subagents, and synthesizes findings with cross-paper comparisons and citations.
context: fork
---

# Literature Review

Conduct a literature review on: $ARGUMENTS

## Step 1: Find relevant papers

1. Call `search_library` with the query "$ARGUMENTS" to search metadata,
   keywords, and summaries.
2. If few results, try broader or alternative queries. Also try
   `search_paper_text` to search full text for relevant passages.
3. Collect all unique bibtex keys that are relevant to the question. Aim for
   breadth -- include papers that are tangentially related if they might
   contribute useful context.

If no papers are found, tell the user and suggest adding papers with
`/add-paper`. Stop here.

## Step 2: Read papers in parallel via subagents

For each relevant paper, launch a `general-purpose` subagent using the Task
tool. Launch **all subagents in a single message** so they run concurrently.

Each subagent prompt should be (fill in bibtex_key and the research question):

> Read the paper with bibtex key "{bibtex_key}" using the `read_paper` MCP tool
> and extract information relevant to this research question: "{question}"
>
> **IMPORTANT**: Only report findings, methods, and claims that appear in the
> paper text returned by `read_paper`. Do not supplement with outside knowledge
> about this paper or its authors. If a section is missing or illegible, say so.
> Every claim must be traceable to specific text you read.
>
> 1. Call `read_paper` with `bibtex_key`, `start_line=1`, `end_line=500` to get
>    the first chunk and total line count.
> 2. If the paper has more than 500 lines, call `read_paper` for all remaining
>    chunks **in parallel** (500-line chunks).
> 3. Read the full paper and extract:
>    - **Relevant findings**: What does this paper say about the research question?
>      Include specific results, numbers, and quotes where useful.
>    - **Methodology**: How did the authors approach this topic?
>    - **Key definitions**: Any definitions or frameworks relevant to the question.
>    - **Data and measurement**: What data sources and measures are used?
>    - **Connections**: How does this paper relate to or cite other work on this topic?
> 4. Return a structured summary focused on the research question. Include
>    specific section references (e.g., "Section 3.2") and page/line numbers
>    for key claims so the user can look them up.

## Step 3: Synthesize across papers

Once all subagents return, synthesize their findings into a coherent review:

1. **Overview**: 2-3 sentence summary of what the literature says about the
   research question.

2. **Key findings by theme**: Group findings across papers by theme rather than
   paper-by-paper. For each theme:
   - What do the papers collectively say?
   - Where do they agree or disagree?
   - Cite specific papers using (Author Year) format.

3. **Methodological approaches**: Compare how different papers approach the
   question. Note differences in data, measurement, identification, etc.

4. **Gaps and open questions**: What aspects of the research question are not
   well-addressed by the available papers? What contradictions remain unresolved?

5. **Paper-level details**: A brief table or list of each paper reviewed with
   its bibtex key, main contribution, and relevance to the question.

## Step 4: Suggest next steps

- Are there important papers missing from the library that would strengthen
  the review? If so, suggest specific papers or searches.
- Are there follow-up questions the user might want to explore?
