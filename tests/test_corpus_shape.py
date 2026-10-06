"""The synthetic corpus must keep covering every parser outcome.

Each hand-authored message in tests/fixtures/_corpus.py declares the
classification it exists to pin down (`expect`). This test mines the
generated fixtures and checks every one of them landed in its declared
bucket — so an edit to the parser, or to the corpus, cannot quietly drop a
case the golden CSVs were relied on to cover.

It also guards the property that makes the fixtures publishable: the corpus
is the only source of fixture content, and it contains no real handles.
"""

import sys
from pathlib import Path

import pytest

from conftest import FIXTURES, ROOT
from ingestion.replay import ReplaySource
from parsing.history import mine_history
from parsing.parser import first_block

sys.path.insert(0, str(FIXTURES))

from _corpus import (  # noqa: E402
    ALPHA_AUTHOR,
    ALPHA_CORE,
    BRAVO_AUTHOR,
    BRAVO_CORE,
    BRAVO_OLD_AUTHOR,
)


def _norm(text: str) -> str:
    """Fixture key: the parsed first block, minus render-only decoration.

    The HTML renderer attaches custom emoji to some messages; the extractor
    keeps their alt text, which is real behaviour worth exercising but is
    not part of the corpus text being matched here.
    """
    return first_block(text).replace(" :rocket:", "").strip()


CASES = [
    ("alpha", FIXTURES / "alpha" / "export.html", ALPHA_CORE, (ALPHA_AUTHOR,)),
    ("bravo", FIXTURES / "bravo" / "alerts_full.txt", BRAVO_CORE,
     (BRAVO_AUTHOR, BRAVO_OLD_AUTHOR)),
]


def _buckets(cfg, alerter: str, export: Path) -> dict[str, str]:
    """first_block(text) -> bucket, for every message in a rendered export."""
    msgs = ReplaySource(export, alerter).messages()
    mined = mine_history(msgs, cfg.alerters[alerter], cfg.parser)
    out: dict[str, str] = {}
    for e in mined.entries:
        out[_norm(e.alert.raw)] = "entry"
    for r in mined.repeats:
        out[_norm(r.alert.raw)] = "repeat"
    for a in mined.ambiguous:
        out[_norm(a.raw)] = "ambiguous"
    hints = tuple(h.lower() for h in cfg.alerters[alerter].author_hints)
    for m in msgs:
        key = _norm(m.text)
        if not any(h in m.author.lower() for h in hints):
            out.setdefault(key, "excluded")
        else:
            out.setdefault(key, "noise")
    return out


@pytest.mark.parametrize("alerter,export,core,_hints", CASES, ids=[c[0] for c in CASES])
def test_every_corpus_case_lands_in_its_declared_bucket(
    cfg, alerter, export, core, _hints
):
    buckets = _buckets(cfg, alerter, export)
    wrong = []
    for m in core:
        key = _norm(m.text)
        got = buckets.get(key)
        if got != m.expect:
            wrong.append(f"{m.expect!r} != {got!r}  [{m.note}]  {key!r}")
    assert not wrong, "corpus cases changed classification:\n  " + "\n  ".join(wrong)


@pytest.mark.parametrize("alerter,export,core,_hints", CASES, ids=[c[0] for c in CASES])
def test_corpus_still_covers_every_outcome(cfg, alerter, export, core, _hints):
    covered = {m.expect for m in core}
    assert covered == {"entry", "repeat", "noise", "ambiguous", "excluded"}, (
        f"{alerter} corpus no longer covers every parser outcome: {sorted(covered)}"
    )


def test_fixture_text_comes_only_from_the_corpus():
    """Every alert line in the generated exports traces back to _corpus.py.

    This is the property that lets the fixtures be published: if a real
    export were ever dropped into tests/fixtures/, this fails.
    """
    from _corpus import BRAVO_PASTED, alpha_messages, bravo_messages

    # The copy-paste shape splits a multi-line message into one message per
    # line, so individual lines are legitimate fixture messages too.
    known: set[str] = set()
    for m in [*alpha_messages(), *bravo_messages(), *BRAVO_PASTED]:
        known.add(_norm(m.text))
        for line in m.text.splitlines():
            if line.strip():
                known.add(_norm(line))

    for alerter, export in (
        ("alpha", FIXTURES / "alpha" / "export.html"),
        ("bravo", FIXTURES / "bravo" / "alerts_full.txt"),
        ("bravo", FIXTURES / "bravo" / "sample_pasted.txt"),
    ):
        for m in ReplaySource(export, alerter).messages():
            block = _norm(m.text)
            assert block in known, (
                f"{export.name}: message not traceable to _corpus.py: {block!r}"
            )


def test_generator_is_reproducible():
    """Regenerating must be byte-stable — no timestamps, no randomness."""
    import subprocess

    before = {
        p.name: p.read_bytes()
        for p in sorted((FIXTURES / "alpha").iterdir())
        if p.is_file()
    }
    before |= {
        "bravo/" + p.name: p.read_bytes()
        for p in sorted((FIXTURES / "bravo").iterdir())
        if p.is_file()
    }
    subprocess.run(
        [sys.executable, str(FIXTURES / "_generate.py")],
        cwd=ROOT, check=True, capture_output=True,
    )
    after = {
        p.name: p.read_bytes()
        for p in sorted((FIXTURES / "alpha").iterdir())
        if p.is_file()
    }
    after |= {
        "bravo/" + p.name: p.read_bytes()
        for p in sorted((FIXTURES / "bravo").iterdir())
        if p.is_file()
    }
    assert before == after
