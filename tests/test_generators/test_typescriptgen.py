import unittest

from linkml_runtime import SchemaView
from linkml_runtime.linkml_model import SlotDefinition

from linkml.generators.typescriptgen import TypescriptGenerator
from linkml.utils.schema_builder import SchemaBuilder
from tests.test_generators.environment import env

SCHEMA = env.input_path("kitchen_sink.yaml")
OUT = env.expected_path("kitchen_sink.ts")


class TypescriptGeneratorTestCase(unittest.TestCase):
    def test_tsgen(self):
        """typescript"""
        tss = TypescriptGenerator(SCHEMA, mergeimports=True).serialize()
        with open(OUT, "w") as stream:
            stream.write(tss)

        def assert_in(s: str) -> None:
            assert s.replace(" ", "") in tss.replace(" ", "")

        assert "export type OrganizationId" in tss
        assert_in("export interface Person  extends HasAliases")
        assert_in("has_familial_relationships?: FamilialRelationship[]")
        assert_in("code_systems?: {[index: CodeSystemId]: CodeSystem }")

    def test_required_slots(self):
        """typescript"""
        sb = SchemaBuilder("test")
        sb.add_defaults()
        id = SlotDefinition(name="id", multivalued=False, range="string", required=True)
        description = SlotDefinition(name="description", multivalued=False, range="string")
        sb.add_class("Person", slots=[id, description])
        schema = sb.schema
        tss = TypescriptGenerator(schema, mergeimports=True).serialize()
        assert "id: string" in tss
        assert "description?: string" in tss

    def test_mutlivalued_string(self):
        """Test that multivalued string slots are generated as string arrays"""

        sb = SchemaBuilder("test")
        sb.add_defaults()
        aliases = SlotDefinition(name="aliases", multivalued=True, range="string")
        sb.add_class("Person", slots=[aliases])
        schema = sb.schema
        tss = TypescriptGenerator(schema, mergeimports=True).serialize()
        assert "aliases?: string[]" in tss

    def test_enums(self):
        unit_test_schema = """
id: unit_test
name: unit_test

prefixes:
  ex: https://example.org/
default_prefix: ex

enums:
  TestEnum:
    permissible_values:
      123:
      +:
      This & that, plus maybe a 🎩:
      Ohio:
"""

        sv = SchemaView(unit_test_schema)
        gen = TypescriptGenerator(schema=unit_test_schema)
        enums = gen.generate_enums(sv.all_enums())
        assert enums
        enum = enums["TestEnum"]
        assert enum
        assert enum["values"]["number_123"]["value"] == "123"
        assert enum["values"]["PLUS_SIGN"]["value"] == "+"
        assert (
            enum["values"]["This_AMPERSAND_that_plus_maybe_a_TOP_HAT"]["value"]
            == "This & that, plus maybe a 🎩"
        )
        assert enum["values"]["Ohio"]["value"] == "Ohio"


if __name__ == "__main__":
    unittest.main()
