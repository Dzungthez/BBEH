from __future__ import annotations

import re

_THINK_OPEN_RE = re.compile(r"^\s*<think>\s*", re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"\s*</think>\s*", re.IGNORECASE)
_NUMBERED_BLOCK_RE = re.compile(
    r"(?:^|\n)(?=(?:Step\s+\d+[:.)]|\d+[.)]|[-*]\s+|Thought\s+\d+[:.)]))",
    re.IGNORECASE,
)


def normalize_trace_text(trace: str) -> str:
    text = trace.replace("\r\n", "\n").strip()
    text = _THINK_OPEN_RE.sub("", text)
    # Only keep content before </think> — everything after is the
    # final answer, not a reasoning step.
    close_match = _THINK_CLOSE_RE.search(text)
    if close_match:
        text = text[: close_match.start()]
    return text.strip()


def split_trace_steps(trace: str) -> list[str]:
    text = normalize_trace_text(trace)
    if not text:
        return []

    chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n", text) if chunk.strip()]
    if len(chunks) > 1:
        return chunks

    numbered = [chunk.strip() for chunk in _NUMBERED_BLOCK_RE.split(text) if chunk.strip()]
    if len(numbered) > 1:
        return numbered

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines


def count_tokens(text: str) -> int:
    return len(re.findall(r"\S+", text))


def merge_short_steps(steps: list[str], min_tokens: int) -> list[str]:
    if min_tokens <= 1:
        return [step.strip() for step in steps if step.strip()]

    merged = [step.strip() for step in steps if step.strip()]
    if len(merged) <= 1:
        return merged

    changed = True
    while changed and len(merged) > 1:
        changed = False
        idx = 0
        while idx < len(merged):
            if count_tokens(merged[idx]) >= min_tokens:
                idx += 1
                continue

            left_idx = idx - 1 if idx > 0 else None
            right_idx = idx + 1 if idx + 1 < len(merged) else None
            if left_idx is None and right_idx is None:
                break
            if left_idx is None:
                merged[right_idx] = merged[idx] + "\n\n" + merged[right_idx]
                del merged[idx]
                changed = True
                continue
            if right_idx is None:
                merged[left_idx] = merged[left_idx] + "\n\n" + merged[idx]
                del merged[idx]
                idx = max(left_idx, 0)
                changed = True
                continue

            left_len = count_tokens(merged[left_idx])
            right_len = count_tokens(merged[right_idx])
            if left_len <= right_len:
                merged[left_idx] = merged[left_idx] + "\n\n" + merged[idx]
                del merged[idx]
                idx = max(left_idx, 0)
            else:
                merged[right_idx] = merged[idx] + "\n\n" + merged[right_idx]
                del merged[idx]
            changed = True

    return [step.strip() for step in merged if step.strip()]
