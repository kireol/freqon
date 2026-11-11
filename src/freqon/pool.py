"""Which tuner serves a request.

A tuner on one frequency serves every HD program of that station, so the choice is only
about frequency. In order:
    1. a tuner already on that frequency (joining never conflicts);
    2. the requester's own tuner, if nobody else is listening to it;
    3. an idle tuner (no listeners), preferring one that is switched off;
    4. with override, the tuner tuned longest ago, even a scanning one (its listeners get bumped);
    5. otherwise NoTunerFree, describing what every tuner is doing and for whom.
"""

from __future__ import annotations


class NoTunerFree(Exception):
    def __init__(self, busy: list[dict]):
        super().__init__("no tuner free")
        self.busy = busy


class TunerPool:
    def __init__(self, radios: list):
        self.radios = radios

    def __len__(self) -> int:
        return len(self.radios)

    def __iter__(self):
        return iter(self.radios)

    def choose(self, frequency: float, client_id: str, holders: dict[int, set[str]], override: bool,
               reserved: set[int] = frozenset(), prefer_idle: bool = False):
        """`holders` maps tuner index -> client ids listening to it. `reserved` tuners (scanning)
        are only ever taken by an override. `prefer_idle` swaps rules 2 and 3, for identities
        shared by several viewers (Plex), whose "own" tuner may be another viewer's."""
        free = [r for r in self.radios if r.index not in reserved]
        for r in free:
            if r.state.running and r.state.frequency == frequency:
                return r
        own = [r for r in free if holders.get(r.index) == {client_id}]
        idle = [r for r in free if not holders.get(r.index)]
        if idle and (prefer_idle or not own):
            return min(idle, key=lambda r: (r.state.running, r.tuned_at))
        if own:
            return own[0]
        if override:
            return min(self.radios, key=lambda r: r.tuned_at)
        busy = []
        for r in self.radios:
            entry = {"tuner": r.index, "frequency": r.state.frequency, "holders": sorted(holders.get(r.index, ()))}
            if r.index in reserved:
                entry["reserved"] = True
            busy.append(entry)
        raise NoTunerFree(busy)
