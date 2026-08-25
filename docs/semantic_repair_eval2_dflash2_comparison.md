# PR5-EVAL2: Qwen3.8 DFlash2 comparison

## Scope

This is optional secondary inference evidence for PR5-EVAL2. It does not change
the primary Qwen3.8 MTP result, prompt, contract, corpus, evaluator, production
runtime, or the decision to keep semantic repair shadow-only.

The comparison ran the same two corpora with temperature zero and three repeats
per model case:

- v1 replay: 60 model calls;
- v2 adversarial: 180 model calls.

Both runs completed with zero timeout, unavailable, or malformed results and
100% whole-corpus repeat stability.

## Inference provenance

```text
container:                qwen38-dflash2-vllm
image:                    qwen38-vllm-dflash2:f94666b-k7
experiment configuration: qwen38-dflash2-k7
configured model:         Qwen/Qwen3.8-27B
served model:             Qwen/Qwen3.8-27B
temperature:              0
max tokens:               512
speculative method:       dflash2
speculative tokens:       7
evaluated branch SHA:     46bba0a7d6bdca40f01c8b5f2575cfff86a710ba
```

The artifact provenance uses the evaluator's historical `mtp_method` field
name with value `dflash2`; it does not claim that DFlash2 is MTP. Prompt,
contract/schema, and corpus hashes match the primary MTP artifacts exactly.

## V1 replay comparison

| Metric | MTP K1 | DFlash2 K7 | Delta |
|---|---:|---:|---:|
| Calls | 60 | 60 | 0 |
| Structural acceptance | 80.00% | 75.00% | -5.00 pp |
| Raw semantic match | 65.00% | 55.00% | -10.00 pp |
| Correct PATCH | 18 | 18 | 0 |
| Correct CLARIFY | 12 | 6 | -6 |
| Unsafe raw/post-gate | 6/6 | 6/6 | 0/0 |
| Stability | 100% | 100% | 0 pp |
| Average latency | 7,720.25 ms | 4,186.13 ms | 1.84x faster |
| P50 latency | 7,735.0 ms | 4,256.0 ms | 1.82x faster |
| P95 latency | 10,685 ms | 5,048 ms | 2.12x faster |

Forty-five of 60 raw signatures were identical. DFlash2 improved zero runs and
regressed six: all three repeats of `correction_geo` and
`correction_business`. Its clarification correctness was 50% versus 100% for
the MTP v1 replay.

## V2 adversarial comparison

| Metric | MTP K1 | DFlash2 K7 | Delta |
|---|---:|---:|---:|
| Calls | 180 | 180 | 0 |
| Structural acceptance | 75.00% | 73.33% | -1.67 pp |
| Raw semantic match | 50.00% | 48.33% | -1.67 pp |
| Correct PATCH | 54 | 48 | -6 |
| Correct CLARIFY | 30 | 33 | +3 |
| Clarification correctness | 47.62% | 52.38% | +4.76 pp |
| Unsafe raw | 39 | 39 | 0 |
| Unsafe post-gate | 21 | 30 | +9 |
| Stability | 100% | 100% | 0 pp |
| Average latency | 7,274.26 ms | 3,931.63 ms | 1.85x faster |
| P50 latency | 6,609.0 ms | 3,847.5 ms | 1.72x faster |
| P95 latency | 10,970 ms | 5,014 ms | 2.19x faster |

One hundred forty-seven of 180 raw signatures were identical. Relative to MTP,
DFlash2 changed correctness as follows:

- MTP correct, DFlash2 wrong: 12 runs / 4 unique cases;
- MTP wrong, DFlash2 correct: 9 runs / 3 unique cases;
- both correct: 78 runs.

DFlash2 regressions were `near_n19_direction_group`,
`ref_r08_zero_previous`, `swap_p11_second_to_first`, and
`swap_p14_route_reverse`. Improvements were `near_n08_zero_relation_short`,
`near_n18_opposite_metric`, and `near_n23_incomplete_named`.

## Swap-direction comparison

| Metric | MTP K1 | DFlash2 K7 |
|---|---:|---:|
| Positive unique cases | 16 | 16 |
| Positive total runs | 48 | 48 |
| Exact correct swaps | 48 | 42 |
| Exact false negatives | 0 | 6 |
| Raw false-positive swaps | 27 | 18 |
| Post-G4 false-positive swaps | 9 | 9 |
| Post-gate precision | 84.21% | 82.35% |
| Exact recall | 100.00% | 87.50% |
| G4 rejects | 18 | 9 |
| Stability | 100% | 100% |

The six exact false negatives are stable over-specification on two positive
cases. DFlash2 emitted `swap_direction` plus an unnecessary
`previous_result` reference for `swap_p11_second_to_first` and
`swap_p14_route_reverse`. The post-gate false-positive families are unchanged:

- `near_n04_ask_availability`;
- `near_n11_new_query`;
- `near_n12_correction`.

The dominant erroneous raw signature remains a single `swap_direction` PATCH,
but occurs 15 times for DFlash2 versus 27 times for MTP. This does not improve
accepted swap safety because both configurations retain nine post-G4 false
positives.

## Per-kind observations

DFlash2 modestly improves v2 clarification precision/recall but worsens
operation/comparison precision and exact swap results. Reference selection
remains unusable: zero accepted correct proposals across 21 expected runs.
The lower raw swap false-positive count is offset by fewer useful G4 rejections
and more unsafe non-swap accepted PATCHes, producing 30 unsafe post-gate outputs
versus 21 for MTP.

## Conclusion

DFlash2 K7 is materially faster: about 1.8x by average/P50 and over 2x by P95.
It is not semantically superior on these corpora. V1 replay quality is clearly
worse, v2 aggregate quality is slightly worse, accepted unsafe transitions rise
from 21 to 30, and swap precision remains below the activation threshold.

The PR5-EVAL2 recommendation is unchanged:

```text
general active semantic repair: NO
narrow swap_direction PR6:      NO
shadow-only evaluation:         CONTINUE
```
