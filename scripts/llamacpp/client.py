"""
HTTP client for a locally-running llama-server (llama.cpp) -- lets every
Liriel inference (GRAPH_REQUEST, MOV_MAINMEMORY_UPDATE, BEST_PREY_GUESS,
reply composition) run against a self-hosted open model, per the
project's decision to keep every inference under the implementer's own
control rather than a closed external API (MS §17.6 "weight ownership"
already argues for exactly this).

Prerequisite: scripts/llamacpp/start_server.sh running (it also warms the
prefix cache for you). See that script's header comment for why there is
no on-disk "prompt cache file" step here -- llama-server does prefix
reuse automatically, in RAM, via --cache-prompt.

IMPORTANT: this talks to /v1/chat/completions, not the raw /completion
endpoint. A first working version used /completion with a hand-built
prompt string and it technically worked, but with no chat template
applied the model has no reliable signal for where its turn ends --
confirmed in testing: it produced a correct JSON object, then kept
generating past it into runaway repetition ("board": "board": "board":
..., then endless "0,0,0,0...") until n_predict cut it off. The chat
endpoint applies Gemma's own template (from the GGUF's
tokenizer.chat_template) and its matching stop tokens automatically, so
the model reliably stops after its actual answer -- this is what fixed
it in practice, not a theoretical concern.

Usage:
    from client import LlamaCppClient

    client = LlamaCppClient()
    result = client.complete(
        metascheme_text=metascheme,          # MS §0.3 block [1] -> system message
        identity_and_state=vov_0000_text,    # block [2] \
        artifacts=mov_and_graph_text,        # block [3]  > user message
        query=query_json_text,               # block [4] /
    )
    # result is already the parsed dict the model emitted (MS §0.4).
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

# Reuses the same JSON-extraction logic (code-fence stripping, brace
# scanning, the couple of syntax-glitch repairs) that llm_client.py
# already relies on for the Anthropic/DeepSeek/Groq path -- no reason for
# the local model to be held to a looser standard, and no reason to fork
# the parsing logic in two places.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from llm_client import LLMError, _extract_json  # noqa: E402

try:
    from config import settings

    DEFAULT_TIMEOUT = float(settings.llm_request_timeout)
except Exception:  # noqa: BLE001 - keep this module usable standalone too
    DEFAULT_TIMEOUT = 600.0

DEFAULT_BASE_URL = "http://localhost:8080"


def build_user_content(identity_and_state: str, artifacts: str, query: str) -> str:
    """Assembles blocks [2]-[4] of MS §0.3 into the user turn. Block [1]
    (the MetaScheme) goes in the system message instead -- see
    LlamaCppClient.complete -- mirroring how llm_client.py already
    splits system vs. user for the Anthropic path (and letting the
    MetaScheme sit at the front of the cached prefix, same idea as
    MS §0.6, translated to a chat-templated local model)."""
    return (
        f"[2] IDENTITY_AND_STATE\n{identity_and_state}\n\n"
        f"[3] ARTIFACTS\n{artifacts}\n\n"
        f"[4] QUERY\n{query}\n"
    )


class LlamaCppClient:
    """Thin wrapper over llama-server's /v1/chat/completions endpoint."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        # Generous on purpose: a ~26B MoE model split across a 12GB GPU
        # and CPU RAM can plausibly take minutes per reply -- explicitly
        # accepted as fine while testing the local path.
        self.timeout = timeout
        self._session = requests.Session()

    def _post_chat(self, messages: list[dict], n_predict: int, **sampling) -> dict:
        payload = {
            "messages": messages,
            "n_predict": n_predict,
            "cache_prompt": True,  # default true anyway; explicit for clarity
            "repeat_penalty": 1.1,  # safety net against the runaway-repetition
            # failure mode seen in testing, independent of the chat-template fix
            # llama.cpp's CLI treats repeat_last_n=-1 as "whole context", but
            # this server's HTTP API validates it strictly as an unsigned int
            # (0 <= value <= 2147483647) and rejects -1 outright with a 400 --
            # confirmed for real, and a worse regression than the 64-token
            # default this was meant to fix, since it broke every call rather
            # than just the rare reasoning-loop one. 100000 is a plain large
            # positive number instead: comfortably above start_server.sh's own
            # default context size (98304, LLAMA_CTX_SIZE) and any realistic
            # n_predict this project sets, so llama.cpp's own internal min()
            # against the actual context/generation length makes it behave
            # the same as "whole context" without tripping the validator.
            # Needed at all because the 64-token default window is far
            # shorter than the ~200-word reasoning_content paragraph a local
            # model was seen looping on, verbatim, dozens of times, until
            # n_predict cut it off with the actual JSON `content` never
            # reached -- too long a repeat unit for the default window to
            # ever even see, let alone penalize.
            "repeat_last_n": 100000,
            **sampling,
        }
        try:
            resp = self._session.post(
                f"{self.base_url}/v1/chat/completions", json=payload, timeout=self.timeout
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise LLMError(
                f"llama-server request failed ({self.base_url}): {exc}"
            ) from exc
        return resp.json()

    def warm_up(self, metascheme_text: str) -> dict:
        """Primes the server's in-memory prefix cache with the MetaScheme
        as a system message -- the fixed, byte-identical prefix MS §0.6
        describes -- so the first real call after a server (re)start
        doesn't pay to reprocess all ~18k tokens of it. Sent as a real
        (trivial) chat request, not the bare text: the cached prefix has
        to match byte-for-byte what a real call's chat-template
        rendering produces, or the cache reuse this is meant to buy
        never actually triggers. n_predict kept small since we only care
        about the prompt being processed and cached, not the answer.

        Returns the raw response so callers can inspect
        usage.prompt_tokens if they want to confirm the size processed.
        """
        return self._post_chat(
            messages=[
                {"role": "system", "content": metascheme_text},
                {"role": "user", "content": "[4] QUERY\n(cache warm-up -- ignore)"},
            ],
            n_predict=1,
        )

    def complete(
        self,
        metascheme_text: str,
        identity_and_state: str,
        artifacts: str,
        query: str,
        n_predict: int = 4096,
        temperature: float = 0.3,
    ) -> dict:
        """Sends the full four-block call and returns the parsed JSON
        object the model is required to emit (MS §0.4: one JSON object,
        nothing else -- same contract every existing call already
        follows against Claude).
        """
        messages = [
            {"role": "system", "content": metascheme_text},
            {"role": "user", "content": build_user_content(identity_and_state, artifacts, query)},
        ]
        result = self._post_chat(messages, n_predict=n_predict, temperature=temperature)
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected llama-server response shape: {result}") from exc
        if not content or not content.strip():
            raise LLMError(f"llama-server returned no content: {result}")
        return _extract_json(content)


if __name__ == "__main__":
    import argparse
    import json
    import sys as _sys

    # Same fix as main.py/telegram_bot.py: the Windows console's default
    # codepage can't render every character a model may emit (e.g. "§"),
    # which otherwise prints as "?" or crashes a naive print(). Cosmetic
    # only -- the JSON value itself is unaffected either way.
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Smoke-test the local llama-server against a MetaScheme file.")
    parser.add_argument("metascheme_path", nargs="?", default="docs/MetaScheme_Liriel_Rev0000.md")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    text = Path(args.metascheme_path).read_text(encoding="utf-8")
    client = LlamaCppClient(base_url=f"http://localhost:{args.port}")

    print("Warming up (this can take a while on first load)...")
    warm = client.warm_up(text)
    usage = warm.get("usage", {})
    print(f"  prompt_tokens={usage.get('prompt_tokens')}")

    print("Sending a minimal GRAPH_REQUEST-shaped smoke test...")
    result = client.complete(
        metascheme_text=text,
        identity_and_state="(placeholder VOV_0000 -- wire in real data before trusting output)",
        artifacts="(placeholder MOV)",
        query='{"query": "GRAPH_REQUEST", "cycle_id": "smoke-test-1"}',
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
