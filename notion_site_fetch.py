import re
import sys
import time
from urllib.parse import unquote, urlparse

import httpx


NOTION_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (notion-reader)",
}
MAX_RETRIES = 8
MAX_BACKOFF_SECONDS = 10.0


def notion_post(
    http_client: httpx.Client, endpoint_url: str, json_payload: dict
) -> dict:
    last_error: Exception | None = None
    for attempt_index in range(MAX_RETRIES):
        try:
            response = http_client.post(
                endpoint_url, headers=NOTION_HEADERS, json=json_payload
            )
            # Notion's public API sporadically returns 502 MemcachedCrossCellError
            # in waves that last 30+ seconds. Back off generously so a single
            # CLI invocation can ride one out instead of failing the user.
            if response.status_code >= 500:
                last_error = httpx.HTTPStatusError(
                    f"{response.status_code} from {endpoint_url}: {response.text[:200]}",
                    request=response.request,
                    response=response,
                )
            else:
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as transport_error:
            last_error = transport_error
        if attempt_index < MAX_RETRIES - 1:
            time.sleep(min(2.0 * (attempt_index + 1), MAX_BACKOFF_SECONDS))
    assert last_error is not None
    raise last_error


def pick_api_base(target_url: str) -> str:
    parsed_url = urlparse(target_url)
    hostname = (parsed_url.hostname or "").lower()
    # Public notion.site subdomains are pinned to the right Notion cell, so calls
    # against the subdomain avoid the cross-cell memcached errors that hit www.notion.so.
    if hostname.endswith(".notion.site"):
        return f"https://{hostname}/api/v3"
    return "https://www.notion.so/api/v3"


def resolve_page_and_space(
    target_url: str, http_client: httpx.Client, api_base: str
) -> tuple[str, str | None]:
    parsed_url = urlparse(target_url)
    hostname = parsed_url.hostname or ""
    path = unquote(parsed_url.path or "/")

    # If the URL itself contains a 32-char page id, skip the spaceDomain
    # lookup entirely. loadCachedPageChunkV2 only needs the page id, and
    # the spaceId surfaces in its response (used later for syncRecordValues).
    hex_only_path = re.sub(r"[^0-9a-fA-F]", "", path)
    direct_match = re.search(r"([0-9a-fA-F]{32})$", hex_only_path)
    if direct_match:
        raw_id = direct_match.group(1).lower()
        return (
            f"{raw_id[0:8]}-{raw_id[8:12]}-{raw_id[12:16]}-{raw_id[16:20]}-{raw_id[20:32]}",
            None,
        )

    if not hostname.endswith(".notion.site"):
        raise ValueError(
            f"Cannot resolve page id from URL: {target_url!r}. "
            "Expected a *.notion.site host or a URL ending in a 32-char page id."
        )

    space_subdomain = hostname.split(".")[0]

    space_lookup_data = notion_post(
        http_client,
        f"{api_base}/getPublicPageData",
        {"spaceDomain": space_subdomain},
    )
    space_id = space_lookup_data["spaceId"]

    space_details_data = notion_post(
        http_client,
        f"{api_base}/getPublicSpaceData",
        {"type": "space-ids", "spaceIds": [space_id]},
    )
    space_details_results = space_details_data.get("results") or []
    if not space_details_results or not space_details_results[0].get("publicHomePage"):
        raise RuntimeError(
            f"Space {space_subdomain!r} has no public home page set."
        )
    return space_details_results[0]["publicHomePage"], space_id


def fetch_all_blocks(
    root_page_id: str,
    http_client: httpx.Client,
    api_base: str,
    space_id: str | None,
) -> dict:
    accumulated_blocks: dict[str, dict] = {}
    cursor_state = {"stack": []}
    chunk_index = 0
    while True:
        chunk_data = notion_post(
            http_client,
            f"{api_base}/loadCachedPageChunkV2",
            {
                "page": {"id": root_page_id},
                "limit": 100,
                "cursor": cursor_state,
                "chunkNumber": chunk_index,
                "verticalColumns": False,
            },
        )
        block_records = chunk_data.get("recordMap", {}).get("block", {})
        for block_id, block_record in block_records.items():
            block_value = block_record.get("value", {}).get("value")
            if block_value:
                accumulated_blocks[block_id] = block_value
            if space_id is None:
                space_id = block_record.get("spaceId") or (
                    block_value.get("space_id") if block_value else None
                )

        response_cursors = chunk_data.get("cursors") or []
        next_cursor = response_cursors[0] if response_cursors else None
        if not next_cursor or not next_cursor.get("stack"):
            break
        cursor_state = {"stack": next_cursor["stack"]}
        chunk_index += 1

    # Toggles (and similar lazy-loaded blocks) reference children that aren't
    # returned by loadCachedPageChunkV2. Fetch them in batches until closed.
    if space_id is None:
        return accumulated_blocks
    while True:
        missing_block_ids = {
            child_id
            for block in accumulated_blocks.values()
            for child_id in (block.get("content") or [])
            if child_id not in accumulated_blocks
        }
        if not missing_block_ids:
            break
        for batch_start in range(0, len(missing_block_ids), 100):
            batch_ids = list(missing_block_ids)[batch_start : batch_start + 100]
            sync_data = notion_post(
                http_client,
                "https://www.notion.so/api/v3/syncRecordValues",
                {
                    "requests": [
                        {
                            "pointer": {
                                "table": "block",
                                "id": block_id,
                                "spaceId": space_id,
                            },
                            "version": -1,
                        }
                        for block_id in batch_ids
                    ]
                },
            )
            for block_id, block_record in (
                sync_data.get("recordMap", {}).get("block", {}).items()
            ):
                block_value = block_record.get("value", {}).get("value")
                if block_value:
                    accumulated_blocks[block_id] = block_value
            # Stop if we got nothing new in this batch (avoid infinite loop).
            if not any(bid in accumulated_blocks for bid in batch_ids):
                return accumulated_blocks

    return accumulated_blocks


def render_rich_text(rich_text_runs) -> str:
    if not rich_text_runs:
        return ""
    output_segments: list[str] = []
    for run in rich_text_runs:
        if not run:
            continue
        plain_text = run[0] if len(run) > 0 else ""
        format_decorations = run[1] if len(run) > 1 else []
        link_url: str | None = None
        is_bold = is_italic = is_strikethrough = is_code = False
        for decoration in format_decorations or []:
            if not decoration:
                continue
            tag = decoration[0]
            if tag == "b":
                is_bold = True
            elif tag == "i":
                is_italic = True
            elif tag == "s":
                is_strikethrough = True
            elif tag == "c":
                is_code = True
            elif tag == "a" and len(decoration) > 1:
                link_url = decoration[1]
        styled_text = plain_text
        if is_code:
            styled_text = f"`{styled_text}`"
        else:
            if is_bold:
                styled_text = f"**{styled_text}**"
            if is_italic:
                styled_text = f"*{styled_text}*"
            if is_strikethrough:
                styled_text = f"~~{styled_text}~~"
        if link_url:
            styled_text = f"[{styled_text}]({link_url})"
        output_segments.append(styled_text)
    return "".join(output_segments)


def render_block_lines(
    block_id: str,
    all_blocks: dict,
    indent_level: int,
    sibling_list_position: int,
) -> list[str]:
    block_value = all_blocks.get(block_id)
    if not block_value:
        return []
    block_type = block_value.get("type")
    title_rich_text = (block_value.get("properties") or {}).get("title") or []
    rendered_title = render_rich_text(title_rich_text)
    indent_prefix = "    " * indent_level
    output_lines: list[str] = []

    if block_type == "page":
        if rendered_title:
            output_lines.append(f"# {rendered_title}")
            output_lines.append("")
    elif block_type == "header":
        output_lines.append(f"{indent_prefix}## {rendered_title}")
        output_lines.append("")
    elif block_type == "sub_header":
        output_lines.append(f"{indent_prefix}### {rendered_title}")
        output_lines.append("")
    elif block_type == "sub_sub_header":
        output_lines.append(f"{indent_prefix}#### {rendered_title}")
        output_lines.append("")
    elif block_type == "text":
        output_lines.append(f"{indent_prefix}{rendered_title}")
        output_lines.append("")
    elif block_type == "bulleted_list":
        output_lines.append(f"{indent_prefix}- {rendered_title}")
    elif block_type == "numbered_list":
        output_lines.append(f"{indent_prefix}{sibling_list_position}. {rendered_title}")
    elif block_type == "to_do":
        is_checked = (block_value.get("properties") or {}).get("checked") == [["Yes"]]
        checkbox_marker = "[x]" if is_checked else "[ ]"
        output_lines.append(f"{indent_prefix}- {checkbox_marker} {rendered_title}")
    elif block_type == "toggle":
        output_lines.append(f"{indent_prefix}- {rendered_title}")
    elif block_type == "quote":
        output_lines.append(f"{indent_prefix}> {rendered_title}")
        output_lines.append("")
    elif block_type == "callout":
        emoji_icon = (block_value.get("format") or {}).get("page_icon", "")
        leading_marker = f"{emoji_icon} " if emoji_icon and not emoji_icon.startswith("/") else ""
        output_lines.append(f"{indent_prefix}> {leading_marker}{rendered_title}")
        output_lines.append("")
    elif block_type == "divider":
        output_lines.append(f"{indent_prefix}---")
        output_lines.append("")
    elif block_type == "code":
        code_language = render_rich_text(
            (block_value.get("properties") or {}).get("language") or []
        ).lower()
        output_lines.append(f"{indent_prefix}```{code_language}")
        for code_line in rendered_title.split("\n"):
            output_lines.append(f"{indent_prefix}{code_line}")
        output_lines.append(f"{indent_prefix}```")
        output_lines.append("")
    elif block_type == "image":
        image_source_runs = (block_value.get("properties") or {}).get("source") or []
        image_caption_runs = (block_value.get("properties") or {}).get("caption") or []
        image_url = render_rich_text(image_source_runs)
        image_caption = render_rich_text(image_caption_runs)
        if image_url:
            output_lines.append(f"{indent_prefix}![{image_caption}]({image_url})")
            output_lines.append("")
    elif block_type in ("bookmark", "video", "embed", "file", "pdf", "audio"):
        link_runs = (block_value.get("properties") or {}).get("source") or (
            (block_value.get("properties") or {}).get("link") or []
        )
        link_url = render_rich_text(link_runs)
        if link_url:
            output_lines.append(f"{indent_prefix}[{rendered_title or link_url}]({link_url})")
            output_lines.append("")
    elif block_type == "equation":
        equation_expression = render_rich_text(
            (block_value.get("properties") or {}).get("title") or []
        )
        output_lines.append(f"{indent_prefix}$${equation_expression}$$")
        output_lines.append("")
    elif block_type in ("column_list", "column"):
        pass
    elif block_type in ("table_of_contents", "breadcrumb"):
        pass
    elif rendered_title:
        output_lines.append(f"{indent_prefix}{rendered_title}")
        output_lines.append("")

    child_block_ids = block_value.get("content") or []
    # Only list-like blocks indent their descendants (to form sub-lists).
    nests_descendants = block_type in ("bulleted_list", "numbered_list", "to_do", "toggle")
    child_indent_level = indent_level + 1 if nests_descendants else indent_level
    if block_type == "page":
        child_indent_level = 0

    numbered_position_counter = 0
    previous_child_type: str | None = None
    for child_block_id in child_block_ids:
        child_block_value = all_blocks.get(child_block_id) or {}
        child_block_type = child_block_value.get("type")
        if child_block_type == "numbered_list":
            if previous_child_type == "numbered_list":
                numbered_position_counter += 1
            else:
                numbered_position_counter = 1
        else:
            numbered_position_counter = 0
        output_lines.extend(
            render_block_lines(
                child_block_id,
                all_blocks,
                child_indent_level,
                numbered_position_counter,
            )
        )
        previous_child_type = child_block_type

    return output_lines


def collapse_blank_lines(rendered_lines: list[str]) -> str:
    output_buffer: list[str] = []
    previous_was_blank = False
    for line in rendered_lines:
        is_blank = line.strip() == ""
        if is_blank and previous_was_blank:
            continue
        output_buffer.append(line)
        previous_was_blank = is_blank
    return "\n".join(output_buffer).rstrip() + "\n"


HELP_TEXT = """\
notion-site-fetch — print a public Notion page as Markdown to stdout.

USAGE
    notion-site-fetch <url>

Converts any publicly-readable Notion page into Markdown. No API token,
no login, no headless browser — just plain HTTPS calls to Notion's
public endpoints. If the page loads in an anonymous browser tab, this
tool can fetch it.

ACCEPTED URLS
    https://<sub>.notion.site/                      site root (public home page)
    https://<sub>.notion.site/<slug-or-pageid>      any page on a public site
    https://www.notion.so/<...>-<32-char-page-id>   notion.so page URL

OUTPUT
    Markdown is written to stdout. Use shell redirection to save:
        notion-site-fetch <url> > page.md
        notion-site-fetch <url> >> notes.md
    Errors go to stderr; exit code is non-zero on failure.

NOTES
    - Only the requested page is fetched. Sub-pages linked from it
      remain as links — run the tool again with each sub-page URL.
    - Toggle/dropdown contents are expanded inline (no hidden text).
    - Private or login-required pages fail with a clear error message.
"""


def main() -> int:
    is_help_request = sys.argv[1:2] in (["-h"], ["--help"])
    if is_help_request:
        sys.stdout.write(HELP_TEXT)
        return 0
    if len(sys.argv) != 2:
        sys.stderr.write(HELP_TEXT)
        return 1
    target_url = sys.argv[1]

    api_base = pick_api_base(target_url)
    with httpx.Client(timeout=30.0, follow_redirects=True) as http_client:
        try:
            root_page_id, space_id = resolve_page_and_space(
                target_url, http_client, api_base
            )
            all_blocks = fetch_all_blocks(
                root_page_id, http_client, api_base, space_id
            )
        except (ValueError, RuntimeError, httpx.HTTPError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    if root_page_id not in all_blocks:
        print(f"error: root page {root_page_id} not found in response", file=sys.stderr)
        return 1

    rendered_lines = render_block_lines(root_page_id, all_blocks, 0, 0)
    sys.stdout.write(collapse_blank_lines(rendered_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
