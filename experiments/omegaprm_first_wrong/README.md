# OmegaPRM-style First Wrong Step Search

This package implements a pragmatic OmegaPRM-inspired pipeline for
`bbeh_multistep_arithmetic`.

## What it does

- Uses the existing exact arithmetic oracle from
  `experiments/multistep_react_verifier/arithmetic_tools.py`
- Estimates `MC(prefix)` by sampling `k` rollouts from a vLLM endpoint
- Uses binary search over a sampled reasoning trace to locate the first wrong step
- Reuses sampled rollouts through a lightweight candidate pool scored with an
  OmegaPRM-style `Q + U` heuristic

## Main entry point

```bash
PYTHONPATH=. python -m experiments.omegaprm_first_wrong.run_search \
  --predictions-jsonl runs/deepseek_r1_multistep_full/predictions.jsonl \
  --ports 8000,8001,8002,8003 \
  --model deepseek-ai/DeepSeek-R1-Distill-Qwen-32B \
  --num-samples 5 \
  --k-rollouts 8 \
  --search-limit 20
```

## Notes

- This is a first-error locator for the arithmetic task, not a full PRM trainer.
- The arithmetic engine supplies the reward oracle through final-answer checking.
- The implementation keeps the paper's main structure: Monte Carlo prefix
  estimation, binary search, and rollout reuse via a scored candidate pool.
