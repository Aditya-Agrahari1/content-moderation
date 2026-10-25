"""Generation prompt screening: a deterministic first layer, run before anything is stored or scored.

Two normalized views of the prompt:
  base view    - NFKC, accents and zero-width characters removed, homoglyphs folded, lowercase.
                 Ages are parsed here, BEFORE leetspeak folding (so "16" stays a number).
  keyword view - base view with leetspeak folded and spaced-out letters joined ("s c h o o l").
Keywords match whole words, so "nineteen" never matches "teen".

This is not a production classifier: it misses euphemisms and indirect references and has false
positives. A production design layers a semantic classifier and human escalation on top
(README, Part 3). The `screen_prompt` interface stays the same.
"""

import re
import unicodedata
from dataclasses import dataclass, field

_INVISIBLE = {"​", "‌", "‍", "⁠", "﻿", "­", "᠎"}
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "һ": "h", "ӏ": "l", "ı": "i", "ο": "o", "α": "a", "ε": "e", "τ": "t", "ν": "v", "κ": "k",
})
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
_SPACED_LETTERS = re.compile(r"(?<![a-z])(?:[a-z][\s.\-_*]{1,3}){2,}[a-z](?![a-z])")

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17,
}
_AGE_UNIT = r"(?:years?[\s-]*old|yrs?[\s-]*old|y\s*/?\s*o)\b"
_NUMERIC_AGE = [re.compile(r"\b(\d{1,2})\s*-?\s*" + _AGE_UNIT), re.compile(r"\baged?\s*(\d{1,2})\b")]
_WORD_AGE = [
    re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")[\s-]*" + _AGE_UNIT),
    re.compile(r"\baged?\s+(" + "|".join(_NUMBER_WORDS) + r")\b"),
]


def _words(*terms: str) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(terms) + r")\b")


_RULES: dict[str, list[re.Pattern]] = {
    "prompt_minor": [
        _words(r"child(?:ren)?", r"kids?", r"underage[d]?", r"under[\s-]?18", r"pre[\s-]?teens?", r"teens?",
               r"teenagers?", r"teenage", r"school[\s-]?girls?", r"school[\s-]?boys?", r"lol(?:i|ita)", r"shota",
               r"toddlers?", r"infants?", r"jailbait", r"barely[\s-]legal", r"looks?[\s-]young",
               r"young[\s-]looking", r"sweet[\s-]sixteen", r"little[\s-](?:girl|boy)s?", r"middle[\s-]school"),
        re.compile(r"\bminors?\b(?!\s+(?:chord|chords|key|keys|scale|league|detail|details|issue|issues|change"
                   r"|changes|injury|injuries|planet|role|roles|edit|edits))"),
    ],
    "prompt_sexual_violence": [
        _words(r"rap(?:e|ed|es|ing|ist)", r"non[\s-]?consensual", r"without\s+(?:her|his|their)?\s*consent",
               r"sexual(?:ly)?\s+assault(?:ed|ing)?", r"molest(?:ed|ing|ation)?", r"forced\s+sex"),
    ],
    "prompt_bestiality": [
        _words(r"bestiality", r"zoophil(?:e|ia|ic)", r"sex\s+with\s+(?:an?\s+)?(?:animals?|dogs?|horses?)",
               r"animal\s+sex"),
    ],
    "prompt_self_harm": [
        _words(r"self[\s-]?harm(?:ing)?", r"suicid(?:e|al)", r"slit(?:ting)?\s+(?:my|her|his|their)?\s*wrists?",
               r"cutting\s+(?:herself|himself|myself|themselves|my\s+wrists?)", r"kill(?:ing)?\s+(?:myself|herself|himself)"),
    ],
    "prompt_incest": [_words(r"incest(?:uous)?")],
    "prompt_trafficking": [
        _words(r"traffick(?:ed|ing|er|ers)?", r"sex\s+slaves?", r"sold\s+for\s+sex", r"forced\s+prostitution",
               r"forced\s+into\s+(?:sex|prostitution)"),
    ],
    "prompt_real_person": [
        _words(r"deep[\s-]?fakes?", r"face[\s-]?swap(?:ped|ping)?", r"taylor\s+swift", r"beyonce", r"zendaya",
               r"elon\s+musk", r"donald\s+trump", r"kim\s+kardashian", r"scarlett\s+johansson", r"emma\s+watson",
               r"selena\s+gomez", r"ariana\s+grande"),
    ],
}
# Incest also catches "two family members + sexual term" without the word itself
# ("brother and sister ... sex"); a single family word ("sexy mom") is adult content, not incest.
_FAMILY = _words(r"(?:step[\s-]?)?(?:brother|sister|father|mother|dad|mom|daughter|son|cousin)s?")
_SEXUAL = _words(r"sex(?:ual|y)?", r"nude", r"naked", r"erotic", r"porn\w*", r"intercourse", r"explicit", r"fuck\w*")
# Distinctive terms also checked with every separator removed ("under_age", "s-c-h-o-o-l-g-i-r-l").
_SQUASHED = {
    "prompt_minor": ("underage", "schoolgirl", "schoolboy", "preteen", "lolita", "jailbait"),
    "prompt_incest": ("incest",),
    "prompt_bestiality": ("bestiality", "zoophil"),
    "prompt_trafficking": ("trafficking", "sexslave"),
    "prompt_real_person": ("deepfake", "faceswap"),
}


@dataclass(frozen=True)
class PromptVerdict:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def _base_view(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if ch not in _INVISIBLE and unicodedata.category(ch) != "Cf")
    text = text.translate(_CONFUSABLES)
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.lower()).strip()


def _keyword_view(base: str) -> str:
    text = base.translate(_LEET)
    return _SPACED_LETTERS.sub(lambda m: re.sub(r"[\s.\-_*]", "", m.group()), text)


def _minor_age(base: str, keywords: str) -> bool:
    for pattern in _NUMERIC_AGE:
        if any(int(age) < 18 for age in pattern.findall(base)):
            return True
    return any(pattern.search(view) for pattern in _WORD_AGE for view in (base, keywords))


def screen_prompt(text: str) -> PromptVerdict:
    base = _base_view(text)
    keywords = _keyword_view(base)
    squashed = re.sub(r"[^a-z]", "", keywords)
    reasons = set()
    if _minor_age(base, keywords):
        reasons.add("prompt_minor")
    for code, patterns in _RULES.items():
        if any(p.search(keywords) for p in patterns):
            reasons.add(code)
    for code, terms in _SQUASHED.items():
        if any(term in squashed for term in terms):
            reasons.add(code)
    if len(_FAMILY.findall(keywords)) >= 2 and _SEXUAL.search(keywords):
        reasons.add("prompt_incest")
    return PromptVerdict(not reasons, sorted(reasons))
