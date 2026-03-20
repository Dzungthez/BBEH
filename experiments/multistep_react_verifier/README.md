# Multi-step Arithmetic ReAct Step Verifier

This folder contains a tool-assisted, ReAct-style step-level verifier for
`bbeh_multistep_arithmetic`.

## Components

- `arithmetic_tools.py`
  - Parses custom operator definitions from a BBEH Multi-step Arithmetic question.
  - Evaluates expressions with composed operators (e.g., `+*+`, `*<>*`) by
    decomposing into atomic ops and applying sequentially.
  - Validates exactness on the full dataset with
    `validate_engine_on_multistep_dataset`.
- `react_verifier.py`
  - ReAct loop that uses an LLM verifier + tools:
    - `get_ground_truth`
    - `evaluate_expression`
    - `check_equation`
  - Produces step verdict: `correct` / `incorrect` / `neutral`.
- `run_verify_5_samples.py`
  - Validates the arithmetic engine on all 200 Multi-step Arithmetic samples.
  - Loads reasoning traces from another model's predictions JSONL.
  - Randomly selects 5 samples and verifies step-level verdicts.
  - Writes detailed JSON + readable markdown logs.

## Run

```bash
PYTHONPATH=. python experiments/multistep_react_verifier/run_verify_5_samples.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3-Next-80B-A3B-Instruct
```

Logs are written to `experiments/multistep_react_verifier/logs/`.
