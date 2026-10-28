#!/usr/bin/env python3
"""Convert Xiaohongshu MCP note details into deterministic Markdown files.

The script only uses the Python standard library.

Examples:
  # Convert an existing MCP JSON-RPC response or its decoded JSON payload.
  python3 render_note_markdown.py response.json --output-dir .

  # Fetch one note through the local xiaohongshu MCP server and convert it.
  python3 render_note_markdown.py \
    --feed-id 6a955c4a000000002303cefa \
    --xsec-token 'AB...' \
    --output-dir .

The MCP response is expected to contain:
  result.content[0].text -> JSON string -> data.note + data.comments
The script also accepts the decoded payload directly.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
TAG_RE = re.compile(r"#([^#\n\[\]]+)\[话题\]#?")
DEFAULT_MCP_URL = "http://127.0.0.1:18060/mcp"


@dataclass
class RenderResult:
    path: Path
    status: str
    comments_collected: bool
    top_comments: int
    sub_comments: int
    platform_comment_count: int | None
    complete: bool


class McpError(RuntimeError):
    """Raised when the local MCP server returns an error."""


def _decode_json_or_sse(body: str) -> dict[str, Any]:
    """Decode a JSON-RPC response from JSON or a text/event-stream body."""
    body = body.strip()
    if not body:
        return {}

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass

    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line.strip() == "" and data_lines:
            candidate = "\n".join(data_lines)
            data_lines.clear()
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)

    if data_lines:
        try:
            value = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            events.append(value)

    if events:
        return events[-1]
    raise ValueError("MCP response is neither JSON nor a supported SSE event stream")


class McpClient:
    """Minimal Streamable HTTP MCP client for xiaohongshu-mcp."""

    def __init__(self, url: str, timeout: float) -> None:
        self.url = url
        self.timeout = timeout
        self.session_id: str | None = None
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _post(
        self,
        payload: dict[str, Any],
        *,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        request = urllib.request.Request(
            self.url,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise McpError(f"MCP HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"Cannot connect to MCP server {self.url}: {exc}") from exc

        if not body.strip():
            if allow_empty:
                return {}
            raise McpError("MCP returned an empty response")

        result = _decode_json_or_sse(body)
        if "error" in result:
            raise McpError(json.dumps(result["error"], ensure_ascii=False))
        return result

    def initialize(self) -> None:
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "xhs-note-to-markdown", "version": "1.0"},
                },
            }
        )
        if "result" not in response:
            raise McpError("MCP initialize response has no result")

        self._post(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            allow_empty=True,
        )

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.session_id:
            raise McpError("MCP client is not initialized")
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpError("MCP tool response has no result object")
        if result.get("isError"):
            raise McpError(extract_tool_text(result) or "MCP tool returned an error")
        return result


def extract_tool_text(tool_result: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in tool_result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def unwrap_note_payload(value: Any) -> dict[str, Any]:
    """Return a payload containing data.note from every supported MCP shape."""
    if isinstance(value, str):
        value = value.strip()
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Response text does not contain valid JSON") from exc

    if not isinstance(value, dict):
        raise ValueError("Unsupported JSON payload")

    if isinstance(value.get("data"), dict) and isinstance(value["data"].get("note"), dict):
        return value

    if isinstance(value.get("structuredContent"), dict):
        structured = value["structuredContent"]
        if isinstance(structured.get("data"), dict) and isinstance(
            structured["data"].get("note"), dict
        ):
            return structured

    if isinstance(value.get("result"), dict):
        return unwrap_note_payload(value["result"])

    if isinstance(value.get("content"), list):
        text = extract_tool_text(value)
        if not text:
            raise ValueError("MCP result.content contains no text JSON")
        return unwrap_note_payload(text)

    raise ValueError(
        "JSON does not match a Xiaohongshu note payload; expected data.note"
    )


def load_payload(path: Path) -> dict[str, Any]:
    text = (
        sys.stdin.read()
        if str(path) == "-"
        else path.read_text(encoding="utf-8")
    )
    return unwrap_note_payload(text)


def fetch_payload(
    *,
    mcp_url: str,
    timeout: float,
    feed_id: str,
    xsec_token: str,
    max_comments: int,
    include_comments: bool,
) -> dict[str, Any]:
    client = McpClient(mcp_url, timeout)
    client.initialize()
    result = call_get_feed_detail(
        client,
        feed_id=feed_id,
        xsec_token=xsec_token,
        max_comments=max_comments,
        include_comments=include_comments,
    )
    text = extract_tool_text(result)
    if not text:
        raise ValueError("get_feed_detail returned no text payload")
    return unwrap_note_payload(text)


def call_get_feed_detail(
    client: McpClient,
    *,
    feed_id: str,
    xsec_token: str,
    max_comments: int,
    include_comments: bool,
) -> dict[str, Any]:
    """Call get_feed_detail across the two schemas used by Xiaohongshu MCP.

    Some versions expose ``limit``/``reply_limit`` and reject additional
    properties. Other versions accept the standalone binary's
    ``max_comment_items``/``max_replies_threshold`` names. Try the extended
    schema first, then automatically fall back to the strict connector schema.
    """
    if not include_comments:
        return client.call_tool(
            "get_feed_detail",
            {
                "feed_id": feed_id,
                "xsec_token": xsec_token,
                "load_all_comments": False,
            },
        )

    common = {
        "feed_id": feed_id,
        "xsec_token": xsec_token,
        "load_all_comments": True,
        "click_more_replies": True,
        "scroll_speed": "normal",
    }
    extended_arguments = {
        **common,
        "limit": max_comments,
        "reply_limit": max_comments,
        "max_comment_items": max_comments,
        "max_replies_threshold": max_comments,
    }
    try:
        return client.call_tool("get_feed_detail", extended_arguments)
    except McpError as exc:
        message = str(exc)
        if not (
            "unexpected additional properties" in message
            or (
                "invalid params" in message
                and (
                    "max_comment_items" in message
                    or "max_replies_threshold" in message
                )
            )
        ):
            raise

    strict_arguments = {
        **common,
        "limit": max_comments,
        "reply_limit": max_comments,
    }
    return client.call_tool("get_feed_detail", strict_arguments)


def format_timestamp(value: int | str | None, *, with_seconds: bool = True) -> str:
    if value in (None, ""):
        return "未公开"
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return "未公开"
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    fmt = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
    return datetime.fromtimestamp(timestamp, SHANGHAI).strftime(fmt)


def protect_plain_text_line(line: str) -> str:
    """Prevent plain note text from being interpreted as Markdown block syntax."""
    escaped: list[str] = []
    for char in line:
        if char == "\\":
            escaped.append("\\\\")
        else:
            escaped.append(char)
    value = "".join(escaped)

    if value.strip() == "-":
        return value.replace("-", "\\-", 1)

    # Escape a block marker only when it starts a line and is followed by space.
    match = re.match(r"^(\s*)(#{1,6}|>|[-+*]|\d+[.)])(?=\s)", value)
    if match:
        value = f"{match.group(1)}\\{value[match.end(1):]}"
    return value


def clean_body(text: str) -> tuple[str, list[str]]:
    text = html.unescape(text or "").replace("\r\n", "\n").replace("\r", "\n")
    tags = [
        re.sub(r"\s+", " ", match).strip()
        for match in TAG_RE.findall(text)
    ]
    body = TAG_RE.sub("", text).replace("\t", "\n")
    protected = [protect_plain_text_line(line.rstrip()) for line in body.splitlines()]
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(protected)).strip()
    return body, list(dict.fromkeys(tags))


def quote_content(text: str) -> str:
    lines = html.unescape(text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    if not lines:
        return "> *（空评论）*"
    return "\n".join(
        f"> {protect_plain_text_line(line)}" if line else ">" for line in lines
    )


def author_name(item: dict[str, Any]) -> str:
    return (item.get("userInfo") or {}).get("nickname") or "未知用户"


def render_subcomment(comment: dict[str, Any], index: int) -> str:
    tags = set(comment.get("showTags") or [])
    role = "（作者）" if "is_author" in tags else ""
    meta = [
        format_timestamp(comment.get("createTime")),
        comment.get("ipLocation") or "地点未公开",
        f"{comment.get('likeCount') or '0'} 赞",
    ]
    return "\n".join(
        [
            f"**子评论 {index} · {author_name(comment)}{role}**",
            "",
            " ｜ ".join(meta),
            "",
            quote_content(comment.get("content", "")),
        ]
    )


def render_parent_comment(comment: dict[str, Any], index: int) -> str:
    tags = set(comment.get("showTags") or [])
    status: list[str] = []
    if "user_top" in tags:
        status.append("置顶")
    if "is_author" in tags:
        status.append("作者")

    meta = [
        format_timestamp(comment.get("createTime")),
        comment.get("ipLocation") or "地点未公开",
        f"{comment.get('likeCount') or '0'} 赞",
    ]
    if status:
        meta.append(" / ".join(status))

    parts = [
        f"### {index}. {author_name(comment)}",
        "",
        " ｜ ".join(meta),
        "",
        quote_content(comment.get("content", "")),
    ]

    subcomments = comment.get("subComments") or []
    if subcomments:
        parts.extend(["", f"**子评论（{len(subcomments)} 条）**", ""])
        for sub_index, subcomment in enumerate(subcomments, start=1):
            parts.append(render_subcomment(subcomment, sub_index))
            if sub_index != len(subcomments):
                parts.extend(["", "---", ""])
    return "\n".join(parts)


def parse_optional_count(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def comment_integrity(
    note: dict[str, Any],
    comments_payload: dict[str, Any],
    comments: list[dict[str, Any]],
) -> tuple[int, int, int | None, bool]:
    subcomments = sum(len(comment.get("subComments") or []) for comment in comments)
    returned_total = len(comments) + subcomments
    platform_total = parse_optional_count(
        (note.get("interactInfo") or {}).get("commentCount")
    )

    complete = not bool(comments_payload.get("hasMore"))
    for comment in comments:
        expected = parse_optional_count(comment.get("subCommentCount"))
        fetched = len(comment.get("subComments") or [])
        if expected is not None and expected > fetched:
            complete = False
            break
    if platform_total is not None and returned_total != platform_total:
        complete = False
    return len(comments), subcomments, platform_total, complete


def markdown_filename(title: str, timestamp: Any) -> str:
    time_part = format_timestamp(timestamp, with_seconds=False)
    safe_title = title
    for char in '<>:"/\\|?*':
        safe_title = safe_title.replace(char, "＿")
    safe_title = re.sub(r"\s+", " ", safe_title).strip().rstrip(".")
    if not safe_title:
        safe_title = "无标题"

    # Keep the complete filename comfortably below common 255-byte limits.
    encoded = safe_title.encode("utf-8")
    if len(encoded) > 180:
        safe_title = encoded[:180].decode("utf-8", errors="ignore").rstrip()
    return f"{time_part}_{safe_title}.md"


def render_markdown(
    payload: dict[str, Any],
    captured_at: datetime,
    *,
    allow_incomplete_comments: bool,
    include_comments: bool,
) -> tuple[str, str]:
    try:
        note = payload["data"]["note"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Payload is missing data.note") from exc

    body, tags = clean_body(note.get("desc", ""))
    title = (note.get("title") or "无标题").strip()

    token = note.get("xsecToken") or ""
    note_url = f"https://www.xiaohongshu.com/explore/{note.get('noteId', '')}"
    if token:
        note_url += "?" + urlencode(
            {"xsec_token": token, "xsec_source": "pc_search"}
        )

    interactions = note.get("interactInfo") or {}
    tag_text = " ".join(f"#{tag}" for tag in tags) if tags else "无"
    location = note.get("ipLocation") or "未公开（笔记接口未返回）"
    note_time = format_timestamp(note.get("time"))

    lines = [
        f"# {title}",
        "",
        f"- 编辑时间（平台笔记时间）：{note_time}",
        f"- 标题：{title}",
        f"- 笔记标签：{tag_text}",
        f"- 发表地点：{location}",
        f"- 作者：{(note.get('user') or {}).get('nickname') or '未知'}",
        f"- 笔记链接：{note_url}",
        (
            "- 互动数据："
            f"点赞 {interactions.get('likedCount') or '0'} ｜ "
            f"收藏 {interactions.get('collectedCount') or '0'} ｜ "
            f"评论 {interactions.get('commentCount') or '0'}"
        ),
        "",
        "## 笔记正文",
        "",
        body or "*（正文为空）*",
    ]

    if include_comments:
        comments_payload = payload["data"].get("comments") or {}
        comments = comments_payload.get("list") or []
        top_count, sub_count, platform_count, complete = comment_integrity(
            note, comments_payload, comments
        )

        if not complete and not allow_incomplete_comments:
            raise ValueError(
                "Comment data is incomplete: "
                f"fetched {top_count} top-level and {sub_count} sub-comments, "
                f"platform count is {platform_count!r}, "
                f"hasMore={comments_payload.get('hasMore')!r}. "
                "Re-fetch with a larger limit or pass --allow-incomplete-comments."
            )

        lines.extend(
            [
                "",
                "## 评论",
                "",
                (
                    f"> 抓取时共 {top_count + sub_count} 条评论："
                    f"{top_count} 条一级评论，{sub_count} 条子评论。"
                    "以下按小红书接口返回顺序排列。"
                ),
            ]
        )
        if not complete:
            lines.extend(
                [
                    ">",
                    f"> **警告：评论数据可能不完整。平台显示 {platform_count!r} 条。**",
                ]
            )
        lines.append("")

        if comments:
            for index, comment in enumerate(comments, start=1):
                lines.append(render_parent_comment(comment, index))
                if index != len(comments):
                    lines.extend(["", "---", ""])
        else:
            lines.append("*暂无可见评论。*")

    lines.extend(
        [
            "",
            "---",
            "",
            f"> 数据抓取时间：{captured_at.strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai）",
            f"> 笔记 ID：{note.get('noteId', '')}",
            "",
        ]
    )
    return markdown_filename(title, note.get("time")), "\n".join(lines)


def write_atomic(path: Path, content: str, *, overwrite: bool) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old_content = path.read_text(encoding="utf-8")
        if old_content == content:
            return "unchanged"
        if not overwrite:
            return "skipped-existing"

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return "written"


def parse_captured_at(value: str | None) -> datetime:
    if not value:
        return datetime.now(SHANGHAI)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert Xiaohongshu MCP get_feed_detail responses to Markdown "
            "without using an LLM."
        )
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="MCP JSON-RPC response files or decoded payloads; use - for stdin",
    )
    parser.add_argument(
        "--feed-id",
        help="Fetch this note from MCP instead of reading input files",
    )
    parser.add_argument(
        "--xsec-token",
        help="xsec_token required together with --feed-id",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory for generated Markdown (default: current directory)",
    )
    parser.add_argument(
        "--mcp-url",
        default=DEFAULT_MCP_URL,
        help=f"Local MCP endpoint (default: {DEFAULT_MCP_URL})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="MCP request timeout in seconds (default: 300)",
    )
    parser.add_argument(
        "--max-comments",
        type=int,
        default=10000,
        help="Maximum comments requested when --include-comments is used",
    )
    parser.add_argument(
        "--include-comments",
        action="store_true",
        help="Download and include comments (disabled by default)",
    )
    parser.add_argument(
        "--captured-at",
        help="Capture timestamp, for example 2026-10-22T16:56:07+08:00",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing Markdown file when its content differs",
    )
    parser.add_argument(
        "--allow-incomplete-comments",
        action="store_true",
        help="Write the file even when comment counts or hasMore indicate truncation",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print Markdown instead of writing a file (one input only)",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if bool(args.feed_id) != bool(args.xsec_token):
        parser.error("--feed-id and --xsec-token must be provided together")
    if args.feed_id and args.inputs:
        parser.error("use either input files or --feed-id/--xsec-token, not both")
    if not args.feed_id and not args.inputs:
        parser.error("provide an input JSON file or --feed-id with --xsec-token")
    if args.stdout and len(args.inputs) > 1:
        parser.error("--stdout supports exactly one input")

    captured_at = parse_captured_at(args.captured_at)
    payloads: list[tuple[str, dict[str, Any]]] = []

    try:
        if args.feed_id:
            payloads.append(
                (
                    args.feed_id,
                    fetch_payload(
                        mcp_url=args.mcp_url,
                        timeout=args.timeout,
                        feed_id=args.feed_id,
                        xsec_token=args.xsec_token,
                        max_comments=args.max_comments,
                        include_comments=args.include_comments,
                    ),
                )
            )
        else:
            for input_path in args.inputs:
                payloads.append((str(input_path), load_payload(input_path)))

        results: list[RenderResult] = []
        for source, payload in payloads:
            filename, markdown = render_markdown(
                payload,
                captured_at,
                allow_incomplete_comments=args.allow_incomplete_comments,
                include_comments=args.include_comments,
            )
            if args.include_comments:
                note = payload["data"]["note"]
                comments_payload = payload["data"].get("comments") or {}
                comments = comments_payload.get("list") or []
                top_count, sub_count, platform_count, complete = comment_integrity(
                    note, comments_payload, comments
                )
            else:
                platform_count = parse_optional_count(
                    (payload["data"]["note"].get("interactInfo") or {}).get(
                        "commentCount"
                    )
                )
                top_count = 0
                sub_count = 0
                complete = True

            if args.stdout:
                sys.stdout.write(markdown)
                continue

            output_path = args.output_dir / filename
            status = write_atomic(output_path, markdown, overwrite=args.overwrite)
            results.append(
                RenderResult(
                    path=output_path,
                    status=status,
                    comments_collected=args.include_comments,
                    top_comments=top_count,
                    sub_comments=sub_count,
                    platform_comment_count=platform_count,
                    complete=complete,
                )
            )

        for result in results:
            print(
                json.dumps(
                    {
                        "path": str(result.path),
                        "status": result.status,
                        "comments_collected": result.comments_collected,
                        "top_comments": result.top_comments,
                        "sub_comments": result.sub_comments,
                        "total_comments": result.top_comments + result.sub_comments,
                        "platform_comment_count": result.platform_comment_count,
                        "complete": result.complete,
                    },
                    ensure_ascii=False,
                )
            )
    except (OSError, ValueError, McpError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
