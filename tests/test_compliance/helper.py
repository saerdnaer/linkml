"""Helper for compliance tests. See README.md for more information."""
import csv
import enum
import json
import logging
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Tuple, Type, Union

import pydantic
import pytest
import yaml
from linkml_runtime import SchemaView
from linkml_runtime.dumpers import yaml_dumper
from linkml_runtime.linkml_model import meta as meta
from linkml_runtime.utils.compile_python import compile_python
from linkml_runtime.utils.introspection import package_schemaview
from linkml_runtime.utils.yamlutils import YAMLRoot
from pydantic import BaseModel

from linkml import generators as generators
from linkml.generators import (
    JsonSchemaGenerator,
    PydanticGenerator,
    PythonGenerator,
    ShaclGenerator,
    ShExGenerator,
    sqlalchemygen,
)
from linkml.utils.generator import Generator
from linkml.utils.sqlutils import SQLStore
from linkml.validators import JsonSchemaDataValidator
from tests.output.types import Decimal

THIS_DIR = Path(__file__).parent
OUTPUT_DIR = THIS_DIR / "output"

SCHEMA_NAME = str
FRAMEWORK = str  ## pydantic, java, etc

PYDANTIC_ROOT_CLASS = "ConfiguredBaseModel"
PYTHON_DATACLASSES_ROOT_CLASS = "YAMLRoot"
PYDANTIC = "pydantic"
PYDANTIC_STRICT = (
    "pydantic_strict"  ## TODO: https://docs.pydantic.dev/latest/usage/types/strict_types/
)
PYTHON_DATACLASSES = "python_dataclasses"
JSON_SCHEMA = "jsonschema"
SHACL = "shacl"
SHEX = "shex"
SQL_ALCHEMY_IMPERATIVE = "sqlalchemy_imperative"
SQL_ALCHEMY_DECLARATIVE = "sqlalchemy_declarative"
SQL_DDL_SQLITE = "sql_ddl_sqlite"
SQL_DDL_POSTGRES = "sql_ddl_postgres"
OWL = "owl"
GENERATORS: Dict[FRAMEWORK, Type[Generator]] = {
    PYDANTIC: generators.PydanticGenerator,
    PYTHON_DATACLASSES: generators.PythonGenerator,
    JSON_SCHEMA: generators.JsonSchemaGenerator,
    SHACL: generators.ShaclGenerator,
    SHEX: generators.ShExGenerator,
    SQL_ALCHEMY_IMPERATIVE: (
        generators.SQLAlchemyGenerator,
        {"template": sqlalchemygen.TemplateEnum.IMPERATIVE},
    ),
    SQL_ALCHEMY_DECLARATIVE: (
        generators.SQLAlchemyGenerator,
        {"template": sqlalchemygen.TemplateEnum.DECLARATIVE},
    ),
    SQL_DDL_SQLITE: (generators.SQLTableGenerator, {"dialect": "sqlite"}),
    SQL_DDL_POSTGRES: (generators.SQLTableGenerator, {"dialect": "postgresql"}),
    OWL: generators.OwlSchemaGenerator,
}


class ValidationBehavior(str, enum.Enum):
    IMPLEMENTS: str = "implements"
    IGNORES: str = "ignores"
    COERCES: str = "coerces"
    FALSE_POSITIVE: str = "false_positive"
    INCOMPLETE: str = "incomplete"
    NOT_APPLICABLE: str = "not_applicable"
    ACCEPTS: str = "accepts"
    MIXED: str = "mixed"
    UNTESTED: str = "untested"


class DataCheck(BaseModel):
    schema_name: str
    data_name: str
    framework: str
    expected_behavior: ValidationBehavior
    description: str = None
    notes: str = None


class Feature(BaseModel):
    """
    A category of tests that can be implemented by multiple frameworks.
    """

    name: str
    description: str
    implementations: Dict[FRAMEWORK, ValidationBehavior]
    num_tests: int = 0

    class Config:
        use_enum_values = True

    def set_framework_behavior(
        self, framework: FRAMEWORK, behavior: ValidationBehavior
    ) -> ValidationBehavior:
        print(f"SET {framework} {behavior}")
        if framework not in self.implementations:
            self.implementations[framework] = behavior
            return behavior
        current = self.implementations[framework]
        if current == behavior:
            return behavior
        if current == ValidationBehavior.INCOMPLETE:
            return current
        if behavior == ValidationBehavior.INCOMPLETE:
            self.implementations[framework] = behavior
            return behavior
        if behavior == ValidationBehavior.COERCES:
            if current == ValidationBehavior.IMPLEMENTS:
                self.implementations[framework] = behavior
                return behavior
        self.implementations[framework] = ValidationBehavior.MIXED
        return self.implementations[framework]


class FeatureSet(BaseModel):
    """A collection of features."""

    features: List[Feature]


cached_generator_output: Dict[Tuple[SCHEMA_NAME, FRAMEWORK], Tuple[Generator, str]] = {}
"""Cache generators and their outputs to avoid repeated computation."""

all_test_results: List[DataCheck] = []
"""Result of each data check."""

feature_dict: Dict[str, Feature] = {}
"""Map from test name to feature."""

schema_name_to_feature: Dict[str, str] = {}
"""Map from schema name to feature."""

schema_name_to_metamodel_elements: Dict[SCHEMA_NAME, List[str]] = {}
"""Map from schema name all metamodels used in that schema."""


def _as_tsv(rows: List[Dict], path: Union[str, Path]) -> str:
    logging.info(f"Writing report to {path}")
    fn = f"{path}.tsv"
    with open(OUTPUT_DIR / fn, "w", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys(), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def report():
    """
    Generate final reports.

    Note: this is run on exit, so if this fails the test suite still
    shows as working; TODO: run in a different way

    Note: some of these are subject to change
    """
    logging.info(f"Generating reports for {len(all_test_results)} tests")
    fset = FeatureSet(features=list(feature_dict.values()))
    suffix = "" if len(fset.features) > 5 else "_partial"
    summary_base_name = f"summary{suffix}"
    path = OUTPUT_DIR / f"{summary_base_name}.yaml"
    with open(path, "w", encoding="utf-8") as stream:
        yaml.dump(fset.dict(), stream, sort_keys=False)
    for feature in fset.features:
        with open(OUTPUT_DIR / f"{feature.name}.yaml", "w", encoding="utf-8") as stream:
            yaml.dump(feature.dict(), stream, sort_keys=False)
    _as_tsv([{"name": f.name, **f.implementations} for f in fset.features], summary_base_name)
    _as_tsv([check.dict() for check in all_test_results], f"report{suffix}")
    pivoted = defaultdict(dict)
    for check in all_test_results:
        key = (check.schema_name, check.data_name)
        pivoted[key]["schema"] = check.schema_name
        pivoted[key]["data"] = check.data_name
        pivoted[key][check.framework] = check.expected_behavior
    _as_tsv(list(pivoted.values()), f"pivoted{suffix}")
    elements = set()
    for v in schema_name_to_metamodel_elements.values():
        elements.update(v)
    msv = metamodel_schemaview()
    all_elts = set([str(k) for k in msv.all_classes()]).union([str(k) for k in msv.all_slots()])
    coverage = len(elements.intersection(all_elts)) / len(all_elts)
    with open(OUTPUT_DIR / f"coverage{suffix}.txt", "w", encoding="utf-8") as stream:
        stream.write(f"Coverage: {coverage}")
        stream.write("\n".join(sorted(set(elements))))


## atexit.register(report)


def metamodel_schemaview() -> SchemaView:
    """
    Get a SchemaView for the LinkML metamodel.

    :return: view
    """
    return package_schemaview(meta.__name__)


def _get_metamodel_elements(obj: Any) -> Iterator[str]:
    if isinstance(obj, list):
        for elt in obj:
            yield from _get_metamodel_elements(elt)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _get_metamodel_elements(v)
    else:
        pass


def _generate_framework_output(
    schema: Dict, framework: str, mappings: List = None
) -> Tuple[Generator, str]:
    pair = (schema["name"], framework)
    if schema["name"] not in schema_name_to_metamodel_elements:
        schema_name_to_metamodel_elements[schema["name"]] = list(_get_metamodel_elements(schema))
    if pair not in cached_generator_output:
        if mappings is None:
            raise AssertionError(f"No mappings for {pair} - called out of order?")
        gen_class = GENERATORS[framework]
        if isinstance(gen_class, tuple):
            gen_class, gen_args = gen_class
        else:
            gen_args = {}
        gen = gen_class(schema=yaml.dump(schema), **gen_args)
        output = gen.serialize()
        cached_generator_output[pair] = (gen, output)
        out_dir = _schema_out_path(schema) / "generated"
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / f"{framework}.{gen.file_extension}", "w", encoding="utf-8") as stream:
            stream.write(output)
        for context, impdict in mappings:
            if framework in impdict:
                expected = impdict[framework]
                if isinstance(expected, str):
                    assert expected in output
                elif isinstance(expected, dict):
                    output_obj = json.loads(output)
                    assert _obj_within_obj(expected, output_obj)
                    # assert expected.items() <= output_obj.items()
                else:
                    raise AssertionError
    else:
        logging.debug(f"Reusing {len(cached_generator_output.items())}")
    return cached_generator_output[pair]


def _obj_within_obj(expected: Dict, actual: Dict) -> bool:
    """
    Check if the expected object is within the actual object.

    :param expected:
    :param actual:
    :return:
    """
    if expected.items() <= actual.items():
        return True
    for k, v in actual.items():
        if isinstance(v, dict):
            if _obj_within_obj(expected, v):
                return True
        elif isinstance(v, list):
            for elt in v:
                if isinstance(elt, dict):
                    if _obj_within_obj(expected, elt):
                        return True
    return False


def _schema_out_path(schema: Dict) -> Path:
    out_dir = OUTPUT_DIR / schema["name"]
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _make_schema(
    test: Callable,
    name: str,
    schema: Dict = None,
    classes: Dict = None,
    slots: Dict = None,
    types: Dict = None,
    prefixes: Dict = None,
    core_elements: List = None,
    post_process: Callable = None,
    **kwargs,
) -> Tuple[Dict, List]:
    schema_name = f"{test.__name__}-{name}"
    if schema is None:
        schema = {
            "id": f"http://example.org/{name}",
            "name": schema_name,
            "description": test.__doc__,
            "prefixes": {
                "ex": "http://example.org/",
                "linkml": "https://w3id.org/linkml/",
            },
            "imports": [
                "linkml:types",
            ],
            "default_prefix": "ex",
            "default_range": "string",
        }
    else:
        schema = deepcopy(schema)
    if classes is not None:
        schema["classes"] = classes
    if slots is not None:
        schema["slots"] = slots
    if types is not None:
        schema["types"] = types
    for k, v in kwargs.items():
        schema[k] = v
    if prefixes:
        schema["prefixes"].update(prefixes)
    if core_elements:
        if "keywords" not in schema:
            schema["keywords"] = []
        schema["keywords"].extend(core_elements)
    if post_process is not None:
        post_process(schema)
    mappings = list(_extract_mappings(schema))
    out_dir = _schema_out_path(schema)
    with open(out_dir / "schema.yaml", "w", encoding="utf-8") as stream:
        yaml.safe_dump(schema, stream, sort_keys=False)
    with open(out_dir / "mappings.txt", "w", encoding="utf-8") as stream:
        stream.write(str(mappings))
    if not schema["name"]:
        raise ValueError(f"Schema name not set: {schema}")
    return schema, mappings


def validated_schema(test: Callable, local_name: str, framework: str, **kwargs) -> Dict:
    """
    Generate a schema and validate it using the given framework.

    Validation is performed using `_mapping` keys in the schema; these are
    extracted out before schema compilation, and used to check the schema output.

    Note that this function performs in-memory caching of schema outputs, to
    avoid recomputation. It will also write out the schema and generated outputs to disk.
    The key for the cache and the base filename are controlled by two arguments:

    - the name of the test function (e.g. test_inlined)
    - a local name that represents a particular pytest parameter combination.

    The first argument specifies the test function that is calling this function.
    This is used to extract metadata about the test, including its name and
    its documentation. When authoring a test you must ensure this is correct, otherwise
    unexpected behavior may occur, such as saving the yaml files in the wrong place or
    incorrectly caching the schema generation.

    :param test: calling test function; MUST match name of test
    :param local_name: name for this particular test combination
    :param framework:
    :param kwargs:
    :return:
    """
    test_name = test.__name__
    if test_name not in feature_dict:
        feature_dict[test_name] = Feature(
            name=test_name,
            description=test.__doc__,
            implementations={},
        )
    schema, mappings = _make_schema(test, local_name, **kwargs)
    schema_name_to_feature[schema["name"]] = test_name
    # ensure this is cached
    _gen, _output = _generate_framework_output(schema, framework, mappings=mappings)
    return schema


def _extract_mappings(schema: Dict) -> Iterator[Tuple[Dict, List]]:
    """
    Extract key-values injected into the schema to represent expected outputs per generator.

    :param schema: dict representation of schema
    :return: yields
    """
    if isinstance(schema, dict):
        if "_mappings" in schema:
            mappings = schema["_mappings"]
            yield schema, mappings
            del schema["_mappings"]
        for k, v in schema.items():
            yield from _extract_mappings(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from _extract_mappings(v)
    else:
        pass


def _as_compact_yaml(obj: Union[YAMLRoot, BaseModel, Dict]) -> str:
    if isinstance(obj, dict):
        ys = yaml.dump(_clean_dict(obj), sort_keys=False)
        ys = ys.replace("{}", "")
        return ys
    return yaml_dumper.dumps(obj)


def _objects_are_equal(
    obj1: Union[YAMLRoot, BaseModel, Dict], obj2: Union[YAMLRoot, BaseModel, Dict]
) -> bool:
    y1 = _as_compact_yaml(obj1)
    y2 = _as_compact_yaml(obj2)
    return y1 == y2


def _clean_dict(value: Any):
    """
    Remove None values from the given object.

    Also converts Decimal to float.

    :param value:
    :return:
    """
    if isinstance(value, list):
        return [_clean_dict(x) for x in value if x is not None]
    elif isinstance(value, dict):
        return {key: _clean_dict(val) for key, val in value.items() if val is not None}
    elif isinstance(value, Decimal):
        return float(value)
    else:
        return value


_sql_store_cache: Dict[str, SQLStore] = {}


def _get_sql_store(schema) -> SQLStore:
    schema_name = schema["name"]
    if schema_name not in _sql_store_cache:
        schema_obj = meta.SchemaDefinition(**schema)
        _schema_out_path(schema) / "temp.db"
        # store = SQLStore(schema_obj, database_path=db_path, include_schema_in_database=False)
        store = SQLStore(schema_obj, use_memory=True, include_schema_in_database=False)
        _, code = _generate_framework_output(schema, PYTHON_DATACLASSES)
        store.native_module = compile_python(code)
        _sql_store_cache[schema_name] = store
        store.compile()
    return _sql_store_cache[schema_name]


def check_data(
    schema: Dict,
    data_name: str,
    framework: FRAMEWORK,
    object_to_validate: Dict,
    valid: bool,
    should_warn: bool = False,
    expected_behavior: Union[
        ValidationBehavior, Tuple[ValidationBehavior, str]
    ] = ValidationBehavior.IMPLEMENTS,
    target_class: str = None,
    description: str = None,
    coerced: Dict = None,
):
    """
    Validate the given object against the given schema using the given framework.

    Note: this will attempt to use cached schema output to avoid repeated computation;
    it is important to ensure this is called *after* `validated_schema`

    Side effects: this will update various report objects

    :param schema: dict representation of schema to validate against
    :param data_name: a unique label for this dataset. Should be unix-friendly
    :param framework: name of framework to check (e.g pydantic)
    :param object_to_validate: dict representation of object to validate
    :param valid: is the object expected to be inferred valid
    :param should_warn: true if the object is valid but fails a recommended or SHOULD NOT case
    :param expected_behavior: is the framework expected to validate or coerce or ignore this scenario?
    :param target_class: the type of the object
    :param description: description of this particular test combination
    :param coerced: Dict representation of repaired/coerced form of object
    :return:
    """
    out_dir = _schema_out_path(schema)
    if valid:
        out_dir = out_dir / "valid"
    else:
        out_dir = out_dir / "invalid"
    out_dir.mkdir(parents=True, exist_ok=True)
    feature = feature_dict[schema_name_to_feature[schema["name"]]]
    feature.num_tests += 1
    # TODO: avoid repeated rewrites of same object shared across frameworks
    with open(out_dir / f"{data_name}.yaml", "w", encoding="utf-8") as stream:
        yaml.safe_dump(object_to_validate, stream)
    notes = None
    if isinstance(expected_behavior, tuple):
        expected_behavior, notes = expected_behavior
    if expected_behavior is None:
        expected_behavior = ValidationBehavior.IMPLEMENTS
    if expected_behavior in [ValidationBehavior.INCOMPLETE, ValidationBehavior.NOT_APPLICABLE]:
        logging.warning(f"Skipping test for {expected_behavior}")
    else:
        gen, output = _generate_framework_output(schema, framework)
        if isinstance(gen, (PydanticGenerator, PythonGenerator)):
            mod = compile_python(output)
            py_cls = getattr(mod, target_class)
            logging.info(
                f"Validating {py_cls} against {object_to_validate}// {expected_behavior} {valid}"
            )
            py_inst = None
            if valid:
                py_inst = py_cls(**object_to_validate)
            else:
                if expected_behavior == ValidationBehavior.COERCES:
                    try:
                        py_inst = py_cls(**object_to_validate)
                    except Exception as e:
                        assert True, f"could not coerce invalid object; exception: {e}"
                    if py_inst:
                        if coerced:
                            assert _as_compact_yaml(py_inst) == _as_compact_yaml(
                                coerced
                            ), f"coerced {py_inst} != {coerced}"
                        else:
                            logging.warning(f"INCOMPLETE TEST: did not check coerced: {py_inst}")
                else:
                    if isinstance(gen, PydanticGenerator):
                        with pytest.raises(pydantic.ValidationError):
                            py_inst = py_cls(**object_to_validate)
                            logging.info(
                                f"Unexpectedly instantiated {py_inst} from {object_to_validate}"
                            )
                    else:
                        with pytest.raises(Exception):
                            py_inst = py_cls(**object_to_validate)
                            logging.info(f"Unexpectedly instantiated {py_inst}")
            if py_inst is not None:
                yaml.safe_load(yaml_dumper.dumps(py_inst))
                # assert roundtripped.items() == object_to_validate.items()
            logging.info(
                f"fwk: {framework}, cls: {target_class}, inst: {object_to_validate}, valid: {valid}"
            )
        elif isinstance(gen, JsonSchemaGenerator):
            validator = JsonSchemaDataValidator(schema=yaml.dump(schema))
            errors = list(
                validator.iter_validate_dict(
                    _clean_dict(object_to_validate), target_class, closed=True
                )
            )
            logging.info(
                f"Expecting {valid}, Validating {object_to_validate} against {target_class}, errors: {errors}"
            )
            if valid:
                assert errors == [], f"Errors found in json schema validation: {errors}"
            else:
                if expected_behavior == ValidationBehavior.ACCEPTS:
                    logging.info(
                        "Does not flag exception for borderline case (e.g. matching an int to a float)"
                    )
                else:
                    assert errors != [], "Expected errors in json schema validation, but none found"
            if should_warn:
                logging.warning("TODO: check for warnings")
        elif isinstance(gen, ShaclGenerator):
            # TODO: use pyshacl
            expected_behavior = ValidationBehavior.UNTESTED
        elif isinstance(gen, ShExGenerator):
            # TODO: use pyshex
            expected_behavior = ValidationBehavior.UNTESTED
        elif framework == SQL_DDL_SQLITE:
            endpoint = _get_sql_store(schema)
            endpoint.db_exists(force=True)
            py_cls = endpoint.native_module.__dict__[target_class]
            if valid:
                py_obj = py_cls(**object_to_validate)
                endpoint.dump(py_obj)
            else:
                with pytest.raises(Exception):
                    py_obj = py_cls(**object_to_validate)
                    endpoint.dump(py_obj)
        else:
            logging.warning(f"Unsupported generator {gen}")
            expected_behavior = ValidationBehavior.UNTESTED
    feature.set_framework_behavior(framework, expected_behavior)
    if not valid:
        all_test_results.append(
            DataCheck(
                schema_name=schema["name"],
                data_name=data_name,
                framework=framework,
                expected_behavior=expected_behavior,
                description=description,
                notes=notes,
            )
        )
