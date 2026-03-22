"""Tests for PromptBuilder — verifies prompt structure with a real tokenizer.

Run:
    pytest experiments/omegaprm_first_wrong/test_prompt_builder.py -v
"""
from __future__ import annotations

import pytest

MODEL_NAME = "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B"


@pytest.fixture(scope="module")
def tokenizer():
    try:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(MODEL_NAME, trust_remote_code=True)
    except Exception as exc:
        pytest.skip(f"Cannot load tokenizer for {MODEL_NAME}: {exc}")


@pytest.fixture()
def builder(tokenizer):
    from experiments.omegaprm_first_wrong.rollout import PromptBuilder

    return PromptBuilder(tokenizer=tokenizer)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_QUESTION = "Compute 12 + 34."
SAMPLE_STEPS = [
    "Step 1: We need to add 12 and 34.",
    "Step 2: 12 + 34 = 46.",
]


def _decode(tokenizer, ids: list[int]) -> str:
    return tokenizer.decode(ids, skip_special_tokens=False)


# ---------------------------------------------------------------------------
# Tests — auto-detection
# ---------------------------------------------------------------------------


class TestAutoDetect:
    def test_detects_deepseek(self, builder):
        assert builder.chat_format == "deepseek"


# ---------------------------------------------------------------------------
# Tests — build_prompt_text
# ---------------------------------------------------------------------------


class TestBuildPromptText:
    def test_no_prefix_has_think_tag(self, builder):
        text = builder.build_prompt_text(SAMPLE_QUESTION)
        assert "<think>\n" in text

    def test_with_prefix_steps(self, builder):
        text = builder.build_prompt_text(SAMPLE_QUESTION, prefix_steps=SAMPLE_STEPS)
        assert "Step 1: We need to add 12 and 34." in text
        assert "Step 2: 12 + 34 = 46." in text

    def test_question_before_assistant(self, builder):
        text = builder.build_prompt_text(SAMPLE_QUESTION)
        q_pos = text.find(SAMPLE_QUESTION)
        a_pos = text.find("<think>")
        assert q_pos < a_pos, "Question must appear before <think> block"

    def test_prefix_after_think_tag(self, builder):
        text = builder.build_prompt_text(SAMPLE_QUESTION, prefix_steps=SAMPLE_STEPS)
        think_pos = text.find("<think>\n")
        step_pos = text.find("Step 1:")
        assert step_pos > think_pos, "Prefix steps must be after <think> tag"

    def test_system_prompt(self, tokenizer):
        from experiments.omegaprm_first_wrong.rollout import PromptBuilder

        builder = PromptBuilder(
            tokenizer=tokenizer, system_prompt="You are a math solver."
        )
        text = builder.build_prompt_text(SAMPLE_QUESTION)
        q_pos = text.find(SAMPLE_QUESTION)
        sys_pos = text.find("You are a math solver.")
        assert sys_pos < q_pos, "System prompt must appear before question"


# ---------------------------------------------------------------------------
# Tests — build_prompt_token_ids (round-trip via decode)
# ---------------------------------------------------------------------------


class TestBuildPromptTokenIds:
    def test_no_prefix_roundtrip(self, builder, tokenizer):
        ids = builder.build_prompt_token_ids(SAMPLE_QUESTION)
        decoded = _decode(tokenizer, ids)
        assert SAMPLE_QUESTION in decoded

    def test_with_prefix_roundtrip(self, builder, tokenizer):
        ids = builder.build_prompt_token_ids(SAMPLE_QUESTION, prefix_steps=SAMPLE_STEPS)
        decoded = _decode(tokenizer, ids)
        assert "Step 1:" in decoded
        assert "Step 2:" in decoded

    def test_prefix_steps_after_think(self, builder, tokenizer):
        ids = builder.build_prompt_token_ids(SAMPLE_QUESTION, prefix_steps=SAMPLE_STEPS)
        decoded = _decode(tokenizer, ids)
        think_pos = decoded.find("<think>")
        step_pos = decoded.find("Step 1:")
        assert step_pos > think_pos, (
            "Prefix steps must appear AFTER the <think> tag"
        )

    def test_no_trailing_eos(self, builder, tokenizer):
        """The prompt must NOT close the assistant turn — model continues."""
        ids = builder.build_prompt_token_ids(SAMPLE_QUESTION, prefix_steps=SAMPLE_STEPS)
        eos_id = tokenizer.eos_token_id
        assert ids[-1] != eos_id, (
            "Last token must not be EOS — assistant turn is open"
        )

    def test_starts_with_bos(self, builder, tokenizer):
        ids = builder.build_prompt_token_ids(SAMPLE_QUESTION)
        bos_id = tokenizer.bos_token_id
        assert ids[0] == bos_id, "First token must be BOS"

    def test_empty_prefix_same_as_none(self, builder):
        ids_none = builder.build_prompt_token_ids(SAMPLE_QUESTION, prefix_steps=None)
        ids_empty = builder.build_prompt_token_ids(SAMPLE_QUESTION, prefix_steps=[])
        assert ids_none == ids_empty

    def test_question_not_in_assistant_section(self, builder, tokenizer):
        """Question must be in user section, not repeated after <think>."""
        ids = builder.build_prompt_token_ids(SAMPLE_QUESTION, prefix_steps=SAMPLE_STEPS)
        decoded = _decode(tokenizer, ids)
        think_pos = decoded.find("<think>")
        after_think = decoded[think_pos:]
        assert SAMPLE_QUESTION not in after_think


# ---------------------------------------------------------------------------
# Tests — count_tokens
# ---------------------------------------------------------------------------


class TestCountTokens:
    def test_positive(self, builder):
        assert builder.count_tokens("Hello, world!") > 0

    def test_matches_tokenizer(self, builder, tokenizer):
        text = "The answer is \\boxed{42}"
        expected = len(tokenizer.encode(text, add_special_tokens=False))
        assert builder.count_tokens(text) == expected


# ---------------------------------------------------------------------------
# Tests — stop_token_ids
# ---------------------------------------------------------------------------


class TestStopTokenIds:
    def test_contains_eos(self, builder, tokenizer):
        eos_id = tokenizer.eos_token_id
        assert eos_id in builder.stop_token_ids


# ---------------------------------------------------------------------------
# Tests — _extract_answer_from_rollout
# ---------------------------------------------------------------------------


class TestExtractAnswerFromRollout:
    def test_boxed_simple(self):
        from experiments.omegaprm_first_wrong.rollout import _extract_answer_from_rollout
        text = "some reasoning...\n\\boxed{Yes}"
        assert _extract_answer_from_rollout(text) == "Yes"

    def test_boxed_with_trailing_text(self):
        from experiments.omegaprm_first_wrong.rollout import _extract_answer_from_rollout
        text = "reasoning\n\\boxed{42}\n\nmore text"
        assert _extract_answer_from_rollout(text) == "42"

    def test_boxed_nested_braces(self):
        from experiments.omegaprm_first_wrong.rollout import _extract_answer_from_rollout
        text = "\\boxed{\\frac{1}{2}}"
        assert _extract_answer_from_rollout(text) == "\\frac{1}{2}"

    def test_boxed_last_wins(self):
        from experiments.omegaprm_first_wrong.rollout import _extract_answer_from_rollout
        text = "\\boxed{wrong}\nwait...\n\\boxed{correct}"
        assert _extract_answer_from_rollout(text) == "correct"

    def test_think_close_fallback(self):
        from experiments.omegaprm_first_wrong.rollout import _extract_answer_from_rollout
        text = "reasoning here</think>\n\nYes"
        assert _extract_answer_from_rollout(text) == "Yes"

    def test_think_close_multiline(self):
        from experiments.omegaprm_first_wrong.rollout import _extract_answer_from_rollout
        text = "blah</think>\n\nNo\n\nExplanation here"
        assert _extract_answer_from_rollout(text) == "No"

    def test_no_answer(self):
        from experiments.omegaprm_first_wrong.rollout import _extract_answer_from_rollout
        text = "just reasoning with no conclusion"
        assert _extract_answer_from_rollout(text) is None

    def test_boxed_with_whitespace(self):
        from experiments.omegaprm_first_wrong.rollout import _extract_answer_from_rollout
        text = "\\boxed{ Ambiguous }\n"
        assert _extract_answer_from_rollout(text) == "Ambiguous"
