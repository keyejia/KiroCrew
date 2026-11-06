"""``dashboard.preferred_language``: the reply-language rule in [RESPONSE PREFERENCES].

The setting is the person's own choice of reply language, so it rides the same
trusted frame as ``dashboard.verbosity`` and is built from a fixed table, never
from the stored text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.change_card_catalog_plans import DASHBOARD_SETTINGS
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import PREFERRED_LANGUAGES
from kiro_crew.context import (
    _RESPONSE_PREFERENCES_FOOTER,
    _RESPONSE_PREFERENCES_HEADER,
    _build_response_preferences_section,
)

_REPO = Path(__file__).resolve().parents[1]


def _section(verbosity: str = "default", preferred_language: object = "") -> str:
    fake_cfg = SimpleNamespace(
        dashboard=SimpleNamespace(
            widget_density="more", verbosity=verbosity, preferred_language=preferred_language
        )
    )
    return _build_response_preferences_section(fake_cfg)


class TestReplyLanguageRule:
    def test_auto_adds_nothing(self):
        """An install that never set the language sees byte-identical context."""
        assert _section("default", "") == ""
        assert "## Reply language" not in _section("answer_only", "")

    def test_a_set_language_emits_the_frame_even_at_default_verbosity(self):
        block = _section("default", "zh-CN")
        assert block.startswith(_RESPONSE_PREFERENCES_HEADER + "\n")
        assert block.rstrip().endswith(_RESPONSE_PREFERENCES_FOOTER)
        assert "## Reply language" in block
        assert "## Reply style" not in block

    def test_the_rule_names_the_language_and_holds_through_mixed_messages(self):
        block = " ".join(_section("default", "zh-CN").split())
        assert "The user's preferred language is Simplified Chinese" in block
        assert "Write every reply in Simplified Chinese" in block
        assert "even when their message mixes in another language" in block
        assert "Switch only when the user asks for another language" in block

    def test_the_rule_outranks_the_levels_user_language_line(self):
        """``answer_only`` ends with "Reply in the user's language.", written for
        auto; the language section comes after it and says it wins."""
        block = _section("answer_only", "ja")
        assert block.index("Reply in the user's language.") < block.index("## Reply language")
        assert 'outranks any "user\'s language" rule above' in block

    def test_payload_and_text_for_other_readers_keep_their_language(self):
        block = " ".join(_section("concise", "de").split())
        assert "Code, commands, paths, identifiers, error strings and quoted text" in block
        assert "commit messages, pull request text, code comments, public posts" in block
        assert "keeps the project's own language" in block

    @pytest.mark.parametrize("raw", ["klingon", "Simplified Chinese", "en-XA", None, ["zh-CN"], {}])
    def test_an_unknown_value_injects_nothing(self, raw):
        """Only table names reach the trusted block; a hand-edited value never does."""
        assert _section("default", raw) == ""
        assert _section("concise", raw) == _section("concise", "")

    def test_a_config_without_the_attribute_is_auto(self):
        fake_cfg = SimpleNamespace(dashboard=SimpleNamespace(verbosity="default"))
        assert _build_response_preferences_section(fake_cfg) == ""


class TestCodeListsAgree:
    """Three places spell the same code list; each one drifting is a silent gap."""

    def test_the_change_card_plan_offers_auto_and_every_code(self):
        key, values = DASHBOARD_SETTINGS["chat.preferred-reply-language"]
        assert key == "preferred_language"
        assert values == ("", *PREFERRED_LANGUAGES)

    def test_the_config_enum_is_auto_plus_every_code(self):
        field = KiroCrewConfig().dashboard.__dataclass_fields__["preferred_language"]
        assert field.metadata["enum"] == ["", *PREFERRED_LANGUAGES]

    def test_the_codes_are_the_dashboards_pickable_ui_languages(self):
        """The picker reuses each UI language's endonym, so the codes must be
        the shipped UI languages (the pseudolocale excluded)."""
        src = (_REPO / "website/src/i18n/languages.ts").read_text(encoding="utf-8")
        entries = re.findall(r"\{ code: '([^']+)', label: '[^']*'(, devOnly: true)? \}", src)
        ui_codes = [code for code, dev in entries if not dev]
        assert ui_codes == list(PREFERRED_LANGUAGES)


class TestConfigRoundTrip:
    @pytest.fixture()
    def cfg_file(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text("{}", encoding="utf-8")
        with patch("kiro_crew.config.loader.config_path", return_value=p):
            yield p

    def test_default_is_auto(self, cfg_file):
        assert KiroCrewConfig.load().dashboard.preferred_language == ""

    def test_a_stored_code_loads(self, cfg_file):
        cfg_file.write_text(
            json.dumps({"dashboard": {"preferred_language": "zh-CN"}}), encoding="utf-8"
        )
        assert KiroCrewConfig.load().dashboard.preferred_language == "zh-CN"

    def test_an_invalid_stored_value_loads_as_auto(self, cfg_file):
        cfg_file.write_text(
            json.dumps({"dashboard": {"preferred_language": "klingon"}}), encoding="utf-8"
        )
        assert KiroCrewConfig.load().dashboard.preferred_language == ""


@pytest.fixture()
def handler_app(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    try:
        import kiro_crew.dashboard.handlers  # noqa: F401
    except ImportError:
        pytest.skip("dashboard handler deps not available locally")
    from kiro_crew.dashboard.handlers.files import api_dashboard_config

    sel = MagicMock()
    with (
        patch("kiro_crew.config.loader.config_path", return_value=p),
        patch("kiro_crew.dashboard.handlers.sel", return_value=sel),
    ):
        app = web.Application()
        app.router.add_put("/api/dashboard/config", api_dashboard_config)
        app.router.add_get("/api/dashboard/config", api_dashboard_config)
        yield as_owner(app)


@pytest.mark.asyncio
async def test_handler_round_trips_a_code_and_auto(handler_app):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"preferred_language": "zh-CN"})
        assert resp.status == 200
        assert (await (await client.get("/api/dashboard/config")).json())[
            "preferred_language"
        ] == "zh-CN"
        resp = await client.put("/api/dashboard/config", json={"preferred_language": ""})
        assert resp.status == 200
    assert KiroCrewConfig.load().dashboard.preferred_language == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["klingon", "en-XA", 7, None, ["zh-CN"]])
async def test_handler_rejects_values_outside_the_list(handler_app, bad):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"preferred_language": bad})
        assert resp.status == 400
        assert "zh-CN" in (await resp.json())["error"]
    assert KiroCrewConfig.load().dashboard.preferred_language == ""
