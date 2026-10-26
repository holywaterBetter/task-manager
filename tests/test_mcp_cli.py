import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from task_manager.gitstore import GitStore
from task_manager.mcp import Session, serve


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = GitStore(Path(self.tmp.name) / "registry")
        self.session = Session(self.store, "alice")

    def request(self, method, params=None, id=1):
        return self.session.handle({"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})

    def test_lifecycle_and_tool_discovery(self):
        self.assertIn("error", self.request("tools/list"))
        reply = self.request("initialize", {"protocolVersion": "2099-01-01"})
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(self.session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = {t["name"]: t for t in self.request("tools/list")["result"]["tools"]}
        self.assertTrue(tools["task_context"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["task_claim"]["annotations"]["readOnlyHint"])
        self.assertIn("version", tools["task_update"]["inputSchema"]["required"])

    def test_tool_roundtrip_and_domain_error_are_mcp_results(self):
        self.request("initialize")
        reply = self.request("tools/call", {"name": "project_create", "arguments": {"key": "TEAM"}})
        self.assertFalse(reply["result"]["isError"])
        record = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(record["key"], "TEAM")
        invalid = self.request("tools/call", {"name": "task_update", "arguments": {"id": "missing"}})
        self.assertTrue(invalid["result"]["isError"])
        self.assertIn("version", invalid["result"]["content"][0]["text"])
        self.assertIn("error", self.request("tools/call", {"name": []}))

    def test_stdio_emits_only_json_and_survives_invalid_input(self):
        source = io.StringIO('not json\n' + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + '\n' +
                             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + '\n' +
                             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "project_list", "arguments": {}}}) + '\n')
        target = io.StringIO()
        serve(self.store, "alice", source, target)
        lines = [json.loads(line) for line in target.getvalue().splitlines()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertFalse(lines[-1]["result"]["isError"])

    def test_cli_works_from_another_directory_and_accepts_utf8_file(self):
        script = Path(__file__).resolve().parents[1] / "team_tasks.py"
        params = Path(self.tmp.name) / "params.json"
        params.write_text(json.dumps({"key": "TEAM", "name": "협업"}, ensure_ascii=False), encoding="utf-8-sig")
        command = [sys.executable, str(script), "--registry", str(self.store.root), "--actor", "alice", "--local-only"]
        result = subprocess.run(command + ["call", "project.create", "--file", str(params)], cwd=self.tmp.name, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["name"], "협업")
        checked = subprocess.run(command + ["check"], cwd=self.tmp.name, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertTrue(json.loads(checked.stdout)["ok"])

    def test_default_cli_refuses_unsynchronized_operation(self):
        script = Path(__file__).resolve().parents[1] / "team_tasks.py"
        result = subprocess.run([sys.executable, str(script), "--registry", str(self.store.root), "call", "project.create", "--data", '{"key":"TEAM"}'],
                                cwd=self.tmp.name, capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(json.loads(result.stderr)["error"], {"sync_precondition", "git_sync"})
        self.assertFalse((self.store.root / "projects").exists())

    def test_synced_mcp_exposes_recovery_without_repeating_business_action(self):
        from unittest.mock import Mock
        synced_store = Mock(sync_required=True)
        synced_store.retry_push.return_value = {"pushed": True, "commit": "sample"}
        synced_store.acknowledge_sync.return_value = {"resolved": True, "commit": "reviewed"}
        session = Session(synced_store, "alice")
        initialized = session.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertIn("pull", initialized["result"]["instructions"])
        discovered = session.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {t["name"]: t for t in discovered["result"]["tools"]}
        self.assertFalse(tools["task_get"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["task_get"]["annotations"]["openWorldHint"])
        self.assertIn("sync_retry", tools)
        reply = session.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "sync_retry", "arguments": {}}})
        self.assertFalse(reply["result"]["isError"])
        synced_store.retry_push.assert_called_once_with()
        resolved = session.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "sync_resolved", "arguments": {}}})
        self.assertTrue(json.loads(resolved["result"]["content"][0]["text"])["resolved"])
        synced_store.acknowledge_sync.assert_called_once_with()
        synced_store.call.assert_not_called()

    def test_invalid_numbers_and_ids_do_not_terminate_stdio(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "task_context", "arguments": {"id": "missing", "max_chars": 10 ** 400}}},
            {"jsonrpc": "2.0", "id": [], "method": "ping"},
            {"jsonrpc": "2.0", "id": "\ud800", "method": "ping"},
        ]
        deep = "[" * 20000 + "0" + "]" * 20000
        source = io.StringIO("\n".join(json.dumps(m) for m in messages) + '\n{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n' + deep + '\n{"jsonrpc":"2.0","id":3,"method":"ping"}\n')
        target = io.StringIO()
        serve(self.store, "alice", source, target)
        replies = [json.loads(line) for line in target.getvalue().splitlines()]
        target.getvalue().encode("utf-8")
        self.assertEqual(len(replies), 7)
        self.assertTrue(replies[1]["result"]["isError"])
        self.assertEqual(replies[2]["error"]["code"], -32600)
        self.assertEqual(replies[3]["id"], "\ud800")
        self.assertEqual(replies[4]["error"]["code"], -32700)
        self.assertEqual(replies[5]["error"]["code"], -32700)
        self.assertEqual(replies[6], {"jsonrpc": "2.0", "id": 3, "result": {}})


if __name__ == "__main__":
    unittest.main()
