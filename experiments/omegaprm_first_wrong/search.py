from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from experiments.omegaprm_first_wrong.rollout import RolloutClient, RolloutSample


@dataclass
class SearchState:
    prefix_steps: tuple[str, ...]
    visit_count: int = 0
    mc_estimate: float | None = None
    rollouts: list[RolloutSample] = field(default_factory=list)
    pooled_rollout_ids: set[str] = field(default_factory=set)

    @property
    def state_id(self) -> str:
        return f"prefix:{len(self.prefix_steps)}:{abs(hash(self.prefix_steps))}"


@dataclass
class CandidateRollout:
    candidate_id: str
    state_id: str
    prefix_steps: tuple[str, ...]
    continuation_steps: tuple[str, ...]
    response_text: str
    completion_tokens: int
    is_correct: bool
    source: str


@dataclass
class BinarySearchProbe:
    global_step_index_1based: int
    local_step_index_1based: int
    prefix_len: int
    mc_estimate: float
    state_id: str


@dataclass
class LocatedFirstWrong:
    found: bool
    first_wrong_step_index_1based: int | None
    previous_step_index_1based: int | None
    search_path: list[BinarySearchProbe]
    selected_rollout_source: str
    selected_rollout_num_steps: int


@dataclass
class SearchResult:
    root_mc: float
    source_first_wrong: LocatedFirstWrong | None
    selected_rollout_results: list[LocatedFirstWrong]
    num_states: int
    num_rollouts_total: int
    num_candidates_remaining: int
    tree_summary: list[dict[str, Any]]


class OmegaPRMFirstWrongSearch:
    def __init__(
        self,
        question: str,
        target: str,
        source_steps: list[str],
        rollout_client: RolloutClient,
        k_rollouts: int = 8,
        search_limit: int = 20,
        alpha: float = 0.5,
        beta: float = 0.9,
        length_penalty_L: int = 500,
        c_puct: float = 0.125,
    ):
        self.question = question
        self.target = str(target)
        self.source_steps = [step.strip() for step in source_steps if step.strip()]
        self.rollout_client = rollout_client
        self.k_rollouts = k_rollouts
        self.search_limit = search_limit
        self.alpha = alpha
        self.beta = beta
        self.length_penalty_L = length_penalty_L
        self.c_puct = c_puct

        self.states: dict[tuple[str, ...], SearchState] = {}
        self.candidate_pool: list[CandidateRollout] = []
        self.num_rollouts_total = 0
        self._candidate_counter = 0

    def run(self) -> SearchResult:
        root_state = self._get_state(())
        root_mc = self._ensure_state_rollouts(root_state)

        source_first_wrong: LocatedFirstWrong | None = None
        if self.source_steps:
            source_first_wrong = self._binary_search_rollout(
                start_state=root_state,
                rollout_steps=self.source_steps,
                rollout_source="source_trace",
            )

        selected_rollout_results: list[LocatedFirstWrong] = []
        while len(selected_rollout_results) < self.search_limit:
            candidate = self._select_candidate()
            if candidate is None:
                break
            state = self._get_state(candidate.prefix_steps)
            state.visit_count += 1
            located = self._binary_search_rollout(
                start_state=state,
                rollout_steps=list(candidate.continuation_steps),
                rollout_source=candidate.source,
            )
            selected_rollout_results.append(located)

        tree_summary = [
            {
                "state_id": state.state_id,
                "prefix_len": len(state.prefix_steps),
                "visit_count": state.visit_count,
                "mc_estimate": state.mc_estimate,
                "num_rollouts": len(state.rollouts),
            }
            for state in sorted(self.states.values(), key=lambda item: (len(item.prefix_steps), item.state_id))
        ]

        return SearchResult(
            root_mc=root_mc,
            source_first_wrong=source_first_wrong,
            selected_rollout_results=selected_rollout_results,
            num_states=len(self.states),
            num_rollouts_total=self.num_rollouts_total,
            num_candidates_remaining=len(self.candidate_pool),
            tree_summary=tree_summary,
        )

    def _get_state(self, prefix_steps: tuple[str, ...] | list[str]) -> SearchState:
        key = tuple(prefix_steps)
        if key not in self.states:
            self.states[key] = SearchState(prefix_steps=key)
        return self.states[key]

    def _ensure_state_rollouts(self, state: SearchState) -> float:
        missing = self.k_rollouts - len(state.rollouts)
        if missing > 0:
            new_rollouts = self.rollout_client.sample_rollouts(
                question=self.question,
                prefix_steps=list(state.prefix_steps),
                target=self.target,
                num_rollouts=missing,
            )
            state.rollouts.extend(new_rollouts)
            self.num_rollouts_total += len(new_rollouts)

        if not state.rollouts:
            state.mc_estimate = 0.0
        else:
            state.mc_estimate = sum(1 for rollout in state.rollouts if rollout.is_correct) / len(state.rollouts)

        if 0.0 < state.mc_estimate < 1.0:
            for rollout in state.rollouts:
                if rollout.is_correct or not rollout.continuation_steps:
                    continue
                rollout_id = self._rollout_id(state, rollout)
                if rollout_id in state.pooled_rollout_ids:
                    continue
                state.pooled_rollout_ids.add(rollout_id)
                self._candidate_counter += 1
                self.candidate_pool.append(
                    CandidateRollout(
                        candidate_id=f"cand-{self._candidate_counter}",
                        state_id=state.state_id,
                        prefix_steps=state.prefix_steps,
                        continuation_steps=tuple(rollout.continuation_steps),
                        response_text=rollout.response_text,
                        completion_tokens=rollout.completion_tokens,
                        is_correct=rollout.is_correct,
                        source=f"sampled@{state.state_id}",
                    )
                )

        return state.mc_estimate

    def _rollout_id(self, state: SearchState, rollout: RolloutSample) -> str:
        key = (state.state_id, tuple(rollout.continuation_steps), rollout.response_text[:120])
        return f"{abs(hash(key))}"

    def _select_candidate(self) -> CandidateRollout | None:
        if not self.candidate_pool:
            return None

        total_visits = sum(state.visit_count for state in self.states.values())
        total_visits = max(total_visits, 1)
        best_idx = 0
        best_score = float("-inf")
        for idx, candidate in enumerate(self.candidate_pool):
            state = self._get_state(candidate.prefix_steps)
            mc = state.mc_estimate if state.mc_estimate is not None else self._ensure_state_rollouts(state)
            q_score = (self.alpha ** (1.0 - mc)) * (self.beta ** (candidate.completion_tokens / self.length_penalty_L))
            u_score = self.c_puct * math.sqrt(total_visits) / (1 + state.visit_count)
            score = q_score + u_score
            if score > best_score:
                best_score = score
                best_idx = idx
        return self.candidate_pool.pop(best_idx)

    def _binary_search_rollout(
        self,
        start_state: SearchState,
        rollout_steps: list[str],
        rollout_source: str,
    ) -> LocatedFirstWrong:
        if not rollout_steps:
            return LocatedFirstWrong(
                found=False,
                first_wrong_step_index_1based=None,
                previous_step_index_1based=None,
                search_path=[],
                selected_rollout_source=rollout_source,
                selected_rollout_num_steps=0,
            )

        probes: list[BinarySearchProbe] = []
        lo = 0
        hi = len(rollout_steps) - 1
        prefix_base = list(start_state.prefix_steps)

        while lo < hi:
            mid = (lo + hi) // 2
            probe_prefix = tuple(prefix_base + rollout_steps[: mid + 1])
            probe_state = self._get_state(probe_prefix)
            mc_estimate = self._ensure_state_rollouts(probe_state)
            probes.append(
                BinarySearchProbe(
                    global_step_index_1based=len(probe_prefix),
                    local_step_index_1based=mid + 1,
                    prefix_len=len(probe_prefix),
                    mc_estimate=mc_estimate,
                    state_id=probe_state.state_id,
                )
            )
            if mc_estimate > 0:
                lo = mid + 1
            else:
                hi = mid

        final_prefix = tuple(prefix_base + rollout_steps[: lo + 1])
        final_state = self._get_state(final_prefix)
        final_mc = self._ensure_state_rollouts(final_state)
        final_probe = BinarySearchProbe(
            global_step_index_1based=len(final_prefix),
            local_step_index_1based=lo + 1,
            prefix_len=len(final_prefix),
            mc_estimate=final_mc,
            state_id=final_state.state_id,
        )
        if not probes or probes[-1] != final_probe:
            probes.append(final_probe)

        found = final_mc == 0.0
        first_wrong = len(prefix_base) + lo + 1 if found else None
        prev_index = first_wrong - 1 if found and first_wrong > 1 else None
        return LocatedFirstWrong(
            found=found,
            first_wrong_step_index_1based=first_wrong,
            previous_step_index_1based=prev_index,
            search_path=probes,
            selected_rollout_source=rollout_source,
            selected_rollout_num_steps=len(rollout_steps),
        )
