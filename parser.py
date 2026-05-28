#!/usr/bin/env python3
"""
Notion Public Page Parser
Parses a public Notion page via the unofficial loadPageChunk JSON API
and outputs GitHub-Flavored Markdown with LaTeX math support.

Usage:
    python parser.py <notion_url>
    python parser.py <notion_url> --output output.md
    python parser.py <notion_url> --format plain
"""

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests


class NotionPageParser:
    BASE_URL = "https://www.notion.so/api/v3"
    MAX_PAGES = 50          # safety limit for pagination
    RETRY_LIMIT = 3         # max retries on network error
    RETRY_BACKOFF = 1.0     # seconds between retries
    CHUNK_DELAY = 0.5       # seconds between pagination requests

    def __init__(self, page_url: str) -> None:
        self.page_url = page_url
        self.page_id = self._extract_page_id(page_url)
        self._record_map: dict = {}

    # ------------------------------------------------------------------
    # Page ID extraction
    # ------------------------------------------------------------------

    def _extract_page_id(self, url: str) -> str:
        """Extract the 32-char hex page ID from a Notion URL and format as UUID."""
        match = re.search(r"([0-9a-f]{32})", url.replace("-", ""))
        if not match:
            raise ValueError(
                f"Could not extract a valid Notion page ID from URL: {url!r}\n"
                "Expected a 32-character hex string in the URL path."
            )
        raw = match.group(1)
        # Insert dashes: 8-4-4-4-12
        uuid = f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"
        return uuid

    # ------------------------------------------------------------------
    # Network layer
    # ------------------------------------------------------------------

    def _post_chunk(self, cursor: dict, chunk_number: int = 0) -> dict:
        """POST a single loadPageChunk request with retry logic."""
        payload = {
            "pageId": self.page_id,
            "limit": 100,
            "cursor": cursor,
            "chunkNumber": chunk_number,
            "verticalColumns": False,
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        url = f"{self.BASE_URL}/loadPageChunk"

        for attempt in range(1, self.RETRY_LIMIT + 1):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
                if resp.status_code == 404:
                    raise RuntimeError(
                        f"Page not found (404). The page may be private or the URL is incorrect.\n"
                        f"URL: {self.page_url}"
                    )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"Notion API returned HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                return resp.json()
            except requests.exceptions.RequestException as exc:
                if attempt == self.RETRY_LIMIT:
                    raise RuntimeError(
                        f"Network error after {self.RETRY_LIMIT} attempts: {exc}"
                    ) from exc
                time.sleep(self.RETRY_BACKOFF * attempt)

    def fetch_page_data(self) -> dict:
        """
        Fetch all blocks for the page, handling pagination.
        Returns a merged dict of all block records.
        """
        all_blocks: dict = {}
        cursor: dict = {"stack": []}
        pages_fetched = 0

        while pages_fetched < self.MAX_PAGES:
            data = self._post_chunk(cursor, chunk_number=pages_fetched)
            record_map = data.get("recordMap", {})
            blocks = record_map.get("block", {})

            if not blocks:
                if pages_fetched == 0:
                    raise RuntimeError(
                        "The page returned an empty recordMap. "
                        "The page may be private or require authentication."
                    )
                break

            all_blocks.update(blocks)
            pages_fetched += 1

            # Check if there are more pages
            next_cursor = data.get("cursor", {})
            if not next_cursor.get("stack"):
                break

            cursor = next_cursor
            time.sleep(self.CHUNK_DELAY)

        return all_blocks

    def _fetch_missing_blocks(self, record_map: dict) -> None:
        """
        Notion's loadPageChunk does not return children of collapsed toggle blocks.
        This method iteratively finds all block IDs referenced in 'content' fields
        that are missing from the record_map, then batch-fetches them via
        syncRecordValues using parallel threads for speed.
        Repeats until no more missing blocks are found (handles multiple nesting levels).
        """
        BATCH_SIZE = 100       # blocks per syncRecordValues request
        MAX_WORKERS = 8        # parallel HTTP threads
        SYNC_URL = f"{self.BASE_URL}/syncRecordValues"
        req_headers = {
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        def fetch_batch(batch: list[str]) -> dict:
            """Fetch one batch of block IDs; returns dict of fetched blocks."""
            payload = {
                "requests": [
                    {"pointer": {"table": "block", "id": bid}, "version": -1}
                    for bid in batch
                ]
            }
            for attempt in range(1, self.RETRY_LIMIT + 1):
                try:
                    resp = requests.post(
                        SYNC_URL, json=payload, headers=req_headers, timeout=30
                    )
                    if resp.status_code == 200:
                        return resp.json().get("recordMap", {}).get("block", {})
                    elif resp.status_code == 429:
                        backoff = self.RETRY_BACKOFF * (2 ** attempt)
                        if attempt < self.RETRY_LIMIT:
                            time.sleep(backoff)
                    else:
                        if attempt < self.RETRY_LIMIT:
                            time.sleep(self.RETRY_BACKOFF * attempt)
                except requests.exceptions.RequestException:
                    if attempt < self.RETRY_LIMIT:
                        time.sleep(self.RETRY_BACKOFF * attempt)
            return {}

        for _round in range(20):  # safety limit on nesting depth
            # Collect all child IDs referenced but not yet in record_map
            missing: list[str] = []
            for bdata in record_map.values():
                val = bdata.get("value", {}).get("value", {})
                for child_id in val.get("content", []) or []:
                    if child_id not in record_map:
                        missing.append(child_id)

            if not missing:
                break

            # Deduplicate while preserving order
            seen: set = set()
            unique_missing = [x for x in missing if not (x in seen or seen.add(x))]  # type: ignore[func-returns-value]

            print(
                f"  Fetching {len(unique_missing)} missing nested blocks (round {_round + 1})...",
                file=sys.stderr,
            )

            # Split into batches
            batches = [
                unique_missing[i : i + BATCH_SIZE]
                for i in range(0, len(unique_missing), BATCH_SIZE)
            ]

            # Fetch all batches in parallel
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(fetch_batch, b): b for b in batches}
                for future in as_completed(futures):
                    fetched = future.result()
                    if fetched:
                        record_map.update(fetched)

    # ------------------------------------------------------------------
    # Rich text rendering
    # ------------------------------------------------------------------

    def _parse_rich_text(self, title_prop) -> str:
        """
        Render a Notion rich text array to a Markdown string.

        Format: [ [text, [[annotation, value], ...]], ... ]
        Annotations: b=bold, i=italic, s=strikethrough, c=code,
                     e=inline equation, a=link, h=highlight, u=underline
        """
        if not title_prop:
            return ""

        parts: list[str] = []
        for segment in title_prop:
            if not isinstance(segment, list) or len(segment) == 0:
                continue

            text: str = segment[0] if isinstance(segment[0], str) else ""
            annotations: list = segment[1] if len(segment) > 1 else []

            # Collect annotation codes and values
            ann_map: dict = {}
            for ann in annotations:
                if isinstance(ann, list) and len(ann) >= 1:
                    code = ann[0]
                    value = ann[1] if len(ann) > 1 else True
                    ann_map[code] = value

            # Inline equation: the LaTeX source is the VALUE of the "e" annotation,
            # NOT the segment text (which is just a "⁍" placeholder in the API response).
            # Format: ["⁍", [["e", "latex_source_here"]]]
            if "e" in ann_map:
                latex = ann_map["e"]
                # ann_map["e"] is True when annotation has no explicit value;
                # fall back to segment text in that case.
                if latex is True or not isinstance(latex, str):
                    latex = text
                parts.append(f"${latex}$")
                continue

            # Apply link annotation
            if "a" in ann_map:
                link_url = ann_map["a"]
                # Apply other formatting to the link text first
                formatted = self._apply_text_formatting(text, ann_map, skip={"a"})
                parts.append(f"[{formatted}]({link_url})")
                continue

            parts.append(self._apply_text_formatting(text, ann_map))

        return "".join(parts)

    def _apply_text_formatting(
        self, text: str, ann_map: dict, skip: Optional[set] = None
    ) -> str:
        """Apply bold/italic/code/strikethrough/highlight/underline annotations."""
        if skip is None:
            skip = set()

        result = text

        if "c" not in skip and "c" in ann_map:
            result = f"`{result}`"
        if "b" not in skip and "b" in ann_map:
            result = f"**{result}**"
        if "i" not in skip and "i" in ann_map:
            result = f"_{result}_"
        if "s" not in skip and "s" in ann_map:
            result = f"~~{result}~~"
        if "u" not in skip and "u" in ann_map:
            result = f"__{result}__"
        if "h" not in skip and "h" in ann_map:
            result = f"=={result}=="

        return result

    # ------------------------------------------------------------------
    # Block-specific renderers
    # ------------------------------------------------------------------

    def _get_children(self, block_id: str) -> list[str]:
        """Return ordered list of child block IDs."""
        value = self._get_block_value(block_id)
        return value.get("content", []) or []

    def _get_block_value(self, block_id: str) -> dict:
        """Safely retrieve the 'value' dict for a block.

        The API response wraps block data in a double 'value' layer:
          recordMap.block[id] = {"spaceId": ..., "value": {"value": {...actual data...}, "role": ...}}
        So we must descend two levels to reach type/properties/content.
        """
        outer = self._record_map.get(block_id, {}).get("value", {})
        return outer.get("value", {})

    def _get_title(self, value: dict) -> str:
        """Extract and render the rich text title from a block value."""
        return self._parse_rich_text(value.get("properties", {}).get("title"))

    def _parse_code_block(self, value: dict) -> str:
        lang_prop = value.get("properties", {}).get("language", [])
        language = ""
        if lang_prop and isinstance(lang_prop, list) and lang_prop[0]:
            language = lang_prop[0][0] if isinstance(lang_prop[0], list) else ""
            language = language.lower().strip()
            if language == "plain text":
                language = ""

        # Code content is stored in properties.title as plain text (no annotations)
        code_prop = value.get("properties", {}).get("title", [])
        code_text = ""
        if code_prop:
            code_text = "".join(
                seg[0] for seg in code_prop if isinstance(seg, list) and seg
            )

        return f"```{language}\n{code_text}\n```"

    def _parse_equation_block(self, value: dict) -> str:
        """
        Render a block-level equation block.

        Notion stores block equations in two formats:
          1. Plain: [["latex_source"]]  — title[0][0] is the LaTeX directly
          2. Rich:  [["⁍", [["e", "latex_source"]]], [" extra text"]]
             — the LaTeX is the value of the "e" annotation on the first segment

        We use _parse_rich_text() which already handles the "e" annotation correctly,
        then strip any surrounding $...$ wrappers it adds (since we wrap in $$...$$).
        """
        title_prop = value.get("properties", {}).get("title", [])
        if not title_prop:
            return "$$\n\n$$"

        # Use the rich text renderer — it handles both plain text and "e" annotations
        rendered = self._parse_rich_text(title_prop)

        # Strip inline $...$ wrappers added by _parse_rich_text for "e" annotations,
        # since we're wrapping the whole thing in $$...$$
        # Pattern: $latex$ → latex  (only at start/end of the rendered string)
        rendered = re.sub(r"^\$(.+)\$$", r"\1", rendered, flags=re.DOTALL)

        return f"$$\n{rendered}\n$$"

    def _parse_table(self, block_id: str) -> str:
        """Build a GFM pipe table from table_row children."""
        child_ids = self._get_children(block_id)
        rows: list[list[str]] = []

        for child_id in child_ids:
            child_value = self._get_block_value(child_id)
            if child_value.get("type") != "table_row":
                continue
            props = child_value.get("properties", {})
            # Table row cells are stored as column-keyed properties
            # The table block stores column order in format.table_block_column_order
            cells: list[str] = []
            # Try to get column order from parent table block
            table_value = self._get_block_value(block_id)
            col_order = (
                table_value.get("format", {}).get("table_block_column_order") or []
            )
            if col_order:
                for col_id in col_order:
                    cell_prop = props.get(col_id)
                    cells.append(self._parse_rich_text(cell_prop) if cell_prop else "")
            else:
                # Fallback: iterate props in insertion order
                for key, val in props.items():
                    cells.append(self._parse_rich_text(val) if val else "")
            rows.append(cells)

        if not rows:
            return ""

        # Determine column count
        col_count = max(len(r) for r in rows)

        # Pad rows to equal width
        padded = [r + [""] * (col_count - len(r)) for r in rows]

        header = padded[0]
        separator = ["---"] * col_count
        body = padded[1:]

        def fmt_row(cells: list[str]) -> str:
            return "| " + " | ".join(cells) + " |"

        lines = [fmt_row(header), fmt_row(separator)]
        for row in body:
            lines.append(fmt_row(row))

        return "\n".join(lines)

    def _parse_callout(self, value: dict) -> str:
        icon = ""
        icon_data = value.get("format", {}).get("page_icon", "")
        if icon_data:
            icon = icon_data + " "
        text = self._get_title(value)
        return f"> {icon}{text}"

    def _parse_image(self, value: dict) -> str:
        source_prop = value.get("properties", {}).get("source", [])
        url = ""
        if source_prop and isinstance(source_prop, list) and source_prop[0]:
            first = source_prop[0]
            url = first[0] if isinstance(first, list) and first else ""
        caption_prop = value.get("properties", {}).get("caption")
        alt = self._parse_rich_text(caption_prop) if caption_prop else ""
        return f"![{alt}]({url})"

    def _parse_bookmark(self, value: dict) -> str:
        link_prop = value.get("properties", {}).get("link", [])
        title_prop = value.get("properties", {}).get("title")
        url = ""
        if link_prop and isinstance(link_prop, list) and link_prop[0]:
            first = link_prop[0]
            url = first[0] if isinstance(first, list) and first else ""
        title = self._parse_rich_text(title_prop) if title_prop else url
        return f"[{title}]({url})"

    # ------------------------------------------------------------------
    # Main block dispatcher
    # ------------------------------------------------------------------

    def _parse_block(self, block_id: str, depth: int = 0) -> str:
        """
        Dispatch on block type and return a Markdown string.
        Recursively processes children where applicable.
        """
        value = self._get_block_value(block_id)
        if not value:
            return ""

        block_type = value.get("type", "")
        indent = "  " * depth
        lines: list[str] = []

        # ---- Heading types ----
        if block_type == "page":
            title = self._get_title(value)
            if depth == 0:
                lines.append(f"# {title}")
            else:
                lines.append(f"## Subpage: {title}")

        elif block_type == "header":
            lines.append(f"# {self._get_title(value)}")

        elif block_type == "sub_header":
            lines.append(f"## {self._get_title(value)}")

        elif block_type == "sub_sub_header":
            lines.append(f"### {self._get_title(value)}")

        # ---- Text / paragraph ----
        elif block_type == "text":
            text = self._get_title(value)
            lines.append(text)

        # ---- List types ----
        elif block_type == "bulleted_list":
            text = self._get_title(value)
            lines.append(f"{indent}- {text}")
            for child_id in self._get_children(block_id):
                child_md = self._parse_block(child_id, depth + 1)
                if child_md:
                    lines.append(child_md)
            return "\n".join(lines)

        elif block_type == "numbered_list":
            text = self._get_title(value)
            lines.append(f"{indent}1. {text}")
            for child_id in self._get_children(block_id):
                child_md = self._parse_block(child_id, depth + 1)
                if child_md:
                    lines.append(child_md)
            return "\n".join(lines)

        elif block_type == "to_do":
            text = self._get_title(value)
            checked_prop = value.get("properties", {}).get("checked", [])
            checked = False
            if checked_prop and isinstance(checked_prop, list) and checked_prop[0]:
                first = checked_prop[0]
                checked = (
                    isinstance(first, list) and first and first[0] == "Yes"
                )
            mark = "x" if checked else " "
            lines.append(f"{indent}- [{mark}] {text}")
            for child_id in self._get_children(block_id):
                child_md = self._parse_block(child_id, depth + 1)
                if child_md:
                    lines.append(child_md)
            return "\n".join(lines)

        # ---- Toggle ----
        elif block_type == "toggle":
            text = self._get_title(value)
            lines.append(f"{indent}> {text}")
            for child_id in self._get_children(block_id):
                child_md = self._parse_block(child_id, depth + 1)
                if child_md:
                    # Indent toggle children
                    indented = "\n".join(
                        f"{indent}  {l}" for l in child_md.splitlines()
                    )
                    lines.append(indented)
            return "\n".join(lines)

        # ---- Quote / Callout ----
        elif block_type == "quote":
            text = self._get_title(value)
            lines.append(f"> {text}")

        elif block_type == "callout":
            lines.append(self._parse_callout(value))

        # ---- Code ----
        elif block_type == "code":
            lines.append(self._parse_code_block(value))

        # ---- Equation (block-level) ----
        elif block_type == "equation":
            lines.append(self._parse_equation_block(value))

        # ---- Divider ----
        elif block_type == "divider":
            lines.append("---")

        # ---- Image ----
        elif block_type == "image":
            lines.append(self._parse_image(value))

        # ---- Video ----
        elif block_type == "video":
            source_prop = value.get("properties", {}).get("source", [])
            url = ""
            if source_prop and isinstance(source_prop, list) and source_prop[0]:
                first = source_prop[0]
                url = first[0] if isinstance(first, list) and first else ""
            lines.append(f"[Video: {url}]")

        # ---- File / PDF ----
        elif block_type == "file":
            source_prop = value.get("properties", {}).get("source", [])
            title_prop = value.get("properties", {}).get("title")
            url = ""
            if source_prop and isinstance(source_prop, list) and source_prop[0]:
                first = source_prop[0]
                url = first[0] if isinstance(first, list) and first else ""
            name = self._parse_rich_text(title_prop) if title_prop else url
            lines.append(f"[File: {name}]({url})")

        elif block_type == "pdf":
            source_prop = value.get("properties", {}).get("source", [])
            url = ""
            if source_prop and isinstance(source_prop, list) and source_prop[0]:
                first = source_prop[0]
                url = first[0] if isinstance(first, list) and first else ""
            lines.append(f"[PDF: {url}]")

        # ---- Bookmark ----
        elif block_type == "bookmark":
            lines.append(self._parse_bookmark(value))

        # ---- Table ----
        elif block_type == "table":
            table_md = self._parse_table(block_id)
            if table_md:
                lines.append(table_md)
            # table_row children are consumed inside _parse_table; skip here
            return "\n".join(lines)

        elif block_type == "table_row":
            # Rendered by parent table block; skip standalone
            return ""

        # ---- Layout blocks (column_list / column) ----
        elif block_type in ("column_list", "column"):
            child_parts: list[str] = []
            for child_id in self._get_children(block_id):
                child_md = self._parse_block(child_id, depth)
                if child_md:
                    child_parts.append(child_md)
            return "\n\n".join(child_parts)

        # ---- Synced block ----
        elif block_type == "synced_block":
            # May reference another block via format.synced_from_pointer
            synced_from = (
                value.get("format", {}).get("synced_from_pointer", {}) or {}
            )
            source_id = synced_from.get("id")
            if source_id and source_id in self._record_map:
                return self._parse_block(source_id, depth)
            # Otherwise render own children
            child_parts = []
            for child_id in self._get_children(block_id):
                child_md = self._parse_block(child_id, depth)
                if child_md:
                    child_parts.append(child_md)
            return "\n\n".join(child_parts)

        # ---- Child page / database ----
        elif block_type == "child_page":
            title = self._get_title(value)
            lines.append(f"## Subpage: {title}")

        elif block_type == "child_database":
            title = self._get_title(value)
            lines.append(f"[Database: {title}]")

        # ---- Embed ----
        elif block_type == "embed":
            source_prop = value.get("properties", {}).get("source", [])
            url = ""
            if source_prop and isinstance(source_prop, list) and source_prop[0]:
                first = source_prop[0]
                url = first[0] if isinstance(first, list) and first else ""
            lines.append(f"[Embed: {url}]")

        # ---- Link to page ----
        elif block_type == "link_to_page":
            page_pointer = value.get("properties", {}).get("page_id", [])
            linked_id = ""
            if page_pointer and isinstance(page_pointer, list) and page_pointer[0]:
                first = page_pointer[0]
                linked_id = first[0] if isinstance(first, list) and first else ""
            lines.append(f"[Link to page: {linked_id}]")

        # ---- Skipped blocks ----
        elif block_type in ("breadcrumb", "table_of_contents"):
            return ""

        # ---- Template ----
        elif block_type == "template":
            child_parts = []
            for child_id in self._get_children(block_id):
                child_md = self._parse_block(child_id, depth)
                if child_md:
                    child_parts.append(child_md)
            return "\n\n".join(child_parts)

        # ---- Fallback ----
        else:
            if block_type:
                lines.append(f"[Unsupported block: {block_type}]")

        # Render children for block types that haven't already done so
        if block_type not in (
            "bulleted_list",
            "numbered_list",
            "to_do",
            "toggle",
            "column_list",
            "column",
            "table",
            "table_row",
            "synced_block",
            "template",
        ):
            for child_id in self._get_children(block_id):
                child_md = self._parse_block(child_id, depth)
                if child_md:
                    lines.append(child_md)

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self) -> str:
        """
        Main entry point: fetch page data, walk the block tree,
        and return a complete Markdown string.
        """
        print(f"Fetching page: {self.page_url}", file=sys.stderr)
        print(f"Page ID (UUID): {self.page_id}", file=sys.stderr)

        self._record_map = self.fetch_page_data()
        print(
            f"Loaded {len(self._record_map)} blocks.", file=sys.stderr
        )

        # Fetch nested blocks not returned by loadPageChunk (e.g. toggle children)
        self._fetch_missing_blocks(self._record_map)
        print(
            f"Total blocks after fetching nested content: {len(self._record_map)}",
            file=sys.stderr,
        )

        # Find the root page block (matches our page_id)
        root_id = self.page_id
        if root_id not in self._record_map:
            # Try without dashes
            root_id_nodash = self.page_id.replace("-", "")
            # Search for a block whose id matches
            for bid, bdata in self._record_map.items():
                bid_clean = bid.replace("-", "")
                if bid_clean == root_id_nodash:
                    root_id = bid
                    break
            else:
                raise RuntimeError(
                    f"Root page block not found in recordMap. "
                    f"Page ID: {self.page_id}"
                )

        # Render root block (page title) then its children
        root_value = self._get_block_value(root_id)
        title = self._get_title(root_value)
        sections: list[str] = [f"# {title}"]

        for child_id in self._get_children(root_id):
            block_md = self._parse_block(child_id, depth=0)
            if block_md:
                sections.append(block_md)

        # Join sections with blank lines, clean up excessive blank lines
        output = "\n\n".join(sections)
        output = re.sub(r"\n{3,}", "\n\n", output)
        return output.strip() + "\n"

    def save(self, output_path: str) -> None:
        """Parse the page and write the result to a file."""
        content = self.parse()
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"Saved to: {output_path}", file=sys.stderr)


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse a public Notion page and output GitHub-Flavored Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python parser.py https://technostrife.notion.site/70e536c9c1dc4cd3a0f23d21d9e1a645
  python parser.py <url> --output output.md
  python parser.py <url> --format plain
        """,
    )
    parser.add_argument(
        "url",
        help="Public Notion page URL (notion.site or notion.so)",
    )
    parser.add_argument(
        "--output",
        "-o",
        metavar="FILE",
        help="Write output to FILE instead of stdout",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "plain"],
        default="markdown",
        help="Output format: 'markdown' (default) or 'plain' (strip formatting)",
    )

    args = parser.parse_args()

    try:
        notion_parser = NotionPageParser(args.url)
        result = notion_parser.parse()

        if args.format == "plain":
            # Strip common Markdown formatting for plain text output
            result = re.sub(r"\*\*(.+?)\*\*", r"\1", result)
            result = re.sub(r"_(.+?)_", r"\1", result)
            result = re.sub(r"~~(.+?)~~", r"\1", result)
            result = re.sub(r"`(.+?)`", r"\1", result)
            result = re.sub(r"^#{1,6}\s+", "", result, flags=re.MULTILINE)
            result = re.sub(r"^[-*]\s+", "", result, flags=re.MULTILINE)
            result = re.sub(r"^\d+\.\s+", "", result, flags=re.MULTILINE)
            result = re.sub(r"^>\s+", "", result, flags=re.MULTILINE)
            result = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", result)
            result = re.sub(r"\$\$[\s\S]+?\$\$", "", result)
            result = re.sub(r"\$(.+?)\$", r"\1", result)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(result)
            print(f"Saved to: {args.output}", file=sys.stderr)
        else:
            print(result, end="")

    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
