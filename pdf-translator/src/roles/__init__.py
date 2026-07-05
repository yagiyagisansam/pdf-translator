"""Role-based PDF EN->JA conversion.

Four roles, each a module with a single documented entry point, orchestrated by
`roles.orchestrator`:

  translator (翻訳者)   EN text -> accurate Japanese, protecting non-translatable
                        tokens (numbers, citations, DOIs). Free Google engine by
                        default; Claude/OpenAI when an API key is present.
  producer   (PDF製作者) source PDF -> a layout spec: page geometry, column lanes,
                        figure/table obstacle boxes, and the reading-order role of
                        every text region.
  editor     (編集者)   layout spec + Japanese -> the reconstructed Japanese PDF.
                        Figures/tables stay at their ORIGINAL positions; Japanese
                        body text is REFLOWED down the original columns around
                        those obstacles (not pinned to the English line boxes),
                        so the result reads cleanly like the source, in Japanese.
  qa         (確認者)   original + produced PDF -> a pass/fail report on Japanese
                        validity (placeholders restored, no residual English, text
                        present) AND layout fidelity (figures preserved, no
                        overlaps, everything fits). Drives the retry loop and owns
                        quality: on failure it asks the other roles to redo, with
                        tightened parameters, up to a bounded number of rounds.
"""
