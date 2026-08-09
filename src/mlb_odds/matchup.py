"""Shared head-to-head team lens (D-034) — one implementation for both web
apps (the baseball/odds app and the contest app cannot share endpoints across
origins, but they can share this).

Live ESPN season stats, curated by MATCHUP_STATS with per-stat direction so
no caller can highlight ERA the wrong way. In-process TTL caches keep a busy
page to a few ESPN calls per hour."""

from __future__ import annotations

from time import monotonic

from mlb_odds.providers.espn import ESPN, MATCHUP_STATS

_LENS_TTL = 3600.0
_IDS_TTL = 86400.0
_lens_cache: dict[tuple[str, str], tuple[float, dict[str, object]]] = {}
_ids_cache: dict[str, tuple[float, dict[str, str]]] = {}


def team_ids(espn: ESPN, sport: str) -> dict[str, str]:
    hit = _ids_cache.get(sport)
    if hit and monotonic() - hit[0] < _IDS_TTL:
        return hit[1]
    ids = espn.fetch_team_ids()
    _ids_cache[sport] = (monotonic(), ids)
    return ids


def team_lens(espn: ESPN, sport: str, code: str, team_id: str) -> dict[str, object]:
    key = (sport, code)
    hit = _lens_cache.get(key)
    if hit and monotonic() - hit[0] < _LENS_TTL:
        return hit[1]
    stats = espn.fetch_team_statistics(team_id)
    profile = espn.fetch_team_profile(team_id)
    rows: dict[str, dict[str, object]] = {}
    for category, name, label, higher in MATCHUP_STATS[sport]:
        entry = stats.get((category, name))
        rows[label] = {
            "value": entry.get("value") if entry else None,
            "display": entry.get("display") if entry else None,
            "higher_is_better": higher,
        }
    lens: dict[str, object] = {
        "record": profile.get("record"),
        "standing": profile.get("standing"),
        "stats": rows,
    }
    _lens_cache[key] = (monotonic(), lens)
    return lens


def matchup_payload(
    espn: ESPN, sport: str, away_code: str, home_code: str
) -> dict[str, object] | None:
    """The full comparison payload, or None when a team id is unavailable.
    Raises ProviderError on ESPN failures (uncached paths only)."""
    ids = team_ids(espn, sport)
    away_id, home_id = ids.get(away_code), ids.get(home_code)
    if away_id is None or home_id is None:
        return None
    away = team_lens(espn, sport, away_code, away_id)
    home = team_lens(espn, sport, home_code, home_id)
    away_stats = away["stats"] if isinstance(away["stats"], dict) else {}
    home_stats = home["stats"] if isinstance(home["stats"], dict) else {}
    rows = []
    for _cat, _name, label, higher in MATCHUP_STATS[sport]:
        a = away_stats.get(label, {})
        h = home_stats.get(label, {})
        better = None
        av, hv = a.get("value"), h.get("value")
        if isinstance(av, int | float) and isinstance(hv, int | float) and av != hv:
            better = (
                ("away" if av > hv else "home")
                if higher
                else ("away" if av < hv else "home")
            )
        rows.append(
            {"label": label, "away": a.get("display"), "home": h.get("display"),
             "better": better}
        )
    return {
        "away_team": away_code,
        "home_team": home_code,
        "away_record": away["record"],
        "home_record": home["record"],
        "away_standing": away["standing"],
        "home_standing": home["standing"],
        "rows": rows,
    }
