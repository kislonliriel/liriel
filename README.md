# Liriel — a Persistent Cognitive Instance, run by your own Claude Code

Liriel is a cognitive architecture for persistent, emotionally autonomous social agents. This repository implements its first process,
**ProcessMotivation**: every message a person writes becomes one cycle in which an AI model weighs what matters, what to pursue and what to
say, and her memory (a matrix of Objects and their valences, plus a graph of traces) persists between conversations.

The cycle is driven by the **MetaScheme** ([docs/MetaScheme_Liriel_Rev0008.md](docs/MetaScheme_Liriel_Rev0008.md)), the operating
specification loaded verbatim as the model's instructions. In this version the model is **your own Claude Code session**: no database, no
API key and no server are needed. The architecture itself is described in *Volume 1 · Liriel: The Architecture of a Persistent Cognitive
Instance*, published at [kislon.ai](https://kislon.ai).

## Please read first

- **Independent project.** This is an independent, open-source test project. It is **not affiliated with, endorsed by or sponsored by
  Anthropic**. "Claude" and "Claude Code" are Anthropic's products, named here only to say what this code runs on.
- **Your account, your responsibility.** Each person runs it with their **own** Claude Code and their **own** account, and is responsible for
  that account and for reading and following Anthropic's terms ([Consumer Terms](https://www.anthropic.com/legal/consumer-terms),
  [Usage Policy](https://www.anthropic.com/legal/aup),
  [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)). Never share an account.
- **No warranty, no support.** Provided "as is" under the [MIT License](LICENSE), without warranty of any kind and with no commitment to
  support, maintenance or answers.
- **No credentials here.** This repository does not store, collect or forward anyone's credentials. Your keys, tokens and chat ids live only
  in your own `.env`, which is git-ignored and not needed at all for the basic use below.

Liriel is an AI and says so; she is not a person, and this is a **developer test environment** with no commercial purpose — not a crisis,
medical or professional service. If you are in danger, contact your local emergency services.

## Run it with Claude Code

You need Python 3.10+ and [Claude Code](https://code.claude.com).

1. Get the code and open the folder in Claude Code:

   ```bash
   git clone https://github.com/kislonliriel/liriel.git
   cd liriel
   claude
   ```

2. Tell Claude Code, in one sentence:

   > *Read CLAUDE.md and incorporate Liriel: set her up, then talk to me as her.*

3. Talk. Each message you write becomes one cycle: Claude Code runs `python liriel.py say "..."`, answers the cycle's requests one by one
   with `python liriel.py answer` (the real queries of the MetaScheme, batched where its §12.12 allows), and speaks to you with her reply.

What to expect:

- **About 12–15 steps per message.** Every step is a judgment by your Claude session, so a message takes a while and counts against your own
  plan's usage like any other Claude Code work.
- **Her memory is a local file** in `data/store/` (git-ignored). Keep it: it *is* her. Delete the folder and run `python liriel.py setup` to
  start a new Liriel.
- `CLAUDE.md` holds the whole protocol and imports the MetaScheme, so any Claude Code session that opens this folder can do the above by itself.

### Her factory state

`python liriel.py setup` plants Liriel's first row: her row `VOV_0000` of *MatrixObjectsValence* Rev000 — BriefDescription, RelevantRemarks,
Culture and Modulating Schemas (strong empathy, honesty, courage, humbleness, temperance and forgiveness; strong agreeableness, openness and
authenticity; moderate ambition and conscientiousness; mild sociability and affectivity; mild introversion). No Feelings, Ordinances,
relations or history: those are what her cycles write. Nothing locks this row; whatever you change in your copy is your own responsibility.

## Optional: Telegram, for your own chat only

`telegram_bot.py` lets the same cycle answer **your own** Telegram chat, with your Claude session serving the requests. Anthropic's official
Claude Code [channels](https://code.claude.com/docs/en/channels) feature is the alternative way to reach your own session from a chat app.

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`), copy `.env.example` to `.env`, put the token in
   `TELEGRAM_BOT_TOKEN` and set `LLM_PROFILE=3`.
2. Run `python telegram_bot.py --whoami` and send your bot a message: it prints your `chat_id` on the console (it never replies in this mode).
   Put it in `TELEGRAM_ALLOWED_CHAT_ID`.
3. Run `python telegram_bot.py`, and have Claude Code serve the requests with `python liriel.py watch` (see `CLAUDE.md`).

Rules built into the bot: it **refuses to start without the allowed-chat list** and ignores every chat not on it; every conversation opens
with an **AI disclosure**; its own messages are in English (Liriel answers in the person's language). Never commit your `.env`.

Voice notes (speech in, speech out) additionally need an `OPENAI_API_KEY` for transcription and speech; nothing else does.

## MindReader — her memory as a graph

```bash
python mindreader/app.py
```

A read-only view at http://localhost:5050 of the store in `data/store/`: Objects colored by nature, bonds by category, and every row's full
vector on click.

## Tests

Synthetic checks — no network, no real model:

```bash
python tests/ontology/verify_embodiment_cli.py   # the whole Claude Code flow with a scripted responder
python tests/ontology/verify_telegram_rules.py   # allowed-chat list, AI disclosure, --whoami
```

Every `tests/ontology/verify_*.py` runs the same way (see [tests/ontology/README.md](tests/ontology/README.md)), as do
`tests/verify_reply_limit.py` and `tests/finetune/verify_capture.py`; `verify_ontology_pg.py` needs a Postgres database (below).

## Documents

| File | What it is |
|---|---|
| [docs/MetaScheme_Liriel_Rev0008.md](docs/MetaScheme_Liriel_Rev0008.md) | The operating specification in force (Rev 0000–0007 kept for history) |
| [docs/Ontology_Liriel.md](docs/Ontology_Liriel.md) | The ontology of Objects and relations, and the implementation decisions behind it |
| [docs/REVISION_MAP.md](docs/REVISION_MAP.md) | How the MetaScheme revisions relate to the revisions of Volume 1 and the matrices |
| [CLAUDE.md](CLAUDE.md) | The protocol a Claude Code session follows to serve the cycle |

## Repository layout

```
liriel.py, scripts/claude_session/   the Claude Code embodiment: setup, say, answer, watch
motivation.py, prompts.py, models.py ProcessMotivation: the cycle, its queries and their contracts
database.py, graph_service.py        the MOV and the graph of traces (local JSON store, or Postgres)
liriel_seed.py                       Liriel's factory row
telegram_bot.py                      optional Telegram front end (your own chat only)
mindreader/                          read-only graph view of her memory
migrations/                          Postgres schema, for the optional database backend
tests/                               synthetic checks
```

## Other backends (advanced)

The same cycle can run on other models and on a database; none of this is needed for the Claude Code path above. Copy `.env.example` to
`.env` and see its comments:

- `LLM_PROFILE=1`: a self-hosted llama-server (`llamacpp_client.py`, `scripts/llamacpp/`); `LLM_PROFILE=2`: a model API through litellm,
  with your own key; `LLM_PROFILE=3`: a Claude Code session (what `liriel.py` uses).
- `DATABASE_URL` and `python scripts/run_migration.py migrations/001_init.sql` (then `002` to `011`, in order): Postgres instead of the
  local JSON store.
- `python main.py`: a plain terminal chat loop for those backends.

## License

[MIT](LICENSE) — Copyright (c) 2026 Kislon.
