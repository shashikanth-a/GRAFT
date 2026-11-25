"""LLM-judge monitors for GRAFT on Countdown-Code (follow-up experiment).

Everything specific to the "GRAFT with LLM monitors" study lives in this
package; the upstream trainer is touched only at three small hook points
(rh_detectors.py registry, train.py Step C, advantages.py), each marked
`[llm_monitor]`.

  judge.py         the judge: prompt, P(hack) scoring, cache, usage accounting,
                   and the `llm_judge_countdown` rh_detector
  soft_routing.py  confidence-weighted routing (per-sample gradient masks)
  hooks.py         trainer-side glue: soft scores + judge-quality logging
  metrics.py       AUROC / per-hack-form confusion (pure Python)
  variants.py      config YAML variants (monitor swapped, rest identical)
  validate.py      offline judge validation -> frozen judge_config.json
  sft_prime.py     the repo's SFT priming recipe without Modal
  pipeline.py      single entry point: staged run with gates
  report.py        REPORT.md + chart from finished runs
"""
