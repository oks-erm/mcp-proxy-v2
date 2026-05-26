#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Sequence, Tuple

import anyio
import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool

CONFIG_PATH_ENV = "MCP_CONFIG_PATH"
PROXY_URL_ENV = "MCP_PROXY_URL"
API_KEY_ENV = "MCP_API_KEY"

DEFAULT_CONFIG_PATH = Path.home() / ".cursor" / "mcp.json"

SAFE_TOOL_SCENARIOS: Dict[str, list[Tuple[str, Dict[str, Any] | None]]] = {
    # SQL gateway
    "run_query": [
        ("select-only", {"sql": "SELECT 1"}),
        ("validation-error", {"sql": "INSERT INTO audit_log DEFAULT VALUES"}),
    ],
    "get_schema": [("default", {})],
    "get_schema_yaml": [("default", {})],
    "lookup_table": [("default", {"table_name": "reservations_reservation"})],
    # Firestore gateway
    "list_collections": [("default", {})],
    "query_documents": [("default", {"collection": "projects"})],
    "count_documents": [("default", {"collection": "projects"})],
    "get_document": [("default", {"collection": "projects", "document_id": "test-doc-id"})],
    # NocoDB gateway
    "nocodb_getTablesList": [("default", {})],
    "nocodb_getBaseInfo": [("default", {})],
    # n8n gateway (read-only tools only)
    "n8n_get_workflows": [("default", {"limit": 1})],
    "n8n_find_workflow_by_name": [
        ("default", {"query": "zz-mcp-smoke-nonexistent-workflow", "limit": 5, "active": None})
    ],
    "n8n_get_workflow": [("validation-missing-workflow-id", {})],
    "n8n_create_workflow": [("validation-missing-required-fields", {})],
    "n8n_update_workflow": [("validation-missing-workflow-id", {})],
    "n8n_get_executions": [("default", {"limit": 1, "summary_only": True})],
    "n8n_get_credentials": [("default", {"limit": 5})],
    "n8n_get_credential_schema": [("validation-missing-credential-type", {})],
    "n8n_create_credential": [("validation-missing-required-fields", {})],
    "n8n_stop_execution": [("validation-missing-execution-id", {})],
    "n8n_retry_execution": [("validation-missing-execution-id", {})],
    "n8n_list_nodes": [("default", {})],
    "n8n_list_node_files": [("default-empty", {})],
    "n8n_get_node_source": [("default-empty", {})],
    "n8n_search_nodes": [("default", {"query": "slack"})],
    "n8n_find_node_by_name_or_capability": [("default", {"query": "slack", "limit": 5})],
    "n8n_validate_workflow_definition": [
        ("default", {"name": "mcp-smoke-validate"}),
        ("validation-nodes-required-with-connections", {"connections": {}}),
    ],
    # QuickBooks gateway (read-only tools)
    "list_bank_accounts": [("default", {})],
    "get_bank_transactions_in": [("default", {"max_results": 1})],
    "get_bank_transactions_out": [("default", {"max_results": 1})],
    "get_bank_balances": [("default", {})],
    "get_bank_account_by_name": [("default-no-match", {"query": "zzzzz-mcp-smoke-nonexistent-account", "limit": 5})],
    "quickbooks_get_bank_account_by_name": [
        ("default-no-match", {"query": "zzzzz-mcp-smoke-nonexistent-account", "limit": 5})
    ],
    # Guesty MCP (read-only tools)
    "guesty_get_reservations": [("default", {"limit": 1})],
    "guesty_search_reservations": [("default", {"limit": 1})],
    "guesty_get_listings": [("default", {"limit": 1})],
    "guesty_search_listings": [("default", {"limit": 1})],
    "guesty_search_reviews": [("default", {"limit": 1})],
    "guesty_search_owners": [("default", {"limit": 1})],
    "guesty_get_guests": [("default", {"limit": 1})],
    "guesty_get_guest": [("validation-missing-guest-id", {})],
    "guesty_search_guests": [("default", {"limit": 1})],
    "guesty_get_listing_calendar": [("validation-missing-required-fields", {})],
    "guesty_find_listing": [("default", {"query": "zzzzz-mcp-smoke-listing", "limit": 1})],
    "find_listing": [("default", {"query": "zzzzz-mcp-smoke-listing", "limit": 1})],
    "guesty_get_listing_summary": [("validation-missing-id", {"listing_id": ""})],
    "guesty_find_reservation": [("default", {"confirmation_code": "ZZ-NONEXISTENT-MCP-SMOKE-999", "limit": 1})],
    "find_reservation": [("default", {"confirmation_code": "ZZ-NONEXISTENT-MCP-SMOKE-999", "limit": 1})],
    "guesty_is_listing_available_on_dates": [
        (
            "validation-bad-dates",
            {"listing_id": "000000000000000000000000", "check_in": "not-a-date", "check_out": "2099-01-02"},
        )
    ],
    "is_listing_available_on_dates": [
        (
            "validation-bad-dates",
            {"listing_id": "000000000000000000000000", "check_in": "not-a-date", "check_out": "2099-01-02"},
        )
    ],
    # Stripe MCP (read-only tools)
    "stripe_get_payouts": [("default", {})],
    "stripe_get_payout_transactions": [("validation-missing-payout-id", {})],
    "stripe_get_charges": [("default", {"limit": 1})],
    "stripe_get_charge": [("validation-missing-charge-id", {})],
    "stripe_get_balance": [("default", {})],
    "stripe_get_balance_transaction": [("validation-missing-transaction-id", {})],
    "stripe_get_refund": [("validation-missing-refund-id", {})],
    # Breezeway MCP (read-only tools; names match breezeway-mcp FastMCP + mcp-proxy)
    "breezeway_list_properties_page": [("default", {"page": 1, "limit": 1})],
    "breezeway_list_users": [("default", {})],
    "breezeway_get_reservation_by_external_id": [
        ("validation-missing-id", {"external_reservation_id": "nonexistent-mcp-smoke-test"})
    ],
    "breezeway_find_property_by_name_or_external_id": [
        ("default-no-match", {"query": "zzzzz-mcp-smoke-no-such-breezeway-property", "max_pages": 1})
    ],
    "breezeway_get_property_summary": [("validation-unlikely-id", {"property_id": 1})],
    # Zendesk MCP (read-only tools with safe inputs)
    "zendesk_search_tickets": [("default", {"query_string": "type:ticket status:open", "limit": 1})],
    "zendesk_get_ticket": [("validation-unlikely-id", {"ticket_id": 999999999})],
    "get_ticket": [("validation-unlikely-id", {"ticket_id": 999999999})],
    "zendesk_find_ticket_by_reservation_id": [
        ("default", {"reservation_id": "ZZ-NONEXISTENT-MCP-SMOKE-ZENDESK-999", "limit": 1})
    ],
    "zendesk_list_views": [("default", {"active_only": True})],
    "zendesk_list_view_tickets": [("validation-unlikely-id", {"view_id": 999999999})],
    "zendesk_get_view_ticket_counts": [("default", {"view_ids": [1]})],
    "find_ticket_by_reservation_id": [
        ("default", {"reservation_id": "ZZ-NONEXISTENT-MCP-SMOKE-ZENDESK-999", "limit": 1})
    ],
    # Pipedrive MCP (read-only list tools; small limit)
    "pipedrive_list_activities": [("default", {"start": 0, "limit": 1})],
    "pipedrive_get_activity": [("default", {"activity_id": 1})],
    "pipedrive_search_activities": [("default", {"term": "xx", "limit": 1})],
    "pipedrive_list_deals": [("default", {"start": 0, "limit": 1})],
    "pipedrive_get_deal": [("default", {"deal_id": 1})],
    "pipedrive_search_deals": [("default", {"term": "xx", "limit": 1})],
    "pipedrive_find_deal_by_title_or_exact_name": [
        ("default", {"query": "zz-mcp-smoke-deal-title-not-found", "limit": 5})
    ],
    "pipedrive_get_deal_summary": [("validation-unlikely-id", {"deal_id": 999999999})],
    "pipedrive_list_leads": [("default", {"start": 0, "limit": 1})],
    "pipedrive_get_lead": [("default", {"lead_id": "00000000-0000-4000-8000-000000000001"})],
    "pipedrive_search_leads": [("default", {"term": "xx", "limit": 1})],
    "pipedrive_list_persons": [("default", {"start": 0, "limit": 1})],
    "pipedrive_get_person": [("default", {"person_id": 1})],
    "pipedrive_search_persons": [("default", {"term": "xx", "limit": 1})],
    "pipedrive_list_pipelines": [("default", {"start": 0, "limit": 1})],
    "pipedrive_get_pipeline": [("default", {"pipeline_id": 1})],
    "pipedrive_search_pipelines": [("default", {"term": "xx", "limit": 1})],
    "pipedrive_list_stages": [("default", {"start": 0, "limit": 1})],
    "pipedrive_get_stage": [("default", {"stage_id": 1})],
    "pipedrive_search_stages": [("default", {"term": "xx", "limit": 1})],
    # absence.io MCP (read-only)
    "absence_list_users": [("default", {"skip": 0, "limit": 1})],
    "absence_list_absences": [("default", {"skip": 0, "limit": 1})],
    "absence_find_user_by_name": [
        ("default", {"name": "zzzzz-nonexistent-mcp-smoke", "limit": 1, "exclude_inactive": True})
    ],
    "absence_is_user_absent_on_date": [
        (
            "default-no-match",
            {
                "user_name": "zzzzz-nonexistent-mcp-smoke-user",
                "date": "2099-01-01",
                "exclude_inactive": True,
            },
        )
    ],
    "absence_get_user_absences": [("validation-missing-user", {"user_id": "000000000000000000000000"})],
    # PriceLabs MCP (read-only; validation scenarios avoid live API dependency)
    "pricelabs_get_neighborhood_data": [("validation-missing-pms", {"pms": "", "listing_id": "mcp-smoke"})],
    "pricelabs_get_date_specific_overrides": [("validation-missing-listing", {"listing_id": "", "pms": "airbnb"})],
    # Known legacy tools
    "firestore_gateway_list_collections": [("default", {})],
    "firestore_gateway_query_documents": [("default", {"collection": "projects"})],
    "firestore_gateway_get_document": [("default", {"collection": "projects", "document_id": "test-doc-id"})],
    "sql_gateway_run_query": [("select-only", {"sql": "SELECT 1"})],
    "sql_gateway_get_schema": [("default", {})],
    "sql_gateway_get_schema_yaml": [("default", {})],
    "sql_gateway_lookup_table": [("default", {"table_name": "reservations_reservation"})],
    "n8n_gateway_n8n_list_nodes": [("default", {})],
}


def find_proxy_config() -> Tuple[str, Dict[str, str]]:
    env_url = os.getenv(PROXY_URL_ENV)
    env_api_key = os.getenv(API_KEY_ENV)
    headers: Dict[str, str] = {}
    if env_api_key:
        headers["X-API-Key"] = env_api_key

    config_path = Path(os.getenv(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH)
    if not config_path.exists():
        raise FileNotFoundError(f"Cursor MCP config not found at {config_path}")

    with config_path.open() as fh:
        config = json.load(fh)

    servers = (config.get("mcpServers") or {}).values()
    for server in servers:
        args = server.get("args", [])
        url, parsed_headers = _parse_args(args)
        if url:
            headers.update(parsed_headers)
            if env_url:
                url = env_url
            return url, headers

    raise ValueError("No MCP server with URL found inside Cursor config.")


def _parse_args(args: Sequence[str]) -> Tuple[str | None, Dict[str, str]]:
    url: str | None = None
    headers: Dict[str, str] = {}
    i = 0
    while i < len(args):
        current = args[i]
        if current.startswith("http://") or current.startswith("https://"):
            url = current
            i += 1
            continue
        if current == "--header" and i + 1 < len(args):
            header_value = args[i + 1]
            if ":" in header_value:
                key, value = header_value.split(":", 1)
                headers[key.strip()] = value.strip()
            i += 2
            continue
        i += 1
    return url, headers


def _format_tool_message(result: Any) -> str:
    structured = getattr(result, "structuredContent", None)
    if structured:
        try:
            return json.dumps(structured, default=str)
        except Exception:
            return str(structured)

    content = getattr(result, "content", [])
    parts: list[str] = []
    for block in content or []:
        dumpable = getattr(block, "model_dump", None)
        if callable(dumpable):
            try:
                block_data = dumpable(by_alias=True, exclude_none=True)
                if text := block_data.get("text"):
                    parts.append(text)
                    continue
                parts.append(json.dumps(block_data, default=str))
                continue
            except Exception:
                pass
        if text := getattr(block, "text", None):
            parts.append(text)
        else:
            parts.append(str(block))
    if parts:
        return " | ".join(parts)
    return "tool error"


def _resolve_scenarios(tool_name: str) -> list[Tuple[str, Dict[str, Any] | None]] | None:
    scenarios = SAFE_TOOL_SCENARIOS.get(tool_name)
    if scenarios is not None:
        return scenarios

    # Proxy-published names are often "<server_id>_<tool_name>".
    # Match by suffix so this script survives server-id changes.
    for base_tool_name, base_scenarios in SAFE_TOOL_SCENARIOS.items():
        if tool_name.endswith(f"_{base_tool_name}"):
            return base_scenarios
    return None


async def _call_all_tools(tools: Sequence[Tool], session: ClientSession) -> int:
    total = 0
    passed = 0
    failed = 0
    transport_errors = 0
    skipped_tools = 0

    if not tools:
        print("WARNING: No tools returned by tools/list.")

    for tool in tools:
        tool_name = tool.name or "unnamed-tool"
        scenarios = _resolve_scenarios(tool_name)
        if scenarios is None:
            skipped_tools += 1
            print(f"{tool_name}: skipped (no safe scenario configured)")
            continue
        total += len(scenarios)

        for scenario_name, arguments in scenarios:
            display_name = f"{tool_name} [{scenario_name}]" if scenario_name != "default" else tool_name
            try:
                result = await session.call_tool(tool_name, arguments=arguments or None)
            except Exception as exc:
                transport_errors += 1
                failed += 1
                print(f"{display_name}: transport error ({exc})")
                continue

            if result.isError:
                failed += 1
                print(f"{display_name}: {_format_tool_message(result)}")
            else:
                passed += 1
                print(f"{display_name}: ok")

    print("\nSummary:")
    print(f"  Tools discovered: {len(tools)}")
    print(f"  Tools skipped: {skipped_tools}")
    print(f"  Tools called: {total}")
    print(f"  Successful results: {passed}")
    print(f"  Tool-level errors: {failed}")
    if transport_errors:
        print(f"  Transport failures: {transport_errors}")

    return 1 if transport_errors else 0


async def main_async() -> int:
    proxy_url, headers = find_proxy_config()
    http_headers = {
        "Accept": "application/json, text/event-stream",
        **headers,
    }
    timeout = httpx.Timeout(30.0, read=60.0)
    async with httpx.AsyncClient(headers=http_headers, timeout=timeout) as http_client:
        async with streamable_http_client(proxy_url, http_client=http_client) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                return await _call_all_tools(tools_result.tools, session)


def main() -> None:
    try:
        exit_code = anyio.run(main_async)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
