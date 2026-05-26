from permissions.write_tools import upstream_tool_requires_write


def test_cloud_run_source_upload_tools_require_write_permission():
    for tool_name in ("start_source_upload", "upload_source_chunk", "finalize_source_upload"):
        assert upstream_tool_requires_write("cloud_run_deployer", tool_name)
        assert upstream_tool_requires_write("cloud-run-deployer", tool_name)
