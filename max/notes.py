"""
Meeting Note Taker — captures everything said during standup.

This processor sits in the pipeline and records all transcribed speech,
then compiles structured meeting notes after the meeting ends.
"""

from datetime import datetime
from typing import List, Optional

from loguru import logger

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class TranscriptEntry:
    """A single utterance in the meeting."""

    def __init__(self, speaker: str, text: str, timestamp: str):
        self.speaker = speaker
        self.text = text
        self.timestamp = timestamp

    def __repr__(self):
        return f"[{self.timestamp}] {self.speaker}: {self.text}"


class MeetingNoteTaker(FrameProcessor):
    """Pipeline processor that captures meeting transcription for note-taking.

    Sits in the pipeline between STT and the LLM context aggregator,
    passively recording everything that's said without modifying the frames.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.transcript: List[TranscriptEntry] = []
        self.events: List[str] = []
        self.meeting_start: Optional[str] = None
        self.meeting_end: Optional[str] = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Pass through all frames while capturing transcription data."""
        # Record the meeting start time on first frame
        if not self.meeting_start:
            self.meeting_start = datetime.now().strftime("%Y-%m-%d %H:%M")

        # Capture transcription frames (speech-to-text results)
        if isinstance(frame, TranscriptionFrame):
            entry = TranscriptEntry(
                speaker=getattr(frame, "user_id", "Unknown"),
                text=frame.text if hasattr(frame, "text") else str(frame),
                timestamp=datetime.now().strftime("%H:%M:%S"),
            )
            self.transcript.append(entry)
            logger.debug(f"📝 {entry}")

        # Always pass the frame through — we're just observing
        await self.push_frame(frame, direction)

    def add_event(self, event: str):
        """Record a meeting event (join/leave/etc)."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.events.append(f"[{timestamp}] {event}")

    def compile_notes(self) -> str:
        """Generate structured meeting notes from the transcript."""
        self.meeting_end = datetime.now().strftime("%Y-%m-%d %H:%M")

        if not self.transcript and not self.events:
            return ""

        notes = []
        notes.append("# Daily Standup Notes")
        notes.append(f"**Date:** {self.meeting_start or 'Unknown'}")
        notes.append(f"**Duration:** {self.meeting_start} — {self.meeting_end}")
        notes.append(f"**Note taker:** Max (AI)")
        notes.append("")

        # Participants
        if self.events:
            notes.append("## Participants")
            for event in self.events:
                notes.append(f"- {event}")
            notes.append("")

        # Full transcript
        if self.transcript:
            notes.append("## Transcript")
            for entry in self.transcript:
                notes.append(f"**[{entry.timestamp}] {entry.speaker}:** {entry.text}")
            notes.append("")

        # Summary placeholder
        notes.append("## Summary")
        notes.append("*(To be generated after meeting ends)*")
        notes.append("")

        # Action items placeholder
        notes.append("## Action Items")
        notes.append("*(To be extracted from transcript)*")

        return "\n".join(notes)
