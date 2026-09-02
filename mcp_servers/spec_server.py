#!/usr/bin/env python3
"""A local, dependency-free MCP server serving fake Jira tickets and Confluence pages.

Why this exists: the MCP benchmark category needs to be *reproducible*. Pointing tasks at
a live Jira or Confluence would make results depend on network conditions, credentials and
mutable external content -- the opposite of a controlled experiment.

This server speaks MCP over stdio with a fixed, versioned corpus, so an MCP task is exactly
as repeatable as any other task, and the requirements a task depends on are retrievable
*only* through tool calls. An agent that skips the tools cannot pass the hidden tests.

Protocol: JSON-RPC 2.0 over stdin/stdout, one message per line.
"""

from __future__ import annotations

import json
import sys
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
CORPUS_VERSION = "1.0.0"

JIRA_ISSUES: dict[str, dict[str, Any]] = {
    "PAY-142": {
        "key": "PAY-142",
        "summary": "Support refunding a captured payment",
        "status": "Ready for Development",
        "priority": "High",
        "reporter": "product@example.com",
        "description": (
            "Support agents need to refund payments. Behaviour is specified in the linked "
            "Confluence page 'Refund Policy v2' (page id CONF-REFUND-2). Follow that page "
            "exactly -- the acceptance tests are derived from it. Do not infer the rules "
            "from the existing charge implementation; several of them are deliberately "
            "different."
        ),
        "linked_pages": ["CONF-REFUND-2"],
        "acceptance_criteria": [
            "Implement PaymentService.refund(payment_id, amount=None).",
            "All rules come from Confluence page CONF-REFUND-2.",
        ],
    },
    "PAY-77": {
        "key": "PAY-77",
        "summary": "Investigate slow payment history endpoint",
        "status": "Done",
        "priority": "Low",
        "reporter": "eng@example.com",
        "description": (
            "Resolved in a previous release. Included here as a distractor: it is NOT the "
            "ticket to implement."
        ),
        "linked_pages": [],
        "acceptance_criteria": [],
    },
}

CONFLUENCE_PAGES: dict[str, dict[str, Any]] = {
    "CONF-REFUND-2": {
        "id": "CONF-REFUND-2",
        "title": "Refund Policy v2",
        "space": "PAY",
        "version": 2,
        "body": """
# Refund Policy v2

These rules are authoritative for `PaymentService.refund`.

## Signature

    refund(payment_id: str, amount: Money | None = None) -> Payment

Passing `amount=None` refunds the full original payment amount.

## Rules

1. **Only captured payments may be refunded.** Refunding a payment whose status is not
   `CAPTURED` raises `RefundNotAllowed`.

2. **Partial refunds are supported.** A refund amount below the original amount is valid.

3. **Over-refunding is rejected.** A refund amount greater than the original payment
   amount raises `RefundAmountTooLarge`.

4. **The processing fee is NOT returned.** This is the rule that differs from the charge
   path: the account is credited the refunded amount only, never the fee. A full refund of
   a 100.00 payment credits exactly 100.00, even though 103.20 was originally debited.

5. **Status becomes `REFUNDED`** after a full refund. After a *partial* refund the status
   stays `CAPTURED`, because further refunds may follow.

6. **An audit entry with the action string `payment.refunded` must be recorded**, with the
   refunded amount in its detail field.

7. **A notification must be sent** to the account's email with the subject exactly
   `Refund issued`.

8. **Repeated full refunds are rejected** by rule 1, since the status is then `REFUNDED`.

## Error types

Add both to `payments.domain.errors`, deriving from `PaymentError`:

* `RefundNotAllowed`
* `RefundAmountTooLarge`

## API mapping

`PaymentAPI.refund_payment(payment_id, payload)` returns:

* `200` with the serialised payment on success
* `409` for `RefundNotAllowed`
* `400` for `RefundAmountTooLarge`
* `404` for `PaymentNotFound`
""",
    },
    "CONF-CHARGE-1": {
        "id": "CONF-CHARGE-1",
        "title": "Charge Flow (current)",
        "space": "PAY",
        "version": 5,
        "body": (
            "# Charge Flow\n\nDescribes the existing charge path, including the 2.9% + "
            "$0.30 fee. Included as a distractor: it is NOT the refund specification."
        ),
    },
}

TOOLS = [
    {
        "name": "search_jira_issues",
        "description": "Search Jira issues by text. Returns matching issue keys and summaries.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Free-text search."}},
            "required": ["query"],
        },
    },
    {
        "name": "get_jira_issue",
        "description": "Fetch a Jira issue by key, including its description and linked pages.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "e.g. PAY-142"}},
            "required": ["key"],
        },
    },
    {
        "name": "get_confluence_page",
        "description": "Fetch a Confluence page by id. Contains authoritative specifications.",
        "inputSchema": {
            "type": "object",
            "properties": {"page_id": {"type": "string", "description": "e.g. CONF-REFUND-2"}},
            "required": ["page_id"],
        },
    },
    {
        "name": "list_confluence_pages",
        "description": "List available Confluence pages with their ids and titles.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _text(payload: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    return {"content": [{"type": "text", "text": body}], "isError": False}


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "search_jira_issues":
        q = str(args.get("query", "")).lower()
        hits = [
            {"key": k, "summary": v["summary"], "status": v["status"]}
            for k, v in JIRA_ISSUES.items()
            if q in v["summary"].lower() or q in v["description"].lower() or q in k.lower()
        ]
        return _text({"results": hits, "count": len(hits)})

    if name == "get_jira_issue":
        issue = JIRA_ISSUES.get(str(args.get("key", "")).upper())
        return _text(issue) if issue else _error(f"issue not found: {args.get('key')}")

    if name == "get_confluence_page":
        page = CONFLUENCE_PAGES.get(str(args.get("page_id", "")).upper())
        return _text(page) if page else _error(f"page not found: {args.get('page_id')}")

    if name == "list_confluence_pages":
        return _text(
            {
                "pages": [
                    {"id": p["id"], "title": p["title"], "space": p["space"]}
                    for p in CONFLUENCE_PAGES.values()
                ]
            }
        )

    return _error(f"unknown tool: {name}")


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "acb-spec-server", "version": CORPUS_VERSION},
        }
    elif method in ("notifications/initialized", "initialized"):
        return None  # notification: no response
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params") or {}
        result = call_tool(params.get("name", ""), params.get("arguments") or {})
    elif method == "ping":
        result = {}
    else:
        if request_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            response = handle(request)
        except Exception as exc:  # noqa: BLE001 - never kill the server on one bad request
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32603, "message": f"internal error: {exc}"},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
