from contract import dump_tools


def test_tool_list_matches_contract():
    """The server's tools/list is exactly contract/tools.json -- the
    contract the Rust port is checked against. If a change to a tool is
    deliberate, rerun `python contract/dump_tools.py` and commit the file."""
    expected = dump_tools.TOOLS_PATH.read_text(encoding="utf-8")
    assert dump_tools.render(dump_tools.tool_list()) == expected
