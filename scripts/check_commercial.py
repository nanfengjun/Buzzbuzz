#!/usr/bin/env python3
"""Heuristic review for a commercial note written in Buzzbuzz's mature voice."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


MARKETING = (
    "闭眼冲", "全网最低", "宝藏神器", "人手一支", "入手不亏", "必入",
    "无限回购", "秒变", "立刻见效", "立竿见影", "拯救一切", "治愈一切",
    "赶紧冲", "买它",
)


def chinese_len(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def strip_heading(text: str) -> tuple[str, str]:
    if "## 笔记正文" in text:
        header, body = text.split("## 笔记正文", 1)
        title_match = re.search(r"(?m)^# (.+)$", header)
        title = title_match.group(1).strip() if title_match else ""
        body = body.split("\n---\n", 1)[0].strip()
        return title, body

    lines = text.splitlines()
    title = ""
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        lines = lines[1:]
    body = "\n".join(lines).strip()
    return title, body


def first_position(text: str, term: str) -> int | None:
    if not term:
        return None
    position = text.casefold().find(term.casefold())
    return position if position >= 0 else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("draft", help="Markdown or text draft")
    parser.add_argument("--brand", required=True, help="Brand product name expected in body")
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Product category term; repeat for synonyms, e.g. 卫生巾 / 月经",
    )
    parser.add_argument(
        "--sensitive",
        action="store_true",
        help="Apply stricter opening rules for hygiene, health, finance, or body products",
    )
    args = parser.parse_args()

    text = Path(args.draft).expanduser().read_text(encoding="utf-8")
    title, body = strip_heading(text)
    length = max(1, len(body))
    warnings: list[str] = []
    observations: list[str] = []
    opening = body[:300]
    literary_frame = any(
        word in opening
        for word in ("读书", "小说", "文学", "电影", "访谈", "课堂", "老师", "书里")
    )

    brand_position = first_position(body, args.brand)
    if brand_position is None:
        warnings.append(f"正文未找到品牌名“{args.brand}”；若品牌只在图片中出现，请人工确认")
    else:
        percentage = brand_position / length * 100
        print(f"品牌首现：{percentage:.0f}%")
        if percentage < 40:
            warnings.append(
                f"品牌首现较早（{percentage:.0f}%）；成熟期语料中位约为 62%"
            )

    category_hits = [term for term in args.category if term and term in body]
    if args.category and category_hits:
        first_category = min(first_position(body, term) for term in category_hits)
        fraction = first_category / length * 100
        print(f"品类首现：{fraction:.0f}%")
        if fraction < 30:
            message = (
                f"产品品类在正文前 {fraction:.0f}% 已出现；"
                "先确认开篇是人物/文学场景，而非品类焦虑"
            )
            if literary_frame:
                observations.append(message + "（检测到文学/教育场景，需人工判断是否服务更大母题）")
            else:
                warnings.append(message)

    title_hits = [term for term in [args.brand, *args.category] if term and term in title]
    if title_hits:
        warnings.append(f"标题含有产品信息：{'、'.join(title_hits)}；成熟期标题通常先写成随笔命题")

    if args.sensitive:
        risky_openings = (
            "包里有没有", "还有卫生巾吗", "突然来月经", "经期必备",
            "出门必备", "忘了带", "怎么办",
        )
        hits = [phrase for phrase in risky_openings if phrase in opening]
        if hits:
            warnings.append(f"敏感品类焦虑开场：{'、'.join(hits)}")
        if category_hits:
            earliest = min(first_position(body, term) for term in category_hits)
            if earliest < 120:
                message = "敏感品类词出现在前 120 字；必须是更大的关系/文学/女性处境场景"
                if literary_frame:
                    observations.append(message + "（已检测到文学/教育入口）")
                else:
                    warnings.append(message)

    phrases = [phrase for phrase in MARKETING if phrase in text]
    if phrases:
        warnings.append(f"叫卖式广告语：{'、'.join(phrases)}")

    for phrase in ("这次和", "合作", "推荐给大家"):
        if phrase in body:
            warnings.append(
                f"正文出现交易转场“{phrase}”；优先通过标签/平台披露合作，除非 brief 或合规要求"
            )

    if warnings:
        print("商业笔记预警：")
        for observation in observations:
            print(f"- 提示：{observation}")
        for warning in warnings:
            print(f"- {warning}")
        print("结论：需要调整叙事骨架，不能只替换措辞。")
        raise SystemExit(1)

    for observation in observations:
        print(f"提示：{observation}")
    print("结论：未命中明显的产品焦虑/叫卖结构；仍须人工检查人物母题、真实事实和披露。")


if __name__ == "__main__":
    main()
