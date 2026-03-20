from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from experiments.multistep_react_verifier.arithmetic_tools import MultiStepArithmeticEngine

@dataclass
class VLLMChatClient:
    base_url: str
    model: str
    timeout_sec: int = 600
    seed: int | None = None

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.0, max_tokens: int = 512) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if self.seed is not None:
            payload["seed"] = self.seed
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        data = resp.json()
        if "choices" not in data or not data["choices"]:
            raise RuntimeError(f"Invalid chat response: {data}")
        return data["choices"][0]["message"]["content"]


class MultiStepReActStepVerifier:
    def __init__(
        self,
        engine: MultiStepArithmeticEngine,
        client: VLLMChatClient,
        max_turns: int = 6,
    ):
        self.engine = engine
        self.client = client
        self.max_turns = max_turns

    @staticmethod
    def split_trace_steps(trace: str) -> list[str]:
        chunks = [c.strip() for c in re.split(r"\n\s*\n", trace) if c.strip()]
        if len(chunks) > 1:
            return chunks
        lines = [ln.strip() for ln in trace.splitlines() if ln.strip()]
        return lines

    def verify_step(
        self,
        question: str,
        step_text: str,
        step_id: int,
        prev_step_text: str = "",
        next_step_text: str = "",
    ) -> dict[str, Any]:
        system_prompt = (
            "You are a strict step-level verifier for a Multi-step Arithmetic reasoning trace.\n"
            "You must classify CURRENT_STEP as one of: correct, incorrect, neutral.\n"
            "Use tools before deciding when there is a concrete claim.\n\n"
            "Available tools:\n"
            "1) get_ground_truth() -> returns computed A, B, C, FINAL for this question.\n"
            "2) evaluate_expression(expr, variables) -> integer value for a single expression.\n"
            "3) check_equation(equation, variables) -> checks if LHS == RHS.\n\n"
            "Output format (STRICT JSON ONLY):\n"
            "For tool call:\n"
            '{"action":"tool","tool":"evaluate_expression|check_equation|get_ground_truth","arguments":{...}}\n'
            "For final answer:\n"
            '{"action":"final","verdict":"correct|incorrect|neutral","reason":"...","claim":"..."}\n\n'
            "Guidelines:\n"
            "- Use PREV_STEP and NEXT_STEP as context, but verify only CURRENT_STEP's claim.\n"
            "- 'neutral' if CURRENT_STEP is generic planning, assumption, or non-verifiable commentary.\n"
            "- 'incorrect' if step contains a verifiable false claim.\n"
            "- 'correct' if verifiable claims in CURRENT_STEP are all true.\n"
            "- Keep reason concise and factual."
        )

        user_prompt = (
            f"QUESTION:\n{question}\n\n"
            f"PREV_STEP:\n{prev_step_text if prev_step_text else '[NONE]'}\n\n"
            f"CURRENT_STEP (#{step_id}):\n{step_text}\n\n"
            f"NEXT_STEP:\n{next_step_text if next_step_text else '[NONE]'}\n\n"
            "Decide using tools if needed."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        react_trace: list[dict[str, Any]] = []

        for _ in range(self.max_turns):
            try:
                raw = self.client.chat(messages, temperature=0.0, max_tokens=400)
            except Exception as exc:  # noqa: BLE001
                react_trace.append({"assistant_error": repr(exc)})
                return {
                    "step_id": step_id,
                    "step_text": step_text,
                    "prev_step_text": prev_step_text,
                    "next_step_text": next_step_text,
                    "verdict": "neutral",
                    "reason": f"Verifier chat error: {repr(exc)}",
                    "react_trace": react_trace,
                }

            parsed = parse_json_like(raw)
            if not isinstance(parsed, dict) or "action" not in parsed:
                react_trace.append({"assistant_raw": raw, "parse_error": True})
                return {
                    "step_id": step_id,
                    "step_text": step_text,
                    "prev_step_text": prev_step_text,
                    "next_step_text": next_step_text,
                    "verdict": "neutral",
                    "reason": "Verifier output format invalid; treated as neutral.",
                    "react_trace": react_trace,
                }

            action = str(parsed.get("action", "")).strip().lower()
            react_trace.append({"assistant": parsed})

            if action == "final":
                verdict = str(parsed.get("verdict", "neutral")).strip().lower()
                if verdict not in {"correct", "incorrect", "neutral"}:
                    verdict = "neutral"
                return {
                    "step_id": step_id,
                    "step_text": step_text,
                    "prev_step_text": prev_step_text,
                    "next_step_text": next_step_text,
                    "verdict": verdict,
                    "reason": str(parsed.get("reason", "")),
                    "claim": str(parsed.get("claim", "")),
                    "react_trace": react_trace,
                }

            if action != "tool":
                return {
                    "step_id": step_id,
                    "step_text": step_text,
                    "prev_step_text": prev_step_text,
                    "next_step_text": next_step_text,
                    "verdict": "neutral",
                    "reason": "Unknown verifier action; treated as neutral.",
                    "react_trace": react_trace,
                }

            tool_name = str(parsed.get("tool", "")).strip()
            tool_args = parsed.get("arguments", {}) or {}
            observation = self._run_tool(tool_name, tool_args)
            react_trace.append({"tool": tool_name, "arguments": tool_args, "observation": observation})

            messages.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})
            messages.append(
                {
                    "role": "user",
                    "content": "TOOL_OBSERVATION:\n" + json.dumps(observation, ensure_ascii=False),
                }
            )

        return {
            "step_id": step_id,
            "step_text": step_text,
            "prev_step_text": prev_step_text,
            "next_step_text": next_step_text,
            "verdict": "neutral",
            "reason": "Reached max ReAct turns without final verdict.",
            "react_trace": react_trace,
        }

    def _run_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if tool_name == "get_ground_truth":
                return {"ok": True, "result": self.engine.compute_named_values()}

            if tool_name == "evaluate_expression":
                expr = str(arguments.get("expr", ""))
                variables = arguments.get("variables", {}) or {}
                val = self.engine.evaluate_expression(expr, variables)
                return {"ok": True, "result": val}

            if tool_name == "check_equation":
                equation = str(arguments.get("equation", ""))
                variables = arguments.get("variables", {}) or {}
                result = self.engine.check_equation(equation, variables)
                return {"ok": True, "result": result}

            return {"ok": False, "error": f"Unknown tool: {tool_name}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": repr(exc)}


def parse_json_like(text: str) -> Any:
    s = text.strip()
    if not s:
        return None

    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        pass

    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except Exception:  # noqa: BLE001
            pass

    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        fragment = s[start : end + 1]
        try:
            return json.loads(fragment)
        except Exception:  # noqa: BLE001
            pass

    return None


def wait_for_server(base_url: str, timeout_sec: int = 1200) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            resp = requests.get(f"{base_url}/models", timeout=10)
            if resp.ok:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    raise TimeoutError(f"vLLM server not ready at {base_url} within {timeout_sec}s")
