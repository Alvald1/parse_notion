# notion-parser

Python script to extract all text (including LaTeX formulas) from any public Notion page.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

**Print to stdout:**
```bash
python parser.py <notion_url>
```

**Save to file:**
```bash
python parser.py <notion_url> --output output.md
```

**Plain text (no Markdown formatting):**
```bash
python parser.py <notion_url> --format plain
```

## Output

By default, the script produces **GitHub-Flavored Markdown**:

- Inline LaTeX formulas are wrapped as `$...$`
- Block LaTeX formulas are wrapped as `$$...$$`
- Headings, lists, code blocks, and other Notion elements are converted to their GFM equivalents

Use `--format plain` to strip all formatting and get raw text only.

## Requirements

- Python 3.8+
- [`requests`](https://pypi.org/project/requests/)
# parse_notion
