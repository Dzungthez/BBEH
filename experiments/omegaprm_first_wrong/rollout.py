from __future__ import annotations

import contextlib
import importlib.util
import io
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from itertools import cycle
from pathlib import Path
from typing import Any, Protocol

import requests
import tiktoken

from experiments.omegaprm_first_wrong.step_splitter import split_trace_steps


def _load_evaluate_correctness():
    spec = importlib.util.spec_from_file_location("bbeh_evaluate", Path("bbeh/evaluate.py"))
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module.evaluate_correctness


evaluate_correctness = _load_evaluate_correctness()
_enc = tiktoken.get_encoding("cl100k_base")


@dataclass
class RolloutSample:
    response_text: str
    completion_tokens: int
    prompt_tokens: int | None
    is_correct: bool
    base_url: str
    duration_sec: float
    continuation_steps: list[str]

    @property
    def rollout_len_tokens(self) -> int:
        return self.completion_tokens


class RolloutClient(Protocol):
    def sample_rollouts(self, question: str, prefix_steps: list[str], target: str, num_rollouts: int) -> list[RolloutSample]:
        ...


def build_rollout_prompt(question: str, prefix_steps: list[str]) -> str:
    if prefix_steps:
        prefix_text = "\n\n".join(f"[step {idx}] {step}" for idx, step in enumerate(prefix_steps, start=1))
        return (
            "Continue solving the following BBEH multi-step arithmetic question.\n"
            "You are given an existing reasoning prefix. Continue from the next step only.\n"
            "Do not restart the solution and do not repeat the prefix.\n"
            "Be concise: use compact calculations, avoid prose, and finish as soon as the answer is determined.\n"
            "Separate each new reasoning step with a blank line.\n"
            "End with the final answer in the format: \\boxed{YOUR_ANSWER}.\n\n"
            f"Question:\n{question}\n\n"
            f"Existing reasoning prefix:\n{prefix_text}\n\n"
            "Continue from the next step."
        )

    return (
        "Solve the following BBEH multi-step arithmetic question step by step.\n"
        "Be concise: use compact calculations, avoid prose, and finish as soon as the answer is determined.\n"
        "Separate each reasoning step with a blank line.\n"
        "End with the final answer in the format: \\boxed{YOUR_ANSWER}.\n\n"
        f"Question:\n{question}"
    )


@dataclass
class VLLMRolloutClient:
    base_urls: list[str]
    model: str
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout_sec: int = 900
    max_retries: int = 4
    max_parallel_requests: int | None = None
    seed: int | None = None
    assistant_prefix_content: str | None = None
    max_context: int = 32768
    context_safety_margin: int = 512

    def __post_init__(self) -> None:
        if not self.base_urls:
            raise ValueError("base_urls must not be empty")
        self._url_lock = threading.Lock()
        self._url_cycle = cycle(self.base_urls)

    def _next_url(self) -> str:
        with self._url_lock:
            return next(self._url_cycle)

    def _count_tokens(self, text: str) -> int:
        return len(_enc.encode(text))

    def _safe_max_tokens(self, prompt: str) -> int:
        input_tokens = self._count_tokens(prompt) + 20
        return min(self.max_tokens, self.max_context - input_tokens - self.context_safety_margin)

    def _backoff_max_tokens_from_error(self, error_text: str) -> int | None:
        match = re.search(r"request has (\d+) input tokens", error_text)
        if not match:
            return None
        input_tokens = int(match.group(1))
        safe_max = self.max_context - input_tokens - self.context_safety_margin
        if safe_max < 100:
            return None
        return safe_max

    def _chat_once(self, prompt: str) -> RolloutSample:
        safe_max_tokens = self._safe_max_tokens(prompt)
        if safe_max_tokens < 100:
            raise RuntimeError(f"Prompt too long for rollout generation: safe_max_tokens={safe_max_tokens}")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": safe_max_tokens,
        }
        if self.seed is not None:
            payload["seed"] = self.seed + int(time.time() * 1000) % 100000
        if self.assistant_prefix_content:
            payload["messages"].append(
                {
                    "role": "assistant",
                    "content": self.assistant_prefix_content,
                    "prefix": True,
                }
            )

        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            base_url = self._next_url()
            start = time.time()
            try:
                response = requests.post(
                    f"{base_url}/chat/completions",
                    json=payload,
                    timeout=self.timeout_sec,
                )
                if response.status_code == 400:
                    safe_backoff = self._backoff_max_tokens_from_error(response.text)
                    if safe_backoff is not None and safe_backoff < int(payload["max_tokens"]):
                        payload["max_tokens"] = safe_backoff
                        last_err = RuntimeError(f"Adjusted max_tokens to {safe_backoff} after 400 context error")
                        continue
                response.raise_for_status()
                data = response.json()
                text = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                completion_tokens = int(usage.get("completion_tokens") or 0)
                if completion_tokens <= 0:
                    completion_tokens = max(1, math.ceil(len(text.split()) * 1.3))
                prompt_tokens = usage.get("prompt_tokens")
                duration_sec = round(time.time() - start, 3)
                return RolloutSample(
                    response_text=text,
                    completion_tokens=completion_tokens,
                    prompt_tokens=int(prompt_tokens) if prompt_tokens is not None else None,
                    is_correct=False,
                    base_url=base_url,
                    duration_sec=duration_sec,
                    continuation_steps=split_trace_steps(text),
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
        raise RuntimeError(f"Rollout request failed after {self.max_retries} retries: {last_err}")

    def sample_rollouts(self, question: str, prefix_steps: list[str], target: str, num_rollouts: int) -> list[RolloutSample]:
        prompt = build_rollout_prompt(question, prefix_steps)
        max_workers = self.max_parallel_requests or min(num_rollouts, len(self.base_urls))
        max_workers = max(1, max_workers)

        outputs: list[RolloutSample] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(self._chat_once, prompt) for _ in range(num_rollouts)]
            for future in as_completed(futures):
                sample = future.result()
                sample.is_correct = bool(evaluate_correctness(sample.response_text, target))
                outputs.append(sample)
        return outputs
