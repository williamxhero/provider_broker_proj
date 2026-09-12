# Provider+Model pricing cutover release record

- Implementation revision: `a91176b31cd359e19081f4ad875ff086a6338d7e`
- Scope: SPEC #59, TICKET #66, TICKET #67
- Pricing migration version: `1`
- Runtime rate authority: active Provider+Model rows; Model remains stage/family metadata and Provider remains inventory identity.
- Compatibility behavior: the legacy catalog is projected only when the active official row is unique; ambiguous legacy writes are rejected and ambiguous public projections return unknown/conflict.
- History policy: existing observation costs are retained; no historical billing recomputation is performed.
- Rollback: migration work is transactional; failures preserve a failed migration record and leave domain writes rolled back. Reverting the candidate commit restores the prior code path without rewriting observations.

## Verification

- `python -m pytest -q`: 157 passed
- Targeted cutover/API/UI suite: 75 passed
- `python -m compileall -q provider_broker scripts`: passed
- `git diff --check`: passed
- Production-shape smoke coverage: passed in the full test suite
- Independent review: Standards and Spec axes both passed after correcting the remaining global-price fallbacks in upstream cost calculation and the model-view console.
