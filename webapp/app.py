"""Streamlit entrypoint: Cognito login gate, then a chat homepage backed by
the chat_agent AgentCore Runtime."""

from __future__ import annotations

import uuid
from datetime import datetime

import streamlit as st

import agent_client
import auth
from config import load_config

st.set_page_config(page_title="AgentCore Chat", page_icon="\U0001f4ac")


def _init_state() -> None:
    defaults = {
        "authenticated": False,
        "tokens": None,
        "username": None,
        "messages": [],  # [{"role": "user"|"assistant", "text": str}]
        "actor_id": None,  # stable per Cognito user; set on login
        "conversation_id": None,  # id of the currently open chat
        "session_id": None,  # actor_id + conversation_id; the AgentCore session for the open chat
        "view": "home",  # "home" | "chat"
        "pending_challenge": None,  # (username, session) while NEW_PASSWORD_REQUIRED
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _logout() -> None:
    for key in (
        "authenticated",
        "tokens",
        "username",
        "actor_id",
        "conversation_id",
        "session_id",
        "messages",
        "view",
        "pending_challenge",
    ):
        st.session_state.pop(key, None)


def _render_login(config) -> None:
    st.title("Sign in")

    if st.session_state.pending_challenge is not None:
        username, session = st.session_state.pending_challenge
        st.info(f"Set a new password for {username} to finish signing in.")
        with st.form("new_password_form"):
            new_password = st.text_input("New password", type="password")
            confirm_password = st.text_input("Confirm new password", type="password")
            submitted = st.form_submit_button("Set password and sign in")
        if submitted:
            if new_password != confirm_password:
                st.error("Passwords don't match.")
            else:
                try:
                    tokens = auth.respond_new_password(config, username, new_password, session)
                except auth.AuthError as err:
                    st.error(str(err))
                else:
                    st.session_state.tokens = tokens
                    st.session_state.username = username
                    st.session_state.actor_id = agent_client.actor_id_for_user(tokens.sub)
                    st.session_state.authenticated = True
                    st.session_state.pending_challenge = None
                    st.rerun()
        return

    with st.form("login_form"):
        username = st.text_input("Username (email)")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")

    if submitted:
        try:
            tokens = auth.login(config, username, password)
        except auth.NewPasswordRequired as challenge:
            st.session_state.pending_challenge = (username, challenge.session)
            st.rerun()
        except auth.AuthError as err:
            st.error(str(err))
        else:
            st.session_state.tokens = tokens
            st.session_state.username = username
            st.session_state.actor_id = agent_client.actor_id_for_user(tokens.sub)
            st.session_state.authenticated = True
            st.rerun()


def _ensure_fresh_tokens(config) -> bool:
    """Refresh the access token if expired. Returns False if the user needs to re-login."""
    tokens = st.session_state.tokens
    if tokens is None:
        return False
    if not tokens.expired:
        return True
    try:
        st.session_state.tokens = auth.refresh(config, tokens.refresh_token)
        return True
    except auth.AuthError:
        _logout()
        return False


def _history_for_api() -> list[dict]:
    return [
        {"role": m["role"], "content": agent_client.to_content_blocks(m["text"])}
        for m in st.session_state.messages
    ]


def _format_timestamp(iso_str: str | None) -> str:
    if not iso_str:
        return ""
    try:
        return datetime.fromisoformat(iso_str).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso_str


def _render_home(config) -> None:
    with st.sidebar:
        st.write(f"Signed in as **{st.session_state.username}**")
        if st.button("Log out"):
            _logout()
            st.rerun()

    st.title("Chats")

    if st.button("+ New chat"):
        st.session_state.conversation_id = uuid.uuid4().hex
        st.session_state.session_id = agent_client.session_id_for_conversation(
            st.session_state.actor_id, st.session_state.conversation_id
        )
        st.session_state.messages = []
        st.session_state.view = "chat"
        st.rerun()

    if not _ensure_fresh_tokens(config):
        st.rerun()
        return

    try:
        chats = agent_client.list_chats(config, st.session_state.actor_id, st.session_state.tokens.access_token)
    except agent_client.AgentInvocationError as err:
        if err.status_code in (401, 403):
            st.error("Your session has expired. Please log in again.")
            _logout()
            st.rerun()
            return
        st.error(str(err))
        return

    if not chats:
        st.caption("No past chats yet -- start one above.")
        return

    st.subheader("Past chats")
    for chat in chats:
        label = f"{chat['title']} — {_format_timestamp(chat.get('created_at'))}"
        if st.button(label, key=f"chat-{chat['session_id']}", use_container_width=True):
            st.session_state.conversation_id = chat["session_id"]
            st.session_state.session_id = chat["session_id"]
            try:
                st.session_state.messages = agent_client.get_messages(
                    config,
                    st.session_state.actor_id,
                    chat["session_id"],
                    st.session_state.tokens.access_token,
                )
            except agent_client.AgentInvocationError as err:
                st.error(str(err))
                return
            st.session_state.view = "chat"
            st.rerun()


def _render_chat(config) -> None:
    with st.sidebar:
        st.write(f"Signed in as **{st.session_state.username}**")
        if st.button("← All chats"):
            st.session_state.view = "home"
            st.rerun()
        if st.button("Log out"):
            _logout()
            st.rerun()

    st.title("Chat")

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["text"])

    prompt = st.chat_input("Ask something...")
    if not prompt:
        return

    if not _ensure_fresh_tokens(config):
        st.rerun()
        return

    history = _history_for_api()
    st.session_state.messages.append({"role": "user", "text": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        chunks: list[str] = []
        try:
            for chunk in agent_client.stream_chat(
                config,
                prompt,
                history,
                st.session_state.actor_id,
                st.session_state.session_id,
                st.session_state.tokens.access_token,
            ):
                chunks.append(chunk)
                placeholder.markdown("".join(chunks))
        except agent_client.AgentInvocationError as err:
            if err.status_code in (401, 403):
                st.error("Your session has expired. Please log in again.")
                _logout()
                st.rerun()
                return
            st.error(str(err))
            return

    reply = "".join(chunks)
    st.session_state.messages.append({"role": "assistant", "text": reply})


def main() -> None:
    try:
        config = load_config()
    except RuntimeError as err:
        st.error(str(err))
        return

    _init_state()

    if not st.session_state.authenticated:
        _render_login(config)
    elif st.session_state.view == "chat":
        _render_chat(config)
    else:
        _render_home(config)


if __name__ == "__main__":
    main()
