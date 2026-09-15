"""Меню и перенос: каждое поле доходит до экрана и не теряется при переносе."""
import dataclasses

import pytest

from gremlin import db, schema
from gremlin.handlers import user_menu as um
from gremlin.services import transfer

SETTINGS_FIELDS = {f.name for f in dataclasses.fields(db.Settings)}


def test_schema_fields_exist_in_settings():
    missing = {f.key for sec in schema.SECTIONS for f in sec.fields} \
        - SETTINGS_FIELDS
    assert missing == set()


def test_transfer_groups_reference_real_fields():
    missing = {f for _label, keys in transfer.GROUPS.values() for f in keys} \
        - SETTINGS_FIELDS
    assert missing == set()


@pytest.mark.parametrize("key", [s.key for s in schema.SECTIONS])
async def test_every_section_renders(chat, key):
    text, kb = await um.view_section(chat, key)
    assert text and kb


async def test_other_pages_render(chat):
    for view in (um.view_games, um.view_punishments, um.view_forgiven,
                 um.view_words, um.view_prof_words, um.view_active):
        text, kb = await view(chat)
        assert text and kb, view.__name__


def test_every_top_section_has_a_group():
    top = {s.key for s in schema.SECTIONS if not s.back}
    covered = {x.key for _k, _t, _h, items in schema.grouped_sections()
               for x in items}
    assert top - covered == set()
