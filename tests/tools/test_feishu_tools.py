"""Tests for feishu_doc_tool and feishu_drive_tool — registration and schema validation."""

import importlib
import json
import unittest
from unittest.mock import MagicMock, patch

from tools.registry import registry

# Trigger tool discovery so feishu tools get registered
importlib.import_module("tools.feishu_doc_tool")
importlib.import_module("tools.feishu_bitable_tool")
importlib.import_module("tools.feishu_drive_tool")


class TestFeishuToolRegistration(unittest.TestCase):
    """Verify feishu tools are registered and have valid schemas."""

    EXPECTED_TOOLS = {
        "feishu_doc_read": "feishu_doc",
        "feishu_bitable_read": "feishu_bitable",
        "feishu_drive_list_comments": "feishu_drive",
        "feishu_drive_list_comment_replies": "feishu_drive",
        "feishu_drive_reply_comment": "feishu_drive",
        "feishu_drive_add_comment": "feishu_drive",
    }

    def test_all_tools_registered(self):
        for tool_name, toolset in self.EXPECTED_TOOLS.items():
            entry = registry.get_entry(tool_name)
            self.assertIsNotNone(entry, f"{tool_name} not registered")
            self.assertEqual(entry.toolset, toolset)


    def test_bitable_read_schema_params(self):
        entry = registry.get_entry("feishu_bitable_read")
        props = entry.schema["parameters"].get("properties", {})
        self.assertIn("doc_token", props)
        self.assertIn("table_id", props)
        self.assertIn("view_id", props)
        self.assertIn("page_size", props)

    def test_drive_tools_require_file_token(self):
        for tool_name in self.EXPECTED_TOOLS:
            if tool_name in {"feishu_doc_read", "feishu_bitable_read"}:
                continue
            entry = registry.get_entry(tool_name)
            props = entry.schema["parameters"].get("properties", {})
            self.assertIn("file_token", props, f"{tool_name} missing file_token param")
            self.assertIn("file_type", props, f"{tool_name} missing file_type param")


class TestFeishuDocReadFallback(unittest.TestCase):
    def test_doc_read_uses_fallback_client_outside_comment_context(self):
        from tools import feishu_doc_tool

        mock_response = MagicMock()
        mock_response.code = 0
        mock_response.raw = MagicMock()
        mock_response.raw.content = json.dumps({"data": {"content": "hello from doc"}})

        mock_client = MagicMock()
        mock_client.request.return_value = mock_response

        mock_builder = MagicMock()
        (
            mock_builder.app_id.return_value
            .app_secret.return_value
            .domain.return_value
            .log_level.return_value
            .build.return_value
        ) = mock_client

        fake_lark = MagicMock()
        fake_lark.Client.builder.return_value = mock_builder
        fake_lark.LogLevel.WARNING = "warning"
        fake_lark.AccessTokenType.TENANT = "tenant"

        request_builder = MagicMock()
        request_builder.http_method.return_value = request_builder
        request_builder.uri.return_value = request_builder
        request_builder.token_types.return_value = request_builder
        request_builder.paths.return_value = request_builder
        request_builder.queries.return_value = request_builder
        request_builder.build.return_value = object()

        with patch.object(feishu_doc_tool, "get_client", return_value=None), \
             patch.dict(
                 "os.environ",
                 {
                     "FEISHU_APP_ID": "cli_test",
                     "FEISHU_APP_SECRET": "secret_test",
                     "FEISHU_DOMAIN": "lark",
                 },
                 clear=False,
             ), \
             patch.dict(
                 "sys.modules",
                 {
                     "lark_oapi": fake_lark,
                     "lark_oapi.core.enum": MagicMock(HttpMethod=MagicMock(GET="GET")),
                     "lark_oapi.core.model.base_request": MagicMock(
                         BaseRequest=MagicMock(builder=MagicMock(return_value=request_builder))
                     ),
                 },
                 clear=False,
             ):
            result = feishu_doc_tool._handle_feishu_doc_read({"doc_token": "doc_123"})

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["content"], "hello from doc")
        mock_client.request.assert_called_once()
        fake_lark.Client.builder.assert_called_once()


class TestFeishuBitableRead(unittest.TestCase):
    def test_bitable_read_resolves_wiki_and_fetches_records(self):
        from tools import feishu_bitable_tool

        wiki_response = MagicMock()
        wiki_response.code = 0
        wiki_response.raw = MagicMock()
        wiki_response.raw.content = json.dumps(
            {"data": {"node": {"obj_type": "bitable", "obj_token": "app_token_123", "title": "问题追踪系统"}}}
        )

        records_response = MagicMock()
        records_response.code = 0
        records_response.raw = MagicMock()
        records_response.raw.content = json.dumps(
            {
                "data": {
                    "items": [
                        {
                            "record_id": "rec1",
                            "fields": {"问题描述": "foo", "Priority": "P0", "Progress": "Open"}
                        }
                    ],
                    "has_more": False,
                    "page_token": "rec1",
                    "total": 1,
                }
            }
        )

        mock_client = MagicMock()
        mock_client.request.side_effect = [wiki_response, records_response]

        request_builder = MagicMock()
        request_builder.http_method.return_value = request_builder
        request_builder.uri.return_value = request_builder
        request_builder.token_types.return_value = request_builder
        request_builder.paths.return_value = request_builder
        request_builder.queries.return_value = request_builder
        request_builder.build.side_effect = ["wiki_request", "records_request"]

        fake_lark = MagicMock()
        fake_lark.AccessTokenType.TENANT = "tenant"

        with patch.object(feishu_bitable_tool, "get_client", return_value=mock_client), \
             patch.dict(
                 "sys.modules",
                 {
                     "lark_oapi": fake_lark,
                     "lark_oapi.core.enum": MagicMock(HttpMethod=MagicMock(GET="GET")),
                     "lark_oapi.core.model.base_request": MagicMock(
                         BaseRequest=MagicMock(builder=MagicMock(return_value=request_builder))
                     ),
                 },
                 clear=False,
             ):
            result = feishu_bitable_tool._handle_feishu_bitable_read(
                {"doc_token": "VuVBwPc1sipNx9kdeGzlp71Mgab", "table_id": "tbloCmDOsFMj47nI", "view_id": "vew5x8IS0e"}
            )

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["title"], "问题追踪系统")
        self.assertEqual(payload["app_token"], "app_token_123")
        self.assertEqual(payload["table_id"], "tbloCmDOsFMj47nI")
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["record_id"], "rec1")
        self.assertEqual(mock_client.request.call_count, 2)

    def test_bitable_read_returns_error_for_non_bitable_wiki(self):
        from tools import feishu_bitable_tool

        wiki_response = MagicMock()
        wiki_response.code = 0
        wiki_response.raw = MagicMock()
        wiki_response.raw.content = json.dumps(
            {"data": {"node": {"obj_type": "docx", "obj_token": "doc_token_123", "title": "普通文档"}}}
        )

        mock_client = MagicMock()
        mock_client.request.return_value = wiki_response

        request_builder = MagicMock()
        request_builder.http_method.return_value = request_builder
        request_builder.uri.return_value = request_builder
        request_builder.token_types.return_value = request_builder
        request_builder.paths.return_value = request_builder
        request_builder.queries.return_value = request_builder
        request_builder.build.return_value = "wiki_request"

        fake_lark = MagicMock()
        fake_lark.AccessTokenType.TENANT = "tenant"

        with patch.object(feishu_bitable_tool, "get_client", return_value=mock_client), \
             patch.dict(
                 "sys.modules",
                 {
                     "lark_oapi": fake_lark,
                     "lark_oapi.core.enum": MagicMock(HttpMethod=MagicMock(GET="GET")),
                     "lark_oapi.core.model.base_request": MagicMock(
                         BaseRequest=MagicMock(builder=MagicMock(return_value=request_builder))
                     ),
                 },
                 clear=False,
             ):
            result = feishu_bitable_tool._handle_feishu_bitable_read(
                {"doc_token": "VuVBwPc1sipNx9kdeGzlp71Mgab", "table_id": "tbloCmDOsFMj47nI"}
            )

        payload = json.loads(result)
        self.assertIn("error", payload)
        self.assertIn("not a bitable", payload["error"])

    def test_bitable_read_uses_fallback_client_outside_comment_context(self):
        from tools import feishu_bitable_tool

        wiki_response = MagicMock()
        wiki_response.code = 0
        wiki_response.raw = MagicMock()
        wiki_response.raw.content = json.dumps(
            {"data": {"node": {"obj_type": "bitable", "obj_token": "app_token_123", "title": "问题追踪系统"}}}
        )

        records_response = MagicMock()
        records_response.code = 0
        records_response.raw = MagicMock()
        records_response.raw.content = json.dumps(
            {"data": {"items": [], "has_more": False, "page_token": "", "total": 0}}
        )

        mock_client = MagicMock()
        mock_client.request.side_effect = [wiki_response, records_response]

        mock_builder = MagicMock()
        (
            mock_builder.app_id.return_value
            .app_secret.return_value
            .domain.return_value
            .log_level.return_value
            .build.return_value
        ) = mock_client

        fake_lark = MagicMock()
        fake_lark.Client.builder.return_value = mock_builder
        fake_lark.LogLevel.WARNING = "warning"
        fake_lark.AccessTokenType.TENANT = "tenant"

        request_builder = MagicMock()
        request_builder.http_method.return_value = request_builder
        request_builder.uri.return_value = request_builder
        request_builder.token_types.return_value = request_builder
        request_builder.paths.return_value = request_builder
        request_builder.queries.return_value = request_builder
        request_builder.build.side_effect = ["wiki_request", "records_request"]

        with patch.object(feishu_bitable_tool, "get_client", return_value=None), \
             patch.dict(
                 "os.environ",
                 {
                     "FEISHU_APP_ID": "cli_test",
                     "FEISHU_APP_SECRET": "secret_test",
                     "FEISHU_DOMAIN": "lark",
                 },
                 clear=False,
             ), \
             patch.dict(
                 "sys.modules",
                 {
                     "lark_oapi": fake_lark,
                     "lark_oapi.core.enum": MagicMock(HttpMethod=MagicMock(GET="GET")),
                     "lark_oapi.core.model.base_request": MagicMock(
                         BaseRequest=MagicMock(builder=MagicMock(return_value=request_builder))
                     ),
                 },
                 clear=False,
             ):
            result = feishu_bitable_tool._handle_feishu_bitable_read(
                {"doc_token": "VuVBwPc1sipNx9kdeGzlp71Mgab", "table_id": "tbloCmDOsFMj47nI"}
            )

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["app_token"], "app_token_123")
        fake_lark.Client.builder.assert_called_once()
        self.assertEqual(mock_client.request.call_count, 2)

    def test_doc_read_resolves_wiki_token_before_reading_raw_content(self):
        from tools import feishu_doc_tool

        wiki_response = MagicMock()
        wiki_response.code = 0
        wiki_response.raw = MagicMock()
        wiki_response.raw.content = json.dumps(
            {"data": {"node": {"obj_type": "docx", "obj_token": "resolved_docx_token"}}}
        )

        raw_response = MagicMock()
        raw_response.code = 0
        raw_response.raw = MagicMock()
        raw_response.raw.content = json.dumps({"data": {"content": "resolved from wiki"}})

        mock_client = MagicMock()
        mock_client.request.side_effect = [wiki_response, raw_response]

        request_builder = MagicMock()
        request_builder.http_method.return_value = request_builder
        request_builder.uri.return_value = request_builder
        request_builder.token_types.return_value = request_builder
        request_builder.paths.return_value = request_builder
        request_builder.queries.return_value = request_builder
        request_builder.build.side_effect = ["wiki_request", "raw_request"]

        fake_lark = MagicMock()
        fake_lark.AccessTokenType.TENANT = "tenant"

        with patch.object(feishu_doc_tool, "get_client", return_value=mock_client), \
             patch.dict(
                 "sys.modules",
                 {
                     "lark_oapi": fake_lark,
                     "lark_oapi.core.enum": MagicMock(HttpMethod=MagicMock(GET="GET")),
                     "lark_oapi.core.model.base_request": MagicMock(
                         BaseRequest=MagicMock(builder=MagicMock(return_value=request_builder))
                     ),
                 },
                 clear=False,
             ):
            result = feishu_doc_tool._handle_feishu_doc_read({"doc_token": "wiki_token_123"})

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["content"], "resolved from wiki")
        self.assertEqual(mock_client.request.call_count, 2)

    def test_doc_read_falls_back_to_wiki_resolution_after_doc_not_found(self):
        from tools import feishu_doc_tool

        not_found_response = MagicMock()
        not_found_response.code = 1770002
        not_found_response.msg = "not found"

        wiki_response = MagicMock()
        wiki_response.code = 0
        wiki_response.raw = MagicMock()
        wiki_response.raw.content = json.dumps(
            {"data": {"node": {"obj_type": "docx", "obj_token": "resolved_docx_token"}}}
        )

        raw_response = MagicMock()
        raw_response.code = 0
        raw_response.raw = MagicMock()
        raw_response.raw.content = json.dumps({"data": {"content": "resolved after fallback"}})

        mock_client = MagicMock()
        mock_client.request.side_effect = [not_found_response, wiki_response, raw_response]

        request_builder = MagicMock()
        request_builder.http_method.return_value = request_builder
        request_builder.uri.return_value = request_builder
        request_builder.token_types.return_value = request_builder
        request_builder.paths.return_value = request_builder
        request_builder.queries.return_value = request_builder
        request_builder.build.side_effect = ["raw_request_1", "wiki_request", "raw_request_2"]

        fake_lark = MagicMock()
        fake_lark.AccessTokenType.TENANT = "tenant"

        with patch.object(feishu_doc_tool, "get_client", return_value=mock_client), \
             patch.dict(
                 "sys.modules",
                 {
                     "lark_oapi": fake_lark,
                     "lark_oapi.core.enum": MagicMock(HttpMethod=MagicMock(GET="GET")),
                     "lark_oapi.core.model.base_request": MagicMock(
                         BaseRequest=MagicMock(builder=MagicMock(return_value=request_builder))
                     ),
                 },
                 clear=False,
             ):
            result = feishu_doc_tool._handle_feishu_doc_read({"doc_token": "VuVBwPc1sipNx9kdeGzlp71Mgab"})

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["content"], "resolved after fallback")
        self.assertEqual(mock_client.request.call_count, 3)

    def test_doc_read_returns_error_for_unsupported_wiki_object_type(self):
        from tools import feishu_doc_tool

        not_found_response = MagicMock()
        not_found_response.code = 1770002
        not_found_response.msg = "not found"

        wiki_response = MagicMock()
        wiki_response.code = 0
        wiki_response.raw = MagicMock()
        wiki_response.raw.content = json.dumps(
            {"data": {"node": {"obj_type": "bitable", "obj_token": "tbl_xxx"}}}
        )

        mock_client = MagicMock()
        mock_client.request.side_effect = [not_found_response, wiki_response]

        request_builder = MagicMock()
        request_builder.http_method.return_value = request_builder
        request_builder.uri.return_value = request_builder
        request_builder.token_types.return_value = request_builder
        request_builder.paths.return_value = request_builder
        request_builder.queries.return_value = request_builder
        request_builder.build.side_effect = ["raw_request", "wiki_request"]

        fake_lark = MagicMock()
        fake_lark.AccessTokenType.TENANT = "tenant"

        with patch.object(feishu_doc_tool, "get_client", return_value=mock_client), \
             patch.dict(
                 "sys.modules",
                 {
                     "lark_oapi": fake_lark,
                     "lark_oapi.core.enum": MagicMock(HttpMethod=MagicMock(GET="GET")),
                     "lark_oapi.core.model.base_request": MagicMock(
                         BaseRequest=MagicMock(builder=MagicMock(return_value=request_builder))
                     ),
                 },
                 clear=False,
             ):
            result = feishu_doc_tool._handle_feishu_doc_read({"doc_token": "VuVBwPc1sipNx9kdeGzlp71Mgab"})

        payload = json.loads(result)
        self.assertIn("error", payload)
        self.assertIn("unsupported obj_type=bitable", payload["error"])

    def test_doc_read_returns_clear_error_when_no_context_and_no_credentials(self):
        from tools import feishu_doc_tool

        with patch.object(feishu_doc_tool, "get_client", return_value=None), \
             patch.dict(
                 "os.environ",
                 {
                     "FEISHU_APP_ID": "",
                     "FEISHU_APP_SECRET": "",
                     "FEISHU_DOMAIN": "",
                 },
                 clear=False,
             ):
            result = feishu_doc_tool._handle_feishu_doc_read({"doc_token": "doc_123"})

        payload = json.loads(result)
        self.assertIn("error", payload)
        self.assertIn("Feishu client not available", payload["error"])


if __name__ == "__main__":
    unittest.main()
