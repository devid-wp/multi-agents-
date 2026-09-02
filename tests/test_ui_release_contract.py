from pathlib import Path


def _read(relative: str) -> str:
    return (Path(__file__).parents[1] / relative).read_text(encoding="utf-8")


def test_ui_contains_minimum_room_controls() -> None:
    html = _read("ui/index.html")
    js = _read("ui/static/app.js")
    assert 'id="room-select"' in html
    assert 'id="btn-new-room"' in html
    assert "async function loadRooms()" in js
    assert "async function createRoom()" in js
    assert "room_id: state.roomId" in js


def test_history_and_terminal_error_states_are_visible() -> None:
    js = _read("ui/static/app.js")
    sse = _read("ui/static/modules/sse.js")
    assert "data.messages.forEach" in js
    assert "Request timed out after 3 minutes" in (js + sse)
    assert "setRunningUI(false)" in js
    assert "refreshChanges()" in js
