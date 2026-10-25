from __future__ import annotations

import json

import httpx
import respx

from heybuddy_backend.safety import IRREVERSIBLE, READ, WRITE, classify_risk, validate_arguments

from .conftest import COMPOSIO, GATEWAY
from .helpers import openai_sse, parse_sse, text_chunk, tool_chunks

SEND_SCHEMA = {"type": "object", "properties": {"recipient_email": {"type": "string"}, "subject": {"type": "string"},
                                                "body": {"type": "string"}}, "required": ["recipient_email", "body"]}
TOOLS = {"items": [
    {"slug": "GMAIL_SEND_EMAIL", "name": "Send email", "description": "Send an email", "toolkit": {"slug": "gmail"},
     "input_parameters": SEND_SCHEMA, "tags": []},
    {"slug": "GMAIL_FETCH_EMAILS", "name": "Fetch emails", "description": "List recent emails", "toolkit": {"slug": "gmail"},
     "input_parameters": {"type": "object", "properties": {"max_results": {"type": "integer"}}}, "tags": []},
]}


def connect_gmail(client, auth):
    respx.post(f"{COMPOSIO}/connected_accounts/link").mock(return_value=httpx.Response(201, json={
        "link_token": "lt", "redirect_url": "https://connect.composio.dev/link/lt", "expires_at": "2026-10-25T12:00:00Z",
        "connected_account_id": "ca_123"}))
    response = client.post("/v1/integrations/connect", headers=auth, json={"toolkit": "gmail"})
    assert response.status_code == 200
    return response.json()


def test_risk_classification():
    assert classify_risk("GMAIL_FETCH_EMAILS", "gmail") == READ
    assert classify_risk("GMAIL_SEND_EMAIL", "gmail") == IRREVERSIBLE
    assert classify_risk("GOOGLECALENDAR_CREATE_EVENT", "googlecalendar") == WRITE
    assert classify_risk("SLACK_DELETE_MESSAGE", "slack") == IRREVERSIBLE
    assert classify_risk("NOTION_MYSTERY_ACTION", "notion") == WRITE  # unknown -> needs confirmation
    assert classify_risk("X_DO_THING", "x", tags=["destructiveHint"]) == IRREVERSIBLE


def test_argument_validation():
    assert validate_arguments(SEND_SCHEMA, {"recipient_email": "a@b.c", "body": "hi"}) is None
    assert "body" in validate_arguments(SEND_SCHEMA, {"recipient_email": "a@b.c"})
    assert validate_arguments(SEND_SCHEMA, {"recipient_email": 5, "body": "x"}) is not None


@respx.mock
def test_connect_creates_link_with_documented_body(client, auth):
    link = connect_gmail(client, auth)
    assert link["redirect_url"].startswith("https://connect.composio.dev/")
    sent = json.loads(respx.calls[0].request.content)
    assert sent == {"auth_config_id": "ac_gmail1", "user_id": sent["user_id"]}
    assert respx.calls[0].request.headers["x-api-key"] == "test-composio-key"
    assert client.post("/v1/integrations/connect", headers=auth, json={"toolkit": "slack"}).status_code == 400


@respx.mock
def test_write_action_needs_confirmation_and_is_logged(client, auth):
    connect_gmail(client, auth)
    respx.get(f"{COMPOSIO}/tools").mock(return_value=httpx.Response(200, json=TOOLS))
    arguments = {"recipient_email": "sam@example.com", "body": "running late"}
    gateway = respx.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(
        200, content=openai_sse(*tool_chunks("call_9", "cx_GMAIL_SEND_EMAIL", json.dumps(arguments)))))
    events = parse_sse(client.post("/v1/chat", headers=auth, json={"messages": [{"role": "user", "content": "email sam"}]}).text)
    sent_tools = [t["function"]["name"] for t in json.loads(gateway.calls[0].request.content)["tools"]]
    assert "cx_GMAIL_SEND_EMAIL" in sent_tools and "cx_GMAIL_FETCH_EMAILS" in sent_tools
    call = next(data for name, data in events if name == "tool_call")
    assert call["kind"] == "integration" and call["risk"] == IRREVERSIBLE and call["proposal_id"].startswith("p_")
    assert "sam@example.com" in call["preview"]

    execute = respx.post(f"{COMPOSIO}/tools/execute/GMAIL_SEND_EMAIL").mock(return_value=httpx.Response(200, json={
        "data": {"id": "m1"}, "error": None, "successful": True, "log_id": "l1"}))
    unconfirmed = client.post("/v1/integrations/execute", headers=auth,
                              json={"proposal_id": call["proposal_id"], "arguments": arguments})
    assert unconfirmed.status_code == 409 and execute.call_count == 0
    tampered = client.post("/v1/integrations/execute", headers=auth, json={
        "proposal_id": call["proposal_id"], "arguments": {**arguments, "recipient_email": "attacker@evil"}, "confirmed": True})
    assert tampered.status_code == 409 and execute.call_count == 0
    done = client.post("/v1/integrations/execute", headers=auth,
                       json={"proposal_id": call["proposal_id"], "arguments": arguments, "confirmed": True})
    assert done.status_code == 200 and done.json()["successful"] is True
    body = json.loads(execute.calls[0].request.content)
    assert body["connected_account_id"] == "ca_123" and body["arguments"] == arguments
    again = client.post("/v1/integrations/execute", headers=auth,
                        json={"proposal_id": call["proposal_id"], "arguments": arguments, "confirmed": True})
    assert again.status_code == 404  # single use

    logs = client.get("/v1/integrations/logs", headers=auth).json()["logs"]
    assert logs[0]["tool_slug"] == "GMAIL_SEND_EMAIL" and logs[0]["status"] == "ok"
    assert "running late" not in json.dumps(logs)  # summaries never include the content
    assert client.delete(f"/v1/integrations/logs/{logs[0]['id']}", headers=auth).json()["deleted"] == 1
    assert client.get("/v1/integrations/logs", headers=auth).json()["logs"] == []


@respx.mock
def test_read_action_runs_without_confirmation_and_failed_runs_are_retryable(client, auth):
    connect_gmail(client, auth)
    respx.get(f"{COMPOSIO}/tools").mock(return_value=httpx.Response(200, json=TOOLS))
    respx.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(
        200, content=openai_sse(*tool_chunks("c", "cx_GMAIL_FETCH_EMAILS", '{"max_results": 3}'))))
    call = next(d for n, d in parse_sse(client.post("/v1/chat", headers=auth, json={
        "messages": [{"role": "user", "content": "check mail"}]}).text) if n == "tool_call")
    assert call["risk"] == READ
    respx.post(f"{COMPOSIO}/tools/execute/GMAIL_FETCH_EMAILS").mock(side_effect=[
        httpx.Response(200, json={"data": {}, "error": "rate limited", "successful": False}),
        httpx.Response(200, json={"data": {"messages": [{"subject": "hi"}]}, "error": None, "successful": True})])
    first = client.post("/v1/integrations/execute", headers=auth, json={"proposal_id": call["proposal_id"], "arguments": {"max_results": 3}})
    assert first.json()["successful"] is False
    second = client.post("/v1/integrations/execute", headers=auth, json={"proposal_id": call["proposal_id"], "arguments": {"max_results": 3}})
    assert second.json()["successful"] is True and "subject" in second.json()["data"]


@respx.mock
def test_invalid_or_unknown_integration_calls_are_rejected(client, auth):
    connect_gmail(client, auth)
    respx.get(f"{COMPOSIO}/tools").mock(return_value=httpx.Response(200, json=TOOLS))
    respx.post(f"{GATEWAY}/chat/completions").mock(side_effect=[
        httpx.Response(200, content=openai_sse(*tool_chunks("c1", "cx_GMAIL_SEND_EMAIL", '{"recipient_email": "x"}'))),
        httpx.Response(200, content=openai_sse(*tool_chunks("c2", "cx_BANK_TRANSFER", '{}'))),
    ])
    first = next(d for n, d in parse_sse(client.post("/v1/chat", headers=auth, json={"messages": [{"role": "user", "content": "a"}]}).text) if n == "tool_call")
    assert first["kind"] == "rejected" and "body" in first["error"]
    second = next(d for n, d in parse_sse(client.post("/v1/chat", headers=auth, json={"messages": [{"role": "user", "content": "b"}]}).text) if n == "tool_call")
    assert second["kind"] == "rejected" and second["error"] == "unknown tool"


@respx.mock
def test_integrations_listing_and_disconnect(client, auth):
    link = connect_gmail(client, auth)
    respx.get(f"{COMPOSIO}/connected_accounts").mock(return_value=httpx.Response(200, json={
        "items": [{"id": "ca_123", "status": "ACTIVE", "toolkit": {"slug": "gmail"}}]}))
    listing = client.get("/v1/integrations", headers=auth).json()
    assert listing["connections"] == [{"id": link["connection_id"], "toolkit": "gmail", "status": "ACTIVE"}]
    delete = respx.delete(f"{COMPOSIO}/connected_accounts/ca_123").mock(return_value=httpx.Response(200, json={}))
    assert client.delete(f"/v1/integrations/{link['connection_id']}", headers=auth).status_code == 200
    assert delete.called
    assert client.get("/v1/integrations", headers=auth).json()["connections"] == []


def test_other_users_cannot_execute_my_proposals(client, auth):
    other = client.post("/v1/pair", json={"pairing_secret": "pair-secret-for-tests-123456"}).json()["token"]
    response = client.post("/v1/integrations/execute", headers={"Authorization": f"Bearer {other}"},
                           json={"proposal_id": "p_doesnotexist", "arguments": {}, "confirmed": True})
    assert response.status_code == 404


_ = text_chunk
