# Liriel — Phase 1 (MVP aligned to the official MetaScheme)

> **⚠️ Status: Work in Progress.** This code is pushed incrementally as the
> project evolves and has **not** been fully tested end to end. Interfaces,
> the database schema, and the prompts may change without notice. Treat
> everything here as a proof of concept, not a stable release.

Implementation of the `ProcessMotivation` cycle from the Liriel
architecture, driven by the official MetaScheme —
[docs/MetaScheme_Liriel_Rev0000.md](docs/MetaScheme_Liriel_Rev0000.md) —
which is loaded verbatim as the system prompt on every LLM call ("MS §n"
below cites its sections).

## Scope of this phase

The cycle follows MS §11.1 faithfully **except** for what the MetaScheme
itself allows a Phase to leave out (MS §0.6: "any QUERY block may narrow
this document but may not contradict §1"):

- **GRAPH_REQUEST (MS §12.1) and the Graph of Traces (MS §8) are
  implemented.** `graph_service.py` is TrackGraphProcess: it traverses
  `mov_relations` (written by `WRITE_RELATION`) across the focus and the
  archive, from the Objects Query 1 names, up to the depth it asks for.
  MS §7/§8 leave the archive's retrieval mechanics and a graph's size
  unspecified ("the form is negotiable, the requirement is not") — this
  phase's answer: rank archived candidates by (1) emotional charge (MS
  §7's own "organized by affective weight", operationalized as the sum of
  `|v|` over an Object's Feeling axes) then (2) recency, and cap how many
  get in via `GRAPH_MAX_NODES` (.env, adjustable). A model can temporarily
  raise that cap for one request by setting `deep_recall_requested: true`
  — meant for when the user explicitly insists Liriel make an effort to
  remember something specific — which uses `GRAPH_MAX_NODES_BOOSTED`
  instead; nothing persists afterward, the next cycle's request simply
  doesn't set the flag. Ordinances (MS §4), Modulating Schemas (MS §5) and
  the full `objective`/`delta_report` blocks (MS §10) already ran as
  specified since the MetaScheme-alignment pass.
- **MainMemory (MS §7)** is the same `mov_objects` table, filtered by an
  `archived_at` column instead of a separate store. `RETRIEVE`/`ARCHIVE`/
  `WRITE_RELATION`/`SOFTEN_CHARGE` (MS §12.4) are all executed now;
  `SEARCH` (free-text/filtered lookup without a known id) still needs a
  real index and is only logged.
- **Nested MOVs (MS §6.8, specular recursion) are materialized.** A
  `CREATE_NESTED_MOV`/`PATCH_NESTED_VOV`/`ARCHIVE_NESTED_MOV` op creates and
  updates a real `mov_id`, stored the same way the default MOV is (the
  reference-dataset import already proved this generic per-mov_id storage
  out). Materialized nested MOVs are gathered (up to `NESTED_MOV_MAX_DEPTH`
  mirror levels, .env, MS §6.8 rule d) and sent back to the model as their
  own Artifact block on every query — this is how, e.g., "what Fábio
  himself thinks of Mike" can be kept distinct from Liriel's own read of
  Mike, instead of one-sided testimony quietly overwriting her independent
  model of a third party.
- **ProcessCommandControl** isn't a separate call. The MetaScheme leaves
  embodiment open (MS §9.3) and defines no output contract for "compose
  the actual reply" — Phase 1 adds one plain-text call (not part of MS §12)
  that turns the Best-Prey Guess's handoff into what Liriel actually says,
  grounded in the full MOV (not just the elected Objective) so a factual
  question about any Object in focus gets a real answer instead of a
  guess. The same handoff also carries `preferred_output_modality`
  (text/voice) when the user explicitly asked for a specific reply channel
  — see "Voice notes" below.
- **Core identity is exempt from MS §7's archive cycle.** Who Liriel is
  (VOV_0000) and who made her (her creator, her developer) can never be
  archived out of focus by a `mov_op`/MainMemory command — every other
  Object, including the rest of the reference dataset, still moves
  normally between the MOV and MainMemory. `PROTECTED_VOV_IDS` (.env)
  controls the list.

## The cycle — four LLM calls per message

1. **`GRAPH_REQUEST`** (MS §12.1) — the model decides which Objects' bonds
   are worth surveying this cycle (or none). `graph_service.py` then
   builds the real `GraphOfTraces` from that — a service call, not an LLM
   call — before Query 2 runs.
2. **`MOV_MAINMEMORY_UPDATE`** (MS §12.3) — the model reviews any Objective
   left open from the previous cycle (retrospective: `SET_DELTA_REPORT` /
   `KEEP_PENDING` / `ARCHIVE` / ...) and emits `mov_ops`
   (`UPSERT_VOV`/`PATCH_VOV`/`ARCHIVE_VOV`/...) for what the new message
   changes, with the Graph of Traces from step 2 in hand. Applied to the
   database immediately.
3. **`BEST_PREY_GUESS`** (MS §12.5) — from the updated MOV, the model elects
   the objective for this turn as a judgment (MS §1.2), with its
   `objective` block (genus/species/gain_form/channel Ordinances) and a
   handoff summary for whoever executes it.
4. **Reply composition** (Phase 1 only, no MS §12 contract) — a final,
   plain-text call turns that handoff into Liriel's actual chat reply.

### A real cost of fidelity: this is prompt-heavy

The MetaScheme alone is **~18,700 tokens**, resident on every one of the
four calls (MS §0.6 by design — it belongs in the provider's prompt-cache
prefix; on Anthropic models `llm_client.py` marks it as cacheable
automatically, and repeated calls within a session mostly read it from
cache rather than reprocessing it). Add the MOV and the JSON schema for
each query's contract (another ~4,000–5,000 tokens) and a single call
routinely sits at 20,000–25,000 prompt tokens. `LLM_EFFORT_UPDATE`/
`LLM_EFFORT_DECISION`/`LLM_EFFORT_GRAPH` (.env, Anthropic-only, default
`low`) trade some reasoning depth on the cycle's three structured-JSON
calls for meaningfully lower latency — measured 5-11x faster than the
provider default in this project's own testing, with no observed loss of
schema validity. If you need a different model entirely, point `LLM_MODEL`
at it instead (see "Switching models" below) — the code doesn't change,
only `.env`.

## Structure

```
config.py       # loads .env and centralizes configuration
models.py       # Pydantic models matching MS §6 (VOV/MOV) and §12 (query contracts)
prompts.py      # loads the MetaScheme from docs/ + the four query templates
llm_client.py   # LLM abstraction via litellm (Ollama/Gemma or external API)
database.py     # persistence (Postgres/Supabase, with a local JSON fallback)
graph_service.py # TrackGraphProcess (MS §8.2) — builds the Graph of Traces
motivation.py   # orchestrates the 4-call ProcessMotivation cycle + applies mov_ops
main.py         # interactive terminal chat
telegram_bot.py # Telegram front end — same cycle, driven by Telegram messages instead
docs/
  MetaScheme_Liriel_Rev0000.md  # the official MetaScheme (system prompt source)
migrations/
  001_init.sql                     # base schema (superseded by 002 below)
  002_metascheme_alignment.sql     # VOV shape per MS §6.4/§12.2
  003_graph_of_traces.sql          # mov_relations — edges for the Graph of Traces (MS §8)
scripts/
  run_migration.py         # applies a .sql file without needing psql installed
  import_reference_mov.py  # one-time import of the reference MatrixObjectsValence_Rev000.xlsx
```

## 1. Prerequisites

- Python 3.10+
- [Ollama](https://ollama.com) installed and running locally
- A [Supabase](https://supabase.com) account/project (or any Postgres)

## 2. Install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Set up Ollama + Gemma

Check what's already installed:

```bash
ollama list
```

If you don't have a Gemma model, pull one (e.g. `ollama pull gemma2`). The
default in `.env.example` is whatever model was detected in the original
dev environment — adjust `LLM_MODEL` to match what you have. Make sure
`ollama serve` is running before starting the chat.

## 4. Configure `.env`

```bash
copy .env.example .env
```

**If `.env` already exists, do not overwrite it blindly — merge instead.**
Copying `.env.example` over an existing `.env` destroys any credentials
already saved there.

Edit `.env`:
- `LLM_MODEL` / `LLM_API_BASE` — already configured for local Ollama.
- `LLM_NUM_CTX` — needs to comfortably exceed the MetaScheme's ~18.7k
  tokens plus the MOV and schema for whichever query is running; the
  default (40960) has margin to spare for a small MOV.
- `PGHOST`, `PGUSER`, `PGPASSWORD` (and optionally `PGPORT`/`PGDATABASE`) —
  fill in with your Supabase details (Project Settings → Database → Connect
  → Direct connection or Session pooler). **Prefer these discrete fields
  over `DATABASE_URL`**: if the database password contains a character like
  `@`, `:`, `/` or `#`, pasting it directly into a single connection string
  breaks the parsing (the host ends up corrupted, and the error is
  confusing). With discrete fields this doesn't happen — paste the password
  exactly as it is. Leave everything blank to use a local
  `local_mov_store.json` file as a fallback for quick testing.

## 5. Run the SQL migrations on Supabase

Run all three, in order (`002` drops and recreates `mov_objects`/
`mov_object_valences`/`motivation_cycles` to match the MetaScheme's VOV
shape; harmless on a fresh database, destructive of any real data in those
tables on an existing one — see the warning at the top of that file).
Paste each into your Supabase project's SQL editor, or run them from the
command line:

```bash
python scripts/run_migration.py migrations/001_init.sql
python scripts/run_migration.py migrations/002_metascheme_alignment.sql
python scripts/run_migration.py migrations/003_graph_of_traces.sql
```

## 6. Test the connections before chatting

```bash
python main.py --check
```

This makes a minimal call to the configured LLM and a single database read,
without entering the chat loop. Errors show the exact cause (wrong
model/URL, Ollama not running, invalid database credentials, etc.). Note
that this check uses a tiny prompt, not the full MetaScheme — it does not
by itself confirm the full-size cycle fits your `LLM_NUM_CTX`.

## 7. Run the chat

```bash
python main.py
```

Type messages normally; `exit` (or `quit`/`bye`) ends the session. Each
message triggers the four-call cycle described above (expect it to take
minutes with a local model — see "A real cost of fidelity").

To see the raw JSON from each step (useful for debugging prompts with a
small local model like Gemma), run with `LIRIEL_VERBOSE=true` in `.env`, or:

```bash
set LIRIEL_VERBOSE=true
python main.py
```

## Switching models later

Just edit `.env` — no code changes needed:

```env
LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-...
```

or

```env
LLM_MODEL=claude-sonnet-5
ANTHROPIC_API_KEY=sk-ant-...
```

## Talking to Liriel from Telegram instead of the terminal

`telegram_bot.py` is a second front end — same `run_motivation_cycle`,
same MOV in the same database, just driven by Telegram messages instead of
terminal input (MS §9.3 leaves the embodiment's form open; this is simply
a second one). It long-polls the Telegram Bot HTTP API directly, so no
extra framework is needed beyond `requests` (already in
`requirements.txt`).

1. **Create a bot.** In Telegram, message **@BotFather** → `/newbot` →
   give it a name (e.g. "Liriel") and a username ending in `bot` (e.g.
   `liriel_yourname_bot` — usernames are global, so you may need to try a
   few). BotFather replies with a token that looks like
   `123456789:AAExampleTokenTextGoesHere`.
2. **Put the token in `.env`** yourself — `TELEGRAM_BOT_TOKEN=<the token>`.
   Treat it like a password: whoever has it can send messages as your bot.
3. **Run the bot:**
   ```bash
   python telegram_bot.py
   ```
   Leave `TELEGRAM_ALLOWED_CHAT_ID` blank the first time — with it unset,
   the bot answers anyone who messages it, which is fine for finding your
   own chat_id but not for leaving running unattended. Open a chat with
   your new bot in Telegram (search its username) and send it anything;
   the console prints `chat_id=<your id>`. Copy that into `.env` as
   `TELEGRAM_ALLOWED_CHAT_ID`, restart `telegram_bot.py`, and from then on
   it only answers that chat. To let more than one person talk to Liriel,
   comma-separate their ids (`TELEGRAM_ALLOWED_CHAT_ID=111111111,222222222`)
   — everyone shares the same MOV/conversation, there's no per-user split.
4. Chat normally — each message runs the same four-call cycle described
   above (see "A real cost of fidelity" for how long that takes), against
   the same MOV `main.py` uses, so the two front ends share one continuous
   conversation with Liriel.

### Voice notes (send audio, get audio back)

Send Liriel a Telegram voice note instead of typing, and she replies with
one too — a typed message still gets a typed reply either way. This uses
OpenAI's Whisper (speech-to-text) and TTS (text-to-speech) via `litellm`,
the same call path as everything else in `llm_client.py`, regardless of
what `LLM_MODEL` is set to for the actual conversation.

1. **Get an OpenAI API key** at <https://platform.openai.com/api-keys> and
   paste it into `.env` yourself as `OPENAI_API_KEY=sk-...` — needed even
   when `LLM_MODEL` is a Claude model, since Anthropic has no STT/TTS
   endpoint. This is a separate, small per-message cost from your main LLM
   provider.
2. `STT_MODEL` (default `whisper-1`), `TTS_MODEL` (default `tts-1`) and
   `TTS_VOICE` (default `nova`, one of
   `alloy`/`echo`/`fable`/`onyx`/`nova`/`shimmer`) are all overridable in
   `.env` if you want a different model or voice. The 6 classic presets
   measured barely distinguishable from each other in Portuguese on
   `tts-1` in this project's own testing — for real control over how she
   sounds, set `TTS_MODEL=gpt-4o-mini-tts` and describe it in
   `TTS_INSTRUCTIONS` (plain language, e.g. "a young woman in her
   twenties, warm and cheerful") instead of just picking a different
   preset; `tts-1`/`tts-1-hd` ignore `TTS_INSTRUCTIONS` entirely.
3. Restart `telegram_bot.py` and send it a voice note. If transcription or
   synthesis fails (e.g. no `OPENAI_API_KEY` set), the bot falls back to a
   typed apology (transcription failure) or a typed version of the reply
   (synthesis failure) rather than losing the message.

By default the reply channel mirrors whatever channel your message came in
on. You can override that explicitly in either direction — type "manda
isso em áudio" and get a voice reply back, or send a voice note asking her
to answer in writing — since ProcessCommandControl's handoff (MS §12.5)
carries that choice when you ask for it (see "Scope of this phase" above).

## Next steps (Phase 2, out of scope here)

- A real semantic/full-text index so `SEARCH` (MS §12.4) can run — today
  it's the one MainMemory command still only logged, not executed.
- A real `ProcessCommandControl` producing `SCENARIO_DATA` (MS §12.6) from
  the world, instead of the user's raw chat text standing in for it.
- `ProcessIntrospection` (MS §12.8) and `ProcessDormancy` (MS §11, weight
  updates between cycles) — currently out of scope entirely.
