import unittest

import jsonasobj

from linkml.generators.jsonschemagen import JsonSchemaGenerator
from tests.test_issues.environment import env
from tests.utils.test_environment import TestEnvironmentTestCase


class IssueJSONSchemaTypesTestCase(TestEnvironmentTestCase):
    env = env

    def test_issue_types(self):
        """Make sure that types are generated as part of the output"""

        def generator() -> str:
            gen = JsonSchemaGenerator(env.input_path("issue_129.yaml"))
            gen.top_class = "c"
            return gen.serialize()

        sobj_str = env.generate_single_file(
            "issue_129.json", lambda: generator(), value_is_returned=True
        )

        sobj = jsonasobj.loads(sobj_str)
        defs = sobj["$defs"]
        C = defs["C"]
        props = C["properties"]
        assert props["age_in_years"]["type"] == "integer"
        assert props["has_prop"]["type"] == "boolean"
        # multivalued primitive type, inlined
        assert props["scores"]["type"] == "array"
        assert props["scores"]["items"]["type"] == "number"
        # single-valued complex type, inlined
        assert props["has_d"]["$ref"] == "#/$defs/D"

        # multi-valued, inlined_as_list
        assert props["has_ds"]["type"] == "array"
        assert props["has_ds"]["items"]["$ref"] == "#/$defs/D"

        # multi-valued, inlined (as dict) #411
        D_id_any_of = props["has_ds2"]["additionalProperties"]["anyOf"]
        D_id_with_ref = next(d for d in D_id_any_of if "$ref" in d)
        assert D_id_with_ref
        D_id_opt = D_id_with_ref["$ref"].replace("#/$defs/", "")
        assert D_id_opt in defs
        assert defs[D_id_opt]["required"] == []
        # D has no required slots other than the id, so the inlined value can also be null
        D_type_null = next(d for d in D_id_any_of if "type" in d and d.type == "null")
        assert D_type_null

        # single-valued, non-inlined (foreign key)
        assert props["parent"]["type"] == "string"

        # multi-valued, non-inlined (foreign key)
        assert props["children"]["type"] == "array"
        assert props["children"]["items"]["type"] == "string"


if __name__ == "__main__":
    unittest.main()
