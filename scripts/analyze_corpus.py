#!/usr/bin/env python3
"""Analyze a Buzzbuzz Xiaohongshu markdown corpus.

The parser expects the export format used in the source workspace: note
metadata followed by a `## 笔记正文` section. The script is dependency-free so
the resulting perspective skill can be copied and re-run anywhere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Note:
    path: str
    filename: str
    title: str
    published_at: str
    year: int
    month: str
    tags: list[str]
    location: str
    likes: int
    favorites: int
    comments: int
    body: str
    chars: int
    paragraphs: int
    sentences: int
    question_sentences: int
    first_person_count: int
    quote_pairs: int
    book_title_marks: int
    separator_lines: int


# The author's export convention marks paid collaborations with at least one
# concrete brand or product tag. This list is intentionally narrower than a
# generic "looks commercial" heuristic: category tags such as #睡眠 or
# #女性成长 are not enough on their own.
COMMERCIAL_BRAND_TOKENS = (
    "Aesop", "伊索", "ABC", "CHAKA", "Color7", "Doppelherz", "双心",
    "fresh", "馥蕾诗", "Gucci", "古驰", "HBN", "HongMall", "Kindle",
    "OLAY", "OPPO", "POPMART", "泡泡玛特", "PRADA", "RIO", "Rogaine",
    "SKII", "SK-II", "SKⅡ", "TCL", "THERMOS", "膳魔师", "tomtoc",
    "Virere", "YSL", "amex", "美国运通", "亚朵星球", "腾讯元宝", "元宝",
    "元祖", "农夫山泉", "兰蔻", "吉谷", "娇兰", "宜家", "好医保",
    "掌阅", "文石", "明基", "朝日唯品", "毛戈平", "水星家纺",
    "水星蚕丝", "滴滴", "淳茶舍", "爱回收", "王卷王", "芝士派",
    "番茄英语", "薄荷英语", "薄荷阅读", "美团", "肯德基", "茶派",
    "苹果新品", "iPhone17", "招商银行", "招行", "中信银行", "星巴克",
    "观夏", "诚天国际", "馥马尔", "野人先生", "baba杯", "KHDEGINE",
    "护舒宝",
)


def parse_count(value: str) -> int:
    value = value.strip().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(万)?", value)
    if not match:
        return 0
    number = float(match.group(1))
    return round(number * 10_000) if match.group(2) else round(number)


def parse_note(path: Path) -> Note | None:
    text = path.read_text(encoding="utf-8")
    if "## 笔记正文" not in text:
        return None

    header, body = text.split("## 笔记正文", 1)
    body = body.split("\n---\n", 1)[0].strip()
    title_match = re.search(r"(?m)^# (.+)$", header)
    time_match = re.search(r"(?m)^- 编辑时间（平台笔记时间）：(.+)$", header)
    tags_match = re.search(r"(?m)^- 笔记标签：(.+)$", header)
    place_match = re.search(r"(?m)^- 发表地点：(.+)$", header)
    stats_match = re.search(
        r"(?m)^- 互动数据：点赞 (.+?) ｜ 收藏 (.+?) ｜ 评论 (.+?)$", header
    )

    if not title_match or not time_match:
        return None

    published_at = time_match.group(1).strip()
    published = datetime.fromisoformat(published_at)
    tags_text = tags_match.group(1).strip() if tags_match else ""
    tags = [] if tags_text in {"", "无"} else re.findall(r"#[^\s#]+", tags_text)

    likes = favorites = comments = 0
    if stats_match:
        likes = parse_count(stats_match.group(1))
        favorites = parse_count(stats_match.group(2))
        comments = parse_count(stats_match.group(3))

    text_paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    sentences = [s for s in re.split(r"(?<=[。！？!?…])", body) if s.strip()]
    separator_lines = sum(
        1 for line in body.splitlines() if line.strip() in {"-", "\\ -", "/", ".", "·"}
    )

    return Note(
        path=str(path),
        filename=path.name,
        title=title_match.group(1).strip(),
        published_at=published_at,
        year=published.year,
        month=published.strftime("%Y-%m"),
        tags=tags,
        location=place_match.group(1).strip() if place_match else "",
        likes=likes,
        favorites=favorites,
        comments=comments,
        body=body,
        chars=len(re.sub(r"\s+", "", body)),
        paragraphs=len(text_paragraphs),
        sentences=len(sentences),
        question_sentences=sum(s.rstrip().endswith(("？", "?")) for s in sentences),
        first_person_count=body.count("我"),
        quote_pairs=min(body.count("“"), body.count("”")),
        book_title_marks=min(body.count("《"), body.count("》")),
        separator_lines=separator_lines,
    )


def pct(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 1) if denominator else 0.0


def summarize_group(notes: list[Note]) -> dict[str, object]:
    char_counts = [n.chars for n in notes]
    sentence_counts = [n.sentences for n in notes]
    all_sentences = []
    for note in notes:
        all_sentences.extend(
            s for s in re.split(r"(?<=[。！？!?…])", note.body) if s.strip()
        )
    sentence_lengths = [
        len(re.sub(r"\s+", "", sentence))
        for sentence in all_sentences
        if re.search(r"[\u4e00-\u9fff]", sentence)
    ]
    return {
        "notes": len(notes),
        "chars_total": sum(char_counts),
        "chars_mean": round(statistics.mean(char_counts), 1) if notes else 0,
        "chars_median": statistics.median(char_counts) if notes else 0,
        "sentences_mean": round(statistics.mean(sentence_counts), 1) if notes else 0,
        "sentence_chars_mean": (
            round(statistics.mean(sentence_lengths), 1) if sentence_lengths else 0
        ),
        "sentence_chars_median": (
            statistics.median(sentence_lengths) if sentence_lengths else 0
        ),
        "question_sentence_pct": pct(
            sum(n.question_sentences for n in notes),
            sum(n.sentences for n in notes),
        ),
        "first_person_per_1k_chars": round(
            sum(n.first_person_count for n in notes)
            / max(1, sum(char_counts))
            * 1000,
            1,
        ),
        "quotes_per_1k_chars": round(
            sum(n.quote_pairs for n in notes) / max(1, sum(char_counts)) * 1000,
            1,
        ),
        "book_marks_per_1k_chars": round(
            sum(n.book_title_marks for n in notes) / max(1, sum(char_counts)) * 1000,
            1,
        ),
        "notes_with_separator_pct": pct(
            sum(n.separator_lines > 0 for n in notes), len(notes)
        ),
        "notes_with_quote_pct": pct(sum(n.quote_pairs > 0 for n in notes), len(notes)),
        "likes_mean": round(statistics.mean([n.likes for n in notes]), 1) if notes else 0,
        "likes_median": statistics.median([n.likes for n in notes]) if notes else 0,
        "favorites_mean": (
            round(statistics.mean([n.favorites for n in notes]), 1) if notes else 0
        ),
        "favorites_median": (
            statistics.median([n.favorites for n in notes]) if notes else 0
        ),
    }


def title_patterns(titles: list[str]) -> dict[str, int]:
    return {
        "question_mark": sum("？" in t or "?" in t for t in titles),
        "colon_or_bar": sum(bool(re.search(r"[：:｜|]", t)) for t in titles),
        "quotes": sum(bool(re.search(r"[“”「」]", t)) for t in titles),
        "first_person": sum("我" in t for t in titles),
        "life_method": sum(
            any(word in t for word in ("方法", "怎么办", "如何", "秘诀", "指南", "tips"))
            for t in titles
        ),
        "state_observation": sum(
            any(word in t for word in ("我发现", "其实", "不要", "不能", "仍然", "终于"))
            for t in titles
        ),
    }


def phrase_counts(notes: list[Note], size: int, limit: int = 100) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for note in notes:
        text = re.sub(r"https?://\S+|[#*`>\-]", "", note.body)
        for segment in re.split(r"[^\u4e00-\u9fff]+", text):
            for index in range(len(segment) - size + 1):
                phrase = segment[index : index + size]
                counter[phrase] += 1
    return [(phrase, count) for phrase, count in counter.most_common(limit) if count >= 3]


def theme_matches(notes: list[Note], keywords: list[str], limit_per_theme: int) -> dict[str, object]:
    results: dict[str, object] = {}
    for label, words in {
        "写作与论文": ["写作", "论文", "文献", "文字", "语感", "文笔"],
        "阅读与书": ["读书", "阅读", "书单", "诗集", "小说", "作家"],
        "学习与读博": ["学习", "读博", "博士", "图书馆", "导师", "哈佛", "考研"],
        "自媒体与流量": ["博主", "流量", "涨粉", "笔记", "账号", "爆文", "推广"],
        "独处与散步": ["独处", "散步", "一个人", "安静", "断联"],
        "精力与身体": ["精力", "睡眠", "睡觉", "累", "疲惫", "干眼", "清醒"],
        "生活秩序": ["秩序", "routine", "生活", "习惯", "记录", "重建"],
        "关系与女性": ["女性", "妈妈", "母亲", "朋友", "友情", "恋爱", "亲密"],
        "旅行": ["旅行", "旅居", "京都", "伊犁", "上海", "solo"],
        "空间与家": ["书房", "书桌", "家", "房间", "村居", "装修"],
        "自由与选择": ["自由", "选择", "决定", "逃跑", "主线", "主动"],
        "商业合作": ["推广", "品牌", "合作", "产品", "礼盒", "新老朋友", "@"],
    }.items():
        scored = []
        for note in notes:
            score = sum(note.body.count(word) + note.title.count(word) for word in words)
            if score:
                scored.append((score, note))
        scored.sort(key=lambda item: (-item[0], item[1].published_at))
        results[label] = [
            {
                "date": note.published_at[:10],
                "title": note.title,
                "score": score,
                "likes": note.likes,
                "favorites": note.favorites,
            }
            for score, note in scored[:limit_per_theme]
        ]
    return results


def matching_brand_tags(note: Note) -> list[str]:
    matches = []
    for tag in note.tags:
        folded_tag = tag.casefold()
        if any(token.casefold() in folded_tag for token in COMMERCIAL_BRAND_TOKENS):
            matches.append(tag)
    return matches


def commercial_notes(notes: list[Note]) -> list[dict[str, object]]:
    results = []
    for note in notes:
        brand_tags = matching_brand_tags(note)
        if brand_tags:
            body_folded = note.body.casefold()
            body_matches = [
                (body_folded.find(token.casefold()), token)
                for token in COMMERCIAL_BRAND_TOKENS
                if body_folded.find(token.casefold()) >= 0
            ]
            body_position, body_brand = (
                min(body_matches, key=lambda item: item[0])
                if body_matches
                else (None, None)
            )
            results.append(
                {
                    "date": note.published_at,
                    "title": note.title,
                    "brand_tags": brand_tags,
                    "body_brand": body_brand,
                    "body_brand_position_pct": (
                        round(body_position / len(note.body) * 100, 1)
                        if body_position is not None and note.body
                        else None
                    ),
                    "chars": note.chars,
                    "likes": note.likes,
                    "favorites": note.favorites,
                    "filename": note.filename,
                }
            )
    return results


def build_report(notes: list[Note], source_root: Path) -> tuple[str, dict[str, object]]:
    notes.sort(key=lambda n: n.published_at)
    source_label = (
        "references/sources/notes"
        if source_root.name == "notes" and source_root.parent.name == "sources"
        else "本地语料目录（未写入公开报告）"
    )
    yearly = defaultdict(list)
    monthly = defaultdict(list)
    tag_counter: Counter[str] = Counter()
    location_counter: Counter[str] = Counter()
    for note in notes:
        yearly[note.year].append(note)
        monthly[note.month].append(note)
        tag_counter.update(note.tags)
        location_counter[note.location] += 1

    phrases = {
        f"{size}-char": phrase_counts(notes, size, 60)
        for size in (2, 3, 4, 5, 6)
    }
    top_notes = sorted(
        notes,
        key=lambda n: (n.favorites * 2 + n.likes + n.comments * 5),
        reverse=True,
    )[:30]
    commercial = commercial_notes(notes)
    commercial_filenames = {item["filename"] for item in commercial}
    commercial_group = [note for note in notes if note.filename in commercial_filenames]
    ordinary_group = [note for note in notes if note.filename not in commercial_filenames]
    commercial_year_counts: Counter[str] = Counter(
        item["date"][:4] for item in commercial
    )
    commercial_positions = sorted(
        item["body_brand_position_pct"]
        for item in commercial
        if item["body_brand_position_pct"] is not None
    )
    commercial_position_stats = {
        "notes_with_body_brand": len(commercial_positions),
        "notes_without_body_brand": len(commercial) - len(commercial_positions),
        "min_pct": commercial_positions[0] if commercial_positions else None,
        "p25_pct": (
            commercial_positions[(len(commercial_positions) - 1) // 4]
            if commercial_positions
            else None
        ),
        "median_pct": (
            statistics.median(commercial_positions) if commercial_positions else None
        ),
        "p75_pct": (
            commercial_positions[(len(commercial_positions) - 1) * 3 // 4]
            if commercial_positions
            else None
        ),
        "max_pct": commercial_positions[-1] if commercial_positions else None,
    }
    themes = theme_matches(notes, [], 20)
    summary = {
        "source_root": source_label,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "corpus_fingerprint": hashlib.sha256(
            "\n".join(n.path for n in notes).encode("utf-8")
        ).hexdigest(),
        "overall": summarize_group(notes),
        "yearly": {str(year): summarize_group(group) for year, group in sorted(yearly.items())},
        "monthly_note_counts": {
            month: len(group) for month, group in sorted(monthly.items())
        },
        "title_patterns": title_patterns([n.title for n in notes]),
        "top_tags": tag_counter.most_common(100),
        "locations": location_counter.most_common(),
        "phrases": phrases,
        "themes": themes,
        "commercial_detection_rule": "至少一个笔记标签含明确品牌或具体产品名",
        "commercial_note_count": len(commercial),
        "commercial_by_year": dict(sorted(commercial_year_counts.items())),
        "commercial_body_position": commercial_position_stats,
        "commercial_notes": commercial,
        "commercial_overall": summarize_group(commercial_group),
        "ordinary_overall": summarize_group(ordinary_group),
        "top_engagement_notes": [
            {
                "date": n.published_at[:10],
                "title": n.title,
                "likes": n.likes,
                "favorites": n.favorites,
                "comments": n.comments,
            }
            for n in top_notes
        ],
        "note_index": [
            {
                "date": n.published_at,
                "title": n.title,
                "chars": n.chars,
                "likes": n.likes,
                "favorites": n.favorites,
                "tags": n.tags,
                "location": n.location,
                "commercial": bool(matching_brand_tags(n)),
                "brand_tags": matching_brand_tags(n),
                "filename": n.filename,
            }
            for n in notes
        ],
    }

    lines = [
        "# Buzzbuzz 本地语料分析报告",
        "",
        f"- 语料目录：{source_label}",
        f"- 笔记数：{len(notes)}",
        f"- 时间范围：{notes[0].published_at[:10]} 至 {notes[-1].published_at[:10]}",
        f"- 正文字符数：{summary['overall']['chars_total']:,}",
        f"- 平均/中位篇幅：{summary['overall']['chars_mean']} / {summary['overall']['chars_median']} 字",
        f"- 平均/中位句子长度：{summary['overall']['sentence_chars_mean']} / {summary['overall']['sentence_chars_median']} 字",
        f"- 每千字第一人称“我”：{summary['overall']['first_person_per_1k_chars']} 次",
        f"- 疑问句占比：{summary['overall']['question_sentence_pct']}%",
        f"- 使用分隔线的笔记：{summary['overall']['notes_with_separator_pct']}%",
        "",
        "## 年度变化",
        "",
        "| 年份 | 篇数 | 平均字数 | 平均赞 | 平均藏 | 每千字“我” | 疑问句% |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for year, group in sorted(yearly.items()):
        stats = summarize_group(group)
        lines.append(
            f"| {year} | {stats['notes']} | {stats['chars_mean']} | "
            f"{stats['likes_mean']} | {stats['favorites_mean']} | "
            f"{stats['first_person_per_1k_chars']} | {stats['question_sentence_pct']} |"
        )

    lines.extend(["", "## 标题模式", ""])
    for key, value in summary["title_patterns"].items():
        lines.append(f"- {key}: {value}")

    lines.extend(["", "## 高频标签", ""])
    lines.append("、".join(f"{tag} ({count})" for tag, count in tag_counter.most_common(40)))

    lines.extend(["", "## 高互动笔记", ""])
    for note in top_notes:
        lines.append(
            f"- {note.published_at[:10]}｜{note.title}｜赞 {note.likes}｜藏 {note.favorites}｜评 {note.comments}"
        )

    lines.extend(["", "## 高频短语", ""])
    for size in (3, 4, 5, 6):
        items = phrases[f"{size}-char"][:20]
        lines.append(f"- {size}字：" + "、".join(f"{p}({c})" for p, c in items))

    lines.extend(["", "## 主题候选", ""])
    for theme, matches in themes.items():
        lines.append(f"### {theme}")
        for match in matches[:12]:
            lines.append(
                f"- {match['date']}｜{match['title']}｜匹配 {match['score']}"
            )
        lines.append("")

    lines.extend(
        [
            "## 商业合作笔记",
            "",
            "识别规则：笔记标签中至少有一个明确品牌或具体产品名。"
            "普通生活方式标签、正文中的 @ 官方账号不会单独触发判定。",
            "",
            f"- 已识别：{len(commercial)} / {len(notes)} 篇",
            "- 年度分布：" + "、".join(
                f"{year}年 {count} 篇"
                for year, count in sorted(commercial_year_counts.items())
            ),
            "- 商业写法分析见 `references/commercial-writing.md`。",
            "",
        ]
    )
    for signal in commercial:
        lines.append(
            f"- {signal['date'][:10]}｜{signal['title']}｜"
            + " ".join(signal["brand_tags"])
        )

    lines.extend(
        [
            "",
            "## 商业合作与普通笔记的篇幅对照",
            "",
            "| 分组 | 篇数 | 平均字数 | 中位字数 | 平均句长 | 引号对数/篇 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for label, group in (
        ("商业合作标签", commercial_group),
        ("普通笔记", ordinary_group),
    ):
        stats = summarize_group(group)
        lines.append(
            f"| {label} | {stats['notes']} | {stats['chars_mean']} | "
            f"{stats['chars_median']} | {stats['sentence_chars_mean']} | "
            f"{round(stats['quotes_per_1k_chars'] * stats['chars_total'] / max(1, stats['notes']) / 1000, 1)} |"
        )

    lines.extend(
        [
            "",
            "## 品牌首次出现在正文的位置",
            "",
            f"- 正文可直接检索到品牌/产品名：{commercial_position_stats['notes_with_body_brand']} / {len(commercial)} 篇",
            f"- 仅在标签或图片信息中出现：{commercial_position_stats['notes_without_body_brand']} 篇",
            f"- 首次出现位置：最短 {commercial_position_stats['min_pct']}%｜"
            f"P25 {commercial_position_stats['p25_pct']}%｜"
            f"中位 {round(commercial_position_stats['median_pct'], 1)}%｜"
            f"P75 {commercial_position_stats['p75_pct']}%｜"
            f"最晚 {commercial_position_stats['max_pct']}%",
            "",
            "位置只用于识别成熟期惯例，不是硬性排版比例；真正决定广告是否高级的是产品出现前是否已有一个不依赖产品成立的人类母题。",
        ]
    )

    return "\n".join(lines).rstrip() + "\n", summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "source_root",
        nargs="?",
        default="references/sources/notes",
        help="Directory containing exported note markdown files",
    )
    parser.add_argument(
        "--out-dir",
        default="references/analysis",
        help="Directory for corpus-report.md and corpus-summary.json",
    )
    args = parser.parse_args()

    source_root = Path(args.source_root).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    notes = [
        note
        for path in source_root.glob("*.md")
        if (note := parse_note(path)) is not None
    ]
    if not notes:
        raise SystemExit(f"No exported notes found under {source_root}")

    report, summary = build_report(notes, source_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "corpus-report.md").write_text(report, encoding="utf-8")
    (out_dir / "corpus-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Parsed {len(notes)} notes")
    print(f"Wrote {out_dir / 'corpus-report.md'}")
    print(f"Wrote {out_dir / 'corpus-summary.json'}")


if __name__ == "__main__":
    main()
