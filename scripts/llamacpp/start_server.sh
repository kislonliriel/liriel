#!/usr/bin/env bash
# Starts llama-server (llama.cpp) for fully local Liriel inference --
# every call (GRAPH_REQUEST, MOV_MAINMEMORY_UPDATE, BEST_PREY_GUESS, reply
# composition) can run here once client.py is wired in, keeping every
# inference under your own control (no closed external API), per the
# project's own decision.
#
# IMPORTANT -- corrected from the original spec: llama-server has NO
# --prompt-cache / --prompt-cache-ro / --prompt-cache-all flags. Those
# exist only on llama-cli (confirmed against this exact binary's --help,
# and against llama.cpp's own docs/GitHub issue #9135). Passing them here
# would fail with "unknown argument".
#
# What llama-server does instead, and what this script relies on:
# --cache-prompt (on by default) keeps the KV cache for the LAST request
# in RAM and reuses the longest matching prefix on the NEXT one -- so once
# warmed, the ~18k-token MetaScheme block is only paid for once per
# server run, automatically, no file needed. It does NOT survive a
# restart, which is exactly what warm_cache.py is for: run it once, right
# after this script reports the server is healthy.
#
# -np 1 IS LOAD-BEARING, NOT A DEFAULT LEFT ALONE: this server defaults to
# 4 parallel "slots" (-np auto), each tracking its own separate cached
# prefix. A cycle's 4 calls (and the next cycle's 4) can land on 4
# different slots round-robin, and a cold slot has no MetaScheme cached at
# all -- confirmed for real: two questions in a row measured ~13 minutes
# total, consistent with most of the cycle's 8 calls (2 questions x 4
# calls) each reprocessing the ~20-30k-token prefix from scratch instead
# of hitting the cache warm_cache.py just built. This app only ever runs
# one call at a time anyway (no concurrent users in Phase 1), so forcing a
# single slot costs nothing and removes the ambiguity entirely: every
# request is compared against the one previous request, guaranteed.
set -euo pipefail

# NOTE ON THE PATHS BELOW: they use C:\Users\USURIO~2, the Windows 8.3
# short name for C:\Users\Usuário, not a typo. llama-server.exe only
# parses plain ANSI argv on Windows -- the accented "á" in the real path
# arrives mangled ("Usu?rio") by the time the program reads its own -m
# argument, even though bash and the filesystem handle it fine
# everywhere else. The short name is pure ASCII and sidesteps this
# entirely. Find yours (if different) with, in PowerShell:
#   (New-Object -ComObject Scripting.FileSystemObject).GetFolder("$HOME").ShortPath

# Model choice: gemma4:12b-it-qat (new default, being evaluated -- fits
# entirely in 12GB VRAM, no CPU offload) or gemma4:26b-a4b-it-qat (the
# original MoE model, kept one env var away as an instant rollback: run
# `LLAMA_MODEL_SIZE=26b ./start_server.sh` if 12B's judgment quality
# doesn't hold up on real BEST_PREY_GUESS cases -- no need to remember or
# retype either blob's sha256 digest). Both already pulled by Ollama on
# this machine -- llama.cpp identifies GGUF files by their internal magic
# bytes, not by extension, so pointing straight at Ollama's blob store
# works with no copy/rename needed. To add another size/model, find its
# weight blob with:
#   cat ~/.ollama/models/manifests/registry.ollama.ai/library/<name>/<tag>
# and take the digest whose mediaType is "application/vnd.ollama.image.model".
case "${LLAMA_MODEL_SIZE:-12b}" in
  12b) MODEL_DEFAULT=/c/Users/USURIO~2/.ollama/models/blobs/sha256-faff1a63667fac17ac5e777f47114688fcefea96e220e211aaa8d62c2c4561f1 ;;
  26b) MODEL_DEFAULT=/c/Users/USURIO~2/.ollama/models/blobs/sha256-4c856523d61d77922dbc0b26753a6bf6208e5d69d80db0c04dcd776832d054c5 ;;
  *) echo "Unknown LLAMA_MODEL_SIZE='${LLAMA_MODEL_SIZE}' (expected 12b or 26b)" >&2; exit 1 ;;
esac
MODEL="${1:-${LLAMA_MODEL:-$MODEL_DEFAULT}}"

# Docker Desktop's bundled llama-server (build ~3191462) is too old to
# know newer model architectures like "gemma4" -- it fails with
# "unknown model architecture" on this exact model. scripts/llamacpp/bin/
# holds a current official build instead (Vulkan, same backend, just
# new enough): fetched from
#   https://github.com/ggml-org/llama.cpp/releases/download/b11009/llama-b11009-bin-win-vulkan-x64.zip
# (not committed to git -- see .gitignore -- re-download if that folder
# is missing). Override LLAMA_SERVER_BIN if you later install a
# dedicated CUDA build (typically faster on Nvidia cards than Vulkan).
LLAMA_SERVER_BIN="${LLAMA_SERVER_BIN:-$(dirname "$0")/bin/llama-server.exe}"

PORT="${LLAMA_PORT:-8080}"
# 32768 measured too small in practice: a real MOV (14 active objects) +
# the MetaScheme (~19.5k, mostly cached) already hit 29,637 prompt tokens
# by themselves, leaving only ~3k for a JSON completion that needed more —
# confirmed via finish_reason="length" at exactly ctx_size total_tokens.
# 98304 gives real headroom for the MOV to grow, nested MOVs, and a full
# GraphOfTraces, while staying well under the model's own 262144 native
# limit. The KV cache grows with this too (-ctk/-ctv q8_0 keeps it from
# growing 2x worse) -- --fit will push more layers to CPU to compensate,
# which is fine given the accepted "up to ~5 minutes per reply" tradeoff.
CTX_SIZE="${LLAMA_CTX_SIZE:-98304}"
NGL="${LLAMA_NGL:-auto}"              # 'auto' = let --fit size this to your VRAM; see note below

echo "Model:  $MODEL"
echo "Binary: $LLAMA_SERVER_BIN"
echo "Port:   $PORT"
echo

# --- VRAM note -------------------------------------------------------------
# Your RTX 4070 has 12GB VRAM (confirmed via nvidia-smi).
#   - gemma4:12b-it-qat (default): ~7GB at this quantization -- fits
#     entirely in VRAM with room for the KV cache too, no CPU offload at
#     all. This is the whole reason to try it: full-GPU residency is
#     typically much faster than a hybrid split, for both prompt
#     processing and (especially) generation, independent of the MoE-vs-
#     dense reasoning below.
#   - gemma4:26b-a4b-it-qat (LLAMA_MODEL_SIZE=26b): 14.4GB, bigger than
#     the card. Ollama already runs it today via a CPU+GPU split; a plain
#     llama-server run needs the same accommodation.
#
# -ngl auto (this build's own default, paired with --fit on by default)
# is what makes that split happen automatically when needed -- it
# measures free VRAM and picks a safe layer count itself, leaving a
# margin (--fit-target, default 1024 MiB). Passing a specific NUMBER here
# (LLAMA_NGL=1024, as an earlier version of this script did) disables
# that and forces every layer onto the GPU regardless of fit -- confirmed
# to OOM outright ("ggml_vulkan: ErrorOutOfDeviceMemory") on the 26B
# model on this exact card. Only set LLAMA_NGL to a number if you want to
# override --fit's own choice.
#
# The 26B model is Mixture-of-Experts ("a4b" = ~4B active params per
# token) -- its CPU-resident layers cost less per generated token than a
# dense model of the same size would, which is exactly why "up to ~5
# minutes per reply" was a plausible number to test against on it, not a
# sign of something broken. The 12B model is dense and (once it fits
# fully in VRAM, as it should) shouldn't need that same allowance --
# that's the tradeoff being tested: likely faster, at a real risk to
# judgment quality on BEST_PREY_GUESS specifically (Google positions the
# 26B-A4B as the family's "advanced reasoning" tier, not just its
# largest) -- watch real replies for that, not just the clock.
# ----------------------------------------------------------------------------

"$LLAMA_SERVER_BIN" \
  -m "$MODEL" \
  -ngl "$NGL" \
  -ctk q8_0 -ctv q8_0 \
  -c "$CTX_SIZE" \
  -np 1 \
  --cache-reuse 256 \
  --port "$PORT" &

SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT INT TERM

echo "Waiting for llama-server (pid $SERVER_PID) to become healthy..."
until curl -sf "http://localhost:${PORT}/health" >/dev/null 2>&1; do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "llama-server exited before becoming healthy -- check the output above" >&2
        exit 1
    fi
    sleep 2
done
echo "Server is healthy."

METASCHEME="${METASCHEME_PATH:-$(dirname "$0")/../../docs/MetaScheme_Liriel_Rev0000.md}"
echo "Warming the prefix cache with $METASCHEME ..."
python "$(dirname "$0")/warm_cache.py" "$METASCHEME" --port "$PORT" || \
    echo "Warm-up failed -- server is still usable, just slower on the first real call." >&2

echo
echo "llama-server is up on http://localhost:${PORT} (pid $SERVER_PID). Ctrl+C to stop."
wait "$SERVER_PID"
