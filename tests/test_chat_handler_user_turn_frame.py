"""The user turn as the agent broadcasts it: where it came from and which
frame it was, so a web tab can tell its own echo from another surface's turn."""

from __future__ import annotations

import types

from captain_claw.web.chat_handler import turn_via, user_turn_frame


def test_via_is_the_frames_surface_else_whatsapp_else_web():
    ws = types.SimpleNamespace()
    assert turn_via("device", ws) == "device"
    assert turn_via(" Glasses ", ws) == "glasses"
    assert turn_via(None, ws, whatsapp_waid="385911234567") == "whatsapp"
    assert turn_via(None, ws, origin={"kind": "whatsapp"}) == "whatsapp"
    assert turn_via(None, ws) == "web"
    # A bridge's socket remembers its surface for frames routed without one.
    remembered = types.SimpleNamespace(_claw_surface="device")
    assert turn_via(None, remembered) == "device"


def test_frame_names_the_surface_and_the_accepted_client_message():
    frame = user_turn_frame("hello", "device", "dev_abc123")
    assert frame["type"] == "chat_message" and frame["role"] == "user"
    assert frame["content"] == "hello"
    assert frame["via"] == "device"
    assert frame["client_msg_id"] == "dev_abc123"
    assert frame["timestamp"]


def test_frame_without_a_client_id_carries_none():
    frame = user_turn_frame("hello", "web")
    assert frame["via"] == "web"
    assert "client_msg_id" not in frame
