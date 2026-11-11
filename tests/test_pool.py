import pytest

from freqon.pool import NoTunerFree, TunerPool


class T:
    def __init__(self, index, frequency=None, tuned_at=0.0):
        self.index = index
        self.tuned_at = tuned_at
        self.state = type("S", (), {"running": frequency is not None, "frequency": frequency})()


def pool(*tuners):
    return TunerPool(list(tuners))


def test_same_frequency_reused():
    a, b = T(0, 97.1, 1), T(1)
    assert pool(a, b).choose(97.1, "web", {0: {"plex"}}, False) is a


def test_own_tuner_preferred_when_alone():
    a, b = T(0, 97.1, 1), T(1)
    assert pool(a, b).choose(99.5, "web", {0: {"web"}}, False) is a


def test_idle_tuner_used():
    a, b = T(0, 97.1, 1), T(1, 101.9, 2)
    assert pool(a, b).choose(99.5, "web", {0: {"plex"}}, False) is b


def test_idle_prefers_a_stopped_tuner():
    a, b = T(0, 101.9, 2), T(1)
    assert pool(a, b).choose(99.5, "web", {}, False) is b


def test_busy_raises_with_holders():
    a, b = T(0, 97.1, 1), T(1, 101.9, 2)
    with pytest.raises(NoTunerFree) as e:
        pool(a, b).choose(99.5, "web", {0: {"plex"}, 1: {"jellyfin", "web"}}, False)
    assert e.value.busy == [
        {"tuner": 0, "frequency": 97.1, "holders": ["plex"]},
        {"tuner": 1, "frequency": 101.9, "holders": ["jellyfin", "web"]},
    ]


def test_override_bumps_oldest():
    a, b = T(0, 97.1, 5), T(1, 101.9, 2)
    assert pool(a, b).choose(99.5, "web", {0: {"plex"}, 1: {"jellyfin"}}, True) is b


def test_override_bumps_requesters_own_when_oldest():
    a, b = T(0, 97.1, 1), T(1, 101.9, 2)
    assert pool(a, b).choose(99.5, "web", {0: {"web", "plex"}, 1: {"jellyfin"}}, True) is a


def test_len():
    assert len(pool(T(0), T(1), T(2))) == 3


def test_reserved_tuner_is_only_taken_by_override():
    a, b = T(0, 97.1, 1), T(1, 101.9, 2)  # tuner 0 is scanning, currently sitting on 97.1
    p = pool(a, b)
    assert p.choose(97.1, "web", {}, False, reserved={0}) is b
    with pytest.raises(NoTunerFree) as e:
        p.choose(99.5, "web", {1: {"jellyfin"}}, False, reserved={0})
    assert e.value.busy[0] == {"tuner": 0, "frequency": 97.1, "holders": [], "reserved": True}
    assert p.choose(99.5, "web", {1: {"jellyfin"}}, True, reserved={0}) is a


def test_shared_identity_prefers_an_idle_tuner_over_its_own():
    a, b = T(0, 97.1, 1), T(1)
    p = pool(a, b)
    assert p.choose(101.9, "plex", {0: {"plex"}}, False, prefer_idle=True) is b
    assert pool(a).choose(101.9, "plex", {0: {"plex"}}, False, prefer_idle=True) is a  # one tuner: retune it
