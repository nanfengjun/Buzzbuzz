#!/usr/bin/env python3
"""Resumable batch crawler for the notes listed in notes_manifest.jsonl.

This script does not use an LLM. It:
  1. Reads a deduplicated note manifest.
  2. Fetches each note through the local Xiaohongshu MCP endpoint.
  3. Caches the raw payload and renders Markdown with render_note_markdown.py.
  4. Records status after every note for interruption-safe resume.

The crawler intentionally uses one worker. Run it only after explicit approval.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from render_note_markdown import (
    DEFAULT_MCP_URL,
    McpClient,
    McpError,
    call_get_feed_detail,
    extract_tool_text,
    render_markdown,
    unwrap_note_payload,
    write_atomic,
)
from profile_manifest import (
    BloggerProfile,
    collect_all_notes,
    resolve_blogger,
    safe_account_name,
    write_manifest,
)


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = Path("/Users/kingdee/rednote")
SHANGHAI = ZoneInfo("Asia/Shanghai")
STATE_VERSION = 1
NOTE_ID_IN_MARKDOWN = re.compile(r"^> 笔记 ID：([A-Za-z0-9_-]+)\s*$", re.MULTILINE)
FATAL_ERROR_PATTERNS = (
    "未登录",
    "登录失效",
    "请重新登录",
    "请先登录",
    "验证码",
    "访问频繁",
    "操作频繁",
    "风控",
    "captcha",
    "HTTP 429",
    "too many requests",
    "blocked",
)
UNAVAILABLE_ERROR_PATTERNS = (
    "笔记不可访问",
    "内容不存在",
    "笔记不存在",
    "笔记已被删除",
    "已失效",
    "私密笔记",
    "仅作者可见",
    "因用户设置，你无法查看",
)


@dataclass(frozen=True)
class ManifestNote:
    ordinal: int
    note_id: str
    xsec_token: str
    title_hint: str


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def load_manifest(path: Path) -> list[ManifestNote]:
    notes: list[ManifestNote] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid manifest JSON at line {line_number}: {exc}") from exc

        note_id = str(row.get("note_id") or "").strip()
        xsec_token = str(row.get("xsec_token") or "").strip()
        if not note_id:
            raise ValueError(f"Manifest line {line_number} has no note_id")
        if not xsec_token:
            raise ValueError(f"Manifest line {line_number} has no xsec_token")
        if note_id in seen:
            continue

        seen.add(note_id)
        notes.append(
            ManifestNote(
                ordinal=len(notes) + 1,
                note_id=note_id,
                xsec_token=xsec_token,
                title_hint=str(row.get("title_hint") or "").strip(),
            )
        )

    if not notes:
        raise ValueError(f"Manifest contains no usable notes: {path}")
    return notes


def scan_existing_markdown(output_dir: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in sorted(output_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\n## 评论\n" in text:
            # The crawler now produces note-only Markdown. Treat older
            # comment-containing files as needing a note-only refresh.
            continue
        matches = NOTE_ID_IN_MARKDOWN.findall(text)
        if matches:
            found.setdefault(matches[-1], path.resolve())
    return found


def empty_state(manifest_path: Path, notes: list[ManifestNote]) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "manifest": str(manifest_path.resolve()),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "notes": {
            note.note_id: {
                "ordinal": note.ordinal,
                "note_id": note.note_id,
                "title_hint": note.title_hint,
                "status": "pending",
                "attempts": 0,
                "started_at": None,
                "completed_at": None,
                "md_path": None,
                "raw_path": None,
                "error": None,
            }
            for note in notes
        },
    }


def load_or_initialize_state(
    state_path: Path,
    manifest_path: Path,
    notes: list[ManifestNote],
) -> dict[str, Any]:
    if not state_path.exists():
        state = empty_state(manifest_path, notes)
        save_state(state_path, state)
        return state

    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("version") != STATE_VERSION:
        raise ValueError(
            f"Unsupported state version {state.get('version')!r}; expected {STATE_VERSION}"
        )

    state.setdefault("notes", {})
    for note in notes:
        entry = state["notes"].setdefault(
            note.note_id,
            {
                "ordinal": note.ordinal,
                "note_id": note.note_id,
                "title_hint": note.title_hint,
                "status": "pending",
                "attempts": 0,
                "started_at": None,
                "completed_at": None,
                "md_path": None,
                "raw_path": None,
                "error": None,
            },
        )
        entry["ordinal"] = note.ordinal
        entry["title_hint"] = note.title_hint
        if entry.get("status") == "running":
            entry["status"] = "pending"
            entry["error"] = "上次运行中断；已自动恢复为 pending"

    save_state(state_path, state)
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    status_counts: dict[str, int] = {}
    for entry in state.get("notes", {}).values():
        status = str(entry.get("status") or "pending")
        status_counts[status] = status_counts.get(status, 0) + 1
    state["status_counts"] = status_counts
    atomic_write_text(
        path,
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
    )


def setup_logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("xhs-note-crawler")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(f"Another crawler instance holds the lock: {path}") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} started_at={now_iso()}\n")
    handle.flush()
    return handle


def is_fatal_error(message: str) -> bool:
    lowered = message.lower()
    return any(pattern.lower() in lowered for pattern in FATAL_ERROR_PATTERNS)


def is_unavailable_error(message: str) -> bool:
    return any(pattern in message for pattern in UNAVAILABLE_ERROR_PATTERNS)


def fetch_from_mcp(
    client: McpClient,
    note: ManifestNote,
) -> dict[str, Any]:
    result = call_get_feed_detail(
        client,
        feed_id=note.note_id,
        xsec_token=note.xsec_token,
        max_comments=0,
        include_comments=False,
    )
    text = extract_tool_text(result)
    if not text:
        raise ValueError("get_feed_detail returned no text payload")
    return unwrap_note_payload(text)


def load_raw_payload(path: Path) -> dict[str, Any]:
    return unwrap_note_payload(json.loads(path.read_text(encoding="utf-8")))


def save_raw_payload(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def resolve_markdown_path(
    output_dir: Path,
    rendered_name: str,
    note_id: str,
) -> Path:
    target = output_dir / rendered_name
    if not target.exists():
        return target
    existing_ids = NOTE_ID_IN_MARKDOWN.findall(
        target.read_text(encoding="utf-8", errors="replace")
    )
    if existing_ids and existing_ids[-1] == note_id:
        return target
    safe_note_id = re.sub(r"[^A-Za-z0-9_-]", "_", note_id)
    return target.with_name(f"{target.stem}_{safe_note_id}{target.suffix}")


def note_details_summary(payload: dict[str, Any]) -> dict[str, Any]:
    note = payload["data"]["note"]
    return {
        "comments_collected": False,
        "top_comments": 0,
        "sub_comments": 0,
        "comments_complete": None,
        "platform_comment_count": (
            note.get("interactInfo") or {}
        ).get("commentCount"),
    }


def sleep_interruptibly(seconds: float) -> None:
    time.sleep(max(0.0, seconds))


def random_pause(
    processed_count: int,
    batch_size: int,
    min_delay: float,
    max_delay: float,
    batch_min_delay: float,
    batch_max_delay: float,
) -> tuple[float, str]:
    if processed_count % batch_size == 0:
        return random.uniform(batch_min_delay, batch_max_delay), "batch-cooldown"
    return random.uniform(min_delay, max_delay), "note-cooldown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resumable Xiaohongshu note crawler using the generated manifest."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--red-id",
        help="Xiaohongshu account number, for example 95886436963",
    )
    parser.add_argument(
        "--nickname",
        help="Exact Xiaohongshu account nickname, required with --red-id",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Root output directory (default: {DEFAULT_OUTPUT_ROOT})",
    )
    parser.add_argument(
        "--refresh-manifest",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Scroll the profile and refresh the note manifest before downloading",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Collect and save the complete note manifest, then stop",
    )
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most this many pending notes in this run",
    )
    parser.add_argument("--min-delay", type=float, default=10)
    parser.add_argument("--max-delay", type=float, default=60)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--batch-min-delay", type=float, default=60)
    parser.add_argument("--batch-max-delay", type=float, default=120)
    parser.add_argument("--retry-min-delay", type=float, default=20)
    parser.add_argument("--retry-max-delay", type=float, default=45)
    parser.add_argument("--max-consecutive-failures", type=int, default=3)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect state and print the next tasks without using MCP or sleeping",
    )
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if bool(args.red_id) != bool(args.nickname):
        parser.error("--red-id and --nickname must be provided together")
    if args.min_delay < 0 or args.max_delay < args.min_delay:
        parser.error("require 0 <= --min-delay <= --max-delay")
    if args.batch_min_delay < 0 or args.batch_max_delay < args.batch_min_delay:
        parser.error(
            "require 0 <= --batch-min-delay <= --batch-max-delay"
        )
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if args.attempts <= 0:
        parser.error("--attempts must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)

    try:
        resolved_profile: BloggerProfile | None = None
        if args.red_id and args.nickname:
            resolved_profile = resolve_blogger(
                mcp_url=args.mcp_url,
                timeout=args.timeout,
                red_id=args.red_id,
                nickname=args.nickname,
            )
            account_dir = args.output_root / safe_account_name(
                resolved_profile.nickname
            )
            args.output_dir = args.output_dir or account_dir
            args.manifest = args.manifest or account_dir / "notes_manifest.jsonl"
            args.state_file = (
                args.state_file or account_dir / "crawl_state.json"
            )
            args.raw_dir = args.raw_dir or account_dir / ".cache" / "raw"
            args.log_file = args.log_file or account_dir / "crawl.log"

            should_collect = args.refresh_manifest or not args.manifest.exists()
            if should_collect:
                print(
                    "正在验证并抓取主页笔记清单："
                    f"{resolved_profile.nickname} ({resolved_profile.red_id})"
                )
                collection = collect_all_notes(
                    resolved_profile,
                    timeout=max(args.timeout, 900),
                )
                manifest_path, summary_path = write_manifest(
                    args.output_dir,
                    collection,
                )
                print(
                    f"清单已更新：{manifest_path} "
                    f"({collection.get('unique_count', 0)} 篇)"
                )
                print(f"清单摘要：{summary_path}")
            if args.manifest_only:
                print("仅生成清单，未开始下载笔记。")
                return 0
        else:
            args.output_dir = args.output_dir or BASE_DIR
            args.manifest = args.manifest or BASE_DIR / "notes_manifest.jsonl"
            args.state_file = args.state_file or BASE_DIR / "crawl_state.json"
            args.raw_dir = args.raw_dir or BASE_DIR / ".cache" / "raw"
            args.log_file = args.log_file or BASE_DIR / "crawl.log"

        manifest = load_manifest(args.manifest)
        state = load_or_initialize_state(
            args.state_file,
            args.manifest,
            manifest,
        )
        existing_markdown = scan_existing_markdown(args.output_dir)

        pending: list[ManifestNote] = []
        skipped_success = 0
        for note in manifest:
            entry = state["notes"][note.note_id]
            md_path = entry.get("md_path")
            existing_md_path = Path(md_path) if md_path else None
            if (
                entry.get("status") == "success"
                and existing_md_path
                and existing_md_path.exists()
                and "\n## 评论\n"
                in existing_md_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            ):
                entry["status"] = "pending"
                entry["error"] = (
                    "旧 Markdown 包含评论；当前配置只生成无评论版本"
                )

            if (
                entry.get("status") == "success"
                and existing_md_path
                and existing_md_path.exists()
            ):
                skipped_success += 1
                continue
            if entry.get("status") == "success":
                entry["status"] = "pending"
                entry["error"] = "状态为 success，但 Markdown 文件不存在"

            existing_path = existing_markdown.get(note.note_id)
            if (
                entry.get("status") != "success"
                and existing_path
                and existing_path.exists()
            ):
                entry["status"] = "success"
                entry["completed_at"] = entry.get("completed_at") or now_iso()
                entry["md_path"] = str(existing_path)
                entry["error"] = None
                entry["recovered_from_existing_markdown"] = True
                skipped_success += 1
                continue

            pending.append(note)

        save_state(args.state_file, state)
        pending_before_limit = len(pending)

        if args.limit is not None:
            pending = pending[: args.limit]

        if args.dry_run:
            print(
                json.dumps(
                    {
                        "manifest_notes": len(manifest),
                        "already_successful": skipped_success,
                        "pending_in_manifest": pending_before_limit,
                        "would_process_now": len(pending),
                        "next_note_ids": [note.note_id for note in pending[:10]],
                        "state_file": str(args.state_file.resolve()),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if not pending:
            print("没有待处理笔记；全部可用清单项已成功生成 Markdown。")
            return 0

        logger = setup_logger(args.log_file)
        lock_handle = acquire_lock(args.state_file.with_suffix(".lock"))
        client = McpClient(args.mcp_url, args.timeout)
        processed_this_run = 0
        success_this_run = 0
        failed_this_run = 0
        consecutive_failures = 0
        stop_reason = "completed"
        run_started_at = now_iso()
        state["current_run"] = {
            "started_at": run_started_at,
            "finished_at": None,
            "planned_notes": len(pending),
            "processed": 0,
            "success": 0,
            "failed": 0,
            "stop_reason": None,
        }
        save_state(args.state_file, state)

        try:
            client.initialize()
            login_result = client.call_tool("check_login_status", {})
            login_text = extract_tool_text(login_result)
            if "✅ 已登录" not in login_text:
                raise RuntimeError(f"MCP login check failed: {login_text}")
            logger.info("MCP 登录检查通过，开始处理 %d 篇笔记", len(pending))

            for position, note in enumerate(pending, start=1):
                entry = state["notes"][note.note_id]
                raw_path = args.raw_dir / f"{note.note_id}.json"
                last_error = ""

                for attempt in range(1, args.attempts + 1):
                    entry["status"] = "running"
                    entry["attempts"] = int(entry.get("attempts") or 0) + 1
                    entry["started_at"] = now_iso()
                    entry["completed_at"] = None
                    entry["error"] = None
                    entry["raw_path"] = str(raw_path.resolve())
                    save_state(args.state_file, state)
                    logger.info(
                        "[%d/%d] 开始 %s 《%s》，第 %d 次尝试",
                        position,
                        len(pending),
                        note.note_id,
                        note.title_hint,
                        attempt,
                    )

                    try:
                        if raw_path.exists() and attempt == 1:
                            payload = load_raw_payload(raw_path)
                            logger.info("%s 使用原始 JSON 缓存", note.note_id)
                        else:
                            payload = fetch_from_mcp(
                                client,
                                note,
                            )
                            save_raw_payload(raw_path, payload)
                            logger.info("%s MCP 详情抓取完成", note.note_id)

                        filename, markdown = render_markdown(
                            payload,
                            datetime.now(SHANGHAI),
                            allow_incomplete_comments=False,
                            include_comments=False,
                        )
                        md_path = resolve_markdown_path(
                            args.output_dir,
                            filename,
                            note.note_id,
                        )
                        write_atomic(md_path, markdown, overwrite=True)

                        entry.update(note_details_summary(payload))
                        entry.update(
                            {
                                "status": "success",
                                "completed_at": now_iso(),
                                "md_path": str(md_path.resolve()),
                                "error": None,
                            }
                        )
                        save_state(args.state_file, state)
                        success_this_run += 1
                        consecutive_failures = 0
                        logger.info(
                            "[%d/%d] 成功 %s -> %s",
                            position,
                            len(pending),
                            note.note_id,
                            md_path.name,
                        )
                        break
                    except (OSError, ValueError, McpError) as exc:
                        last_error = str(exc)
                        entry["error"] = last_error
                        save_state(args.state_file, state)
                        logger.warning(
                            "%s 第 %d 次尝试失败：%s",
                            note.note_id,
                            attempt,
                            last_error,
                        )

                        if is_fatal_error(last_error):
                            entry["status"] = "failed"
                            entry["completed_at"] = now_iso()
                            save_state(args.state_file, state)
                            failed_this_run += 1
                            stop_reason = f"fatal-error:{last_error}"
                            raise
                        if is_unavailable_error(last_error):
                            entry["status"] = "unavailable"
                            entry["completed_at"] = now_iso()
                            save_state(args.state_file, state)
                            break
                        if attempt < args.attempts:
                            delay = random.uniform(
                                args.retry_min_delay,
                                args.retry_max_delay,
                            )
                            logger.info(
                                "%s 重试前等待 %.1f 秒", note.note_id, delay
                            )
                            sleep_interruptibly(delay)

                if entry.get("status") not in {"success", "unavailable"}:
                    entry["status"] = "failed"
                    entry["completed_at"] = now_iso()
                    failed_this_run += 1
                    consecutive_failures += 1
                    save_state(args.state_file, state)

                processed_this_run += 1
                state["current_run"].update(
                    {
                        "processed": processed_this_run,
                        "success": success_this_run,
                        "failed": failed_this_run,
                    }
                )
                save_state(args.state_file, state)

                if consecutive_failures >= args.max_consecutive_failures:
                    stop_reason = (
                        f"stopped-after-{consecutive_failures}-consecutive-failures"
                    )
                    logger.error("连续失败过多，停止本次运行")
                    break

                if position < len(pending):
                    delay, kind = random_pause(
                        position,
                        args.batch_size,
                        args.min_delay,
                        args.max_delay,
                        args.batch_min_delay,
                        args.batch_max_delay,
                    )
                    logger.info(
                        "%s：等待 %.1f 秒后处理下一篇",
                        kind,
                        delay,
                    )
                    sleep_interruptibly(delay)

        except KeyboardInterrupt:
            stop_reason = "keyboard-interrupt"
            for entry in state["notes"].values():
                if entry.get("status") == "running":
                    entry["status"] = "pending"
                    entry["error"] = "用户中断；已恢复为 pending"
            save_state(args.state_file, state)
            logger.warning("收到中断，状态已保存；下次运行会从断点继续")
            return 130
        except Exception as exc:
            if stop_reason == "completed":
                stop_reason = f"error:{exc}"
            logger.exception("运行停止：%s", exc)
            return 1
        finally:
            if stop_reason == "completed" and failed_this_run:
                stop_reason = "completed-with-failures"
            state["current_run"].update(
                {
                    "finished_at": now_iso(),
                    "processed": processed_this_run,
                    "success": success_this_run,
                    "failed": failed_this_run,
                    "stop_reason": stop_reason,
                }
            )
            save_state(args.state_file, state)
            logger.info(
                "本次结束：处理 %d，成功 %d，失败 %d，原因 %s",
                processed_this_run,
                success_this_run,
                failed_this_run,
                stop_reason,
            )
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                lock_handle.close()

        return 0 if stop_reason == "completed" else 1

    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
