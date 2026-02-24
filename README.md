# Papertrail

An MCP server for managing an academic paper library with Claude Code. Search for papers, download and convert them to markdown, generate structured summaries, and search across your library.

## What it does

- **Find papers** via Semantic Scholar, arXiv, and SSRN
- **Download and convert** PDFs to searchable markdown (pymupdf4llm)
- **Generate summaries** with section-level detail, key results, tables, and figures
- **Manage tags** with a growing vocabulary for consistent categorization
- **Full-text search** over metadata, summaries, and paper content (SQLite FTS5)
- **Sync across machines** via rclone mount (S3, Google Cloud, Backblaze, Dropbox, and [40+ other backends](https://rclone.org/overview/))
- **Literature reviews** across multiple papers using parallel subagents

## Installation

### Step 1: Install uv

[uv](https://docs.astral.sh/uv/) is a fast Python package manager. If you don't have it:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or with Homebrew
brew install uv
```

### Step 2: Clone the repo and install skills

```bash
git clone https://github.com/YOURUSER/papertrail.git
cd papertrail

# Copy the skills into your Claude Code config directory.
# Skills are slash commands (/add-paper, /search-papers, etc.) that
# tell Claude how to use papertrail step by step.
mkdir -p ~/.claude/skills
cp -r skills/* ~/.claude/skills/
```

### Step 3: Add the MCP server to Claude Code

Claude Code uses MCP (Model Context Protocol) servers to give Claude access
to external tools. You need to tell Claude Code where to find papertrail.

Open (or create) `~/.claude.json` and add the following. Replace
`/path/to/papertrail` with the actual path where you cloned the repo:

```json
{
  "mcpServers": {
    "papertrail": {
      "command": "uv",
      "args": ["--directory", "/path/to/papertrail", "run", "papertrail"],
      "env": {
        "PAPERTRAIL_DATA_DIR": "${HOME}/.papertrail",
        "PAPERTRAIL_RCLONE_REMOTE": ""
      }
    }
  }
}
```

Restart Claude Code (or run `/mcp` to reload servers). You should see
papertrail's tools become available.

At this point you're done -- papertrail will store everything locally in
`~/.papertrail/`. If you want to sync across machines, continue to step 4.

### Step 4: Set up cloud sync with rclone (optional)

[rclone](https://rclone.org/) lets you mount cloud storage (S3, Wasabi, Google
Cloud, Backblaze, Dropbox, etc.) as a local folder. When configured, papertrail
reads and writes directly to the mount, so your library stays in sync across
machines automatically.

#### 4a. Install rclone

```bash
# macOS
brew install rclone

# Linux
sudo apt install rclone   # Debian/Ubuntu
# Or: curl https://rclone.org/install.sh | sudo bash
```

#### 4b. Install a FUSE provider (macOS only)

`rclone mount` needs FUSE (Filesystem in Userspace) to make remote storage
appear as a local directory. Linux has this built in. On macOS, install one of:

```bash
# FUSE-T (recommended -- lightweight, no reboot needed)
brew install --cask fuse-t

# OR macFUSE (more mature, but requires a reboot after install)
brew install --cask macfuse
```

#### 4c. Configure a remote

Run `rclone config` and follow the interactive prompts to connect your storage
provider. This creates a named remote you'll reference later. See
[rclone's provider list](https://rclone.org/overview/) for setup guides for
each backend.

```bash
rclone config
# Example: create a remote named "myremote" pointing to an S3-compatible bucket
```

#### 4d. Create your bucket and update the config

```bash
# Create the bucket on your remote
rclone mkdir myremote:my-papertrail-bucket

# Then update PAPERTRAIL_RCLONE_REMOTE in your ~/.claude.json:
# "PAPERTRAIL_RCLONE_REMOTE": "myremote:my-papertrail-bucket"
```

Restart Claude Code. On startup, papertrail will automatically mount the remote
at `~/.papertrail` and unmount it on shutdown.

## Usage

### Skills (slash commands)

**`/add-paper <identifier>`** -- Add a paper to the library. Accepts:
- arXiv ID: `/add-paper 2301.12345`
- DOI: `/add-paper 10.1257/aer.2024.001`
- SSRN URL: `/add-paper https://ssrn.com/abstract=1234567`
- Title search: `/add-paper "Causal Inference in Economics"`
- Author + title: `/add-paper Cunningham and Shah decriminalization`
- Local PDF: `/add-paper ~/Downloads/paper.pdf`

Runs the full pipeline: find, download, convert to markdown, generate a structured summary, assign tags, and store. Uses a subagent to read and summarize the paper in parallel with tag fetching.

**`/search-papers <query>`** -- Search your library by topic, author, method, or keyword.

**`/read-paper <key or description>`** -- Read a paper's summary and bring relevant sections into context.

**`/lit-review <research question>`** -- Conduct a literature review across papers in the library. Finds relevant papers, reads them all in parallel via subagents, and synthesizes findings with cross-paper comparisons, thematic groupings, and citations.

### MCP tools (available to Claude directly)

| Tool | Purpose |
|------|---------|
| `find_paper` | Search Semantic Scholar/arXiv for papers |
| `ingest_paper` | Download and start converting a paper |
| `ingest_paper_manual` | Provide a PDF for a paper that failed automatic download |
| `conversion_status` | Check PDF-to-markdown conversion progress |
| `read_paper` | Read paper markdown (full or line range) |
| `store_summary` | Store a generated summary |
| `search_library` | Search metadata, topics, keywords, summaries |
| `search_paper_text` | Full-text search over paper content |
| `get_paper_metadata` | Get metadata + summary for a paper |
| `list_papers` | Browse the library, filter by status or tag |
| `list_tags` | View the tag vocabulary |
| `add_tags` | Add new tags to the vocabulary |
| `tag_paper` | Tag a paper |
| `delete_paper` | Remove a paper from the library |
| `rebuild_index` | Force a full rescan and index rebuild |

## Configuration

| Environment variable | Default | Description |
|---------------------|---------|-------------|
| `PAPERTRAIL_DATA_DIR` | `~/.papertrail` | Data directory (rclone mount point if remote is set) |
| `PAPERTRAIL_RCLONE_REMOTE` | (empty) | rclone remote path (e.g., `myremote:my-bucket`) |
| `PAPERTRAIL_INDEX_DIR` | `~/.cache/papertrail` | Local directory for the ephemeral search index |
| `PAPERTRAIL_SEMANTIC_SCHOLAR_API_KEY` | (none) | Optional API key for higher rate limits |
| `PAPERTRAIL_HTTP_PROXY` | (none) | HTTP proxy for PDF downloads |
| `PAPERTRAIL_UNPAYWALL_EMAIL` | (none) | Email for Unpaywall API (finds legal open access PDFs) |

### Accessing paywalled papers

- **On VPN**: PDF downloads through DOI links work automatically -- publishers see your institutional IP.
- **Off VPN**: Set `PAPERTRAIL_HTTP_PROXY` to your institution's proxy URL to route PDF downloads through it.
- **Unpaywall**: Set `PAPERTRAIL_UNPAYWALL_EMAIL` to any email address to enable the Unpaywall API, which finds legal open access copies of papers. No API key needed.

