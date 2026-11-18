"""Gradio chat web UI for the scheduling agent.

Each browser session gets its own SchedulingAgent (kept in gr.State), so the
conversation history and any pending clarification stay per user.

Run:  .venv/bin/python app/web.py      -> http://127.0.0.1:7860
"""

from __future__ import annotations

import os
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from agent import SchedulingAgent  # noqa: E402  (needs .env loaded first)

EXAMPLES = [
    "book a 30-min intro with a candidate Thursday afternoon",
    "what's on my calendar tomorrow?",
    "move my 3pm to later today",
]


async def chat(message: str, history: list, agent: SchedulingAgent | None):
    """One chat turn: reuse this session's agent (create it on the first message)."""
    if agent is None:
        agent = SchedulingAgent()
    reply = await agent.respond(message)
    return reply, agent


def build_ui() -> gr.ChatInterface:
    agent_state = gr.State(None)
    return gr.ChatInterface(
        fn=chat,
        additional_inputs=[agent_state],
        additional_outputs=[agent_state],
        chatbot=gr.Chatbot(height="70vh", placeholder="Book, show, reschedule or cancel your Cal.com meetings."),
        textbox=gr.Textbox(placeholder="e.g. book a 30-min intro with Steven tomorrow at 2pm", autofocus=True),
        examples=[[e, None] for e in EXAMPLES],
        title="Scheduling Assistant",
        description=f"Manages your Cal.com calendar. Times are in {os.environ.get('DEFAULT_TIMEZONE', 'UTC')}.",
        fill_height=True,
        flagging_mode="never",
    )


if __name__ == "__main__":
    build_ui().launch(
        server_name=os.environ.get("WEB_HOST", "127.0.0.1"),
        server_port=int(os.environ.get("WEB_PORT", "7860")),
        theme=gr.themes.Soft(),
    )
