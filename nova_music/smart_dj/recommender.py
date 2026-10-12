"""Transparent, local-only Smart DJ scoring and playlist generation."""
from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import datetime, timezone


GENRE_GROUPS = {
    "pop": "Pop", "dance pop": "Pop", "synthpop": "Pop", "indie pop": "Pop",
    "rap": "Rap/Hip-Hop", "hip hop": "Rap/Hip-Hop", "hip-hop": "Rap/Hip-Hop", "trap": "Rap/Hip-Hop",
    "rock": "Rock", "alternative rock": "Rock", "indie rock": "Rock", "punk": "Rock",
    "electronic": "Electronic", "edm": "Electronic", "house": "Electronic", "techno": "Electronic", "trance": "Electronic", "ambient": "Electronic",
    "classical": "Classical", "orchestral": "Classical", "opera": "Classical",
    "traditional": "Traditional", "folk": "Traditional", "world": "Traditional", "persian": "Traditional",
    "jazz": "Jazz", "blues": "Jazz",
    "metal": "Metal", "heavy metal": "Metal", "death metal": "Metal",
    "country": "Country",
}

MODES = ("Favorites", "Balanced", "Discovery")
MODE_WEIGHTS = {
    "Favorites": (0.72, 0.22, 0.06),
    "Balanced": (0.48, 0.34, 0.18),
    "Discovery": (0.24, 0.36, 0.40),
}


def normalize_genre(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    text = value.casefold().strip()
    for alias, group in GENRE_GROUPS.items():
        if text == alias or alias in text:
            return group
    return "Other"


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _age_days(stamp: str | None, now: datetime) -> float:
    if not stamp:
        return 3650.0
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds() / 86400)
    except (ValueError, TypeError):
        return 3650.0


class SmartDJRecommender:
    """Generate a varied playlist from local library tags and listening sessions."""

    HALF_LIFE_DAYS = 90.0
    RECENT_PENALTY_HOURS = 72.0

    def __init__(self, conn, rng: random.Random | None = None):
        self.conn = conn
        self.rng = rng or random.Random()

    def build_playlist(self, mode: str = "Balanced", diversity: int = 50,
                       limit: int = 50, exclude_paths=()) -> list[dict]:
        mode = mode if mode in MODE_WEIGHTS else "Balanced"
        diversity = int(_clamp(int(diversity) / 100, 0, 1) * 100)
        tracks = self.conn.execute(
            "SELECT path,title,artist,album,duration,favorite,genre FROM tracks ORDER BY artist,title"
        ).fetchall()
        tracks = [t for t in tracks if t[0] and __import__("os").path.isfile(t[0])]
        if not tracks:
            return []
        rows = self.conn.execute(
            "SELECT track_path,started_at,ended_at,listened_ms,duration_ms,completion_ratio,outcome,manual_selection "
            "FROM listening_sessions"
        ).fetchall()
        now = datetime.now(timezone.utc)
        stats = defaultdict(lambda: {"plays": 0.0, "complete": 0.0, "skip": 0.0,
                                     "engagement": 0.0, "manual": 0.0, "last": None})
        artist_affinity = defaultdict(float)
        genre_affinity = defaultdict(float)
        album_affinity = defaultdict(float)
        lookup = {t[0]: t for t in tracks}
        for path, started, ended, listened, duration, ratio, outcome, manual in rows:
            weight = math.exp(-math.log(2) * _age_days(started, now) / self.HALF_LIFE_DAYS)
            s = stats[path]
            s["plays"] += weight
            s["complete"] += weight if outcome == "completed" else 0
            s["skip"] += weight if outcome == "skipped" else 0
            s["engagement"] += weight * _clamp(float(ratio or 0))
            s["manual"] += weight if manual else 0
            if not s["last"] or (started and started > s["last"]):
                s["last"] = started
            track = lookup.get(path)
            if track:
                artist_affinity[track[2]] += weight * _clamp(float(ratio or 0))
                group = normalize_genre(track[6])
                if group:
                    genre_affinity[group] += weight * _clamp(float(ratio or 0))
                album_affinity[track[3]] += weight * _clamp(float(ratio or 0))

        max_plays = max((s["plays"] for path, s in stats.items() if path in lookup), default=0.0)
        max_artist = max(artist_affinity.values(), default=0.0)
        max_genre = max(genre_affinity.values(), default=0.0)
        max_album = max(album_affinity.values(), default=0.0)
        excluded = set(exclude_paths)
        candidates = []
        for track in tracks:
            path, title, artist, album, _, favorite, raw_genre = track
            s = stats[path]
            play_signal = math.log1p(s["plays"]) / math.log1p(max_plays) if max_plays else 0.0
            engagement = s["engagement"] / s["plays"] if s["plays"] else 0.0
            complete_rate = s["complete"] / s["plays"] if s["plays"] else 0.0
            skip_rate = s["skip"] / s["plays"] if s["plays"] else 0.0
            manual_rate = s["manual"] / s["plays"] if s["plays"] else 0.0
            favorite_signal = 1.0 if favorite else 0.0
            popularity = _clamp(0.45 * engagement + 0.25 * complete_rate +
                                0.18 * play_signal + 0.12 * manual_rate +
                                0.30 * favorite_signal - 0.28 * skip_rate)
            group = normalize_genre(raw_genre)
            artist_score = artist_affinity[artist] / max_artist if max_artist and artist else 0.0
            genre_score = genre_affinity[group] / max_genre if max_genre and group else 0.0
            album_score = album_affinity[album] / max_album if max_album and album else 0.0
            similarity = _clamp(0.52 * artist_score + 0.35 * genre_score + 0.13 * album_score)
            discovery = _clamp((1 - play_signal) * 0.58 + similarity * 0.42)
            score = 100 * (MODE_WEIGHTS[mode][0] * popularity +
                           MODE_WEIGHTS[mode][1] * similarity +
                           MODE_WEIGHTS[mode][2] * discovery)
            age_hours = _age_days(s["last"], now) * 24
            if s["last"]:
                score *= 0.58 + 0.42 * (1 - math.exp(-age_hours / self.RECENT_PENALTY_HOURS))
            if path in excluded:
                continue
            if not s["plays"] and not favorite:
                score = max(score, 8 + 12 * diversity / 100)
            reason = self._reason(favorite_signal, s["plays"], artist_score, genre_score, similarity)
            candidates.append({
                "path": path, "title": title, "artist": artist, "album": album,
                "genre": group, "score": max(0.1, score), "reason": reason,
            })

        if not candidates:
            return []
        desired = min(max(1, int(limit)), len(candidates))
        selected = []
        used = set()
        artist_run, album_run, genre_run = defaultdict(int), defaultdict(int), defaultdict(int)
        diversity_factor = diversity / 100
        while len(selected) < desired:
            weighted = []
            for item in candidates:
                if item["path"] in used:
                    continue
                penalty = 1.0
                if item["artist"]:
                    penalty *= max(0.18, 1 - 0.66 * diversity_factor * artist_run[item["artist"]])
                if item["album"]:
                    penalty *= max(0.22, 1 - 0.52 * diversity_factor * album_run[item["album"]])
                if item["genre"]:
                    penalty *= max(0.4, 1 - 0.20 * diversity_factor * genre_run[item["genre"]])
                weighted.append((item, max(0.001, item["score"] * penalty)))
            chosen = self.rng.choices([x[0] for x in weighted], weights=[x[1] for x in weighted], k=1)[0]
            selected.append(chosen)
            used.add(chosen["path"])
            if chosen["artist"]: artist_run[chosen["artist"]] += 1
            if chosen["album"]: album_run[chosen["album"]] += 1
            if chosen["genre"]: genre_run[chosen["genre"]] += 1
        return selected

    @staticmethod
    def _reason(favorite: float, plays: float, artist: float, genre: float, similarity: float) -> str:
        if favorite:
            return "به‌دلیل علاقه‌مندی شما به این آهنگ"
        if plays >= 2:
            return "به‌دلیل شنیدن و پسندیدن این آهنگ"
        if artist >= 0.7:
            return "به‌دلیل علاقه شما به این خواننده"
        if genre >= 0.65:
            return "متناسب با سبک‌های محبوب شما"
        if similarity > 0.15:
            return "هم‌خوان با خواننده یا سبک موردعلاقه شما"
        return "برای افزایش تنوع و کشف موسیقی جدید"

    def profile(self) -> dict:
        tracks = self.conn.execute("SELECT path,artist,album,genre,favorite FROM tracks").fetchall()
        stats = self.conn.execute(
            "SELECT track_path,COUNT(*),SUM(outcome='completed'),SUM(outcome='skipped'),"
            "SUM(listened_ms),MAX(started_at),SUM(manual_selection) "
            "FROM listening_sessions GROUP BY track_path"
        ).fetchall()
        by_path = {row[0]: row for row in stats}
        genre_scores = defaultdict(float)
        artist_scores = defaultdict(float)
        album_scores = defaultdict(float)
        for path, artist, album, raw_genre, favorite in tracks:
            row = by_path.get(path)
            score = (int(row[2] or 0) * 1.0 + int(row[1] or 0) * 0.25 +
                     int(row[6] or 0) * 0.3 - int(row[3] or 0) * 0.35) if row else 0.0
            if favorite:
                score += 0.6
            if score > 0 and artist:
                artist_scores[artist] += score
            if score > 0 and album:
                album_scores[album] += score
            group = normalize_genre(raw_genre)
            if score > 0 and group:
                genre_scores[group] += score
        missing_genre_count = sum(1 for row in tracks if not normalize_genre(row[3]))
        event_count = self.conn.execute("SELECT COUNT(*) FROM listening_events").fetchone()[0]
        analyzed = sum(1 for row in stats if row[1])
        top_genres = sorted(genre_scores.items(), key=lambda x: (-x[1], x[0]))[:5]
        top_artists = sorted(artist_scores.items(), key=lambda x: (-x[1], x[0]))[:5]
        top_albums = sorted(album_scores.items(), key=lambda x: (-x[1], x[0]))[:5]
        total_genre_score = sum(genre_scores.values())
        genre_shares = [(name, score / total_genre_score * 100) for name, score in top_genres] if total_genre_score else []
        return {"track_count": len(tracks), "analyzed_count": analyzed,
                "event_count": event_count, "missing_genre_count": missing_genre_count,
                "genres": top_genres, "genre_shares": genre_shares,
                "artists": top_artists, "albums": top_albums}

    def history(self, limit: int = 100) -> list[tuple]:
        return self.conn.execute(
            "SELECT e.created_at,e.event_type,e.track_path,COALESCE(t.title,e.track_path),"
            "e.listened_ms,e.completion_ratio FROM listening_events e "
            "LEFT JOIN tracks t ON t.path=e.track_path ORDER BY e.id DESC LIMIT ?", (int(limit),)
        ).fetchall()

    def top_tracks(self, skipped: bool = False, limit: int = 10) -> list[tuple]:
        query = ("SELECT t.path,t.title,t.artist,COUNT(*) AS n FROM listening_sessions s "
                 "JOIN tracks t ON t.path=s.track_path WHERE s.outcome=? "
                 "GROUP BY t.path,t.title,t.artist ORDER BY n DESC,t.title LIMIT ?")
        return self.conn.execute(query, ("skipped" if skipped else "completed", int(limit))).fetchall()
