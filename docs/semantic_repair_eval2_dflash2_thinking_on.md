# PR5-EVAL2: Qwen3.8 DFlash2 with thinking enabled

## Scope and outcome

This run repeats the v1 semantic-shadow corpus against the existing
Qwen3.8-27B DFlash2 K7 endpoint with `enable_thinking=true`. The override is
evaluation-only; the production pipeline configuration remains unchanged.

Thinking mode is not viable for this bounded structured-output task in the
tested configuration. At the original 512-token budget, all 60 calls exhausted
their budget in reasoning and returned no structured content. Raising the
budget to 4096 improved availability to 50%, but the complete compare/reference
family still exhausted all 4096 tokens without producing content.

The v2 adversarial corpus was not run. With 10 of 20 v1 cases systemically
unavailable at the evaluator's 4096-token ceiling, a 60-case v2 run would not
provide a meaningful semantic comparison and would take multiple hours.

## Inference provenance

```text
container:                qwen38-dflash2-vllm
image:                    qwen38-vllm-dflash2:f94666b-k7
experiment configuration: qwen38-dflash2-k7
configured/served model:  Qwen/Qwen3.8-27B
temperature:              0
speculative method:       dflash2
speculative tokens:       7
chat template:            enable_thinking=true
evaluated branch SHA:     f53aad4e11a7ae9b189856a562b8766fa4588c01
```

Prompt, schema, corpus, routing, contextual gates, and deterministic controls
were unchanged. The offline backend token ceiling was raised from 1024 to 4096
so the requested mode could be evaluated; each artifact records its actual
budget.

## Budget diagnostics

Raw probes used the same prompt, context serialization, JSON schema, model,
temperature, and thinking flag as the evaluator. Only response metadata was
inspected.

| Case | Budget | Finish reason | Completion / reasoning tokens | Content |
|---|---:|---|---:|---|
| first eligible v1 case | 512 | `length` | 512 / 512 | null |
| first eligible v1 case | 1024 | `length` | 1024 / 1024 | null |
| first eligible v1 case | 2048 | `stop` | 1648 / 1545 | JSON, 301 chars |
| `reverse_direction_explicit` | 4096 | `stop` | 2613 / 2506 | JSON, 304 chars |
| `compare_bare` | 4096 | `length` | 4096 / 4096 | null |

The pipeline client correctly classifies HTTP 200 responses without
`message.content` as protocol failures. The shadow backend intentionally
fail-closes them as `unavailable`; they are not transport timeouts or validator
rejections.

## Corpus results

| Metric | Thinking OFF, 512, repeat 3 | Thinking ON, 512, repeat 3 | Thinking ON, 2048, repeat 1 | Thinking ON, 4096, repeat 1 |
|---|---:|---:|---:|---:|
| Model calls | 60 | 60 | 20 | 20 |
| Valid proposals | 45 (75%) | 0 (0%) | 8 (40%) | 10 (50%) |
| Unavailable | 0 | 60 | 11 | 10 |
| Malformed | 0 | 0 | 1 | 0 |
| Validator rejects | 15 | 0 | 0 | 0 |
| Semantic match | 55% | 0% | 30% | 40% |
| Correct PATCH | 18 | 0 | 1 | 3 |
| Correct CLARIFY | 6 | 0 | 2 | 2 |
| Unsafe post-gate | 6 | 0 | 0 | 0 |
| Average latency | 4,186 ms | 18,097 ms | 58,724 ms | 73,130 ms |
| P50 latency | 4,256 ms | 18,265 ms | 61,628 ms | 85,784 ms |
| P95 latency | 5,048 ms | 19,527 ms | 70,248 ms | 90,019 ms |

The OFF result has three repeats, while the 2048 and 4096 viability runs have
one. Counts must therefore not be compared without normalization. In the OFF
run, correct PATCH and CLARIFY counts normalize to 6 and 2 per repeat. The
4096 Thinking ON run produced 3 and 2 respectively.

The zero unsafe count under Thinking ON is not evidence of improved safety:
half of the cases returned no executable proposal. Likewise, the 100% stability
reported by the 512 artifact only means that all three repeats failed in the
same way.

At 4096, these ten cases remained unavailable:

```text
compare_bare
compare_pronoun
compare_last_two
compare_named_seasons
absolute_difference
percent_difference
larger_value
reference_first
reference_second
same_for_first
```

## Artifacts

- `artifacts/semantic_shadow_eval2_qwen38_dflash2_thinking_on_v1_replay.json`
  - controlled original budget, three repeats;
- `artifacts/semantic_shadow_eval2_qwen38_dflash2_thinking_on_2048_v1_viability.json`
  - one-repeat viability pass;
- `artifacts/semantic_shadow_eval2_qwen38_dflash2_thinking_on_4096_v1_replay_r1.json`
  - one-repeat maximum-budget pass.

## Conclusion

`thinking=ON` was verified as actually sent to the endpoint. It does not improve
this shadow evaluator under bounded generation: it is 17-20 times slower at
4096 than Thinking OFF at 512, remains only 50% available, and has lower
semantic match on the full corpus denominator. Keep Thinking OFF for this
contract unless the model's reasoning behavior or output protocol is changed
and independently re-evaluated.

