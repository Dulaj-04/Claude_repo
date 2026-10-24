import asyncio
import importlib.util
import re
from pathlib import Path

from acad_electrical_mcp import server

ROOT = Path(__file__).resolve().parents[1]


def _gen():
    spec = importlib.util.spec_from_file_location("gen_tool_docs", ROOT / "scripts" / "gen_tool_docs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tools_md_matches_server():
    assert (ROOT / "docs" / "tools.md").read_text() == _gen().render(), (
        "docs/tools.md is stale: run python scripts/gen_tool_docs.py")


def test_readme_mentions_every_tool():
    readme = (ROOT / "README.md").read_text()
    names = [t.name for t in asyncio.run(server.mcp.list_tools())]
    missing = [n for n in names if f"`{n}`" not in readme]
    assert not missing, f"README.md is missing tools: {missing}"


def test_relative_links_resolve():
    for md in [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]:
        text = md.read_text()
        targets = re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text) + re.findall(
            r'(?:src|href)="([^"#]+)(?:#[^"]*)?"', text)
        for target in targets:
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            assert (md.parent / target).exists(), f"{md.name}: broken link {target}"
