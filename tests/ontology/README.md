# Ontology verification (MetaScheme Rev 0006)

Synthetic checks of the ontology of Objects and relations — no network, no real model.
See `docs/Ontology_Liriel.md`, section *Verification*, for what each one proves.

```
python tests/ontology/verify_ontology_db.py          # persistence: bonds, identity records, draft, atomic commit (JSON backend)
python tests/ontology/verify_ontology_graph.py       # retrieval: filters, limits, records on request, several subjects
python tests/ontology/verify_ontology_cycle.py       # one whole cycle with mocked queries: ledger, IDENTITY_UPDATE, atomicity
python tests/ontology/verify_ontology_mindreader.py  # MindReader API
python tests/ontology/verify_ontology_pg.py dry      # Postgres: migration 010 + the same checks, INSIDE a rolled-back transaction
python tests/ontology/verify_ontology_pg.py live     # Postgres, after 010 is applied: real transaction()/rollback (throw-away rows, removed)
```

The two `pg` modes connect to the database configured in `.env`. `dry` writes nothing that survives
(DDL is transactional in Postgres and the script rolls back); `live` creates and deletes a few rows in a
throw-away MOV (`MOV_ONTOLOGY_TEST`) and refuses to run if its test ids already exist.
