"""The emotional direction of a spoken reply (OpenAI gpt-4o-mini-tts `instructions`): Liriel writes it from her own Feelings and the
words of the reply; the code only tidies it and passes it on. No network, every model/speech call faked."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import llm_client  # noqa: E402
import motivation  # noqa: E402
import prompts  # noqa: E402
import telegram_bot  # noqa: E402
from config import settings  # noqa: E402
from models import AxisValence, VectorObjectValence  # noqa: E402

SELF = settings.liriel_self_vov_id
liriel = VectorObjectValence(vov_id=SELF, object_nature="PCI", brief_description="Liriel",
                             feelings={"LoveAngerEros": AxisValence(v=3.0, c=4)})

print("[check] the prompt: MetaScheme as system prompt, her own row, the message, the reply word for word, and the rules of the direction")
msgs = prompts.build_voice_delivery_prompt("Sinto muito, Fabio. Estou aqui.", "Meu pai morreu hoje.", liriel)
assert msgs[0] == {"role": "system", "content": prompts.META_SCHEME} and msgs[1]["role"] == "user"
u = msgs[1]["content"]
assert SELF in u and "Sinto muito, Fabio. Estou aqui." in u and "Meu pai morreu hoje." in u
assert "no cheer on a reply that is heavy or worrying" in u and "Output only the direction" in u
assert "do not invent a mood the reply does not carry" in u and "Nothing you write is spoken" in u
assert "None" not in prompts.build_voice_delivery_prompt("oi", "oi", None)[1]["content"].split("ARTIFACT: Liriel's own VOV")[0]

print("[check] compose_voice_delivery: the model's direction is tidied (quotes, whitespace, cap), never judged; a failure or an empty answer is None")
with patch.object(motivation, "chat", return_value='  "Speak softly,\n   slowly; a small pause after the first word."  '):
    assert motivation.compose_voice_delivery("m", "r", liriel) == "Speak softly, slowly; a small pause after the first word."
long = ("Warm and slow. " * 80).strip()
with patch.object(motivation, "chat", return_value=long):
    out = motivation.compose_voice_delivery("m", "r", liriel)
assert len(out) <= motivation._VOICE_DELIVERY_MAX_CHARS and out.endswith("."), out[-30:]
with patch.object(motivation, "chat", return_value="   "):
    assert motivation.compose_voice_delivery("m", "r", liriel) is None
with patch.object(motivation, "chat", side_effect=RuntimeError("model down")):
    assert motivation.compose_voice_delivery("m", "r", liriel) is None

print("[check] synthesize_speech: the fixed persona (TTS_INSTRUCTIONS) comes first, the per-reply direction after it; neither -> no `instructions` at all")
seen = {}


class Resp:
    def write_to_file(self, path):
        Path(path).write_bytes(b"x" * 5000)


def fake_speech(**kw):
    seen.update(kw)
    return Resp()


def speech_with(persona, delivery, model="gpt-4o-mini-tts"):
    seen.clear()
    fake = SimpleNamespace(tts_model=model, tts_voice="marin", tts_instructions=persona, llm_request_timeout=30)
    with patch.object(llm_client, "settings", fake), patch.object(llm_client.litellm, "speech", side_effect=fake_speech):
        llm_client.synthesize_speech("oi", str(Path(__file__).with_suffix(".tmp.ogg")), delivery=delivery)
    Path(__file__).with_suffix(".tmp.ogg").unlink(missing_ok=True)
    return seen.get("instructions"), seen["model"], seen["voice"]


assert speech_with("A young woman.", "Soft and slow.") == ("A young woman. Soft and slow.", "gpt-4o-mini-tts", "marin")
assert speech_with(None, "Soft and slow.")[0] == "Soft and slow."
assert speech_with("A young woman.", None)[0] == "A young woman."
assert speech_with(None, None)[0] is None and "instructions" not in seen

print("[check] only a steerable model is asked for a direction (tts-1 / tts-1-hd are not)")
for model, expect in (("tts-1", False), ("tts-1-hd", False), ("gpt-4o-mini-tts", True), ("GPT-4o-mini-tts", True)):
    with patch.object(llm_client, "settings", SimpleNamespace(tts_model=model)):
        assert llm_client.tts_is_steerable() is expect, model

print("[check] the front end: no model call for a non-steerable TTS, none with TTS_DELIVERY=0 or without a MOV; the direction otherwise")
mov = SimpleNamespace(get=lambda vid: liriel if vid == SELF else None)
calls = []


def fake_compose(user_text, reply_text, self_row):
    calls.append((user_text, reply_text, self_row))
    return "Warm, a little shaky."


on = SimpleNamespace(tts_delivery=True, liriel_self_vov_id=SELF)
with patch.object(telegram_bot, "compose_voice_delivery", side_effect=fake_compose):
    with patch.object(telegram_bot, "settings", on), patch.object(telegram_bot, "tts_is_steerable", return_value=False):
        assert telegram_bot._voice_delivery("m", "r", mov) is None and not calls
    with patch.object(telegram_bot, "settings", SimpleNamespace(tts_delivery=False, liriel_self_vov_id=SELF)), \
            patch.object(telegram_bot, "tts_is_steerable", return_value=True):
        assert telegram_bot._voice_delivery("m", "r", mov) is None and not calls
    with patch.object(telegram_bot, "settings", on), patch.object(telegram_bot, "tts_is_steerable", return_value=True):
        assert telegram_bot._voice_delivery("m", "r", None) is None and not calls
        assert telegram_bot._voice_delivery("minha msg", "minha resposta", mov) == "Warm, a little shaky."
        assert calls == [("minha msg", "minha resposta", liriel)], calls   # her own row, the message and the reply -- nothing else

print("[check] a spoken reply carries the direction to the speech call; a typed one never asks for it; a failed direction still speaks")
sent, spoken = [], {}


def fake_synth(text, path, delivery=None):
    spoken["delivery"] = delivery
    Path(path).write_bytes(b"x" * max(3000, len(text) * 400))


def deliver(modality, input_voice, delivery_value):
    sent.clear(); spoken.clear()
    with patch.object(telegram_bot, "synthesize_speech", side_effect=fake_synth), \
            patch.object(telegram_bot, "_send_voice", side_effect=lambda cid, p: sent.append("voice")), \
            patch.object(telegram_bot, "_send_message", side_effect=lambda cid, t: sent.append("text")), \
            patch.object(telegram_bot, "_voice_delivery", return_value=delivery_value):
        telegram_bot._deliver_reply(1, "I am here with you.", modality, input_voice, user_text="hi", mov=mov)


deliver(None, True, "Warm, a little shaky.")
assert sent == ["voice"] and spoken["delivery"] == "Warm, a little shaky."
deliver("voice", False, None)
assert sent == ["voice"] and spoken["delivery"] is None
deliver(None, False, "never used")
assert sent == ["text"] and not spoken

print("[check] the reply prompt asks for spoken marks only when the reply will be spoken, and never allows tags or stage directions")
from models import BestPreyGuessResult  # noqa: E402

decision = BestPreyGuessResult(best_prey_guess=VectorObjectValence(vov_id="Objective_Verify", object_nature="Objective", brief_description="verify"))
assert prompts._SPOKEN_REPLY_RULE not in prompts.build_reply_prompt(decision, "oi", liriel, None)[1]["content"]
spoken_prompt = prompts.build_reply_prompt(decision, "oi", liriel, None, spoken=True)[1]["content"]
assert prompts._SPOKEN_REPLY_RULE in spoken_prompt and "[laughs]" in prompts._SPOKEN_REPLY_RULE and "Never stage directions" in prompts._SPOKEN_REPLY_RULE
assert spoken_prompt.index("WILL BE SPOKEN ALOUD") < spoken_prompt.index("Write your reply now")

print("[check] the cycle decides `spoken` exactly as the front end decides the channel: an explicit voice request, else the channel the message arrived on")
import re  # noqa: E402
import tempfile  # noqa: E402

from database import JsonFileDatabase  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="verify_voice_cycle_"))
db = JsonFileDatabase(path=tmp / "db.json")
top = settings.default_mov_id
db.ensure_mov(top)
db.upsert_object(top, VectorObjectValence(vov_id=SELF, object_nature="PCI", brief_description="Liriel"))
FAKE = {"modality": None}


def fake_chat_json(messages, temperature=None, effort=None, model=None):
    user = messages[1]["content"]
    query = re.search(r"^query: (\w+)", user, re.M).group(1)
    fixed = {
        "SAFETY_SCREEN": {"at_risk": "none"},
        "SCENE_SUBJECT_CHECK": {"is_new_subject": True, "interlocutor": SELF,
                                "elements": [{"vov_id": SELF, "object_nature": "PCI", "is_hunter": True, "new_this_cycle": False}]},
        "GRAPH_REQUEST": {"requests": [], "search_commands": [], "retrieval_satisfied": True},
        "TACTICAL_SCENE_INTERPRETATION": {"board": {"summary": "s"}, "hunters": []},
        "HUNTER_READING": {"ordinances_read": [], "schemas_read": []},
        "ANCHOR_REVIEW": {"evidence": []},
        "MAINMEMORY_FILING": {"archive": [], "restore": []},
        "MOV_UPDATE": {"mov_ops": [], "nested_mov_ops": []},
        "RELATIONS_UPDATE": {"write_relations": [], "soften_charge": []},
        "IDENTITY_UPDATE": {"records": []},
    }
    if query in fixed:
        return {"query": query, **fixed[query]}
    return {"query": query, "best_prey_guess": {"vov_id": "Objective_Verify", "brief_description": "verify", "relevant_relations": []},
            "accompanying_objectives": [], "handoff_to_processcommandcontrol": {"preferred_output_modality": FAKE["modality"]},
            "mov_ops": [], "nested_mov_ops": []}


real_builder = prompts.build_reply_prompt
seen_spoken = []


def spy(*a, **kw):
    seen_spoken.append(kw.get("spoken"))
    return real_builder(*a, **kw)


def cycle(source, modality):
    FAKE["modality"] = modality
    seen_spoken.clear()
    with patch.object(motivation, "chat_json", side_effect=fake_chat_json), patch.object(motivation, "chat", side_effect=lambda *a, **k: "ok"), \
            patch.object(motivation, "build_reply_prompt", side_effect=spy):
        _text, _mov, out = motivation.run_motivation_cycle(db, db.load_mov(top), "oi", source=source, sender="Fabio")
    assert out == modality, out
    return seen_spoken[0]


assert cycle("telegram_voice", None) is True      # a voice note comes back as voice
assert cycle("telegram", None) is False           # a typed message comes back typed
assert cycle("telegram", "voice") is True         # ... unless she was asked for voice
assert cycle("telegram_voice", "text") is False   # ... or asked for text
print("\nALL CHECKS PASSED")
