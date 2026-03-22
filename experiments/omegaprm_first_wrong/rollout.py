from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from itertools import cycle
from pathlib import Path
from typing import Any, Protocol

import requests

from experiments.omegaprm_first_wrong.step_splitter import split_trace_steps

# ---------------------------------------------------------------------------
# Lazy loader for evaluate_correctness (avoids import errors in tests)
# ---------------------------------------------------------------------------
_evaluate_correctness = None


def _get_evaluate_correctness():
    global _evaluate_correctness
    if _evaluate_correctness is None:
        spec = importlib.util.spec_from_file_location(
            "bbeh_evaluate", Path("bbeh/evaluate.py")
        )
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        with contextlib.redirect_stdout(io.StringIO()):
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        _evaluate_correctness = module.evaluate_correctness
    return _evaluate_correctness


# ---------------------------------------------------------------------------
# PromptBuilder – manually constructs token-ID prompts (no apply_chat_template)
# ---------------------------------------------------------------------------

QUESTION_TEMPLATE = (
    "Please answer the following question.\n"
    "You should provide your final answer in the format \\boxed{{YOUR_ANSWER}}.\n"
    "Separate your following steps using \\n\\n.\n"
    "Question:\n\n{question}"
)


def _detect_chat_format(tokenizer: Any) -> str:
    """Auto-detect chat format from tokenizer vocabulary."""
    vocab = tokenizer.get_vocab()
    if "<|im_start|>" in vocab:
        return "chatml"
    if "<\uff5cUser\uff5c>" in vocab:  # <｜User｜>
        return "deepseek"
    raise ValueError(
        "Cannot auto-detect chat format from tokenizer. "
        "Pass chat_format='chatml' or chat_format='deepseek' explicitly."
    )


class PromptBuilder:
    """Builds raw prompts for the vLLM ``/completions`` endpoint.

    Constructs the token-ID sequence **manually** — does **not** call
    ``tokenizer.apply_chat_template``.

    Supported formats:
      - ``chatml``   — Qwen 2 / 2.5 / 3 (``<|im_start|>`` / ``<|im_end|>``)
      - ``deepseek`` — DeepSeek-R1-Distill-Qwen
                       (``<｜User｜>`` / ``<｜Assistant｜>``, ``<think>`` block)

    Pass ``chat_format="auto"`` (the default) to detect from the tokenizer.
    """

    # ChatML tokens
    _CHATML_IM_START = "<|im_start|>"
    _CHATML_IM_END = "<|im_end|>"

    # DeepSeek tokens (full-width ｜, half-width spaces replaced by ▁)
    _DS_BOS = "<\uff5cbegin\u2581of\u2581sentence\uff5c>"
    _DS_EOS = "<\uff5cend\u2581of\u2581sentence\uff5c>"
    _DS_USER = "<\uff5cUser\uff5c>"
    _DS_ASSISTANT = "<\uff5cAssistant\uff5c>"

    def __init__(
        self,
        tokenizer: Any,
        chat_format: str = "auto",
        system_prompt: str | None = None,
    ) -> None:
        self.tokenizer = tokenizer
        self.system_prompt = system_prompt

        if chat_format == "auto":
            chat_format = _detect_chat_format(tokenizer)
        self.chat_format = chat_format

        vocab = tokenizer.get_vocab()
        if chat_format == "chatml":
            self._im_start_id: int = vocab[self._CHATML_IM_START]
            self._im_end_id: int = vocab[self._CHATML_IM_END]
        elif chat_format == "deepseek":
            self._ds_bos_id: int = vocab[self._DS_BOS]
            self._ds_eos_id: int = vocab[self._DS_EOS]
            self._ds_user_id: int = vocab[self._DS_USER]
            self._ds_assistant_id: int = vocab[self._DS_ASSISTANT]
        else:
            raise ValueError(f"Unsupported chat_format: {chat_format!r}")

    # -- public API ----------------------------------------------------------

    def build_prompt_token_ids(
        self,
        question: str,
        prefix_steps: list[str] | None = None,
    ) -> list[int]:
        """Return the full prompt as a list of token IDs."""
        if self.chat_format == "chatml":
            return self._build_chatml_ids(question, prefix_steps)
        if self.chat_format == "deepseek":
            return self._build_deepseek_ids(question, prefix_steps)
        raise ValueError(f"Unsupported chat_format: {self.chat_format!r}")

    def build_prompt_text(
        self,
        question: str,
        prefix_steps: list[str] | None = None,
    ) -> str:
        """Return a human-readable text representation (for logging)."""
        if self.chat_format == "chatml":
            return self._build_chatml_text(question, prefix_steps)
        if self.chat_format == "deepseek":
            return self._build_deepseek_text(question, prefix_steps)
        raise ValueError(f"Unsupported chat_format: {self.chat_format!r}")

    @property
    def stop_token_ids(self) -> list[int]:
        if self.chat_format == "chatml":
            return [self._im_end_id]
        if self.chat_format == "deepseek":
            return [self._ds_eos_id]
        return []

    def count_tokens(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False))

    # -- internals -----------------------------------------------------------

    def _encode(self, text: str) -> list[int]:
        return self.tokenizer.encode(text, add_special_tokens=False)

    # ---- ChatML ------------------------------------------------------------

    def _build_chatml_ids(
        self,
        question: str,
        prefix_steps: list[str] | None = None,
    ) -> list[int]:
        ids: list[int] = []

        if self.system_prompt:
            ids.append(self._im_start_id)
            ids.extend(self._encode("system\n" + self.system_prompt))
            ids.append(self._im_end_id)
            ids.extend(self._encode("\n"))

        user_content = QUESTION_TEMPLATE.format(question=question)
        ids.append(self._im_start_id)
        ids.extend(self._encode("user\n" + user_content))
        ids.append(self._im_end_id)
        ids.extend(self._encode("\n"))

        ids.append(self._im_start_id)
        ids.extend(self._encode("assistant\n"))
        if prefix_steps:
            ids.extend(self._encode("\n\n".join(prefix_steps) + "\n\n"))

        return ids

    def _build_chatml_text(
        self,
        question: str,
        prefix_steps: list[str] | None = None,
    ) -> str:
        S, E = self._CHATML_IM_START, self._CHATML_IM_END
        parts: list[str] = []
        if self.system_prompt:
            parts.append(f"{S}system\n{self.system_prompt}{E}\n")
        user_content = QUESTION_TEMPLATE.format(question=question)
        parts.append(f"{S}user\n{user_content}{E}\n")
        parts.append(f"{S}assistant\n")
        if prefix_steps:
            parts.append("\n\n".join(prefix_steps) + "\n\n")
        return "".join(parts)

    # ---- DeepSeek-R1 -------------------------------------------------------

    def _build_deepseek_ids(
        self,
        question: str,
        prefix_steps: list[str] | None = None,
    ) -> list[int]:
        ids: list[int] = []

        # BOS + optional system prompt
        ids.append(self._ds_bos_id)
        if self.system_prompt:
            ids.extend(self._encode(self.system_prompt))

        # User turn
        user_content = QUESTION_TEMPLATE.format(question=question)
        ids.append(self._ds_user_id)
        ids.extend(self._encode(user_content))

        # Assistant turn — starts with <think>\n per DeepSeek-R1 convention
        ids.append(self._ds_assistant_id)
        ids.extend(self._encode("<think>\n"))
        if prefix_steps:
            ids.extend(self._encode("\n\n".join(prefix_steps) + "\n\n"))

        return ids

    def _build_deepseek_text(
        self,
        question: str,
        prefix_steps: list[str] | None = None,
    ) -> str:
        parts: list[str] = []
        parts.append(self._DS_BOS)
        if self.system_prompt:
            parts.append(self.system_prompt)
        user_content = QUESTION_TEMPLATE.format(question=question)
        parts.append(f"{self._DS_USER}{user_content}")
        parts.append(f"{self._DS_ASSISTANT}<think>\n")
        if prefix_steps:
            parts.append("\n\n".join(prefix_steps) + "\n\n")
        return "".join(parts)


# ---------------------------------------------------------------------------
# RolloutSample & protocol
# ---------------------------------------------------------------------------


def _extract_boxed_answer(text: str) -> str | None:
    """Extract the last \\boxed{...} answer from text, handling nested braces."""
    last_match: str | None = None
    for m in re.finditer(r"\\boxed\{", text):
        start = m.end()
        depth = 1
        pos = start
        while pos < len(text) and depth > 0:
            if text[pos] == "{":
                depth += 1
            elif text[pos] == "}":
                depth -= 1
            pos += 1
        if depth == 0:
            last_match = text[start : pos - 1]
    return last_match


def _extract_answer_from_rollout(text: str) -> str | None:
    """Extract an answer from rollout text.

    Priority:
      1. Last ``\\boxed{...}`` in the text
      2. Text after ``</think>`` (first non-empty line)
      3. ``None`` if nothing found
    """
    boxed = _extract_boxed_answer(text)
    if boxed is not None:
        return boxed.strip()

    think_close = re.search(r"</think>\s*", text, re.IGNORECASE)
    if think_close:
        after = text[think_close.end() :].strip()
        if after:
            first_line = after.split("\n")[0].strip()
            if first_line:
                return first_line
    return None


@dataclass
class RolloutSample:
    response_text: str
    completion_tokens: int
    prompt_tokens: int | None
    is_correct: bool
    base_url: str
    duration_sec: float
    continuation_steps: list[str]
    finish_reason: str = "stop"

    @property
    def is_truncated(self) -> bool:
        return self.finish_reason in ("length", "repetition")

    @property
    def rollout_len_tokens(self) -> int:
        return self.completion_tokens


class RolloutClient(Protocol):
    def sample_rollouts(
        self,
        question: str,
        prefix_steps: list[str],
        target: str,
        num_rollouts: int,
    ) -> list[RolloutSample]: ...


# ---------------------------------------------------------------------------
# VLLMRolloutClient — uses /completions with prompt_token_ids
# ---------------------------------------------------------------------------


@dataclass
class VLLMRolloutClient:
    prompt_builder: PromptBuilder
    base_urls: list[str]
    model: str
    temperature: float = 0.6
    top_p: float = 0.95
    max_tokens: int = 4096
    timeout_sec: int = 900
    max_retries: int = 4
    max_parallel_requests: int | None = None
    seed: int | None = None
    max_context: int = 32768
    context_safety_margin: int = 512
    repetition_max_pattern: int = 100
    repetition_min_count: int = 3

    def __post_init__(self) -> None:
        if not self.base_urls:
            raise ValueError("base_urls must not be empty")
        self._url_lock = threading.Lock()
        self._url_cycle = cycle(self.base_urls)

    def _next_url(self) -> str:
        with self._url_lock:
            return next(self._url_cycle)

    def _safe_max_tokens(self, num_prompt_tokens: int) -> int:
        return min(
            self.max_tokens,
            self.max_context - num_prompt_tokens - self.context_safety_margin,
        )

    def _backoff_max_tokens_from_error(self, error_text: str) -> int | None:
        match = re.search(r"request has (\d+) input tokens", error_text)
        if not match:
            return None
        input_tokens = int(match.group(1))
        safe_max = self.max_context - input_tokens - self.context_safety_margin
        return safe_max if safe_max >= 100 else None

    def _generate_once(
        self,
        question: str,
        prefix_steps: list[str] | None = None,
    ) -> RolloutSample:
        prompt_ids = self.prompt_builder.build_prompt_token_ids(
            question, prefix_steps
        )
        safe_max_tokens = self._safe_max_tokens(len(prompt_ids))
        if safe_max_tokens < 100:
            raise RuntimeError(
                f"Prompt too long for rollout: safe_max_tokens={safe_max_tokens}"
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt_ids,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": safe_max_tokens,
            "stop_token_ids": self.prompt_builder.stop_token_ids,
        }
        if self.repetition_max_pattern > 0:
            payload["repetition_detection"] = {
                "max_pattern_size": self.repetition_max_pattern,
                "min_count": self.repetition_min_count,
            }
        if self.seed is not None:
            payload["seed"] = self.seed + int(time.time() * 1000) % 100000

        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            base_url = self._next_url()
            start = time.time()
            try:
                resp = requests.post(
                    f"{base_url}/completions",
                    json=payload,
                    timeout=self.timeout_sec,
                )
                if resp.status_code == 400:
                    safe_backoff = self._backoff_max_tokens_from_error(
                        resp.text
                    )
                    if safe_backoff is not None and safe_backoff < int(
                        payload["max_tokens"]
                    ):
                        payload["max_tokens"] = safe_backoff
                        last_err = RuntimeError(
                            f"Adjusted max_tokens to {safe_backoff}"
                        )
                        continue
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                text: str = choice["text"]
                text = text.rstrip()
                finish_reason: str = choice.get(
                    "finish_reason", "stop"
                ) or "stop"

                usage = data.get("usage", {})
                completion_tokens = int(usage.get("completion_tokens") or 0)
                if completion_tokens <= 0:
                    completion_tokens = max(
                        1, self.prompt_builder.count_tokens(text)
                    )
                prompt_tokens = usage.get("prompt_tokens")
                duration_sec = round(time.time() - start, 3)
                return RolloutSample(
                    response_text=text,
                    completion_tokens=completion_tokens,
                    prompt_tokens=(
                        int(prompt_tokens)
                        if prompt_tokens is not None
                        else None
                    ),
                    is_correct=False,
                    base_url=base_url,
                    duration_sec=duration_sec,
                    continuation_steps=split_trace_steps(text),
                    finish_reason=finish_reason,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
        raise RuntimeError(
            f"Rollout request failed after {self.max_retries} retries: "
            f"{last_err}"
        )

    def sample_rollouts(
        self,
        question: str,
        prefix_steps: list[str],
        target: str,
        num_rollouts: int,
    ) -> list[RolloutSample]:
        max_workers = self.max_parallel_requests or min(
            num_rollouts, len(self.base_urls)
        )
        max_workers = max(1, max_workers)
        steps = prefix_steps if prefix_steps else None

        evaluate_correctness = _get_evaluate_correctness()
        target_lower = target.strip().lower()
        outputs: list[RolloutSample] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(self._generate_once, question, steps)
                for _ in range(num_rollouts)
            ]
            for future in as_completed(futures):
                sample = future.result()
                # Try our robust extractor first, fall back to bbeh's
                extracted = _extract_answer_from_rollout(sample.response_text)
                if extracted is not None:
                    sample.is_correct = (
                        extracted.strip().lower() == target_lower
                        or bool(
                            evaluate_correctness(
                                sample.response_text, target
                            )
                        )
                    )
                else:
                    sample.is_correct = bool(
                        evaluate_correctness(
                            sample.response_text, target
                        )
                    )
                outputs.append(sample)
        return outputs
