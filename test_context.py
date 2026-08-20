"""Context-usage extraction: the transcript is the only source (Claude Code
records per-message usage but never the window size)."""

import json
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


def test_statusline_tee():
    with tempfile.TemporaryDirectory() as tmp:
        claudetell.CTX_DIR = Path(tmp) / "ctx"

        # Claude Code's own percentage wins — it knows what it reserves
        claudetell.record_context(
            {
                "session_id": "s1",
                "context_window": {
                    "context_window_size": 1_000_000,
                    "used_percentage": 14.7,
                    "current_usage": {
                        "input_tokens": 2,
                        "cache_read_input_tokens": 147_000,
                        "cache_creation_input_tokens": 319,
                    },
                },
            }
        )
        rec = claudetell.read_context("s1")
        assert rec["pct"] == 14.7 and rec["limit"] == 1_000_000
        assert rec["used"] == 147_321

        # ...and without it we compute the ratio ourselves
        claudetell.record_context(
            {
                "session_id": "s2",
                "context_window": {
                    "context_window_size": 200_000,
                    "current_usage": {"input_tokens": 50_000},
                },
            }
        )
        assert claudetell.read_context("s2")["pct"] == 25.0

        # a payload with no context_window must not leave a file behind
        claudetell.record_context({"session_id": "s3"})
        assert claudetell.read_context("s3") is None

        # a bulk redraw states the size but no usage: keep the size, and never
        # let its zero clobber the reading we already had
        zeroed = {
            "session_id": "s1",
            "context_window": {
                "context_window_size": 1_000_000,
                "used_percentage": 0,
                "current_usage": {},
            },
        }
        claudetell.record_context(zeroed)
        assert claudetell.read_context("s1")["used"] == 147_321  # not wiped

        zeroed["session_id"] = "s4"  # ...and with nothing prior, fall back
        claudetell.record_context(zeroed)
        claudetell._transcript_cache.clear()
        claudetell._ctx_cache.clear()
        _write(tmp, [_asst({"cache_read_input_tokens": 500_000})])
        claudetell._transcript_cache["s4"] = claudetell._transcript_cache["s"]
        used, limit, pct = claudetell.session_context("s4")
        assert (used, limit, pct) == (500_000, 1_000_000, 50.0)  # real size, not a tier

        # the tee'd percentage beats the guessed tier — the ppump-validation bug:
        # 147k reads as 74% of a guessed 200k, but is really 15% of a real 1M
        assert claudetell.ctx_fraction({"ctx": 147_321}) > 0.7
        assert claudetell.ctx_fraction({"ctx": 147_321, "ctx_pct": 14.7}) == 0.147


def test_statusline_wrap_roundtrip():
    original = "bash -c 'exec \"/n/bin/node\" \"$(ls -d /x/*/ | tail -1)dist/index.js\"'"
    settings = {"statusLine": {"type": "command", "command": original}}
    claudetell._wrap_statusline(settings)
    assert claudetell.STATUSLINE_TAG in settings["statusLine"]["command"]
    # the displaced command survives byte-for-byte — nested quotes intact
    assert settings["statusLine"]["_claudetell_wrapped"] == original
    claudetell._wrap_statusline(settings)  # idempotent: no double wrap
    assert settings["statusLine"]["_claudetell_wrapped"] == original
    claudetell._unwrap_statusline(settings)
    assert settings == {"statusLine": {"type": "command", "command": original}}

    # nothing configured → we never install ourselves as the statusline
    empty = {}
    claudetell._wrap_statusline(empty)
    assert empty == {}


if __name__ == "__main__":
    test_context_used()
    test_limit_tier_and_fraction()
    test_statusline_tee()
    test_statusline_wrap_roundtrip()
    print("ok")


def test_record_limits_ignores_stale_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(claudetell, "STATE_DIR", tmp_path)
    monkeypatch.setattr(claudetell, "LIMITS_FILE", tmp_path / "limits.json")
    fresh = {
        "rate_limits": {
            "five_hour": {"used_percentage": 19, "resets_at": time.time() + 3600},
            "seven_day": {"used_percentage": 28, "resets_at": time.time() + 86400},
        }
    }
    claudetell.record_limits(fresh)
    assert claudetell.read_limits()["five_hour"]["pct"] == 19
    # an idle session still reporting a window that already reset must not win
    claudetell.record_limits(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": 46, "resets_at": time.time() - 60},
                "seven_day": {"used_percentage": 11, "resets_at": time.time() + 86400},
            }
        }
    )
    assert claudetell.read_limits()["five_hour"]["pct"] == 19
