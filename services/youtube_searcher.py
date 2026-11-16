import asyncio
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any
import yt_dlp

from services.gemini_extractor import CaptionAnalysisResult

logger = logging.getLogger(__name__)


@dataclass
class YouTubeCandidate:
    """Data model for a candidate YouTube video across Phase 1 and Phase 2."""

    video_id: str
    title: str
    channel: str
    url: str
    duration_seconds: int | None = None
    view_count: int | None = None
    thumbnail_url: str = ""

    # Phase 1 & Phase 2 scores
    metadata_score: float = 0.0
    audio_match_score: float | None = None
    confidence_score: float = 0.0

    # Phase 2 transcript matching & timestamp data
    matched_timestamp_seconds: int | None = None
    matched_transcript_snippet: str | None = None
    match_reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.thumbnail_url and self.video_id:
            self.thumbnail_url = f"https://i.ytimg.com/vi/{self.video_id}/hqdefault.jpg"

    @property
    def formatted_duration(self) -> str:
        """Format total video duration as MM:SS or HH:MM:SS."""
        if not self.duration_seconds or self.duration_seconds <= 0:
            return "نامشخص"
        return self._format_seconds(self.duration_seconds)

    @property
    def formatted_timestamp(self) -> str | None:
        """Format matched clip start second as MM:SS or HH:MM:SS."""
        if self.matched_timestamp_seconds is None:
            return None
        return self._format_seconds(self.matched_timestamp_seconds)

    @property
    def deep_link_url(self) -> str:
        """Direct YouTube URL with exact start timestamp if discovered."""
        if self.matched_timestamp_seconds is not None and self.matched_timestamp_seconds > 0:
            return f"https://youtu.be/{self.video_id}?t={self.matched_timestamp_seconds}"
        return f"https://youtu.be/{self.video_id}"

    @property
    def confidence_percent(self) -> int:
        """Final confidence percentage clamped between 0 and 99."""
        return min(99, max(0, int(round(self.confidence_score * 100))))

    @property
    def audio_match_percent(self) -> int | None:
        """Audio transcript match percentage."""
        if self.audio_match_score is None:
            return None
        return min(99, max(0, int(round(self.audio_match_score * 100))))

    @staticmethod
    def _format_seconds(total_seconds: int) -> str:
        hours, remainder = divmod(int(total_seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


class YouTubeSearcher:
    """
    YouTube search module using yt-dlp in extract_flat mode (no proxy)
    with heuristic metadata scoring (Tier 1).
    """

    def __init__(self, results_per_query: int = 5) -> None:
        self.results_per_query = results_per_query
        self._ydl_opts: dict[str, Any] = {
            "extract_flat": "in_playlist",
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
        }

    def _search_sync(self, query: str) -> list[dict[str, Any]]:
        """Run flat YouTube search synchronously inside a worker thread."""
        search_target = f"ytsearch{self.results_per_query}:{query}"
        try:
            with yt_dlp.YoutubeDL(self._ydl_opts) as ydl:
                info = ydl.extract_info(search_target, download=False)
                if not info or "entries" not in info:
                    return []
                return [entry for entry in info["entries"] if entry is not None]
        except Exception as e:
            logger.error("yt-dlp search failed for query '%s': %s", query, e)
            return []

    async def find_best_matches(
        self, analysis: CaptionAnalysisResult, top_k: int = 4
    ) -> list[YouTubeCandidate]:
        """
        Execute search queries in parallel, deduplicate candidates, score metadata,
        and return the top K candidates.
        """
        queries = [q.strip() for q in analysis.search_queries if q.strip()][:3]
        if not queries:
            return []

        tasks = [asyncio.to_thread(self._search_sync, q) for q in queries]
        raw_results_lists = await asyncio.gather(*tasks, return_exceptions=True)

        candidates_map: dict[str, YouTubeCandidate] = {}

        for result_list in raw_results_lists:
            if isinstance(result_list, Exception) or not isinstance(result_list, list):
                continue

            for entry in result_list:
                video_id = entry.get("id")
                title = entry.get("title")
                if not video_id or not title or video_id in candidates_map:
                    continue

                raw_url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
                if not raw_url.startswith("http"):
                    raw_url = f"https://www.youtube.com/watch?v={video_id}"

                duration = entry.get("duration")
                duration_sec = int(duration) if duration is not None else None

                channel_name = (
                    entry.get("channel")
                    or entry.get("uploader")
                    or "Unknown Channel"
                )

                thumbnails = entry.get("thumbnails")
                thumb_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                if isinstance(thumbnails, list) and thumbnails:
                    last_thumb = thumbnails[-1].get("url")
                    if last_thumb:
                        thumb_url = str(last_thumb)

                candidate = YouTubeCandidate(
                    video_id=video_id,
                    title=str(title),
                    channel=str(channel_name),
                    url=raw_url,
                    duration_seconds=duration_sec,
                    view_count=entry.get("view_count"),
                    thumbnail_url=thumb_url,
                )

                self._score_candidate_metadata(candidate, analysis)
                candidates_map[video_id] = candidate

        sorted_candidates = sorted(
            candidates_map.values(),
            key=lambda c: c.confidence_score,
            reverse=True,
        )

        return sorted_candidates[:top_k]

    def _score_candidate_metadata(
        self, candidate: YouTubeCandidate, analysis: CaptionAnalysisResult
    ) -> None:
        """Compute initial Tier 1 metadata score based on entity overlap and duration proximity."""
        full_target_text = f"{candidate.title} {candidate.channel}".lower()
        target_tokens = set(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", full_target_text))

        entity_score = 0.0
        matched_entities: list[str] = []

        if analysis.exact_entities:
            for entity in analysis.exact_entities:
                ent_lower = entity.lower().strip()
                if not ent_lower:
                    continue

                ent_tokens = set(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", ent_lower))
                if ent_lower in full_target_text or (
                    ent_tokens and ent_tokens.issubset(target_tokens)
                ):
                    matched_entities.append(entity)
                else:
                    ratio = (
                        SequenceMatcher(None, ent_lower, full_target_text)
                        .find_longest_match(0, len(ent_lower), 0, len(full_target_text))
                        .size
                        / max(len(ent_lower), 1)
                    )
                    if ratio >= 0.85:
                        matched_entities.append(entity)

            entity_score = len(matched_entities) / len(analysis.exact_entities)
            if matched_entities:
                candidate.match_reasons.append(
                    f"کلمات کلیدی: {', '.join(matched_entities[:4])}"
                )

        best_query_score = 0.0
        for query in analysis.search_queries:
            q_tokens = set(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", query.lower()))
            if not q_tokens:
                continue
            overlap = len(q_tokens.intersection(target_tokens)) / len(q_tokens)
            best_query_score = max(best_query_score, overlap)

        text_relevance_score = (0.60 * entity_score) + (0.40 * best_query_score)

        target_dur = analysis.mentioned_duration_seconds
        cand_dur = candidate.duration_seconds

        if target_dur is not None and target_dur > 0 and cand_dur is not None and cand_dur > 0:
            diff_seconds = abs(cand_dur - target_dur)
            diff_ratio = diff_seconds / target_dur

            if diff_ratio <= 0.08 or diff_seconds <= 35:
                duration_score = 1.0
                candidate.match_reasons.append(
                    f"تطابق دقیق زمانی ({candidate.formatted_duration})"
                )
            elif diff_ratio <= 0.20:
                duration_score = 0.75
                candidate.match_reasons.append(
                    f"زمان نزدیک ({candidate.formatted_duration})"
                )
            elif diff_ratio <= 0.35:
                duration_score = 0.40
            else:
                duration_score = 0.0

            final_score = (0.65 * text_relevance_score) + (0.35 * duration_score)

            if target_dur >= 180 and cand_dur < 90:
                final_score *= 0.3
        else:
            final_score = text_relevance_score * 0.88

        clamped = round(min(0.95, max(0.0, final_score)), 4)
        candidate.metadata_score = clamped
        candidate.confidence_score = clamped
