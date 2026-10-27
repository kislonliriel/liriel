# Contributing to Liriel

Thank you for wanting to help. Liriel is an independent, non-commercial research project by [Kislon.ai](https://kislon.ai): an open
architecture for AI that lives with people for years, where preventing harmful emotional dependency is a design requirement. The work is
early, and it gets better every time someone reads it critically.

You do not need to be a programmer to contribute. Some of the most useful help is a careful reader saying where the architecture is wrong.

## Ways to help

| You are… | You can… |
|---|---|
| An engineer | Pick up an issue labelled `good first issue`, add a test, improve a tool (MindReader, the CLI), or propose a change to the code. |
| A researcher in cognitive science, philosophy of mind or HCI | Challenge the assumptions of the [MetaScheme](docs/MetaScheme_Liriel_Rev0008.md) and of *Volume 1* (at [kislon.ai](https://kislon.ai)); propose experiments that could falsify them. |
| A psychologist or clinician | Review how the architecture handles attachment, dependency, risk and the duty of care, and tell us what does not match real practice. |
| Anyone who tried it | Report what confused you, what broke, and what felt wrong in a conversation with Liriel. |
| A translator or writer | Make the README and the documents readable for more people. |

## Before you start

1. Read the [README](README.md) and run Liriel once with your own Claude Code (it takes a few minutes and needs no API key).
2. Install the dependencies and run the synthetic checks — no network, no real model:

   ```bash
   pip install -r requirements.txt
   python tests/run_all.py
   ```

   It prints one PASS/FAIL line per check, shows the output of the failing ones, and exits non-zero if any fails
   (`verify_ontology_pg.py` is skipped: it needs Postgres).

3. Look at the open [issues](https://github.com/kislonliriel/liriel/issues). Comment on one before you start, so two people do not do the same
   work.

## How to propose a change

- **Small fixes** (a typo, a broken link, a clearer sentence): open a pull request directly.
- **Code changes:** open or comment on an issue first, then a pull request that does one thing, with a test when the change can be tested.
  Every `verify_*.py` runs without a model and exits non-zero on failure; follow the same pattern.
- **Changes to the architecture** (the MetaScheme, the ontology, a query or its contract): open an issue describing the problem first, with
  an example — a message, what the cycle did, and what it should have done. The MetaScheme is never edited between cycles of one session
  (§0.6); changes are made as a new revision.

## Principles we keep

- **The core stays public.** Code and specifications are open under the [MIT License](LICENSE).
- **Safety before capability.** Work that protects the person — the right to step back, honest disclosure that Liriel is an AI, the duty
  of care — comes before work that makes the system more engaging.
- **No credentials, no personal data.** Never commit a `.env`, a key, a token or anyone's conversations. Liriel's memory (`data/store/`)
  stays on your own machine.
- **Honest claims.** Liriel is not a person and is not conscious; nothing we write should suggest otherwise.
- **Respectful disagreement.** Criticise ideas, never people. Harassment of any kind is not welcome here.

## Contact

Questions, ideas or collaboration proposals: [contact@kislon.ai](mailto:contact@kislon.ai). News and early releases:
[the Kislon Dispatch](https://kislon.ai/#dispatch).
