#!/usr/bin/env python3
"""Heuristic anti-AI-style checker for drafts written with this skill.

This is not a classifier. It flags obvious drift patterns and gives editors a
short, actionable pass before publishing.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


BANNED = {
    "万能时代开场": ("在这个快节奏的时代", "在这个信息爆炸的时代"),
    "万能人生比喻": ("人生就像一场旅行", "生活就像一面镜子"),
    "模板祝福": ("愿你被世界温柔以待", "愿我们都能", "希望每一个"),
    "空泛允许": ("允许一切发生",),
    "AI套话": ("值得注意的是", "总的来说", "综上所述", "毋庸置疑", "不难发现"),
}

MARKETING_CLICHES = (
    "闭眼冲", "全网最低", "宝藏神器", "人手一支", "入手不亏", "必入",
    "无限回购", "秒变", "立刻见效", "立竿见影", "拯救一切", "治愈一切",
    "赶紧冲", "买它",
)

ABSTRACT_WORDS = (
    "松弛感", "生命力", "自我成长", "内在力量", "情绪价值", "生命能量",
    "治愈", "赋能", "课题", "边界感", "长期主义", "自洽",
)


def chinese_chars(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def paragraph_stats(text: str) -> tuple[int, float, list[int]]:
    paragraphs = [
        p.strip()
        for p in re.split(r"\n\s*\n", text)
        if p.strip() and not p.lstrip().startswith("#")
    ]
    lengths = [chinese_chars(p) for p in paragraphs]
    if not lengths:
        return 0, 0.0, []
    average = sum(lengths) / len(lengths)
    variance = sum((length - average) ** 2 for length in lengths) / len(lengths)
    coefficient = variance**0.5 / average if average else 0
    return len(paragraphs), coefficient, lengths


def analyze(text: str) -> list[str]:
    warnings: list[str] = []
    chars = chinese_chars(text)
    paragraphs, variation, paragraph_lengths = paragraph_stats(text)
    sentences = [
        s.strip()
        for s in re.split(r"(?<=[。！？!?…])", text)
        if chinese_chars(s) >= 2
    ]
    sentence_lengths = [chinese_chars(s) for s in sentences]

    for label, phrases in BANNED.items():
        found = [phrase for phrase in phrases if phrase in text]
        if found:
            warnings.append(f"{label}：{'、'.join(found)}")

    marketing = [phrase for phrase in MARKETING_CLICHES if phrase in text]
    if marketing:
        warnings.append(f"叫卖式广告语：{'、'.join(marketing)}")

    transitions = sum(text.count(word) for word in ("首先", "其次", "最后"))
    if transitions >= 3:
        warnings.append(f"列表模板腔：首先/其次/最后合计出现 {transitions} 次")

    dashes = text.count("——")
    if chars and dashes / chars * 1000 > 4:
        warnings.append(f"破折号偏多：{dashes} 个，约每千字 {dashes/chars*1000:.1f} 个")

    quotes = min(text.count("“"), text.count("”"))
    if chars and quotes / chars * 1000 > 10:
        warnings.append(f"引用/引号偏密：{quotes} 组，容易显得像知识卡片")

    abstract_hits = {word: text.count(word) for word in ABSTRACT_WORDS if text.count(word)}
    if sum(abstract_hits.values()) >= 5:
        detail = "、".join(f"{word}{count}次" for word, count in abstract_hits.items())
        warnings.append(f"抽象热词偏多：{detail}")

    if paragraphs < 3 and chars >= 200:
        warnings.append("段落过少：至少保留 3 个可呼吸的段落")
    if variation < 0.28 and paragraphs >= 4:
        warnings.append(f"段落过于整齐：长度变异系数 {variation:.2f}，建议有意制造长短差")

    if sentence_lengths:
        average = sum(sentence_lengths) / len(sentence_lengths)
        if average < 16:
            warnings.append(f"平均句长过短：{average:.1f} 字，可能像碎片化口号")
        elif average > 46:
            warnings.append(f"平均句长过长：{average:.1f} 字，可能像论文摘要")

    if "我" not in text and "你" not in text:
        warnings.append("缺少第一/第二人称，可能脱离她的个人叙事")

    if "。" in text:
        fullwidth_ratio = text.count("。") / max(1, text.count("。") + text.count(". "))
        if fullwidth_ratio < 0.7:
            warnings.append("英文句点偏多：她的正文基本使用中文全角句号")

    if paragraph_lengths and max(paragraph_lengths) / max(1, min(paragraph_lengths)) > 18:
        warnings.append("段落长短差距极大：检查是否有一段误粘或过度独白")

    return warnings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("draft", help="Markdown or text draft")
    args = parser.parse_args()

    text = Path(args.draft).expanduser().read_text(encoding="utf-8")
    warnings = analyze(text)
    paragraphs, variation, _ = paragraph_stats(text)
    print(f"中文字符：{chinese_chars(text)}")
    print(f"段落数：{paragraphs}｜长度变异系数：{variation:.2f}")
    if warnings:
        print("风格预警：")
        for warning in warnings:
            print(f"- {warning}")
        print("结论：需要人工重写，不要只替换同义词。")
        raise SystemExit(1)
    print("结论：未命中明显模板腔；仍须核对事实、真实细节和商业标注。")


if __name__ == "__main__":
    main()
