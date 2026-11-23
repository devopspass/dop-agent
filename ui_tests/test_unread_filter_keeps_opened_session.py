"""
Feature: with the "Unread" quick-filter active, a session you click on stays
in the left column (even though opening it marks it read) until you switch to
another session: the currently open session is always kept, and drops out
only after you open a different one.
"""

from __future__ import annotations

import time

import requests


def _new(page, text):
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)
    return page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")


def test_unread_filter_keeps_opened_session_until_you_switch(logged_in_page, live_server):
    page = logged_in_page
    a = _new(page, "unread keep A")
    b = _new(page, "unread keep B")
    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    requests.post(f"{live_server}/api/send", json={"conversation_id": a, "text": "bump A"}, cookies=cookies, timeout=10)
    sel_a = f".session-item[data-conversation-id='{a}']"
    page.wait_for_selector(f"{sel_a} .session-unread-dot", timeout=5000)

    page.click(".filter-chip[data-filter='unread']")
    page.click(sel_a)
    page.wait_for_function("(id) => currentConversationId === id", arg=a, timeout=3000)
    time.sleep(0.5)
    assert page.query_selector(sel_a) is not None  # still listed though read

    page.click(".filter-chip[data-filter='all']")
    page.click(".filter-chip[data-filter='unread']")
    page.click(f".session-item[data-conversation-id='{b}']") if page.query_selector(f".session-item[data-conversation-id='{b}']") else page.evaluate("(id) => openSession(id)", b)
    page.wait_for_function("(id) => currentConversationId === id", arg=b, timeout=3000)
    page.wait_for_function("(sel) => !document.querySelector(sel)", arg=sel_a, timeout=3000)
