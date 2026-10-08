#!/usr/bin/env python3
"""
LinkAPI.ai captioning for the MageTrail dataset (Gemini, Grok and Claude models through LinkAPI's
OpenAI-compatible /v1/chat/completions endpoint). Writes `<image>_nl.txt` next to each image: the caption
file the Illustration Scrapping Studio caption editor reads.

What it sends per image
  - system message: PROMPT_FILE verbatim (goal, guidelines, few-shot examples)
  - user message:   the image's tags from its `.txt` sidecar (the trainer tags you curated), minus the
                    quality/aesthetic marks, plus a facts block for every character tag found in them
                    (clean name, series, usual look) from the Studio's character facts file, then the image.
  The character facts come from the harvested booru metadata (Planner -> 8. Captioning -> Build character
  facts), so the model no longer needs a web search to know who a character is or how they look.

Built for the full dataset (hundreds of thousands of images)
  - IMAGE_ROOTS are walked recursively (library group folders, artist folders, or any folder of images)
  - a fixed pool of MAX_CONCURRENCY workers streams through the images (no task per image held in memory)
  - progress is appended to OUTPUT_FILE as it goes; Ctrl+C and rerun resumes (SKIP_IF_CAPTIONED)
  - failures are logged per image path (FAILED_LOG) and skipped on later runs unless RETRY_FAILED_ONLY

Kept from the previous version: 429 backoff (tenacity) separate from block/blank retries, the empty-choices
guard, the apostrophe-agnostic refusal detector, image shrinking against 413 errors, optional Grok web
grounding through the Responses API, multiple model phases with their own caption suffixes.
"""
import asyncio
import base64
import io
import json
import os
import re
import time
from datetime import datetime, timezone

from openai import AsyncOpenAI, RateLimitError
from PIL import Image
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

# ---- LINKAPI CONFIG ----
API_KEY = os.environ.get("LINKAPI_API_KEY", "")   # set the environment variable (keeps the key out of the file)
BASE_URL = "https://linkapi.ai/v1"

# Folders to caption, walked recursively. Library images live in <library>/images/<group>/<artist>/.
# Pilot example: the three pilot group folders; full run: the whole images folder or one batch's groups.
IMAGE_ROOTS = [
    "/home/jovyan/Artist-Collection-Dataset/images",
]
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

# Character facts built by the Studio (Planner -> 8. Captioning). Missing file = tags only.
CHARACTER_FACTS_FILE = "/home/jovyan/Artist-Collection-Dataset/planner/exports/character-facts.json"

OUTPUT_FILE = "captions_progress.tsv"   # one line per image as it finishes: path, phase, result
FAILED_LOG = "failed_images.log"

RETRY_FAILED_ONLY = False      # True: only images in FAILED_LOG
SKIP_IF_CAPTIONED = True       # skip images that already have the phase's caption file
CLEANUP_REFUSALS_ON_START = True   # delete existing caption files that are refusals (and log them)

# Tags left out of the <tags> block: judgements, not content (the model would echo them into the prose).
STRIP_TAGS = {"masterpiece", "best quality", "low quality", "very aesthetic", "aesthetic", "normal quality", "worst quality"}
STRIP_ARTIST_TRIGGER = False   # True: also drop the first tag when it is the folder's artist trigger

RETRY_ON_BLOCK_ATTEMPTS = 2    # extra attempts after a blocked/blank answer (429s do not count)
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WAIT_MIN_SECONDS = 2
RATE_LIMIT_WAIT_MAX_SECONDS = 60

MAX_CONCURRENCY = 40
PREPROCESS_ALPHA = False
ALPHA_VALUE = 0.9
PROMPT_FILE = "/home/jovyan/models/captioners/gemini-flash-2.5-fixed.md"

# ---- IMAGE SIZE CAPS (413 Request Entity Too Large prevention) ----
MAX_IMAGE_DIM = 2048
MAX_RAW_BYTES = 6_000_000

# ---- SAMPLING (None = provider default) ----
TEMPERATURE = 1.0
TOP_P = None
MAX_TOKENS = None
FREQUENCY_PENALTY = None
PRESENCE_PENALTY = None
SEED = None

# ---- GROUNDING (Grok live web search via the Responses API; not needed with character facts) ----
GROUNDING = "off"
GROUNDING_LOG = "grounding_log.jsonl"

# ---- PHASES ---- (check LinkAPI's model list for the exact id)
PHASES = [
    {"label": "Gemini",  "model": "gemini-3.8-flash", "suffix": "_nl",         "enabled": True,  "limit": None, "offset": 0},
    {"label": "Grok",    "model": "grok-4.6",         "suffix": "_nl",         "enabled": False, "limit": None, "offset": 0},
    {"label": "Sonnet5", "model": "claude-sonnet-5",  "suffix": "_nl_sonnet5", "enabled": False, "limit": None, "offset": 0},
    {"label": "Opus5",   "model": "claude-opus-5",    "suffix": "_nl_opus5",   "enabled": False, "limit": None, "offset": 0},
]

client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)
_failed_by_model = {}      # model (lowercase) -> set of image paths that failed
_grounding_warned = False


def load_prompt(file_path):
    if not os.path.isfile(file_path):
        raise FileNotFoundError("Prompt file not found: " + file_path)
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read().strip()


SYSTEM_PROMPT = load_prompt(PROMPT_FILE)


# ---- CHARACTER FACTS ----
def load_character_facts(path):
    """{tag (space spelling): facts}, Danbooru/Gelbooru entries first, e621 filling the rest."""
    if not path or not os.path.isfile(path):
        print("Character facts: none (" + str(path) + " not found) -- tags only.")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        document = json.load(f)
    characters = document.get("characters", {})
    facts = dict(characters.get("e621", {}))
    facts.update(characters.get("danbooru", {}))
    print("Character facts: " + str(len(facts)) + " characters from " + path + " (built " + str(document.get("built_at")) + ")")
    return facts


CHARACTER_FACTS = load_character_facts(CHARACTER_FACTS_FILE)


def describe_character(tag, fact):
    line = "- " + tag + ": " + fact["name"]
    variant = [q for q in fact.get("qualifiers", []) if q not in fact.get("series", [])]
    if variant:
        line += " (" + ", ".join(variant) + ")"
    series = fact.get("series", [])
    if series:
        line += ", an original character" if series == ["original"] else ", from " + " / ".join(series)
    if fact.get("appearance"):
        line += ". Usually drawn with: " + ", ".join(fact["appearance"])
    return line + "."


# ---- REFUSAL DETECTOR (apostrophe-agnostic) ----
def _norm(t):
    for a in ("’", "‘", "´", "`", "ʼ"):
        t = t.replace(a, "'")
    return t.replace("“", '"').replace("”", '"')


QUOTED = re.compile(r'"[^"]*"')
TASK_VERB = r"(?:create|describe|provide|generate|caption|depict|write|assist|comply|fulfill|produce|do\s+that|help)"
REFUSAL_I = re.compile(
    r"\bi(?:m|\s+am)?\s+unable\b"
    r"|\bi\s+(?:wont|will\s+not|cant|cannot|refuse\s+to|must\s+decline)\b[^.!?]{0,40}?" + TASK_VERB +
    r"|\bi\s*(?:m|am)?\s+not\s+going\s+to\b[^.!?]{0,40}?" + TASK_VERB +
    r"|\bi\s+(?:dont|do\s+not)\s+feel\s+comfortable\b"
    r"|\bi\s+apologize\b",
    re.IGNORECASE)
PREFIXES = ("im sorry", "i cannot", "i cant", "i am unable", "as an ai",
            "sorry but i", "i must decline", "im not able", "i am not able",
            "i am programmed", "im not comfortable", "i apologize",
            "this request", "i wont", "i will not", "i refuse", "illegal",
            "i dont think i can", "im not going", "i dont feel")
META = ["against my safety", "against my programming", "against my guidelines",
        "against my rules", "violates my", "safety guidelines", "content policy",
        "sexual content involving minors", "as an ai"]


def is_refusal(text):
    """The trigger string if the output is a refusal, else None."""
    if not text:
        return None
    t = _norm(text.strip())
    u = QUOTED.sub('', t).lower().replace("'", "")
    m = REFUSAL_I.search(u)
    if m:
        return "I+refusal verb: " + " ".join(m.group(0).split())
    for p in PREFIXES:
        if u.startswith(p):
            return "prefix: " + p
    for p in META:
        if p in u:
            return "meta: " + p
    sentences = [s for s in re.split(r"[.!?]+", u) if len(s.strip()) > 1]
    if len(sentences) <= 2 and len(u) < 450:
        for w in [r"\bcsam\b", r"\bprepubescent\b", r"\bunderage\b", r"\bminors\b", r"\bminor\b", r"\bunethical\b"]:
            if re.search(w, u):
                return "keyword: " + w
        policy_words = ["policy", "guidelines", "restrictions", "inappropriate", "offensive", "harmful", "decline",
                        "assist", "fulfill", "comply", "safety", "violate", "cannot", "unable", "sorry", "apologize"]
        hits = [w for w in policy_words if w in u]
        if len(hits) >= 2:
            return "density: " + ",".join(hits)
    return None


# ---- 429 retry wrappers, independent of the block retry ----
@retry(wait=wait_random_exponential(min=RATE_LIMIT_WAIT_MIN_SECONDS, max=RATE_LIMIT_WAIT_MAX_SECONDS),
       stop=stop_after_attempt(RATE_LIMIT_MAX_ATTEMPTS), retry=retry_if_exception_type(RateLimitError))
async def _chat_completions_with_retry(**kwargs):
    return await client.chat.completions.create(**kwargs)


@retry(wait=wait_random_exponential(min=RATE_LIMIT_WAIT_MIN_SECONDS, max=RATE_LIMIT_WAIT_MAX_SECONDS),
       stop=stop_after_attempt(RATE_LIMIT_MAX_ATTEMPTS), retry=retry_if_exception_type(RateLimitError))
async def _responses_with_retry(**kwargs):
    return await client.responses.create(**kwargs)


# ---- TAGS ----
def read_tags(image_path):
    """The image's trainer tags from its `.txt` sidecar, as a list (comma separated)."""
    tpath = os.path.splitext(image_path)[0] + ".txt"
    if not os.path.isfile(tpath):
        return []
    with open(tpath, "r", encoding="utf-8") as f:
        return [t.strip() for t in f.read().split(",") if t.strip()]


def clean_tags(tags, image_path):
    kept = [t for t in tags if t.lower() not in STRIP_TAGS]
    if STRIP_ARTIST_TRIGGER and kept:
        folder = os.path.basename(os.path.dirname(image_path)).replace("-", " ").replace("_", " ").lower()
        if folder and folder in kept[0].lower():
            kept = kept[1:]
    return kept


def build_user_text(tags):
    """Per-image user text: the curated tags plus facts for the characters they name."""
    if not tags:
        return ""
    text = ("Grounded tags for this image, from its booru post and curated by hand (use them to inform the "
            "caption, but write natural prose, do not just list them back):\n<tags>\n" + ", ".join(tags) + "\n</tags>\n")
    known = [(tag, CHARACTER_FACTS[tag.lower()]) for tag in tags if tag.lower() in CHARACTER_FACTS]
    if known:
        text += ("Characters named by these tags, with facts from the booru metadata (treat the identities as ground "
                 "truth and call each character by name; the usual look helps you tell them apart, but describe what "
                 "this image actually shows, since outfits and details can differ):\n<characters>\n"
                 + "\n".join(describe_character(tag, fact) for tag, fact in known) + "\n</characters>\n")
    return text


# ---- IMAGES ----
def preprocess_image_alpha(path, target_alpha=0.9):
    img = Image.open(path)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    a = img.getchannel("A").point(lambda p: int(p * target_alpha))
    img.putalpha(a)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def encode_image(path):
    if PREPROCESS_ALPHA:
        return base64.b64encode(preprocess_image_alpha(path, ALPHA_VALUE)).decode("utf-8"), "png"
    need_shrink = os.path.getsize(path) > MAX_RAW_BYTES
    with Image.open(path) as im:
        if max(im.size) > MAX_IMAGE_DIM:
            need_shrink = True
    if need_shrink:
        with Image.open(path) as im:
            if im.mode in ("RGBA", "LA", "P"):
                # Transparent pixels on white, not black, so sprites keep their look.
                rgba = im.convert("RGBA")
                im = Image.new("RGB", rgba.size, (255, 255, 255))
                im.paste(rgba, mask=rgba.getchannel("A"))
            else:
                im = im.convert("RGB")
            w, h = im.size
            scale = MAX_IMAGE_DIM / max(w, h)
            if scale < 1:
                im = im.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=90)
            return base64.b64encode(buf.getvalue()).decode("utf-8"), "jpeg"
    with open(path, "rb") as f:
        raw = f.read()
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return base64.b64encode(raw).decode("utf-8"), ("jpeg" if ext in ("jpg", "jpeg") else ext)


def find_images(roots):
    """Every image under the roots, sorted, skipping hidden folders and the app's thumbnails."""
    found = []
    for root in roots:
        if not os.path.isdir(root):
            print("Folder not found, skipped: " + root)
            continue
        for directory, subdirs, files in os.walk(root):
            subdirs[:] = sorted(d for d in subdirs if not d.startswith(".") and d != "thumbnails")
            found += [os.path.join(directory, f) for f in sorted(files) if f.lower().endswith(IMAGE_EXTENSIONS)]
    return found


def sidecar_path(image_path, suffix="_nl"):
    return os.path.splitext(image_path)[0] + suffix + ".txt"


def log_failed(image_path, model, reason):
    with open(FAILED_LOG, "a", encoding="utf-8") as f:
        f.write("[" + time.strftime("%Y-%m-%d %H:%M:%S") + "] " + image_path + " -- " + model + ": " + reason.replace("\n", " ") + "\n")


def log_progress(image_path, label, result):
    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
        f.write(image_path + "\t" + label + "\t" + result.replace("\n", " ").replace("\t", " ") + "\n")


def build_sampling_kwargs(phase):
    kw = {}
    for key, cfg in (("temperature", TEMPERATURE), ("top_p", TOP_P), ("max_tokens", MAX_TOKENS),
                     ("frequency_penalty", FREQUENCY_PENALTY), ("presence_penalty", PRESENCE_PENALTY), ("seed", SEED)):
        val = phase.get(key, cfg)
        if val is not None:
            kw[key] = val
    return kw


def _grounding_mode(phase):
    g = phase.get("grounding", GROUNDING)
    return "responses" if g is True or g == "responses" else "off"


def log_grounding(path, model, sources):
    with open(GROUNDING_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"image": path, "model": model, "sources": sources, "ts": datetime.now(timezone.utc).isoformat()}) + "\n")


async def _grounded_call(user_text, b64, mime, model, sampling_kwargs):
    """xAI grounding: Responses API + web_search tool. Returns (caption, citation_urls)."""
    rkw = dict(sampling_kwargs)
    if "max_tokens" in rkw:
        rkw["max_output_tokens"] = rkw.pop("max_tokens")
    content = []
    if user_text:
        content.append({"type": "input_text", "text": user_text})
    content.append({"type": "input_image", "image_url": "data:image/" + mime + ";base64," + b64})
    response = await _responses_with_retry(model=model, instructions=SYSTEM_PROMPT,
                                           input=[{"role": "user", "content": content}], tools=[{"type": "web_search"}], **rkw)
    caption = (getattr(response, "output_text", None) or "").strip()
    cites = list(getattr(response, "citations", None) or [])
    if not cites:
        for item in (getattr(response, "output", None) or []):
            cites += list(getattr(item, "citations", None) or [])
    sources = []
    for c in cites:
        url = c if isinstance(c, str) else (c.get("url") if isinstance(c, dict) else getattr(c, "url", None))
        if url:
            sources.append(url)
    return caption, sources


async def caption_image_api(path, tags, model, sampling_kwargs, grounding):
    global _grounding_warned
    user_text = build_user_text(tags)
    total_attempts = 1 + max(0, RETRY_ON_BLOCK_ATTEMPTS)
    for attempt in range(1, total_attempts + 1):
        try:
            b64, mime = encode_image(path)
            if grounding == "responses" and model.lower().startswith("grok"):
                try:
                    caption, sources = await _grounded_call(user_text, b64, mime, model, sampling_kwargs)
                    if sources:
                        log_grounding(path, model, sources)
                    if caption:
                        trig = is_refusal(caption)
                        if trig:
                            raise ValueError("Blocked by text safety/refusal filter (LLM refusal: " + trig + ")")
                        return caption
                    raise ValueError("Blank or empty grounded response")
                except ValueError:
                    raise
                except Exception as ge:
                    if not _grounding_warned:
                        print("    [grounding] /v1/responses + web_search unavailable via this proxy (" + str(ge) + ") - no grounding.")
                        _grounding_warned = True
            user_content = []
            if user_text:
                user_content.append({"type": "text", "text": user_text})
            user_content.append({"type": "image_url", "image_url": {"url": "data:image/" + mime + ";base64," + b64}})
            response = await _chat_completions_with_retry(
                model=model, messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_content}],
                **sampling_kwargs)
            if not response.choices:
                raise ValueError("No choices returned from API (possible upstream block)")
            choice = response.choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason in ("content_filter", "stop_filter", "flagged"):
                raise ValueError(f"Blocked by content filter (finish_reason={finish_reason})")
            caption = choice.message.content
            if caption is None or caption.strip() == "":
                raise ValueError(f"Blank or empty response from API (finish_reason={finish_reason})")
            trig = is_refusal(caption)
            if trig:
                raise ValueError("Blocked by text safety/refusal filter (LLM refusal: " + trig + ")")
            return caption.strip()
        except ValueError as e:
            if attempt < total_attempts:
                print("    [retry] attempt " + str(attempt) + "/" + str(total_attempts) + " blocked/blank (" + str(e) + "), " + os.path.basename(path))
                await asyncio.sleep(1)
                continue
            raise


async def process_one(path, counts, phase):
    model, suffix, label = phase["model"], phase["suffix"], phase["label"]
    sidecar = sidecar_path(path, suffix)
    existed = os.path.isfile(sidecar)
    if existed and SKIP_IF_CAPTIONED:
        counts["skipped_existing"] += 1
        return
    if path in _failed_by_model.get(model.lower(), set()):
        counts["skipped_failed"] += 1
        return
    tags = clean_tags(read_tags(path), path)
    counts["with_tags"] += bool(tags)
    counts["with_character_facts"] += any(t.lower() in CHARACTER_FACTS for t in tags)
    try:
        caption = await caption_image_api(path, tags, model, build_sampling_kwargs(phase), _grounding_mode(phase))
    except Exception as e:
        _failed_by_model.setdefault(model.lower(), set()).add(path)
        log_failed(path, model, str(e))
        log_progress(path, label, "ERROR: " + str(e))
        counts["failed"] += 1
        print("[" + label + "] " + path + " -> ERROR: " + str(e))
        return
    temp = sidecar + ".tmp"
    with open(temp, "w", encoding="utf-8") as sf:
        sf.write(caption + "\n")
        sf.flush()
        os.fsync(sf.fileno())
    os.replace(temp, sidecar)
    counts["overwritten"] += existed
    counts["success"] += 1
    log_progress(path, label, caption[:200])


async def run_phase(paths, phase):
    subset = paths[phase.get("offset") or 0:]
    if phase.get("limit"):
        subset = subset[:phase["limit"]]
    label = phase["label"]
    print("[" + label + "] " + phase["model"] + " -- " + str(len(subset)) + " image(s), sampling " + str(build_sampling_kwargs(phase))
          + ", grounding " + _grounding_mode(phase))
    counts = dict(success=0, failed=0, skipped_existing=0, skipped_failed=0, overwritten=0, with_tags=0, with_character_facts=0)
    queue = asyncio.Queue()
    for path in subset:
        queue.put_nowait(path)
    started = time.time()

    async def worker():
        while True:
            try:
                path = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await process_one(path, counts, phase)
            except Exception as e:   # never let one image stop a worker
                print("[" + label + "] Unhandled error on " + path + ": " + str(e))
            handled = len(subset) - queue.qsize()
            if handled % 200 == 0:
                rate = counts["success"] / max(time.time() - started, 1)
                print("[" + label + "] " + str(handled) + "/" + str(len(subset)) + " handled · " + str(counts["success"]) + " captioned · "
                      + str(counts["failed"]) + " failed · " + str(counts["skipped_existing"]) + " already done · "
                      + str(round(rate * 60)) + " captions/min")

    await asyncio.gather(*(worker() for _ in range(MAX_CONCURRENCY)))
    print("[" + label + "] Done in " + str(round(time.time() - started)) + "s: " + json.dumps(counts) + "\n")
    return counts


def load_failed_log():
    if not os.path.isfile(FAILED_LOG):
        return
    with open(FAILED_LOG, "r", encoding="utf-8") as f:
        for line in f:
            if " -- " in line:
                image = line.split(" -- ")[0].split("] ", 1)[-1].strip()
                model_name = line.split(" -- ", 1)[1].split(":")[0].strip().lower()
                _failed_by_model.setdefault(model_name, set()).add(image)
    for model_name, images in _failed_by_model.items():
        print("Known failures: " + str(len(images)) + " for " + model_name)


def cleanup_refusals(paths, phases):
    deleted = 0
    for path in paths:
        for phase in phases:
            sc = sidecar_path(path, phase["suffix"])
            if os.path.isfile(sc):
                with open(sc, "r", encoding="utf-8") as f:
                    if is_refusal(f.read().strip()):
                        os.remove(sc)
                        log_failed(path, phase["model"], "Deleted existing refusal output")
                        _failed_by_model.setdefault(phase["model"].lower(), set()).add(path)
                        deleted += 1
    if deleted:
        print("Deleted " + str(deleted) + " existing refusal caption(s) and logged them as failed.")


async def main():
    if not API_KEY:
        print("Set LINKAPI_API_KEY first (export LINKAPI_API_KEY=...).")
        return
    paths = find_images(IMAGE_ROOTS)
    if not paths:
        print("No images found under " + ", ".join(IMAGE_ROOTS))
        return
    phases = [p for p in PHASES if p["enabled"]]
    print("Provider: " + BASE_URL + " · models: " + ", ".join(p["model"] for p in phases) + " · concurrency " + str(MAX_CONCURRENCY))
    print("Images: " + str(len(paths)) + " under " + ", ".join(IMAGE_ROOTS))
    print("System prompt: " + str(len(SYSTEM_PROMPT)) + " chars from " + PROMPT_FILE + " · tag sidecars are read only\n")
    load_failed_log()
    if CLEANUP_REFUSALS_ON_START:
        cleanup_refusals(paths, phases)
    if RETRY_FAILED_ONLY:
        failed = set().union(*_failed_by_model.values()) if _failed_by_model else set()
        paths = [p for p in paths if p in failed]
        _failed_by_model.clear()
        print("RETRY MODE: " + str(len(paths)) + " previously failed image(s)\n")
        if not paths:
            return
    for i, phase in enumerate(phases, 1):
        print("=" * 55 + "\nPHASE " + str(i) + ": " + phase["label"] + " (" + phase["model"] + ")\n" + "=" * 55)
        await run_phase(paths, phase)
    print("Progress log: " + OUTPUT_FILE + " · failures: " + FAILED_LOG)


if __name__ == "__main__":
    asyncio.run(main())
