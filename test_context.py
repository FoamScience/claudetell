"""Context-usage extraction: the transcript carries per-message usage, and
claude-hud's cache carries the window size Claude Code states nowhere else."""

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

import claudetell


def _asst(usage, sidechain=False):
    return json.dumps(
        {"type": "assistant", "isSidechain": sidechain, "message": {"usage": usage}}
    )


def _write(tmp, lines):
    p = Path(tmp) / "s.jsonl"
    p.write_text("\n".join(lines) + "\n")
    claudetell._transcript_cache["s"] = p
    claudetell._ctx_cache.clear()
    return p


def test_context_used():
    with tempfile.TemporaryDirectory() as tmp:
        _write(
            tmp,
            [
                _asst({"input_tokens": 5, "cache_read_input_tokens": 1000}),
                # the newest main-chain message wins...
                _asst(
                    {
                        "input_tokens": 2,
                        "cache_read_input_tokens": 50000,
                        "cache_creation_input_tokens": 1456,
                    }
                ),
                # ...and a subagent's own window never counts as this session's
                _asst({"input_tokens": 10, "cache_read_input_tokens": 300}, True),
                json.dumps({"type": "user", "message": {}}),
            ],
        )
        assert claudetell.context_used("s") == 51458

    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, [json.dumps({"type": "user", "message": {}})])
        assert claudetell.context_used("s") == 0  # no assistant turn yet

    claudetell._transcript_cache.pop("s", None)
    assert claudetell.context_used("nope") == 0  # no transcript at all


def test_limit_tier_and_fraction():
    assert claudetell.context_limit(51458) == 200_000
    assert claudetell.context_limit(200_000) == 200_000
    assert claudetell.context_limit(727_306) == 1_000_000  # a 1M session
    assert claudetell.context_limit(9_000_000) == 1_000_000  # never divides by 0

    assert claudetell.ctx_fraction({"ctx": 0}) is None  # unknown, not 0%
    assert claudetell.ctx_fraction({"ctx": 100_000, "ctx_limit": 200_000}) == 0.5
    assert claudetell.ctx_fraction({"ctx": 100_000}) == 0.5  # tier inferred
    assert claudetell.ctx_fraction({"ctx": 100_000, "ctx_limit": 200_000}, 400_000) == 0.25
    assert claudetell.ctx_fraction({"ctx": 300_000, "ctx_limit": 200_000}) == 1.0  # clamped


def _hud_cache(tmp, session_id, transcript, **fields):
    """Write a snapshot where claude-hud keys it: sha256 of the transcript path."""
    claudetell.HUD_CTX_DIR = Path(tmp) / "hud"
    claudetell.HUD_CTX_DIR.mkdir(parents=True, exist_ok=True)
    claudetell._transcript_cache[session_id] = transcript
    key = hashlib.sha256(os.path.abspath(transcript).encode()).hexdigest()
    (claudetell.HUD_CTX_DIR / f"{key}.json").write_text(json.dumps(fields))


def test_context_from_hud_cache():
    with tempfile.TemporaryDirectory() as tmp:
        t = _write(tmp, [_asst({"cache_read_input_tokens": 147_000})])
        _hud_cache(
            tmp,
            "s1",
            t,
            used_percentage=14.7,
            context_window_size=1_000_000,
            current_usage={
                "input_tokens": 2,
                "cache_read_input_tokens": 147_000,
                "cache_creation_input_tokens": 319,
            },
            saved_at=1_787_666_589_794,
        )
        rec = claudetell.read_context("s1")
        assert rec["pct"] == 14.7 and rec["limit"] == 1_000_000
        assert rec["used"] == 147_321
        assert rec["ts"] == 1_787_666_589.794  # hud stamps milliseconds

        # a session hud has no snapshot for → the transcript plus a guessed tier
        other = Path(tmp) / "s2.jsonl"
        other.write_text(_asst({"cache_read_input_tokens": 147_000}) + "\n")
        claudetell._transcript_cache["s2"] = other
        used, limit, pct = claudetell.session_context("s2")
        assert (used, limit, pct) == (147_000, 200_000, None)

        # ...and with one, the real window size wins over the tier
        assert claudetell.session_context("s1") == (147_321, 1_000_000, 14.7)

        # the cached percentage beats the guessed tier — the ppump-validation bug:
        # 147k reads as 74% of a guessed 200k, but is really 15% of a real 1M
        assert claudetell.ctx_fraction({"ctx": 147_321}) > 0.7
        assert claudetell.ctx_fraction({"ctx": 147_321, "ctx_pct": 14.7}) == 0.147

    claudetell._transcript_cache.clear()
    assert claudetell.read_context("nope") is None  # no transcript at all


if __name__ == "__main__":
    test_context_used()
    test_limit_tier_and_fraction()
    test_context_from_hud_cache()
    print("ok")


def test_read_limits_reports_every_cswap_account(tmp_path, monkeypatch):
    monkeypatch.setattr(claudetell, "STATE_DIR", tmp_path)
    monkeypatch.setattr(claudetell, "CSWAP_DIR", tmp_path / "cswap")
    cache = tmp_path / "cswap" / "cache"
    cache.mkdir(parents=True)
    (tmp_path / "cswap" / "sequence.json").write_text('{"activeAccountNumber": 2}')
    (cache / "usage.json").write_text(
        json.dumps(
            {
                "accounts": {
                    "1": {
                        "fetchedAt": time.time(),
                        "lastGood": {
                            "five_hour": {
                                "pct": 100.0,
                                "resets_at": "2099-01-01T00:00:00+00:00",
                            }
                        },
                    },
                    "2": {
                        "fetchedAt": time.time(),
                        "lastGood": {
                            "five_hour": {
                                "pct": 5.0,
                                "resets_at": "2099-01-01T00:00:00+00:00",
                            },
                            "seven_day": {
                                "pct": 9.0,
                                "resets_at": "2099-01-02T00:00:00+00:00",
                            },
                        },
                    },
                }
            }
        )
    )
    # every account cswap knows, not only the one you are on
    accounts = claudetell.read_limits()
    assert [a["id"] for a in accounts] == ["1", "2"]
    assert [a["active"] for a in accounts] == [False, True]
    assert accounts[1]["five_hour"]["pct"] == 5.0
