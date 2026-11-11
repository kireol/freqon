import numpy as np
import pytest

from freqon.mixer import AUDIO_RATE, Mixer

CHUNK = 4410 * 4  # 0.1 s of stereo int16


def tone(value: int, nbytes: int = CHUNK) -> bytes:
    return np.full(nbytes // 2, value, dtype=np.int16).tobytes()


def level(pcm: bytes) -> int:
    return int(np.frombuffer(pcm, dtype=np.int16)[-1])


def test_analog_until_digital_is_prefilled():
    m = Mixer(prefill_seconds=0.5)
    m.push_digital(0, tone(2000, CHUNK * 3))  # 0.3 s, below prefill
    assert level(m.mix(tone(1000))) == 1000
    assert m.source == "analog"
    m.push_digital(0, tone(2000, CHUNK * 3))  # now 0.6 s buffered
    assert level(m.mix(tone(1000))) == 2000
    assert m.source == "digital"


def test_falls_back_to_analog_when_digital_runs_dry():
    m = Mixer(prefill_seconds=0.2)
    m.push_digital(0, tone(2000, CHUNK * 2))
    m.mix(tone(1000))
    m.mix(tone(1000))
    assert m.source == "digital"
    assert level(m.mix(tone(1000))) == 1000
    assert m.source == "analog"


def test_output_length_always_matches_analog_chunk():
    m = Mixer(prefill_seconds=0.1)
    m.push_digital(0, tone(5, CHUNK + 12))
    for _ in range(3):
        assert len(m.mix(tone(1))) == CHUNK


def test_forced_analog_ignores_digital():
    m = Mixer(prefill_seconds=0.1)
    m.set_mode("analog")
    m.push_digital(0, tone(2000, CHUNK * 5))
    assert level(m.mix(tone(1000))) == 1000


def test_forced_digital_is_silent_without_signal():
    m = Mixer()
    m.set_mode("digital")
    assert level(m.mix(tone(1000))) == 0
    assert m.source == "none"


def test_hd2_has_no_analog_fallback():
    m = Mixer()
    m.reset(program=1)
    assert level(m.mix(tone(1000))) == 0
    m.push_digital(0, tone(2000, CHUNK * 20))  # HD1 audio is ignored on HD2
    assert level(m.mix(tone(1000))) == 0


def test_crossfade_avoids_jump():
    m = Mixer(prefill_seconds=0.1, crossfade_seconds=0.05)
    m.mix(tone(0))
    m.push_digital(0, tone(10000, CHUNK * 2))
    out = np.frombuffer(m.mix(tone(0)), dtype=np.int16)[0::2]
    assert out[0] < 500  # starts near the previous level
    assert out[-1] == 10000
    assert np.all(np.diff(out[: int(0.05 * AUDIO_RATE)]) >= 0)


def test_buffer_is_capped():
    m = Mixer(prefill_seconds=0.5, max_buffer_seconds=1.0)
    m.push_digital(0, tone(1, CHUNK * 15))
    assert m.digital_buffered_seconds <= 1.0


def test_rejects_unknown_mode():
    with pytest.raises(ValueError):
        Mixer().set_mode("stereo")


# -- weak-HD rule ----------------------------------------------------------------------------

from freqon.mixer import HDPolicy


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


POLICY = HDPolicy(fallback_seconds=1.5, return_seconds=3.0, max_fallbacks=3, window_seconds=300.0)


def play(m, clock, seconds, good=True, program=0):
    """Feed `seconds` of HD audio (music if good, nrsc5's concealment silence if not) and
    play the same amount, 0.1 s at a time. Returns the sources heard."""
    heard = []
    for _ in range(round(seconds * 10)):
        m.push_digital(program, tone(2000 if good else 0), ok=good)
        heard.append(m.source if m.mix(tone(1000)) is not None else None)
        clock.t += 0.1
    return heard


def hd_mixer():
    clock = Clock()
    return Mixer(prefill_seconds=0.2, policy=POLICY, clock=clock), clock


def test_hd_starts_only_after_it_has_been_solid_for_y_seconds():
    m, clock = hd_mixer()
    heard = play(m, clock, 2.5)
    assert set(heard) == {"analog"}  # buffered, but not solid long enough yet
    play(m, clock, 1.0)
    assert m.source == "digital"


def test_weak_hd_falls_back_to_analog_after_x_seconds():
    m, clock = hd_mixer()
    play(m, clock, 4)
    assert m.source == "digital"
    heard = play(m, clock, 1.0, good=False)
    assert heard[-1] == "digital"  # not yet: weak for less than X
    play(m, clock, 1.5, good=False)
    assert m.source == "analog"
    assert m.hd_quality is not None and m.hd_quality < 0.5


def test_a_short_glitch_does_not_switch():
    m, clock = hd_mixer()
    play(m, clock, 4)
    play(m, clock, 0.2, good=False)  # a couple of lost frames
    heard = play(m, clock, 3)
    assert set(heard) == {"digital"}


def test_back_to_hd_only_after_y_seconds_of_solid_signal():
    m, clock = hd_mixer()
    play(m, clock, 4)
    play(m, clock, 3, good=False)
    assert m.source == "analog"
    heard = play(m, clock, 2.5)  # solid again, but not for Y yet (window still holds bad audio)
    assert set(heard) == {"analog"}
    play(m, clock, 3)
    assert m.source == "digital"


def bounce(m, clock):
    play(m, clock, 5)  # solid -> HD
    play(m, clock, 3, good=False)  # weak -> FM


def test_too_many_bounces_stick_to_analog():
    m, clock = hd_mixer()
    for _ in range(3):
        bounce(m, clock)
    assert m.hd_hold
    heard = play(m, clock, 20)  # solid HD for a long time: still analog
    assert set(heard) == {"analog"}


def test_bounces_outside_the_window_do_not_count():
    m, clock = hd_mixer()
    bounce(m, clock)
    bounce(m, clock)
    clock.t += 301
    bounce(m, clock)
    assert not m.hd_hold
    play(m, clock, 5)
    assert m.source == "digital"


def test_choosing_auto_or_hd_again_or_retuning_clears_the_hold():
    for clear in (lambda m: m.set_mode("auto"), lambda m: m.set_mode("digital"), lambda m: m.reset(0)):
        m, clock = hd_mixer()
        for _ in range(3):
            bounce(m, clock)
        assert m.hd_hold
        clear(m)
        assert not m.hd_hold
        play(m, clock, 6)
        assert m.source == "digital"


def test_forced_hd_mode_never_falls_back():
    m, clock = hd_mixer()
    m.set_mode("digital")
    play(m, clock, 2)
    assert m.source == "digital"  # forced: no Y wait either
    heard = play(m, clock, 4, good=False)
    assert set(heard) == {"digital"}


def test_hd2_is_not_affected():
    m, clock = hd_mixer()
    m.reset(1)
    play(m, clock, 1, program=1)
    assert m.source == "digital"  # no analog to wait on
    heard = play(m, clock, 4, good=False, program=1)
    assert "analog" not in heard


def test_without_a_policy_behaviour_is_unchanged():
    m = Mixer(prefill_seconds=0.2)
    m.push_digital(0, tone(2000, CHUNK * 3), ok=False)
    m.mix(tone(1000))
    assert m.source == "digital" and not m.hd_hold
