import json

from src.api.assemblyai_streaming import AssemblyAIStreamingSession, TurnTranscriptAssembler


def test_multi_turn_transcript_is_joined_in_order():
    assembler = TurnTranscriptAssembler()
    assembler.handle_turn({"turn_order": 0, "transcript": "how do i", "end_of_turn": False})
    assert assembler.handle_turn({"turn_order": 0, "transcript": "how do i", "end_of_turn": True}) is True
    assembler.handle_turn({"turn_order": 0, "transcript": "How do I?", "end_of_turn": True, "turn_is_formatted": True})
    assembler.handle_turn({"turn_order": 1, "transcript": "rename a", "end_of_turn": False})
    assert assembler.full_transcript() == "How do I? rename a"
    assembler.handle_turn({"turn_order": 1, "transcript": "Rename a file.", "end_of_turn": True, "turn_is_formatted": True})
    assert assembler.full_transcript() == "How do I? Rename a file."


def test_unformatted_copy_never_replaces_formatted():
    assembler = TurnTranscriptAssembler()
    assembler.handle_turn({"turn_order": 0, "transcript": "Hello there.", "end_of_turn": True, "turn_is_formatted": True})
    assembler.handle_turn({"turn_order": 0, "transcript": "hello there", "end_of_turn": True})
    assert assembler.full_transcript() == "Hello there."


class _FakeProxy:
    def transcription_token(self) -> str:
        return "tok"


def test_session_url_and_message_handling():
    partials = []
    session = AssemblyAIStreamingSession(_FakeProxy(), speech_model="universal-streaming-english",
                                         on_partial=partials.append)
    url = session.build_url("tok")
    assert url.startswith("wss://streaming.assemblyai.com/v3/ws?")
    for expected in ("sample_rate=16000", "encoding=pcm_s16le", "format_turns=true",
                     "speech_model=universal-streaming-english", "token=tok"):
        assert expected in url
    session._on_message(None, json.dumps({"type": "Begin", "id": "x"}))
    session._on_message(None, json.dumps({"type": "Turn", "turn_order": 0, "transcript": "open", "end_of_turn": False}))
    session._on_message(None, json.dumps({"type": "Turn", "turn_order": 0, "transcript": "Open settings.",
                                          "end_of_turn": True, "turn_is_formatted": True}))
    assert partials[-1] == "Open settings."
    # stop() without a live socket must still return the assembled text.
    assert session.stop(final_wait_seconds=0.05) == "Open settings."
