#!/usr/bin/env python3
"""Resolve a Xiaohongshu blogger and collect a complete note manifest."""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from render_note_markdown import (
    McpClient,
    McpError,
    extract_tool_text,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
MANIFEST_RESULT_PREFIX = "XHS_MANIFEST_RESULT="


@dataclass(frozen=True)
class BloggerProfile:
    red_id: str
    nickname: str
    internal_user_id: str
    xsec_token: str
    profile_url: str
    initial_notes: list[dict[str, Any]]


def _tool_json(client: McpClient, name: str, arguments: dict[str, Any]) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            result = client.call_tool(name, arguments)
            break
        except McpError as exc:
            last_error = exc
            message = str(exc).lower()
            retryable = any(
                marker in message
                for marker in (
                    "context deadline exceeded",
                    "timeout",
                    "temporarily unavailable",
                    "connection reset",
                )
            )
            if not retryable or attempt == 2:
                raise
            time.sleep(5)
    else:
        raise RuntimeError(f"{name} failed: {last_error}")
    text = extract_tool_text(result)
    if not text:
        raise ValueError(f"{name} returned no JSON text")
    return json.loads(text)


def _search_user_candidates(
    client: McpClient,
    keyword: str,
) -> dict[str, dict[str, Any]]:
    payload = _tool_json(
        client,
        "search_feeds",
        {
            "keyword": keyword,
            "filters": {
                "sort_by": "综合",
                "note_type": "不限",
                "publish_time": "不限",
                "search_scope": "不限",
                "location": "不限",
            },
        },
    )
    candidates: dict[str, dict[str, Any]] = {}
    for feed in payload.get("feeds") or []:
        card = feed.get("noteCard") or {}
        user = card.get("user") or {}
        user_id = str(user.get("userId") or user.get("user_id") or "").strip()
        nickname = str(user.get("nickname") or user.get("nickName") or "").strip()
        token = str(feed.get("xsecToken") or card.get("xsecToken") or "").strip()
        if not user_id or not nickname or not token:
            continue
        candidates.setdefault(
            user_id,
            {
                "internal_user_id": user_id,
                "nickname": nickname,
                "xsec_token": token,
            },
        )
    return candidates


def resolve_blogger(
    *,
    mcp_url: str,
    timeout: float,
    red_id: str,
    nickname: str,
) -> BloggerProfile:
    """Confirm the account by nickname, search result, and profile redId."""
    red_id = red_id.strip()
    nickname = nickname.strip()
    if not red_id or not nickname:
        raise ValueError("red_id and nickname are both required")

    client = McpClient(mcp_url, timeout)
    client.initialize()

    candidates = _search_user_candidates(client, red_id)
    exact = {
        user_id: candidate
        for user_id, candidate in candidates.items()
        if candidate["nickname"].casefold() == nickname.casefold()
    }
    if not exact:
        candidates = _search_user_candidates(client, nickname)
        exact = {
            user_id: candidate
            for user_id, candidate in candidates.items()
            if candidate["nickname"].casefold() == nickname.casefold()
        }
    if not exact:
        raise ValueError(
            f"Could not find blogger {nickname!r} from Xiaohongshu ID {red_id!r}. "
            "Check that both values are correct."
        )

    failures: list[str] = []
    for candidate in list(exact.values())[:5]:
        user_id = candidate["internal_user_id"]
        try:
            profile = _tool_json(
                client,
                "user_profile",
                {
                    "user_id": user_id,
                    "xsec_token": candidate["xsec_token"],
                    "tab": "note",
                },
            )
            basic = profile.get("userBasicInfo") or {}
            profile_red_id = str(basic.get("redId") or basic.get("red_id") or "")
            profile_nickname = str(
                basic.get("nickname") or basic.get("nickName") or nickname
            )
            if profile_red_id != red_id:
                failures.append(
                    f"{user_id}: profile redId={profile_red_id!r}"
                )
                continue
            token = candidate["xsec_token"]
            feeds = profile.get("feeds") or []
            if feeds and feeds[0].get("xsecToken"):
                token = str(feeds[0]["xsecToken"])
            query = urllib.parse.urlencode(
                {"xsec_token": token, "xsec_source": "pc_note"}
            )
            return BloggerProfile(
                red_id=red_id,
                nickname=profile_nickname,
                internal_user_id=user_id,
                xsec_token=token,
                profile_url=(
                    f"https://www.xiaohongshu.com/user/profile/{user_id}?{query}"
                ),
                initial_notes=feeds,
            )
        except (McpError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{user_id}: {exc}")

    detail = "; ".join(failures[-3:])
    raise ValueError(
        f"Search found nickname {nickname!r}, but the profile redId did not "
        f"match {red_id!r}. {detail}"
    )


def _browser_script(profile: BloggerProfile, task_name: str) -> str:
    profile_url = json.dumps(profile.profile_url, ensure_ascii=False)
    expected_nickname = json.dumps(profile.nickname, ensure_ascii=False)
    task_name_json = json.dumps(task_name, ensure_ascii=False)
    result_prefix = json.dumps(MANIFEST_RESULT_PREFIX)
    return rf"""
const task = await taskSpace({task_name_json});
const page = task.page("p1");
let finished = false;
try {{
  await page.goto({profile_url});
  await page.waitForLoadState("domcontentloaded");
  await page.waitForFunction(
    () => window.__INITIAL_STATE__?.user,
    undefined,
    {{ timeout: 30000 }}
  );

  const login = await page.evaluate(() => {{
    const unwrap = (o) => (o && o.value !== undefined) ? o.value : (o && o._value);
    const u = window.__INITIAL_STATE__?.user || {{}};
    return {{
      loggedIn: Boolean(unwrap(u.loggedIn)),
      url: String(location.href),
    }};
  }});
  if (!login.loggedIn) {{
    throw new Error("Ego 浏览器未登录小红书，请先登录后重试");
  }}

  await page.waitForFunction(
    () => window.__INITIAL_STATE__?.user?.userPageData,
    undefined,
    {{ timeout: 30000 }}
  );
  const initial = await page.evaluate(() => {{
    const unwrap = (o) => (o && o.value !== undefined) ? o.value : (o && o._value);
    const u = window.__INITIAL_STATE__?.user || {{}};
    const pageData = unwrap(u.userPageData) || {{}};
    const basic = pageData.basicInfo || pageData.basic_info || {{}};
    return {{
      nickname: String(basic.nickname || basic.nickName || ""),
      redId: String(basic.redId || basic.red_id || ""),
      url: String(location.href),
    }};
  }});
  if (initial.nickname !== {expected_nickname}) {{
    throw new Error(
      `主页昵称不符: expected={expected_nickname}, actual=${{initial.nickname}}`
    );
  }}

  const collected = new Map();
  let last = null;
  let stagnant = 0;
  let reachedBottom = false;
  const rounds = [];

  async function snapshot() {{
    return await page.evaluate(() => {{
      const unwrap = (o) => (o && o.value !== undefined) ? o.value : (o && o._value);
      const u = window.__INITIAL_STATE__?.user || {{}};
      const groups = unwrap(u.notes) || [];
      const active = unwrap(u.activeTab) || {{}};
      const feeds = Array.isArray(groups) ? (groups[active.index || 0] || []) : [];
      return {{
        feeds: feeds.map((feed) => {{
          const card = feed?.noteCard || {{}};
          const user = card.user || {{}};
          return {{
            note_id: String(card.noteId || feed?.id || ""),
            xsec_token: String(feed?.xsecToken || card.xsecToken || ""),
            title_hint: String(card.displayTitle || ""),
            note_type: String(card.type || ""),
            source_index: Number.isFinite(feed?.index) ? feed.index : null,
            author_user_id: String(user.userId || user.user_id || ""),
            author_nickname: String(user.nickname || user.nickName || ""),
          }};
        }}),
        state_count: feeds.length,
        scroll_y: window.scrollY,
        document_height: document.documentElement.scrollHeight,
        at_bottom: (
          window.scrollY + window.innerHeight
          >= document.documentElement.scrollHeight - 8
        ),
      }};
    }});
  }}

  function addFeeds(snapshot, round) {{
    let added = 0;
    for (const feed of snapshot.feeds) {{
      if (!feed.note_id || collected.has(feed.note_id)) continue;
      collected.set(feed.note_id, {{
        ...feed,
        first_seen_round: round,
        discovered_at: new Date().toISOString(),
      }});
      added++;
    }}
    return added;
  }}

  last = await snapshot();
  addFeeds(last, 0);
  for (let round = 1; round <= 100; round++) {{
    const beforeCount = collected.size;
    await page.evaluate(() => window.scrollTo(
      0,
      document.documentElement.scrollHeight
    ));
    await page.waitForTimeout(2200);
    last = await snapshot();
    const added = addFeeds(last, round);
    if (collected.size === beforeCount && last.at_bottom) stagnant++;
    else stagnant = 0;
    reachedBottom = stagnant >= 4;
    const progress = {{
      round,
      state_count: last.state_count,
      added,
      unique_count: collected.size,
      at_bottom: last.at_bottom,
    }};
    console.log("XHS_MANIFEST_ROUND=" + JSON.stringify(progress));
    if (reachedBottom) break;
  }}

  const rows = [...collected.values()].sort((a, b) => {{
    const ai = a.source_index == null ? Number.MAX_SAFE_INTEGER : a.source_index;
    const bi = b.source_index == null ? Number.MAX_SAFE_INTEGER : b.source_index;
    return ai - bi;
  }});
  console.log({result_prefix} + JSON.stringify({{
    profile: {{
      red_id: {json.dumps(profile.red_id)},
      nickname: {expected_nickname},
      internal_user_id: {json.dumps(profile.internal_user_id)},
      profile_url: {profile_url},
    }},
    rows,
    reached_bottom: reachedBottom,
    state_count: last?.state_count || 0,
    unique_count: rows.length,
    rounds: rounds.length,
  }}));
}} finally {{
  if (!finished) {{
    await task.finish({{ keep: [] }});
    finished = true;
  }}
}}
"""


def collect_all_notes(
    profile: BloggerProfile,
    *,
    timeout: float = 900,
) -> dict[str, Any]:
    task_name = (
        f"xhs-manifest-{profile.red_id}-{int(time.time())}"
    )
    script = _browser_script(profile, task_name)
    try:
        process = subprocess.run(
            ["ego-browser", "nodejs"],
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ego-browser is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Ego profile collection timed out after {timeout:.0f} seconds"
        ) from exc

    output = process.stdout or ""
    if process.returncode != 0:
        detail = (process.stderr or output).strip()
        raise RuntimeError(f"Ego profile collection failed: {detail}")

    result_line = next(
        (
            line
            for line in reversed(output.splitlines())
            if line.startswith(MANIFEST_RESULT_PREFIX)
        ),
        None,
    )
    if not result_line:
        raise RuntimeError("Ego did not return a manifest result")
    result = json.loads(result_line[len(MANIFEST_RESULT_PREFIX) :])
    if not result.get("reached_bottom"):
        raise RuntimeError(
            "Profile scrolling did not reach a confirmed bottom; manifest is incomplete"
        )
    return result


def safe_account_name(name: str) -> str:
    safe = name.strip()
    for char in '<>:"/\\|?*':
        safe = safe.replace(char, "＿")
    safe = re.sub(r"\s+", " ", safe).strip().rstrip(".")
    if not safe:
        raise ValueError("Blogger nickname cannot be used as a directory name")
    encoded = safe.encode("utf-8")
    if len(encoded) > 180:
        safe = encoded[:180].decode("utf-8", errors="ignore").rstrip()
    return safe


def write_manifest(
    output_dir: Path,
    result: dict[str, Any],
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = result.get("rows") or []
    if not rows:
        raise ValueError("Profile returned no notes")
    if any(not row.get("note_id") for row in rows):
        raise ValueError("Manifest contains rows without note_id")
    if any(not row.get("xsec_token") for row in rows):
        raise ValueError("Manifest contains rows without xsec_token")

    finalized = []
    seen: set[str] = set()
    for row in rows:
        note_id = str(row["note_id"])
        if note_id in seen:
            continue
        seen.add(note_id)
        finalized.append({"ordinal": len(finalized) + 1, **row})

    manifest_path = output_dir / "notes_manifest.jsonl"
    summary_path = output_dir / "notes_manifest.summary.json"
    text = "".join(
        json.dumps(row, ensure_ascii=False) + "\n" for row in finalized
    )
    temporary = manifest_path.with_name(manifest_path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(manifest_path)

    profile = result.get("profile") or {}
    summary = {
        "author_red_id": profile.get("red_id"),
        "author_nickname": profile.get("nickname"),
        "internal_user_id": profile.get("internal_user_id"),
        "profile_url": profile.get("profile_url"),
        "collected_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "total_unique_notes": len(finalized),
        "page_state_records": int(result.get("state_count") or 0),
        "duplicate_state_entries": int(result.get("state_count") or 0)
        - len(finalized),
        "reached_bottom": True,
        "missing_note_ids": sum(not row.get("note_id") for row in finalized),
        "missing_xsec_tokens": sum(
            not row.get("xsec_token") for row in finalized
        ),
    }
    summary_tmp = summary_path.with_name(summary_path.name + ".tmp")
    summary_tmp.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary_tmp.replace(summary_path)
    return manifest_path, summary_path


def merge_manifest_with_state(
    manifest_path: Path,
    state_path: Path,
) -> None:
    """Keep previously successful state entries when refreshing a manifest."""
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.setdefault("notes", {})
    temporary = state_path.with_name(state_path.name + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(state_path)
